"""Model router + harness adapters.

route():   model policy -> ordered candidates whose harness is enabled in config/models, installed, and not circuit-broken.
call():    run one candidate non-interactively, parse the agent-result JSON, report cost.
fallback:  call_with_fallback() tries candidates in order (primary model -> fallback model -> fallback harness),
           at most fallback.max_candidates_per_attempt. Task-level retry/reassign/escalate lives in the orchestrator.

Invocation templates come from model-routing/providers/*.harness.yaml; the per-harness builders below only fill in
what a template cannot express (tool lists, modes). Adding a harness = descriptor + one builder.
"""
import functools
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from . import quota
from .registry import Registry
from .supervisor import Limits, Supervisor
from .validate import AI_SCHEMA, load, schema_file, validator_for


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
    on_progress: Callable[[dict], None] | None = None  # called every PROGRESS_SECONDS while the worker runs
    limits: Limits | None = None  # supervisor thresholds (stuck / loop); None = defaults. timeout_seconds is only the ceiling
    run_dir: str | None = None  # persist the worker's pid record, output and exit code here (<run_id>.json/.out/.err/.rc) so a restarted server can adopt it
    run_id: str = "worker"


@dataclass
class CallResult:
    ok: bool
    harness: str
    model: str
    output: dict | None = None
    error: str = ""
    cost_usd: float = 0.0              # charged to budgets: 0 on a flat plan
    cost_estimated: bool = False
    equivalent_cost_usd: float | None = None  # at list price, flat plans included (data for cost projections); None = unpriced
    duration_s: float = 0.0
    raw: str = field(default="", repr=False)
    provider: str | None = None
    effort: str | None = None
    tokens: dict = field(default_factory=dict)  # zh --json usage (input/output/cache/reasoning)
    stopped: str = ""                  # supervisor verdict that killed the worker: stuck | loop | manual | wall (never a reason to try another route)


class Unavailable(Exception):
    pass


class Stopped(Exception):
    """The supervisor stopped the worker (and killed its process tree). `kind`: stuck | loop | manual."""
    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind, self.reason = kind, reason


@dataclass
class Route:
    hdef: dict
    model: str
    cfg: dict
    provider: str | None = None
    effort: str | None = None  # approved-mapping fallbacks: their own effort, "" = none (never inherited); None = the resolved selection's effort


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


def standalone(schema: dict) -> dict:
    """The result schema as one document: claude --json-schema cannot resolve the shared `../ai/1` reference (it fails every call),
    and rejects the draft-2020-12 meta-schema URI, so the AI schema is inlined and $schema/$id dropped."""
    ai = {k: v for k, v in load(schema_file(AI_SCHEMA)).items() if k not in ("$schema", "$id")}
    inline = lambda o: ({**ai, **{k: v for k, v in o.items() if k != "$ref"}} if isinstance(o, dict) and o.get("$ref") == "../ai/1"  # noqa: E731
                        else {k: inline(v) for k, v in o.items()} if isinstance(o, dict) else [inline(x) for x in o] if isinstance(o, list) else o)
    return {k: inline(v) for k, v in schema.items() if k not in ("$schema", "$id")}


def build_claude(hdef, model, req: Request, schema: dict) -> list[str]:
    tools = ",".join(_tools(hdef, req.capabilities))
    args = _fill(hdef["invocation"]["non_interactive_args"], {
        "model": model, "tools": tools, "max_budget_usd": req.max_budget_usd or 1,
        "result_schema": json.dumps(standalone(schema)), "cwd": req.cwd})
    # prompt and system prompt are substituted last so their text is never scanned for placeholders
    args = [req.prompt if a == "{prompt}" else req.system_prompt if a == "{system_prompt}" else a for a in args]
    if not tools:  # no tools at all: drop the empty allow-list
        i = args.index("--allowedTools")
        del args[i:i + 2]
    if req.max_budget_usd is None:
        i = args.index("--max-budget-usd")
        del args[i:i + 2]
    return [hdef["invocation"]["command"], *args]


CONTRACT_GUIDE = """Rules for the JSON object (it is machine-validated; extra or wrongly shaped keys make the whole result rejected):
- Allowed top-level keys only: status, summary, artifacts, decision, verdict, findings, actions, state_updates, memory_notes, follow_up_tasks, plan, blocked_reason, metrics. Omit every key you do not need.
- `actions` is ONLY for actions you ask the orchestrator to perform: [{"capability": "<capability id>", "reason": "..."}]. It is not a log of what you did; omit it otherwise.
- `findings` is a list of plain strings, never objects. `follow_up_tasks`/`plan` items need key (kebab-case), objective, agent, acceptance_criteria (list of strings).
- Each artifact is {"type", "title", "content" (markdown), "evidence" (object with the keys that artifact type requires)}; the key is `type`, never `kind`. A type with no rule of its own (e.g. `other`) needs evidence {"summary": "..."}.
- `decision` (decision tasks only): selected_option (one of the task's decision_options), reasoning_summary, confidence (0-1), and optionally options_considered [{"id", "summary", "pros": [...], "cons": [...]}], impact and risk ("low" | "medium" | "high"), reversibility ("easy" | "costly" | "irreversible"), cost_estimate_usd, parameters (object).
- `state_updates` is a list of {"section", "op" ("set" | "merge" | "append"), "value"}, never an object; omit it when you change nothing.
Valid example:
{"status": "completed", "summary": "Researched X; two sources agree.", "artifacts": [{"type": "research_report", "title": "X market", "content": "# X\\n...", "evidence": {"sources": ["https://example.com/a", "https://example.com/b"], "findings": ["finding one"], "confidence": 0.7}}]}
Valid decision example:
{"status": "completed", "summary": "Chose A: cheaper and reversible.", "decision": {"selected_option": "A", "options_considered": [{"id": "A", "summary": "Option A"}, {"id": "B", "summary": "Option B"}], "reasoning_summary": "A costs less and is easy to undo.", "confidence": 0.7, "impact": "low", "risk": "low", "reversibility": "easy", "cost_estimate_usd": 0}, "artifacts": [{"type": "decision_record", "title": "Decision: A", "content": "# Decision: A\\n...", "evidence": {"selected_option": "A"}}]}"""


def _mode(caps: set[str]) -> str:
    return "yolo" if caps & {"execute_command", "run_tests"} else "edit" if "write_repository" in caps else "plan"


def contract_section(schema: dict) -> str:
    """How to answer: the result schema and the guide. Worker harnesses get it appended; native subagents get it from task_start."""
    return f"Return ONLY one JSON object matching this schema:\n{json.dumps(schema)}\n\n{CONTRACT_GUIDE}"


def _contract_prompt(req: Request, schema: dict) -> str:
    return f"{req.system_prompt}\n\n---\n\n{req.prompt}\n\n{contract_section(schema)}"


def run_marker(run_id: str) -> str:
    return f"[run-id:{run_id}]"


def build_zcode(hdef, model, req: Request, schema: dict) -> list[str]:
    caps = set(req.capabilities)
    prompt = f"{run_marker(req.run_id)}\n{_contract_prompt(req, schema)}"  # the marker lets the supervisor find this run's rollout log
    args = _fill(hdef["invocation"]["non_interactive_args"], {"cwd": req.cwd, "mode": _mode(caps)})
    args = [prompt if a == "{prompt}" else a for a in args]
    if not caps & {"execute_command", "run_tests"}:
        args += ["--disallowed-tools", "Bash"]
    return [hdef["invocation"]["command"], *args]


def build_dsh(hdef, model, req: Request, schema: dict) -> list[str]:
    caps, inv = set(req.capabilities), hdef["invocation"]
    prompt = _contract_prompt(req, schema)
    args = _fill(inv["non_interactive_args"], {"cwd": req.cwd, "mode": _mode(caps), "timeout": max(1, req.timeout_seconds - 5)})
    args = [prompt if a == "{prompt}" else a for a in args]
    if not caps & {"execute_command", "run_tests"}:
        args += ["--disallowed-tools", "bash"]
    return [*inv.get("prefix", []), inv["command"], *args]


def build_agy(hdef, model, req: Request, schema: dict) -> list[str]:
    """No system-prompt/cwd/tool flags (cwd = subprocess cwd). Always --dangerously-skip-permissions (Board approval 2026-10-12, quote in the
    descriptor notes): without it agy auto-denies run_command/web/out-of-cwd access and Gemini models then end the turn EMPTY even on
    text-only tasks (profiling 2026-10-12). The flag auto-approves every tool; --sandbox did not restrict anything on this Linux VM."""
    prompt = _contract_prompt(req, schema)
    args = [prompt if a == "{prompt}" else a for a in hdef["invocation"]["non_interactive_args"]]
    return [hdef["invocation"]["command"], *args, "--dangerously-skip-permissions"]


RETRYABLE = ("no agent-result JSON", "result violates agent-result contract")  # worth one corrective retry on the same route
BUILDERS = {"claude-code": build_claude, "zcode": build_zcode, "agy": build_agy, "dsh": build_dsh}


def default_provider(hdef: dict, provider: str | None) -> str | None:
    """The selection's provider, or the harness's only provider when it offers one (claude-code: anthropic)."""
    only = [p["id"] for p in hdef.get("providers") or []]
    return provider or (only[0] if len(only) == 1 else None)


def price_for(hdef: dict, provider: str | None, model: str) -> dict | None:
    """Pricing entry ({in, cache_hit, out} or {flat: true}) of a route, or None when the descriptor has none."""
    provider = default_provider(hdef, provider)
    table = hdef.get("pricing_usd_per_mtok") or {}
    return table.get(f"{provider}/{model}") or table.get(f"{provider}/*")


def token_cost(price: dict | None, usage: dict) -> float | None:
    """USD at list price for one call from token usage; None = unpriced (a flat entry without list prices included)."""
    if not price or "in" not in price:
        return None
    hit, write = usage.get("cacheReadTokens") or 0, usage.get("cacheWriteTokens") or 0  # both are part of inputTokens
    return round(((usage.get("inputTokens") or 0) - hit - write) * price["in"] / 1e6 + hit * price.get("cache_hit", price["in"]) / 1e6
                 + write * price.get("cache_write", price["in"]) / 1e6 + (usage.get("outputTokens") or 0) * price["out"] / 1e6, 6)


def parse_zcode_json(stdout: str) -> tuple[str, dict]:
    """zh `--json` -> (response text, usage). Anything else is returned as plain text with no usage."""
    i = stdout.find("{")
    if i >= 0:
        try:
            d, _ = json.JSONDecoder().raw_decode(stdout, i)
            if isinstance(d, dict) and "response" in d:
                return d.get("response") or "", d.get("usage") or {}
        except ValueError:
            pass
    return stdout, {}


def parse_agy_json(stdout: str) -> tuple[str, dict, str]:
    """agy `--output-format json` -> (response text, usage in zh key names, error). `input_tokens` excludes cache reads (total = in + out)."""
    i = stdout.find("{")
    try:
        d, _ = json.JSONDecoder().raw_decode(stdout, i)
    except ValueError:
        return stdout, {}, "non-JSON output from agy"
    u = d.get("usage") or {}
    usage = {"inputTokens": (u.get("input_tokens") or 0) + (u.get("cache_read_tokens") or 0), "outputTokens": u.get("output_tokens") or 0,
             "cacheReadTokens": u.get("cache_read_tokens") or 0, "reasoningTokens": u.get("thinking_tokens") or 0}
    text = d.get("response") or ""
    if d.get("status") != "SUCCESS":
        return text, usage, f"agy {d.get('status')}: {str(d.get('error'))[:300]}"
    if d.get("denied_actions") and not text.strip():  # headless: a tool needing permission is auto-denied, the turn ends empty
        return text, usage, "agy denied tool permission: " + ",".join(a.get("action", "?") for a in d["denied_actions"])
    return text, usage, ""


def parse_dsh_json(stdout: str) -> tuple[str, dict, str]:
    """dsh `--json` -> (reply text, usage in zh key names, error). prompt_tokens already includes cache hits.
    Truncated or non-JSON stdout is an error (not retried on the same route: the transport, not the answer, is broken)."""
    i = stdout.find("{")
    try:
        d, _ = json.JSONDecoder().raw_decode(stdout, i) if i >= 0 else (None, 0)
    except ValueError:
        d = None
    if not isinstance(d, dict) or "status" not in d:
        return stdout, {}, "non-JSON output from dsh (truncated or not a result object)"
    u = d.get("usage") or {}
    usage = {"inputTokens": u.get("prompt_tokens") or 0, "outputTokens": u.get("completion_tokens") or 0,
             "cacheReadTokens": u.get("prompt_cache_hit_tokens") or 0, "reasoningTokens": u.get("reasoning_tokens") or 0}
    text = d.get("reply") or ""
    if d["status"] != "completed":
        return text, usage, f"dsh {d['status']}: {str(d.get('failReason'))[:300]}"
    return text, usage, ""


JSON_PARSERS = {"agy": parse_agy_json, "dsh": parse_dsh_json}  # harnesses whose --json output also carries an error status


def claude_usage(d: dict) -> dict:
    """claude `--output-format json` usage -> zh key names; input includes the cache tokens (as token_cost expects)."""
    u = d.get("usage") or {}
    write, read = u.get("cache_creation_input_tokens") or 0, u.get("cache_read_input_tokens") or 0
    if not (u.get("input_tokens") or write or read or u.get("output_tokens")):
        return {}
    return {"inputTokens": (u.get("input_tokens") or 0) + write + read, "cacheWriteTokens": write, "cacheReadTokens": read,
            "outputTokens": u.get("output_tokens") or 0}


def usage_of(harness_id: str, stdout: str) -> dict:
    if harness_id == "claude-code":
        try:
            return claude_usage(json.loads(stdout))
        except ValueError:
            return {}
    if harness_id == "zcode":
        return parse_zcode_json(stdout)[1]
    p = JSON_PARSERS.get(harness_id)
    return p(stdout)[1] if p else {}


def billing(price: dict | None, listed: float | None) -> tuple[float | None, float | None]:
    """(charged, equivalent) for a call whose list-price cost is `listed`: a flat plan charges 0 and keeps the list price as
    data; otherwise both are the list price (None = unpriced, the Router applies the config estimate)."""
    return (0.0 if price and price.get("flat") else listed), listed


def parse(harness_id: str, stdout: str, price: dict | None = None) -> tuple[dict | None, float | None, str]:
    """-> (agent result, cost at list price or None, error). Billing (flat plans) is applied by `billing`."""
    if harness_id == "claude-code":
        try:
            d = json.loads(stdout)
        except ValueError:
            return None, None, "non-JSON output from claude"
        cost = token_cost(price, claude_usage(d)) if claude_usage(d) else None
        cost = d.get("total_cost_usd") if cost is None else cost  # no tariff in the descriptor: the CLI's own figure
        if d.get("is_error") or d.get("subtype") != "success":
            return None, cost, f"claude error: {d.get('subtype')} {str(d.get('result'))[:300]}"
        out = d.get("structured_output") or extract_json(d.get("result") or "")
        return out, cost, "" if out else "no agent-result JSON in claude output"
    if harness_id in JSON_PARSERS:
        text, usage, err = JSON_PARSERS[harness_id](stdout)
        if err:
            return None, token_cost(price, usage) if usage else None, err
    else:
        text, usage = parse_zcode_json(stdout) if harness_id == "zcode" else (stdout, {})
    out = extract_json(text)
    return out, token_cost(price, usage) if usage else None, "" if out else "no agent-result JSON in output"


@functools.cache
def installed_version(command: str) -> str:
    """`<command> --version` -> x.y.z ('' when it cannot be read). Cached per process: checked at first use, not on every call."""
    try:
        m = re.search(r"\d+\.\d+\.\d+", subprocess.run([command, "--version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).stdout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return m.group(0) if m else ""


PROGRESS_SECONDS = 30
SUPERVISE_SECONDS = 10  # how often the supervisor looks at the worker's log, workdir and CPU
STALL_SECONDS = 300  # no CPU used by the worker's process tree for this long: reported as `stalled` in the progress message


def _tree_cpu(sid: int) -> tuple[int, int]:
    """(processes, cpu clock ticks) of every process in session `sid` (the worker and what it spawned, e.g. wrangler/pnpm)."""
    n = ticks = 0
    for d in os.listdir("/proc"):
        try:
            f = open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()  # state ppid pgrp session ... utime(12) stime(13)
            if int(f[3]) == sid and f[0] != "Z":
                n, ticks = n + 1, ticks + int(f[11]) + int(f[12])
        except (OSError, IndexError, ValueError):
            continue
    return n, ticks


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def tree_alive(sid: int) -> bool:
    return _tree_cpu(sid)[0] > 0


def kill_group(pgid: int):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        for _ in range(50):
            if not tree_alive(pgid):
                return
            time.sleep(0.1)


def _kill_tree(p: subprocess.Popen):
    kill_group(p.pid)
    p.wait()


def run_worker(argv, req: Request, out, errf, poll: float = 1.0, record: Path | None = None, meta: dict | None = None,
               sup: Supervisor | None = None) -> int:
    """Run the worker in its own session so a kill takes its whole process tree (subprocess.run's timeout killed
    only the direct child and then hung while a grandchild held the stderr pipe). While it runs, the supervisor (if any) judges its
    progress every SUPERVISE_SECONDS and a stuck or looping worker is killed (raises Stopped); `req.timeout_seconds` is only the
    ceiling (raises TimeoutExpired). `req.on_progress` gets, every PROGRESS_SECONDS, elapsed seconds, stdout bytes, process count,
    whether the tree has used no CPU for STALL_SECONDS and the latest activity lines."""
    p = subprocess.Popen(argv, cwd=req.cwd, stdout=out, stderr=errf, text=True, stdin=subprocess.DEVNULL, start_new_session=True)
    if record:  # the owner (this server) may die; the worker keeps running and the next server settles it from these files
        record.write_text(json.dumps({**(meta or {}), "pid": p.pid, "owner": os.getpid(), "started": time.time(), "limit": req.timeout_seconds,
                                      "cwd": req.cwd, "limits": asdict(req.limits or Limits()), "done": False}))
    t0 = last_tick = last_look = last_cpu = time.time()
    ticks = -1
    while True:
        try:
            return p.wait(poll)
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        if now - t0 >= req.timeout_seconds:
            _kill_tree(p)
            raise subprocess.TimeoutExpired(argv, req.timeout_seconds)
        if now - last_look >= SUPERVISE_SECONDS or now - last_tick >= PROGRESS_SECONDS:
            last_look = now
            n, t = _tree_cpu(p.pid)
            if t != ticks:
                ticks, last_cpu = t, now
            out.flush()
            size = os.fstat(out.fileno()).st_size
            if sup and (v := sup.poll(now, t, size)):
                _kill_tree(p)
                raise Stopped(*v)
            if now - last_tick >= PROGRESS_SECONDS:
                last_tick = now
                if req.on_progress:
                    req.on_progress({"elapsed_s": int(now - t0), "limit_s": req.timeout_seconds, "output_bytes": size, "processes": n,
                                     "idle_cpu_s": int(now - last_cpu), "stalled": now - last_cpu >= STALL_SECONDS,
                                     "activity": sup.tail(5) if sup else []})


def invoke(hdef: dict, model: str, req: Request, schema: dict) -> CallResult:
    """Real subprocess call. Tests inject a different callable with the same signature."""
    builder = BUILDERS.get(hdef["id"])
    if builder is None:
        return CallResult(False, hdef["id"], model, error=f"no adapter builder for harness {hdef['id']}")
    pin = hdef.get("pinned_version")
    if pin and (v := installed_version(hdef["invocation"]["command"])) != pin:
        return CallResult(False, hdef["id"], model, error=f"{hdef['id']} version {v or 'unknown'} does not match the pinned {pin}")
    argv = builder(hdef, model, req, schema) + selection_args(hdef, model, req)
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        rd = Path(req.run_dir or tmp)
        rd.mkdir(parents=True, exist_ok=True)
        base = rd / req.run_id
        base.with_suffix(".rc").unlink(missing_ok=True)  # never a previous attempt's exit code
        base.with_suffix(".stop").unlink(missing_ok=True)  # nor a previous attempt's stop request
        wrapped = ["sh", "-c", '"$@"; rc=$?; echo $rc > "$0"; exit $rc', str(base.with_suffix(".rc")), *argv]
        meta = {"harness": hdef["id"], "model": model, "provider": req.provider, "effort": req.effort}
        sup = Supervisor(base, req.cwd, hdef["id"], req.limits or Limits(), t0, run_marker(req.run_id))
        sup.note(f"# start {hdef['id']} {model}{' ' + req.effort if req.effort else ''} (ceiling {req.timeout_seconds}s)", t0)
        with open(base.with_suffix(".out"), "w+") as out, open(base.with_suffix(".err"), "w+") as errf:
            try:
                returncode = run_worker(wrapped, req, out, errf, record=base.with_suffix(".json") if req.run_dir else None, meta=meta, sup=sup)
            except Stopped as e:
                return halted(hdef, model, req.provider, sup, e.kind, e.reason, time.time() - t0)
            except subprocess.TimeoutExpired:
                return halted(hdef, model, req.provider, sup, "wall", f"safety ceiling of {req.timeout_seconds}s reached (max_wall_seconds), worker process group killed",
                              time.time() - t0)
            except OSError as e:
                return CallResult(False, hdef["id"], model, error=f"cannot start harness: {e}")
        stdout, stderr = base.with_suffix(".out").read_text(), base.with_suffix(".err").read_text()
        sup.note(f"# exit {returncode} after {int(time.time() - t0)}s")
    return collect(hdef, model, req.provider, stdout, stderr, returncode, time.time() - t0)


def halted(hdef: dict, model: str, provider: str | None, sup: Supervisor, kind: str, reason: str, duration_s: float) -> CallResult:
    """CallResult of a worker the supervisor (or the ceiling) killed. The tokens it used so far come from the worker's own log (a killed
    process prints no usage), priced like a finished call."""
    report = sup.stop_report(kind, reason)
    sup.note(f"# STOPPED ({kind}): {reason}")
    tokens = sup.tokens
    price = price_for(hdef, provider, model)
    cost, equivalent = billing(price, token_cost(price, tokens) if tokens else None)
    return CallResult(False, hdef["id"], model, error=report, cost_usd=cost or 0.0, cost_estimated=cost is None and bool(tokens),
                      equivalent_cost_usd=equivalent, duration_s=duration_s, tokens=tokens, stopped=kind)


def collect(hdef: dict, model: str, provider: str | None, stdout: str, stderr: str, returncode: int, duration_s: float) -> CallResult:
    """A finished worker's files -> CallResult (also used to settle a worker a previous server left running)."""
    price = price_for(hdef, provider, model)
    output, listed, err = parse(hdef["id"], stdout, price)
    cost, equivalent = billing(price, listed)
    usage = usage_of(hdef["id"], stdout)
    if returncode != 0 and (not err or not stdout.strip()):
        err = f"exit {returncode}: {stderr.strip()[-300:] or err}"
    return CallResult(not err, hdef["id"], model, output=output, error=err, cost_usd=cost or 0.0,
                      cost_estimated=cost is None, equivalent_cost_usd=equivalent, duration_s=duration_s, raw=stdout[-4000:],
                      tokens={k: usage[k] for k in ("inputTokens", "outputTokens", "cacheWriteTokens", "cacheReadTokens", "reasoningTokens") if k in usage})


class Router:
    def __init__(self, reg: Registry, health: dict | None = None, check_installed: bool = True):
        self.reg = reg
        self.check_installed = check_installed
        self.health = health if health is not None else {}  # harness -> {"failures": n, "since": ts}
        self.cfg = reg.config["models"]
        self._validator = validator_for(reg.result_schema, reg.root)

    skip = staticmethod(quota.skip_reason)  # tests replace it

    def _breaker_open(self, harness_id: str) -> bool:
        fb = self.cfg.get("fallback") or {}
        h = self.health.get(harness_id) or {}
        return (h.get("failures", 0) >= fb.get("circuit_breaker_failures", 3)
                and time.time() - h.get("since", 0) < fb.get("circuit_breaker_seconds", 600))

    def route(self, policy_id: str, ai: dict | None = None, fallbacks: list[dict] | None = None) -> list[Route]:
        """Routes in preference order: the resolved AI selection first (if it names a harness), then the Board-approved
        mapping fallbacks (each with its own effort), then the model policy candidates. Empty = nothing routable."""
        enabled = self.reg.enabled_harnesses()
        cands = list(self.reg.model_policies[policy_id]["candidates"])
        lead = []
        if ai and ai.get("harness"):
            lead.append({"harness": ai["harness"], "model": ai.get("model") or (enabled.get(ai["harness"]) or {}).get("default_model"),
                         "provider": ai.get("provider")})
        lead += [{"harness": f["harness"], "model": f.get("model") or (enabled.get(f["harness"]) or {}).get("default_model"),
                  "provider": f.get("provider"), "effort": f.get("effort") or ""} for f in fallbacks or [] if f.get("harness")]
        seen = {(c["harness"], c.get("provider"), c["model"], c.get("effort")) for c in lead}
        cands = lead + [c for c in cands if (c["harness"], c.get("provider"), c["model"], None) not in seen
                        and (c["harness"], c["model"]) not in {(x["harness"], x["model"]) for x in lead}]
        out = []
        for c in cands:
            hdef, hcfg = self.reg.harnesses.get(c["harness"]), enabled.get(c["harness"])
            if not hdef or not hcfg or self._breaker_open(c["harness"]) or c.get("provider") in hcfg.get("inactive_providers", ()):
                continue  # inactive provider: the Board marks it in config (e.g. a plan whose promo ended); never tried
            if self.check_installed and not shutil.which(hdef["invocation"]["command"]):
                continue
            out.append(Route(hdef, c["model"], hcfg, default_provider(hdef, c.get("provider")), c.get("effort")))
        return out

    def _record(self, harness_id: str, ok: bool):
        h = self.health.setdefault(harness_id, {"failures": 0, "since": 0})
        if ok:
            h.update(failures=0, since=0)
        else:
            h["failures"] += 1
            h["since"] = h["since"] or time.time()

    def call_with_fallback(self, policy_id: str, req: Request, invoke_fn=invoke, which=None, ai: dict | None = None,
                           fallbacks: list[dict] | None = None) -> list[CallResult]:
        """All attempts in order; the last one is the outcome. Raises Unavailable if nothing is routable.
        A route that cannot honour the selection (provider/model/effort) is recorded as a failed attempt without a call.
        An answer with no result object or one that breaks the contract gets ONE corrective retry on the same route
        (the rejection reason is fed back) before the next route is tried."""
        routes = self.route(policy_id, ai, fallbacks) if which is None else which
        if not routes:
            raise Unavailable(f"no enabled, installed harness for model policy '{policy_id}'")
        limit = (self.cfg.get("fallback") or {}).get("max_candidates_per_attempt", 3)
        attempts, t0 = [], time.time()
        for rt in routes[:limit]:
            hdef, model, hcfg = rt.hdef, rt.model, rt.cfg
            effort = (rt.effort if rt.effort is not None else (ai or {}).get("effort")) or None
            why = unsupported(hdef, model, rt.provider, effort)
            if why:
                attempts.append(CallResult(False, hdef["id"], model, error=f"unsupported selection: {why}", provider=rt.provider, effort=effort))
                continue
            left = int(req.timeout_seconds - (time.time() - t0))  # max_wall_seconds is the total for all routes
            if left < 1:
                break
            # `left` is only the safety ceiling (max_wall_seconds, total for all routes): a worker that makes progress is never cut short and a
            # stuck or looping one is stopped by the supervisor, so no per-route time cut and no moving on for being slow
            if self.check_installed and (why := self.skip(hdef, model)):  # spent subscription: fail fast, next route (never wait for a reset)
                attempts.append(CallResult(False, hdef["id"], model, error=f"route skipped: {why}", provider=rt.provider, effort=effort))
                continue
            this = Request(**{**req.__dict__, "provider": rt.provider, "effort": effort, "timeout_seconds": left})
            for retry in (False, True):
                r = invoke_fn(hdef, model, this, self.reg.result_schema)
                r.provider, r.effort = rt.provider, effort
                r.error = quota.classify(r.error)
                if r.ok and r.cost_estimated and hdef.get("cost_reporting") in ("none", "tokens"):  # unpriced route: config estimate
                    r.cost_usd = r.equivalent_cost_usd = hcfg.get("estimated_cost_usd_per_call") or 0.0
                if r.ok:
                    errs = [e.message for e in self._validator.iter_errors(r.output)]
                    if errs:
                        r.ok, r.error = False, f"result violates agent-result contract: {errs[:2]}"
                if not r.stopped:  # a run the supervisor or the ceiling stopped says nothing about the provider's health
                    self._record(hdef["id"], r.ok)
                attempts.append(r)
                if r.stopped:  # stop and report: another route only after a real failure (exit, provider error, quota) or the CEO's decision
                    return attempts
                if r.ok or retry or not r.error.startswith(RETRYABLE):
                    break
                this = Request(**{**this.__dict__, "prompt": f"{req.prompt}\n\nYour previous answer was rejected: {r.error[:400]}\n"
                                                              "Answer again with ONLY one corrected JSON object."})
            if attempts[-1].ok:
                break
        return attempts
