"""Model router + harness adapters.

route():   model policy -> ordered candidates whose harness is enabled in config/models, installed, and not circuit-broken.
call():    run one candidate non-interactively, parse the agent-result JSON, report cost.
fallback:  call_with_fallback() tries candidates in order (primary model -> fallback model -> fallback harness),
           at most fallback.max_candidates_per_attempt. Task-level retry/reassign/escalate lives in the orchestrator.

Invocation templates come from model-routing/providers/*.harness.yaml; the per-harness builders below only fill in
what a template cannot express (tool lists, modes). Adding a harness = descriptor + one builder.
"""
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from .registry import Registry
from .validate import validator_for


@dataclass
class Request:
    system_prompt: str
    prompt: str
    cwd: str
    capabilities: list[str]
    max_budget_usd: float | None
    timeout_seconds: int
    provider: str | None = None
    effort: str | None = None


@dataclass
class CallResult:
    ok: bool
    harness: str
    model: str
    output: dict | None = None
    error: str = ""
    cost_usd: float = 0.0
    cost_estimated: bool = False
    duration_s: float = 0.0
    raw: str = field(default="", repr=False)


class Unavailable(Exception):
    pass


@dataclass
class Route:
    hdef: dict
    model: str
    cfg: dict
    provider: str | None = None


def unsupported(hdef: dict, model: str | None, provider: str | None, effort: str | None) -> str | None:
    """Why this harness cannot honour the selection per call (None = it can). Never drop a selection silently."""
    inv, hid = hdef["invocation"], hdef["id"]
    if provider and provider not in {p["id"] for p in hdef["providers"]}:
        return f"{hid} does not offer provider '{provider}'"
    if (provider or (model and model != "configured")) and not inv.get("model_args"):
        return f"{hid} cannot select a model/provider per call"
    if provider is None and any("{provider}" in a for a in inv.get("model_args", [])) and model and model != "configured":
        return f"{hid} needs a provider with model '{model}'"
    if effort and not inv.get("effort_args"):
        return f"{hid} cannot select thinking effort per call"
    if effort and hdef.get("efforts") and effort not in hdef["efforts"]:
        return f"{hid} does not accept effort '{effort}' (accepts {hdef['efforts']})"
    return None


def selection_args(hdef: dict, model: str | None, req: Request) -> list[str]:
    inv, out = hdef["invocation"], []
    if model and model != "configured":
        out += _fill(inv["model_args"], {"model": model, "provider": req.provider or ""})
    if req.effort:
        out += _fill(inv["effort_args"], {"effort": req.effort})
    return out


def extract_json(text: str) -> dict | None:
    """Last top-level JSON object with a 'status' key in free text (for harnesses without structured output)."""
    dec, best, i = json.JSONDecoder(), None, text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
            if isinstance(obj, dict) and "status" in obj:
                best = obj
            i = text.find("{", end)
        except ValueError:
            i = text.find("{", i + 1)
    return best


def _fill(template: list[str], values: dict) -> list[str]:
    out = []
    for a in template:
        for k, v in values.items():
            a = a.replace("{" + k + "}", str(v))
        out.append(a)
    return out


def _tools(hdef: dict, caps: list[str]) -> list[str]:
    m = hdef.get("capability_tools") or {}
    return sorted({t for c in caps for t in m.get(c, [])})


def build_claude(hdef, model, req: Request, schema: dict) -> list[str]:
    tools = ",".join(_tools(hdef, req.capabilities))
    args = _fill(hdef["invocation"]["non_interactive_args"], {
        "model": model, "tools": tools, "max_budget_usd": req.max_budget_usd or 1,
        # claude's validator rejects the draft-2020-12 meta-schema URI; the schema body itself is accepted
        "result_schema": json.dumps({k: v for k, v in schema.items() if k not in ("$schema", "$id")}), "cwd": req.cwd})
    # prompt and system prompt are substituted last so their text is never scanned for placeholders
    args = [req.prompt if a == "{prompt}" else req.system_prompt if a == "{system_prompt}" else a for a in args]
    if not tools:  # no tools at all: drop the empty allow-list
        i = args.index("--allowedTools")
        del args[i:i + 2]
    if req.max_budget_usd is None:
        i = args.index("--max-budget-usd")
        del args[i:i + 2]
    return [hdef["invocation"]["command"], *args]


def build_zcode(hdef, model, req: Request, schema: dict) -> list[str]:
    caps = set(req.capabilities)
    mode = "yolo" if caps & {"execute_command", "run_tests"} else "edit" if "write_repository" in caps else "plan"
    prompt = (f"{req.system_prompt}\n\n---\n\n{req.prompt}\n\nReturn ONLY one JSON object matching this schema:\n"
              f"{json.dumps(schema)}")
    args = _fill(hdef["invocation"]["non_interactive_args"], {"cwd": req.cwd, "mode": mode})
    args = [prompt if a == "{prompt}" else a for a in args]
    if not caps & {"execute_command", "run_tests"}:
        args += ["--disallowed-tools", "Bash"]
    return [hdef["invocation"]["command"], *args]


BUILDERS = {"claude-code": build_claude, "zcode": build_zcode}


def parse(harness_id: str, stdout: str) -> tuple[dict | None, float | None, str]:
    """-> (agent result, exact cost or None, error)."""
    if harness_id == "claude-code":
        try:
            d = json.loads(stdout)
        except ValueError:
            return None, None, "non-JSON output from claude"
        cost = d.get("total_cost_usd")
        if d.get("is_error") or d.get("subtype") != "success":
            return None, cost, f"claude error: {d.get('subtype')} {str(d.get('result'))[:300]}"
        out = d.get("structured_output") or extract_json(d.get("result") or "")
        return out, cost, "" if out else "no agent-result JSON in claude output"
    out = extract_json(stdout)
    return out, None, "" if out else "no agent-result JSON in output"


def invoke(hdef: dict, model: str, req: Request, schema: dict) -> CallResult:
    """Real subprocess call. Tests inject a different callable with the same signature."""
    builder = BUILDERS.get(hdef["id"])
    if builder is None:
        return CallResult(False, hdef["id"], model, error=f"no adapter builder for harness {hdef['id']}")
    argv = builder(hdef, model, req, schema) + selection_args(hdef, model, req)
    t0 = time.time()
    with tempfile.TemporaryFile("w+") as out:
        try:
            p = subprocess.run(argv, cwd=req.cwd, stdout=out, stderr=subprocess.PIPE, text=True,
                               timeout=req.timeout_seconds, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return CallResult(False, hdef["id"], model, error=f"timeout after {req.timeout_seconds}s", duration_s=time.time() - t0)
        except OSError as e:
            return CallResult(False, hdef["id"], model, error=f"cannot start harness: {e}")
        out.seek(0)
        stdout = out.read()
    output, cost, err = parse(hdef["id"], stdout)
    if p.returncode != 0 and (not err or not stdout.strip()):
        err = f"exit {p.returncode}: {p.stderr.strip()[-300:] or err}"
    return CallResult(not err, hdef["id"], model, output=output, error=err, cost_usd=cost or 0.0,
                      cost_estimated=cost is None, duration_s=time.time() - t0, raw=stdout[-4000:])


class Router:
    def __init__(self, reg: Registry, health: dict | None = None, check_installed: bool = True):
        self.reg = reg
        self.check_installed = check_installed
        self.health = health if health is not None else {}  # harness -> {"failures": n, "since": ts}
        self.cfg = reg.config["models"]
        self._validator = validator_for(reg.result_schema)

    def _breaker_open(self, harness_id: str) -> bool:
        fb = self.cfg.get("fallback") or {}
        h = self.health.get(harness_id) or {}
        return (h.get("failures", 0) >= fb.get("circuit_breaker_failures", 3)
                and time.time() - h.get("since", 0) < fb.get("circuit_breaker_seconds", 600))

    def route(self, policy_id: str, ai: dict | None = None) -> list[Route]:
        """Routes in preference order: the resolved AI selection first (if it names a harness), then the model policy
        candidates as fallbacks. Empty = nothing routable."""
        enabled = self.reg.enabled_harnesses()
        cands = list(self.reg.model_policies[policy_id]["candidates"])
        if ai and ai.get("harness"):
            first = {"harness": ai["harness"], "model": ai.get("model") or (enabled.get(ai["harness"]) or {}).get("default_model"),
                     "provider": ai.get("provider")}
            cands = [first] + [c for c in cands if (c["harness"], c["model"]) != (first["harness"], first["model"])]
        out = []
        for c in cands:
            hdef, hcfg = self.reg.harnesses.get(c["harness"]), enabled.get(c["harness"])
            if not hdef or not hcfg or self._breaker_open(c["harness"]):
                continue
            if self.check_installed and not shutil.which(hdef["invocation"]["command"]):
                continue
            out.append(Route(hdef, c["model"], hcfg, c.get("provider")))
        return out

    def _record(self, harness_id: str, ok: bool):
        h = self.health.setdefault(harness_id, {"failures": 0, "since": 0})
        if ok:
            h.update(failures=0, since=0)
        else:
            h["failures"] += 1
            h["since"] = h["since"] or time.time()

    def call_with_fallback(self, policy_id: str, req: Request, invoke_fn=invoke, which=None, ai: dict | None = None) -> list[CallResult]:
        """All attempts in order; the last one is the outcome. Raises Unavailable if nothing is routable.
        A route that cannot honour the selection (provider/model/effort) is recorded as a failed attempt without a call."""
        routes = self.route(policy_id, ai) if which is None else which
        if not routes:
            raise Unavailable(f"no enabled, installed harness for model policy '{policy_id}'")
        limit = (self.cfg.get("fallback") or {}).get("max_candidates_per_attempt", 3)
        effort = (ai or {}).get("effort")
        attempts = []
        for rt in routes[:limit]:
            hdef, model, hcfg = rt.hdef, rt.model, rt.cfg
            why = unsupported(hdef, model, rt.provider, effort)
            if why:
                attempts.append(CallResult(False, hdef["id"], model, error=f"unsupported selection: {why}"))
                continue
            r = invoke_fn(hdef, model, Request(**{**req.__dict__, "provider": rt.provider, "effort": effort,
                                                  "timeout_seconds": min(req.timeout_seconds, hcfg.get("timeout_seconds", 1800))}),
                          self.reg.result_schema)
            if r.ok and r.cost_estimated and hdef.get("cost_reporting") == "none":
                r.cost_usd = hcfg.get("estimated_cost_usd_per_call") or 0.0
            if r.ok:
                errs = [e.message for e in self._validator.iter_errors(r.output)]
                if errs:
                    r.ok, r.error = False, f"result violates agent-result contract: {errs[:2]}"
            self._record(hdef["id"], r.ok)
            attempts.append(r)
            if r.ok:
                break
        return attempts
