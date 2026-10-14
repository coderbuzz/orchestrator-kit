"""Runtime: the deterministic guardrails an orchestrator session (Claude Code or zh) works through.

The orchestrator decides what happens: the plan, who does what, the order, retries, when to stop.
These operations refuse what must not happen: an invalid task transition, completion without evidence, an action outside
an agent's capabilities or above its authority, a side effect repeated after a crash, spend over the daily cap, a state
write outside an agent's scope, an invalid product stage change. A refusal raises `Refused` with the reason.

Work is done either by the orchestrator's own subagents (native/: `task start`, then `task submit` with the agent-result
JSON) or by a worker harness (`dispatch`: claude -p / zh -p with the task's AI selection, then submit). Exposed to sessions by the MCP server (orchestrator_kit.mcp, extended by the pack).

Organization-specific behavior (company state, decisions, products, deploy gates) plugs in through the hook methods below
(`init_state`, `filter_extras`, `apply_result`, `record_outcome`, `action_gate`, `visible_state`, `workspace_key`, `subagent_name`);
a pack subclasses Runtime and overrides only those.
"""
import json
import os
import threading
import time
from pathlib import Path

import yaml

from . import harness, supervisor, verify
from .pack import Pack
from .policy import Policy
from .registry import Registry
from .store import InvalidRecord, Store, new_id, now
from .validate import AI_SCHEMA, ROOT, errors_for, load, schema_file, validate_instance, validator_for

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
LEGACY_WALL = 1800  # what task_add stored as max_wall_seconds before the progress supervisor: treated as "not set"


class Refused(Exception):
    """A guardrail said no. The message is the reason."""


BUDGET_WARNING = 0.8  # share of the daily cap at which the human is told (once per day)


def projects(workspace: Path, project_file: str) -> dict[str, Path]:
    """Workspace projects the company runs: folders holding the pack's project file (company.yaml)."""
    return {p.parent.name: p.parent for p in sorted(Path(workspace).glob(f"*/{project_file}"))}


def read_project(project_dir: Path, root: Path) -> dict:
    """A project file, schema-checked (Refused if invalid)."""
    pack = Pack.load(root)
    f = Path(project_dir) / pack.project_file
    cfg = yaml.safe_load(f.read_text()) or {}
    if errs := errors_for(validator_for(load(schema_file("company/project.schema.json", root)), root), cfg):
        raise Refused(f"{f}: {errs[:3]}")
    return cfg


def write_project(project_dir: Path, root: Path, cfg: dict, new: bool = False) -> dict:
    """Write a project file after checking it against the schema and the company catalog (roster ids must be enabled agents)."""
    pack, reg = Pack.load(root), Registry(root)
    f = Path(project_dir) / pack.project_file
    if not Path(project_dir).is_dir() or (new and f.exists()):
        raise Refused(f"{project_dir} must exist" + (f" and not have a {pack.project_file} yet" if new else ""))
    if errs := errors_for(validator_for(load(schema_file("company/project.schema.json", root)), root), cfg):
        raise Refused(f"{f}: {errs[:3]}")
    if unknown := sorted(set(cfg.get("roster") or []) - reg.enabled_agents):
        raise Refused(f"not in the company catalog (or not enabled): {unknown}; hire into the company first")
    f.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    return cfg


def _tokens(u: dict) -> int:
    t = u.get("tokens") or {}
    return t.get("totalTokens") or (t.get("inputTokens") or 0) + (t.get("outputTokens") or 0)


class Runtime:
    policy_class = Policy
    builtin_adapters: dict = {}  # capability -> adapter fn the pack always has (internal adapters)
    evidence_checks: dict = {}   # special check name -> fn, beyond verify.CHECKS (see verify.py)
    actor = "orchestrator"  # default `by` of tasks and decisions a session makes; a pack names its orchestrator agent

    @classmethod
    def for_project(cls, project_dir: Path, **kw) -> "Runtime":
        """The company as configured for one workspace project: its own state, budget, agents and default AI."""
        project_dir, root = Path(project_dir), Path(kw.get("root") or ROOT)
        pack = Pack.load(root)
        cfg = read_project(project_dir, root)
        c = cls(state_dir=project_dir / pack.state_dir / "state", **kw)
        c.project_dir, c.project = project_dir, cfg
        conf = c.reg.config
        spending = {**(conf["policies"].get("parameters") or {}).get("spending", {}),
                    **({"max_cost_usd_per_day": cfg["daily_budget_usd"]} if "daily_budget_usd" in cfg else {}),
                    **({"max_cost_usd_per_task": cfg["task_budget_usd"]} if "task_budget_usd" in cfg else {})}
        conf["policies"] = {**conf["policies"], "parameters": {**(conf["policies"].get("parameters") or {}), "spending": spending}}
        if "roster" in cfg:  # the hired team; orchestrators (the session's own agent) are always on it
            unknown = set(cfg["roster"]) - set(c.reg.agents)
            if unknown:
                raise Refused(f"{project_dir / pack.project_file}: unknown agents {sorted(unknown)}")
            hired = set(cfg["roster"]) | {a for a, d in c.reg.agents.items() if d["type"] == "orchestrator"}
            c.off_roster = {a for a in conf["agents"].get("enabled", []) if a not in hired}
            conf["agents"] = {**conf["agents"], "enabled": [a for a in conf["agents"].get("enabled", []) if a in hired]}
        if "default_ai" in cfg:
            conf["models"] = {**conf["models"], "default_ai": cfg["default_ai"]}
        return c

    def __init__(self, root: Path | None = None, state_dir: Path | None = None, invoke=None, adapters=None):
        self.project_dir: Path | None = None
        self.project: dict = {}
        self.off_roster: set[str] = set()  # enabled in the company but not hired by this project
        self.reg = Registry(root)
        self.store = Store(state_dir or self.reg.state_dir, self.reg.root)
        self.workspaces_dir = (Path(state_dir) / "workspaces") if state_dir else self.reg.workspaces_dir
        self.adapters = {**self.builtin_adapters, **(adapters or {})}
        self.policy = self.policy_class(self.reg, set(adapters or {}))
        self.router = harness.Router(self.reg, self.store.get("runtime", "health") or {}, check_installed=invoke is None)
        self.invoke = invoke or harness.invoke
        self.on_progress = None  # fn(dict) with the worker's liveness, for a synchronous `dispatch`
        self._active: set[str] = set()  # tasks whose worker this server runs on a background thread
        self.init_state()

    # ------------------------------------------------------------------ pack hooks (defaults: no shared state, no extras)
    def init_state(self):
        """Create the pack's shared state document, if it has one."""

    def subagent_name(self, agent_id: str) -> str:
        """The native subagent that executes an agent's tasks."""
        return agent_id

    def visible_state(self, agent: dict) -> dict | None:
        """Shared state an agent may read in its prompt (None = the pack has none)."""
        return None

    def workspace_key(self, t: dict) -> str:
        """Scratch workspace shared by tasks of one initiative: the root task by default."""
        return t["parent_task_id"] or t["task_id"]

    def filter_extras(self, t: dict, res: dict, dropped: list[str]):
        """Drop result items beyond actions that the agent may not produce (append the reason to `dropped`)."""
        for key in ("state_updates", "memory_notes"):
            if res.get(key):
                dropped.append(f"{key}: this pack keeps no shared state")
                res[key] = []

    def apply_result(self, t: dict, res: dict):
        """Persist a verified result's side effects (state, memory). May raise InvalidRecord: nothing is then written."""

    def record_outcome(self, t: dict, res: dict):
        """After evidence is accepted, just before COMPLETED: record the decision, if the pack records them."""

    def action_gate(self, t: dict, capability: str) -> str | None:
        """A reason to deny an action before it runs (a pack's own gates, e.g. deploy order); None = allowed."""
        return None

    # ------------------------------------------------------------------ records
    def audit(self, actor: str, action: str, why: str, task: dict | None = None, **extra):
        self.store.append("audit.jsonl", {"at": now(), "actor": actor, "action": action, "why": why[:1000],
                                          "task_id": (task or {}).get("task_id"), **extra})

    def notify_human(self, sender: str, message: str, task_id: str | None = None, kind: str = "info"):
        self.store.append(f"{self.reg.pack.human_id}/inbox.jsonl", {"at": now(), "from": sender, "kind": kind, "message": message[:4000], "task_id": task_id})

    def task(self, task_id: str) -> dict:
        t = self.store.get("tasks", task_id)
        if t is None:
            raise Refused(f"no task {task_id}")
        return t

    def _set(self, t: dict, status: str, why: str, actor: str = "system") -> dict:
        if status not in self.reg.task_lifecycle["transitions"][t["status"]]:
            raise Refused(f"{t['task_id']}: {t['status']} -> {status} is not an allowed transition")
        prev = t["status"]
        t["status"], t["updated_at"] = status, now()
        self.store.put("tasks", t)
        self.audit(actor, f"task {status}", why, t)
        self._lifecycle(t, prev, status, why)
        return t

    # ------------------------------------------------------------------ events (docs/dashboard-events.md, layer A: written by the kit, no model involved)
    def emit(self, kind: str, t: dict, **extra):
        """Append one `kit-event/1` line to events.jsonl: what happened to a task, with a per-project `seq` a reader can resume from."""
        try:
            with self.store.tx():
                self.store.append("events.jsonl", {"v": 1, "seq": self.store.last_seq("events.jsonl") + 1, "ts": now(),
                                                   "project": (self.project or {}).get("name"), "run": f"{t['task_id']}#{t['attempts']}", "task": t["task_id"],
                                                   "attempt": t["attempts"], "agent": t["assigned_agent"], "kind": kind, **extra})
        except OSError:  # observability never blocks the runtime: a task transition must not fail because the event log could not be written
            pass

    def _lifecycle(self, t: dict, prev: str, status: str, why: str):
        text = lambda s, n: supervisor.clean(s, n)  # noqa: E731  one redacted line
        if status == "RUNNING" and prev != "WAITING_APPROVAL":
            return self.emit("task.start", t, state="working", objective=text(t["objective"], 200))
        if status in ("COMPLETED", "FAILED", "CANCELLED", "BLOCKED"):
            open_ = ("PENDING", "PLANNED")
            tasks = self.store.all("tasks")
            done = {x["task_id"] for x in tasks if x["status"] == "COMPLETED"}  # a dependent is next only when this task completed and its other dependencies did too
            nxt = [x for x in tasks if status == "COMPLETED" and t["task_id"] in x["dependencies"] and x["status"] in open_ and set(x["dependencies"]) <= done] or \
                  sorted((x for x in tasks if x["assigned_agent"] == t["assigned_agent"] and x["status"] in open_), key=lambda x: x["created_at"])
            self.emit("task.end", t, state={"COMPLETED": "done", "FAILED": "failed"}.get(status, "stopped"), status=status,
                      summary=text(((t.get("result") or {}).get("summary") if status == "COMPLETED" else why) or why, 300),
                      cost_usd=t["cost_usd"], next=[{"task": x["task_id"], "objective": text(x["objective"], 120)} for x in nxt[:3]])
            if status != "BLOCKED" and all(x["status"] in TERMINAL for x in tasks):
                counts = {}
                for x in tasks:
                    counts[x["status"]] = counts.get(x["status"], 0) + 1
                self.emit("project.done", t, state="done", counts=counts, cost_usd=round(sum(x["cost_usd"] for x in tasks), 6))
            return
        self.emit("task.status", t, state={"WAITING_APPROVAL": "waiting"}.get(status, "working" if status == "RUNNING" else "idle"),
                  **{"from": prev, "to": status, "why": text(why, 200)})

    # ------------------------------------------------------------------ tasks
    def add_task(self, agent: str, objective: str, criteria: list[str], *, skill: str | None = None, artifacts: list[str] | None = None,
                 after: list[str] = (), budget_usd: float | None = None, ai: dict | None = None, parent: str | None = None,
                 level: int = 0, decision_options: list[str] | None = None, inputs: dict | None = None, by: str | None = None) -> dict:
        by = by or self.actor
        if agent in self.off_roster:
            raise Refused(f"'{agent}' is not on this project's roster (use {self.reg.pack.human_prefix}staff)")
        if agent not in self.reg.agents or agent not in self.reg.enabled_agents:
            raise Refused(f"unknown or disabled agent '{agent}'")
        if by in self.reg.agents and not self.policy.can_delegate(by, agent):
            raise Refused(f"decision-rights/delegation-chain: {by} may not assign work to {agent}")
        try:
            sk = self.reg.agent_skill(agent, skill) if skill else None
        except KeyError as e:
            raise Refused(str(e)) from None
        missing = [d for d in after if self.store.get("tasks", d) is None]
        if missing or (parent and self.store.get("tasks", parent) is None):
            raise Refused(f"unknown task(s): {missing or [parent]}")
        cap = self.reg.policy_params("spending").get("max_cost_usd_per_task")
        if budget_usd is not None and cap is not None and budget_usd > cap:
            raise Refused(f"budget {budget_usd} exceeds spending/max_cost_usd_per_task {cap}")
        if ai:
            self._check_ai(ai)
        art_types = set(self.reg.evidence_rules["types"])
        required = list(artifacts or (sk or {}).get("outputs") or [])
        if unknown := [a for a in required if a not in art_types]:
            raise Refused(f"unknown artifact type(s) {unknown}")
        t = {"task_id": new_id("task"), "parent_task_id": parent, "objective": objective, "description": "", "assigned_agent": agent,
             "created_by": by, "owner": by, "priority": "normal", "status": "PENDING", "dependencies": list(after),
             "budget": {"max_cost_usd": budget_usd if budget_usd is not None else cap, "max_wall_seconds": None},
             "inputs": inputs or {}, "required_artifacts": required, "acceptance_criteria": list(criteria),
             "approval_level": level, "evidence": [], "result": None, "created_at": now(), "updated_at": now(),
             "skill": skill, "model_policy": None, "ai": ai, "attempts": 0,
             "max_attempts": self.reg.policy_params("autonomy-limits").get("max_task_attempts", 2),
             "cost_usd": 0.0, "blocked_reason": None, "verification": None, "decision_options": decision_options or [], "approval_id": None}
        self.store.put("tasks", t)
        self.audit(by, "task add", objective[:300], t)
        return t

    def _check_ai(self, ai: dict):
        if errs := errors_for(validator_for(load(schema_file(AI_SCHEMA, self.reg.root)), self.reg.root), ai):
            raise Refused(f"invalid ai selection: {errs[:2]}")
        if ai.get("harness"):
            hdef = self.reg.harnesses.get(ai["harness"])
            why = f"unknown harness '{ai['harness']}'" if hdef is None else harness.unsupported(hdef, ai.get("model"), ai.get("provider"), ai.get("effort"))
            if why:
                raise Refused(why)

    def start(self, task_id: str, actor: str = "system") -> dict:
        """Mark a task RUNNING (counts an attempt). Its dependencies must be COMPLETED; attempts are bounded."""
        with self.store.tx():
            t = self.task(task_id)
            if t["status"] in TERMINAL or t["status"] == "WAITING_APPROVAL":
                raise Refused(f"task {task_id} is {t['status']}")
            unmet = [d for d in t["dependencies"] if self.task(d)["status"] != "COMPLETED"]
            if unmet:
                raise Refused(f"dependencies not completed: {unmet}")
            if t["attempts"] >= t["max_attempts"]:
                raise Refused(f"attempts exhausted ({t['attempts']}/{t['max_attempts']}): fail or cancel it, or add a new task")
            if t["status"] == "PENDING":
                self._set(t, "PLANNED", "dependencies satisfied", actor)
            t["attempts"] += 1
            return self._set(t, "RUNNING", f"attempt {t['attempts']}", actor)

    def release_dependency(self, task_id: str, dependency: str, why: str, actor: str | None = None) -> dict:
        """Drop a dependency that will never complete (CANCELLED or FAILED) from a task that is not terminal, e.g. when its work is
        already proven elsewhere. A decision of the orchestrator: the reason is mandatory and goes to the audit log."""
        if not why.strip():
            raise Refused("a reason is required to release a dependency")
        with self.store.tx():
            t = self.task(task_id)
            if t["status"] in TERMINAL or t["status"] == "RUNNING":
                raise Refused(f"{task_id} is {t['status']}")
            if dependency not in t["dependencies"]:
                raise Refused(f"{task_id} does not depend on {dependency}")
            if (st := self.task(dependency)["status"]) not in ("CANCELLED", "FAILED"):
                raise Refused(f"{dependency} is {st}: only a CANCELLED or FAILED dependency can be released (complete, wait for or cancel it instead)")
            t["dependencies"] = [d for d in t["dependencies"] if d != dependency]
            self.store.put("tasks", t)
            self.audit(actor or self.actor, "dependency released", f"{dependency} ({st}): {why.strip()[:300]}", t)
            return t

    def cancel(self, task_id: str, why: str, actor: str | None = None) -> dict:
        actor = actor or self.actor
        with self.store.tx():
            return self._set(self.task(task_id), "CANCELLED", why, actor)

    def fail(self, task_id: str, why: str, actor: str | None = None) -> dict:
        actor = actor or self.actor
        with self.store.tx():
            t = self.task(task_id)
            t["blocked_reason"] = why[:500]
            return self._set(t, "FAILED", why, actor)

    def submit(self, task_id: str, result: dict, usage: dict | None = None) -> dict:
        """Process an agent-result for a RUNNING task: contract check (a violation uses the attempt), policy filter, approval gate, actions (exactly once), evidence
        verification, then COMPLETED, or back to PLANNED with feedback (bounded by attempts), or FAILED / BLOCKED.
        Returns {"status", "notes"}. `usage`: what a native subagent consumed, as its harness reports it (recorded, never charged)."""
        errs = validate_instance("agent-result", result, self.reg.root)
        if usage:  # as the session saw it (a total only); the SubagentStop hook records the exact split instead
            self.record_native_usage(task_id, model=usage.get("model"), duration_s=round((usage.get("duration_ms") or 0) / 1000, 1),
                                     tokens={"totalTokens": int(usage["total_tokens"])} if isinstance(usage.get("total_tokens"), (int, float)) else {},
                                     tool_uses=usage.get("tool_uses"))
        with self.store.tx():
            t = self.task(task_id)
            if t["status"] != "RUNNING":
                raise Refused(f"task {task_id} is {t['status']}, not RUNNING")
            if errs:  # counts as the attempt: the feedback reaches the next start through inputs.previous_attempt_feedback
                why = f"result violates the agent-result contract: {errs[:5]}"
                self.audit(t["assigned_agent"], "reject result", why, t, result="rejected")
                return self._retry_or_fail(t, why)
            t["result"] = {"summary": result["summary"][:3000], "raw": result, "attempt": t["attempts"]}
            self.store.put("tasks", t)
            return self._process(t)

    def _process(self, t: dict) -> dict:
        res, agent = t["result"]["raw"], t["assigned_agent"]
        if res["status"] == "blocked":
            t["blocked_reason"] = (res.get("blocked_reason") or res["summary"])[:500]
            self._set(t, "BLOCKED", t["blocked_reason"], agent)
            return {"status": "BLOCKED", "notes": [t["blocked_reason"]]}
        if res["status"] == "failed":
            return self._retry_or_fail(t, f"agent reported failure: {res['summary'][:300]}")
        if t["result"].get("attempt_registered") != t["attempts"]:  # re-processing after approval must not duplicate artifacts
            self._register_artifacts(t, res.get("artifacts") or [], agent)
            self._filter_unauthorized(t, res)
            t["result"]["attempt_registered"] = t["attempts"]
            self.store.put("tasks", t)
        actions = res.get("actions") or []
        spend = sum(a.get("estimated_cost_usd") or 0 for a in actions) + ((res.get("decision") or {}).get("cost_estimate_usd") or 0)
        level = self.policy.required_level(caps=[a["capability"] for a in actions], spend=spend, extra=t["approval_level"])
        if level and self.policy.needs_approval(agent, level) and not self._approved(t):
            what = "; ".join(f"{a['capability']}: {a['reason']}" for a in actions) or t["objective"]
            apr = self._request_approval(t, level, what, res["summary"])
            return {"status": "WAITING_APPROVAL", "notes": [f"approval {apr['approval_id']} (level {level}) from {apr['approver']}"]}
        if level == 3 and not self.store.done(f"notify3:{t['task_id']}"):
            self.store.mark(f"notify3:{t['task_id']}")
            self.notify_human(agent, f"Level-3 action proceeding: {t['objective'][:200]} -> {res['summary'][:400]}", t["task_id"], "level-3")
        failed = self._execute_actions(t, actions)
        if failed:
            t["blocked_reason"] = failed
            self._set(t, "FAILED", failed + " (no automatic retry)", "system")
            self.notify_human("system", f"Irreversible action failed: {failed}", t["task_id"], "escalation")
            return {"status": "FAILED", "notes": [failed]}
        arts = [self.store.get("artifacts", a) for a in self._artifact_ids(t)]
        known = {a["artifact_id"] for a in self.store.all("artifacts")}
        verdict, notes = verify.verify_task(self.reg.evidence_rules, t, arts, res, self._workspace(t), known, self.evidence_checks)
        t["verification"] = {"verdict": verdict, "notes": notes[:20], "verifier": "deterministic"}
        self.store.put("tasks", t)
        if verdict == "FAIL":
            return self._retry_or_fail(t, "verification failed: " + "; ".join(notes)[:800])
        try:
            self.apply_result(t, res)
        except InvalidRecord as e:  # nothing was written: company state is validated before the atomic write
            return self._retry_or_fail(t, f"state update rejected: {e}")
        for a in arts:
            self.store.put("artifacts", {**a, "status": "accepted"})
        t["evidence"] = [a["artifact_id"] for a in arts]
        self.record_outcome(t, res)
        self._set(t, "COMPLETED", "evidence verified" + (" (partial)" if res["status"] == "partial" else ""), agent)
        return {"status": "COMPLETED", "notes": t["result"].get("policy_rejections", [])}

    def _retry_or_fail(self, t: dict, reason: str) -> dict:
        t["inputs"] = {**t["inputs"], "previous_attempt_feedback": reason[:1500]}
        over = t["budget"]["max_cost_usd"] is not None and t["cost_usd"] >= t["budget"]["max_cost_usd"]
        if t["attempts"] < t["max_attempts"] and not over:
            self._set(t, "PLANNED", f"retry: {reason[:300]}")
            return {"status": "PLANNED", "notes": [reason]}
        t["blocked_reason"] = reason[:500]
        self._set(t, "FAILED", reason[:300] + (" (task budget exhausted)" if over else " (attempts exhausted)"))
        return {"status": "FAILED", "notes": [reason]}

    def _workspace(self, t: dict) -> Path:
        """Where a worker runs: inputs.workdir inside the project (e.g. one of its repos), else a scratch dir per initiative."""
        if self.project_dir and t["inputs"].get("workdir"):
            ws = (self.project_dir / t["inputs"]["workdir"]).resolve()
            if not ws.is_relative_to(self.project_dir.resolve()):
                raise Refused(f"workdir {t['inputs']['workdir']} is outside the project")
            return ws
        ws = self.workspaces_dir / self.workspace_key(t)
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def _artifact_ids(self, t: dict) -> list[str]:
        d = self.store.path / "artifacts"
        return sorted(p.stem for p in d.glob(f"art-{t['task_id']}-{t['attempts']}-*.json")) if d.exists() else []

    def _register_artifacts(self, t: dict, items: list[dict], creator: str, offset: int = 0):
        allowed = set(self.reg.evidence_rules["types"])
        for i, a in enumerate(items, start=offset):
            aid = f"art-{t['task_id']}-{t['attempts']}-{i:02d}"
            loc = a.get("location") or f"artifacts/{aid}.md"
            if a.get("content"):
                self.store.put_text(f"artifacts/{aid}.md", f"# {a['title']}\n\n{a['content']}")
            self.store.put("artifacts", {"artifact_id": aid, "type": a["type"] if a["type"] in allowed else "other", "location": loc,
                                         "created_by": creator, "created_at": now(), "version": "1", "status": "submitted",
                                         "evidence": a.get("evidence") or {"summary": a["title"]}, "task_id": t["task_id"]})

    def _filter_unauthorized(self, t: dict, res: dict):
        """Drop result items the agent may not produce; each one is recorded."""
        agent, dropped, kept = t["assigned_agent"], [], []
        for a in res.get("actions") or []:
            why = self.policy.check_capability(agent, a["capability"])
            (dropped.append(f"action: {why}") if why else kept.append(a))
        res["actions"] = kept
        self.filter_extras(t, res, dropped)
        for d in dropped:
            self.audit("policy", "reject", d, t, result="rejected")
        if dropped:
            t["result"]["policy_rejections"] = dropped
        t["result"]["raw"] = res

    def _execute_actions(self, t: dict, actions: list[dict]) -> str | None:
        """Run each action through its adapter exactly once (ledger). Returns a reason if an irreversible action failed."""
        produced = []
        for i, act in enumerate(actions):
            key, cap = f"act:{t['task_id']}:{t['attempts']}:{i}", act["capability"]
            prior = self.store.done(key)
            if prior:
                outcome = prior["outcome"]
            elif self.store.done(key + ":start"):
                return f"{cap} started before a crash with no recorded outcome; not repeated automatically (needs review)"
            else:
                gate = self.action_gate(t, cap)
                fn = self.adapters.get(cap)
                if gate:
                    outcome = {"ok": False, "status": "denied", "detail": gate}
                elif fn is None or not self.policy.available(cap):
                    outcome = {"ok": False, "status": "unavailable", "detail": f"{cap} has no adapter in this environment"}
                else:
                    self.store.mark(key + ":start")
                    try:
                        outcome = {"status": "done", **fn(self, t, act.get("params") or {})}
                    except Exception as e:  # noqa: BLE001 - an adapter error is an outcome, not a crash
                        outcome = {"ok": False, "status": "error", "detail": f"{type(e).__name__}: {e}"}
                self.store.mark(key, outcome=outcome)
                self.audit(t["assigned_agent"], f"action {cap}", act.get("reason", ""), t, result=outcome.get("status"))
            if outcome.get("artifact"):
                produced.append(outcome["artifact"])
            if outcome["status"] not in ("unavailable", "denied") and not outcome.get("ok") and self.reg.capabilities[cap]["risk"] == "irreversible":
                return f"{cap} failed: {outcome.get('detail')}"
        t["result"]["actions"] = [self.store.done(f"act:{t['task_id']}:{t['attempts']}:{i}")["outcome"] for i in range(len(actions))]
        self.store.put("tasks", t)
        if produced:
            self._register_artifacts(t, [{**p, "content": None} for p in produced], "system", offset=50)
        return None

    # ------------------------------------------------------------------ approvals
    def _approved(self, t: dict) -> bool:
        a = self.store.get("approvals", t["approval_id"]) if t.get("approval_id") else None
        return bool(a and a["status"] == "granted")

    def _request_approval(self, t: dict, level: int, action: str, justification: str) -> dict:
        approver = self.policy.approver_for(t["assigned_agent"], level)
        a = {"approval_id": new_id("apr"), "task_id": t["task_id"], "requested_by": t["assigned_agent"], "level": level,
             "action": action[:1000], "justification": justification[:1500], "evidence": self._artifact_ids(t), "status": "pending",
             "requested_at": now(), "resolved_by": None, "resolved_at": None, "approver": approver, "review_task_id": None}
        self.store.put("approvals", a)
        t["approval_id"] = a["approval_id"]
        self._set(t, "WAITING_APPROVAL", f"level {level} approval from {approver}")
        self.emit("gate", t, state="waiting", gate="approval", decision="requested", action=supervisor.clean(action, 200), by=approver)
        if approver == self.reg.pack.human_id:
            self.notify_human(t["assigned_agent"], f"Approval needed (level {level}) [{a['approval_id']}]: {action[:500]} — {justification[:600]}",
                               t["task_id"], "approval")
        return a

    def resolve_approval(self, approval_id: str, granted: bool, by: str, note: str = "") -> dict:
        """`by` is 'board' or an agent id. Agents may not approve their own request or above their authority;
        level 4 is the Board's alone. A granted approval resumes the task with its saved result."""
        with self.store.tx():
            a = self.store.get("approvals", approval_id)
            if a is None or a["status"] != "pending":
                raise Refused(f"approval {approval_id} is not pending")
            human = self.reg.pack.human_id
            if by != human:
                if by not in self.reg.agents:
                    raise Refused(f"unknown approver '{by}'")
                if by == a["requested_by"]:
                    raise Refused("approval-levels/no-self-escalation: a requester cannot approve its own request")
                if a["approver"] == human or self.reg.agents[by]["authority"]["max_approval_level"] < a["level"]:
                    raise Refused(f"{by} cannot approve level {a['level']} (approver: {a['approver']})")
            a.update(status="granted" if granted else "rejected", resolved_by=by, resolved_at=now(), resolution_note=note[:1000])
            self.store.put("approvals", a)
            t = self.task(a["task_id"])
            self.audit(by, "approve" if granted else "reject", note or "-", t, approval_id=approval_id)
            self.emit("gate", t, state="working" if granted else "failed", gate="approval", decision="granted" if granted else "rejected", action=supervisor.clean(a["action"], 200), by=by)
            if t["status"] != "WAITING_APPROVAL":
                return {"status": t["status"], "notes": []}
            if not granted:
                t["blocked_reason"] = f"approval rejected by {by}: {note[:300]}"
                self._set(t, "FAILED", t["blocked_reason"], by)
                return {"status": "FAILED", "notes": [t["blocked_reason"]]}
            self._set(t, "RUNNING", f"approval {approval_id} granted by {by}", by)
            return self._process(t)

    # ------------------------------------------------------------------ worker harness dispatch
    def record_native_usage(self, task_id: str, *, model: str | None = None, tokens: dict | None = None, duration_s: float = 0.0,
                            equivalent: float | None = None, tool_uses: int | None = None, key: str | None = None):
        """A native subagent runs on the orchestrator session's own account: never charged to the budget, its usage kept as
        data. `key` (the subagent id) records it once; `equivalent` is None when only a total is known."""
        t = self.task(task_id)
        with self.store.tx():
            if key and self.store.done(f"native:{key}"):
                return
            self.store.append("usage.jsonl", {"at": now(), "task_id": task_id, "agent": t["assigned_agent"], "harness": "native",
                                              "model": str(model or "")[:60] or None, "provider": None, "effort": None, "tokens": tokens or {},
                                              "cost_usd": 0.0, "equivalent_cost_usd": equivalent, "estimated": False, "ok": True,
                                              "duration_s": duration_s, "error": "", "tool_uses": tool_uses})
            if key:
                self.store.mark(f"native:{key}")

    def spend(self) -> dict:
        """Charged spend (what budgets count; flat plans 0) and list-price equivalent (data for projections), today and in total."""
        today, calls = now()[:10], self.store.read_log("usage.jsonl")
        tot = lambda key, rows: round(sum(u.get(key) or 0 for u in rows), 6)  # noqa: E731
        todays = [u for u in calls if u["at"][:10] == today]
        return {"today_usd": tot("cost_usd", todays), "today_equivalent_usd": tot("equivalent_cost_usd", todays),
                "total_usd": tot("cost_usd", calls), "total_equivalent_usd": tot("equivalent_cost_usd", calls),
                "unpriced_calls": sum(u.get("equivalent_cost_usd") is None for u in calls),
                "today_tokens": sum(_tokens(u) for u in todays), "total_tokens": sum(_tokens(u) for u in calls)}

    def daily_spend(self) -> float:
        today = now()[:10]
        return sum(u["cost_usd"] for u in self.store.read_log("usage.jsonl") if u["at"][:10] == today)

    def dispatch(self, task_id: str) -> dict:
        """Run the task on a worker harness (its AI selection first, model policy candidates as fallback), charge the
        cost, and submit the result; returns when it is settled. Refused when the daily cap is reached or the task already has a worker."""
        prep = self._prepare(task_id)
        return prep if isinstance(prep, dict) else self._execute(task_id, *prep)

    def dispatch_bg(self, task_id: str, wait_seconds: float = 0) -> dict:
        """Start the worker and return at once with {"status": "RUNNING"} (or the settled result when it finished within `wait_seconds`).
        The run settles by itself on a background thread (COMPLETED / PLANNED / FAILED / BLOCKED); a server that dies leaves the detached
        worker to the next server's `reconcile`. Follow it with `run_log`, check it with `task_show`."""
        prep = self._prepare(task_id)
        if isinstance(prep, dict):
            return prep
        box: dict = {}

        def work():
            try:
                box["r"] = self._execute(task_id, *prep)
            except Exception as e:  # noqa: BLE001 - never leave the task RUNNING with nobody to settle it
                box["e"] = e
                with self.store.tx():
                    t = self.task(task_id)
                    if t["status"] == "RUNNING":
                        t["blocked_reason"] = f"dispatch error: {type(e).__name__}: {e}"[:500]
                        self._set(t, "BLOCKED", t["blocked_reason"])
            finally:
                self._active.discard(task_id)

        self._active.add(task_id)
        th = threading.Thread(target=work, daemon=True, name=f"dispatch-{task_id}")
        th.start()
        th.join(wait_seconds)
        if "r" in box:
            return box["r"]
        if "e" in box:
            raise box["e"]
        return {"status": "RUNNING", "started": True, "notes": ["the worker runs in the background and the task settles by itself (COMPLETED, PLANNED, FAILED or BLOCKED); "
                                                              "follow it with run_log, check it with task_show or tasks"]}

    def _running(self, task_id: str) -> bool:
        """A worker of this task is alive (in this server, or detached from an earlier one)."""
        f = self._run_file(task_id)
        rec = json.loads(f.read_text()) if f.exists() else None
        return task_id in self._active or bool(rec and not rec["done"] and harness.tree_alive(rec["pid"]))

    def _prepare(self, task_id: str):
        """Checks and the worker request; a dict is a final answer (BLOCKED), a tuple goes to `_execute`."""
        cap = self.reg.policy_params("spending").get("max_cost_usd_per_day")
        if cap is not None and self.daily_spend() >= cap:
            raise Refused(f"budget-control/daily-cap: {self.daily_spend():.2f} >= {cap} USD today")
        t = self.task(task_id)
        if self._running(task_id):
            raise Refused(f"{task_id} already has a worker running (run_log shows it, run_stop ends it)")
        if t["status"] != "RUNNING":  # already started by `task_start`: keep its attempt
            t = self.start(task_id)
        agent = self.reg.agents[t["assigned_agent"]]
        skill = self.reg.agent_skill(agent["id"], t["skill"]) if t.get("skill") else None
        lacking = [c for c in (skill or {}).get("required_capabilities", []) if c not in agent["tools"] or not self.policy.available(c)]
        if lacking:
            with self.store.tx():
                t["blocked_reason"] = f"capabilities missing or unavailable: {lacking}"
                self._set(t, "BLOCKED", t["blocked_reason"])
            return {"status": "BLOCKED", "notes": [t["blocked_reason"]]}
        system, prompt = self.prompt(t, agent, skill)
        caps = [c for c in agent["tools"] if self.reg.capabilities[c].get("adapter") == "harness"]
        budget = t["budget"]["max_cost_usd"]
        limits = self.limits()
        wall = t["budget"]["max_wall_seconds"]
        req = harness.Request(system, prompt, str(self._workspace(t)), caps, max(budget, 0.01) if budget is not None else None,
                              wall if wall and wall != LEGACY_WALL else limits.max_wall_seconds, on_progress=self.on_progress,
                              limits=limits, run_dir=str(self.store.path / "runs"), run_id=task_id)
        ai = self.reg.ai_for(agent["id"], skill, t.get("ai"))
        policy_id = self.reg.model_policy_for(agent["id"], skill, t.get("model_policy"))
        return req, ai, policy_id, agent, skill

    def _run_end(self, task_id: str, a, ai: dict):
        self.emit("run.end", self.task(task_id), state="working" if a.ok else "failed", harness=a.harness, model=a.model, provider=a.provider, effort=a.effort or ai.get("effort"),
                  ok=a.ok, error=supervisor.clean(a.error, 300) or None, stop=a.stopped or None, tokens=a.tokens or None,
                  cost_usd=a.cost_usd, equivalent_cost_usd=a.equivalent_cost_usd, duration_s=round(a.duration_s, 1))

    def _execute(self, task_id: str, req, ai: dict, policy_id: str, agent: dict, skill):
        def on_attempt(kind: str, x):  # runs on the worker thread, outside the store lock: `task()` is re-read so `attempt` is current
            if kind == "end":
                return self._run_end(task_id, x, ai)
            prior, extra = x.pop("prior"), {}
            if prior:
                extra = {"prior_attempt": {"harness": prior.harness, "model": prior.model, "error": supervisor.clean(prior.error, 300), "stop": prior.stopped or None}}
                if wd := supervisor.workdir_summary(req.cwd):
                    extra["workdir"] = wd
            self.emit("run.start", self.task(task_id), state="working", **x, **extra)
        try:
            attempts = self.router.call_with_fallback(policy_id, req, self.invoke, ai=ai, on_attempt=on_attempt,
                                                      fallbacks=self.reg.mapped_fallbacks(agent["id"], (skill or {}).get("id")))
        except harness.Unavailable as e:
            with self.store.tx():
                t = self.task(task_id)
                t["attempts"] -= 1
                t["blocked_reason"] = f"harness unavailable: {e}"
                self._set(t, "BLOCKED", t["blocked_reason"])
            return {"status": "BLOCKED", "notes": [t["blocked_reason"]]}
        return self._settle(task_id, attempts, ai)

    def limits(self) -> supervisor.Limits:
        """Supervisor thresholds from config/models `supervisor` (defaults in supervisor.DEFAULTS)."""
        return supervisor.Limits.from_config(self.reg.config["models"].get("supervisor"))

    def _settle(self, task_id: str, attempts: list, ai: dict) -> dict:
        """Charge and record a worker's attempts, then submit the last result (or retry/fail the task)."""
        with self.store.tx():
            self.store.put("runtime", self.router.health, key="health")
            t = self.task(task_id)
            for a in attempts:
                self.store.append("usage.jsonl", {"at": now(), "task_id": task_id, "agent": t["assigned_agent"], "harness": a.harness,
                                                  "model": a.model, "provider": a.provider, "effort": a.effort or ai.get("effort"), "tokens": a.tokens, "cost_usd": a.cost_usd, "equivalent_cost_usd": a.equivalent_cost_usd, "estimated": a.cost_estimated,
                                                  "ok": a.ok, "duration_s": round(a.duration_s, 1), "error": a.error[:300]})
                t["cost_usd"] = round(t["cost_usd"] + a.cost_usd, 6)
            self.store.put("tasks", t)
            self._budget_warning()
            last = attempts[-1]
            if last.stopped:  # stuck, looping, stopped on request or hit the ceiling: stop and report, no retry and no other route on its own
                self._mark_run(task_id, last.stopped, last.error)
                t["blocked_reason"] = last.error[:500]
                t["inputs"] = {**t["inputs"], "previous_attempt_feedback": last.error[:1500]}
                self._set(t, "BLOCKED", f"{last.stopped}: {last.error[:300]}")
                return {"status": "BLOCKED", "stopped": last.stopped, "notes": [last.error], "activity_log": str(self._run_file(task_id).with_suffix(".activity.log"))}
            if not last.ok:
                self._mark_run(task_id)
                return self._retry_or_fail(t, f"all model candidates failed; last: {last.harness}/{last.model}: {last.error}")
        self._mark_run(task_id)
        return self.submit(task_id, last.output)


    # ------------------------------------------------------------------ workers that outlive the server
    def _run_file(self, task_id: str) -> Path:
        return self.store.path / "runs" / f"{task_id}.json"

    def _mark_run(self, task_id: str, stopped: str = "", why: str = ""):
        f = self._run_file(task_id)
        if f.exists():
            f.write_text(json.dumps({**json.loads(f.read_text()), "done": True, **({"stopped": stopped, "stop_reason": why[:1500]} if stopped else {})}))

    def run_log(self, task_id: str, lines: int = 20) -> dict:
        """The last `lines` lines of a task's activity log (one line per tool call: time, tool, short target, model seconds) and whether its worker runs."""
        t = self.task(task_id)
        f = self._run_file(task_id)
        log, rec = f.with_suffix(".activity.log"), json.loads(f.read_text()) if f.exists() else None
        running = task_id in self._active or bool(rec and not rec["done"] and harness.tree_alive(rec["pid"]))
        return {"task_id": task_id, "status": t["status"], "running": running, "elapsed_s": int(time.time() - rec["started"]) if running and rec and not rec["done"] else None,
                "seconds_since_last_line": int(time.time() - log.stat().st_mtime) if log.exists() else None, "log_file": str(log) if log.exists() else None,
                "stopped": (rec or {}).get("stopped"), "lines": supervisor.tail_lines(log, max(1, min(int(lines), 200)))}

    def run_stop(self, task_id: str, reason: str, by: str | None = None) -> dict:
        """Ask the supervisor to stop a running worker (it kills the process tree within seconds and the task becomes BLOCKED with this reason).
        A decision of the orchestrator, never routine: the reason is mandatory and goes to the audit log."""
        if not reason.strip():
            raise Refused("a reason is required to stop a run")
        t, f = self.task(task_id), self._run_file(task_id)
        for _ in range(50 if task_id in self._active else 1):  # a worker being started has no record yet
            rec = json.loads(f.read_text()) if f.exists() else None
            if rec and not rec["done"]:
                break
            time.sleep(0.1)
        if t["status"] != "RUNNING" or not rec or rec["done"] or not harness.tree_alive(rec["pid"]):
            raise Refused(f"{task_id} has no running worker (status {t['status']})")
        f.with_suffix(".stop").write_text(reason.strip()[:500])
        self.audit(by or self.actor, "run stop", reason.strip()[:300], t)
        return {"task_id": task_id, "stop_requested": True, "note": "the worker is killed within ~10 s and the task becomes BLOCKED; its changes stay in the workdir"}

    def reconcile(self) -> list[str]:
        """On server start: settle RUNNING tasks whose worker an earlier server left behind (record not done, its owner dead).
        A worker still alive is awaited here (killed at its wall limit); a finished one is collected from its files; one that
        left nothing fails the task with the reason. Returns the task ids taken over."""
        taken = []
        for t in self.store.all("tasks"):
            f = self._run_file(t["task_id"])
            if t["status"] != "RUNNING" or not f.exists():
                continue
            rec = json.loads(f.read_text())
            if rec["done"] or rec["owner"] == os.getpid() or harness.pid_alive(rec["owner"]):
                continue
            taken.append(t["task_id"])
            threading.Thread(target=self._adopt, args=(t["task_id"], rec), daemon=True).start()
        return taken

    def _adopt(self, task_id: str, rec: dict):
        base = self._run_file(task_id).with_suffix("")
        sup = (supervisor.Supervisor(base, rec["cwd"], rec["harness"], supervisor.Limits(**rec["limits"]), rec["started"], harness.run_marker(task_id))
               if rec.get("cwd") and rec.get("limits") else None)  # records of a 0.8.x server carry no supervisor settings
        verdict, last = None, 0.0
        while harness.tree_alive(rec["pid"]):
            now_ = time.time()
            if now_ - rec["started"] >= rec["limit"]:
                verdict = ("wall", f"safety ceiling of {rec['limit']}s reached (max_wall_seconds), worker process group killed")
                break
            if sup and now_ - last >= harness.SUPERVISE_SECONDS:
                last = now_
                verdict = sup.poll(now_, harness._tree_cpu(rec["pid"])[1], base.with_suffix(".out").stat().st_size if base.with_suffix(".out").exists() else 0)
                if verdict:
                    break
            time.sleep(2)
        hdef = self.reg.harnesses[rec["harness"]]
        if verdict:
            harness.kill_group(rec["pid"])
            r = harness.halted(hdef, rec["model"], rec["provider"], sup or supervisor.Supervisor(base, rec.get("cwd") or ".", rec["harness"], supervisor.Limits(),
                                                                                                 rec["started"]), *verdict, time.time() - rec["started"])
        elif (rc := base.with_suffix(".rc")).exists():
            r = harness.collect(hdef, rec["model"], rec["provider"], base.with_suffix(".out").read_text(), base.with_suffix(".err").read_text(),
                                int(rc.read_text() or 1), time.time() - rec["started"])
        else:
            r = harness.CallResult(False, hdef["id"], rec["model"], error="worker lost: the server that started it stopped and the worker did not finish in time")
        r.provider, r.effort = rec["provider"], rec["effort"]
        t = self.task(task_id)
        agent = self.reg.agents[t["assigned_agent"]]
        ai = self.reg.ai_for(agent["id"], self.reg.agent_skill(agent["id"], t["skill"]) if t.get("skill") else None, t.get("ai"))
        self._run_end(task_id, r, ai)
        self._settle(task_id, [r], ai)

    def _budget_warning(self):
        """Tell the human once a day when spend reaches BUDGET_WARNING of the daily cap (the cap itself refuses dispatch)."""
        cap, key = self.reg.policy_params("spending").get("max_cost_usd_per_day"), f"budget-warning:{now()[:10]}"
        if cap and self.daily_spend() >= BUDGET_WARNING * cap and not self.store.done(key):
            self.store.mark(key)
            self.notify_human("system", f"Spend today {self.daily_spend():.2f} USD reached {BUDGET_WARNING:.0%} of the daily cap {cap} USD", kind="budget")

    def prompt(self, t: dict, agent: dict, skill: dict | None) -> tuple[str, str]:
        """System prompt (role, policies, skill) and the task contract for a worker harness."""
        caps = {c: ("available" if self.policy.available(c) else "UNAVAILABLE (requires configuration)") for c in agent["tools"]}
        definition = {k: agent[k] for k in self.reg.pack.def_keys if k in agent}
        pol = [f"- {p['id']}/{r['id']} ({r['enforcement']}): {r['statement']}" for ref in agent["policies"]
               for p in [self.reg.policy(ref)] for r in p["rules"]]
        parts = [self.reg.preamble, (self.reg.root / agent["prompt_file"]).read_text(),
                 "## Your definition\n```yaml\n" + yaml.safe_dump(definition, sort_keys=False, allow_unicode=True) + "```",
                 "## Your capabilities\n" + "\n".join(f"- {c}: {s}" for c, s in caps.items()),
                 "## Policies in force\n" + "\n".join(pol)]
        if skill:
            parts.append(f"## Skill for this task: {skill['id']}@{skill['version']}\n```yaml\n" + yaml.safe_dump(
                {k: skill[k] for k in ("purpose", "preconditions", "procedure", "validation", "failure_modes", "outputs")},
                sort_keys=False, allow_unicode=True) + "```")
        rules = self.reg.evidence_rules["types"]
        contract = {"task_id": t["task_id"], "objective": t["objective"], "acceptance_criteria": t["acceptance_criteria"],
                    "required_artifacts": {a: [r["key"] for r in rules.get(a, [])] for a in t["required_artifacts"]},
                    "decision_options": t["decision_options"] or None, "budget_usd": t["budget"]["max_cost_usd"],
                    "attempt": t["attempts"], "inputs": t["inputs"], **({"project_goal": self.project["goal"]} if self.project.get("goal") else {})}
        deps = [{"task": d["objective"], "agent": d["assigned_agent"], "summary": (d.get("result") or {}).get("summary"),
                 "artifacts": [self._artifact_view(a) for a in d["evidence"]]} for d in map(self.task, t["dependencies"])]
        visible = self.visible_state(agent)
        user = "\n\n".join([
            "# Task contract\n```json\n" + json.dumps(contract, indent=1, default=str)[:20000] + "\n```",
            "# Results of the tasks this one depends on\n```json\n" + json.dumps(deps, indent=1, default=str)[:60000] + "\n```",
            *(["# Company state (your read scope)\n```json\n" + json.dumps(visible, indent=1, default=str)[:20000] + "\n```"] if visible is not None else []),
            "# Answer\nReturn one agent-result JSON object. Each required artifact type needs the evidence keys listed in the contract. "
            "Use `decision.selected_option` from decision_options when given. Work in the current directory (your task workspace)."])
        return "\n\n".join(parts), user

    def _artifact_view(self, aid: str) -> dict | None:
        a = self.store.get("artifacts", aid)
        if a is None:
            return None
        body = self.store.path / a["location"]
        return {"type": a["type"], "evidence": a["evidence"],
                "content": body.read_text()[:8000] if a["location"].startswith("artifacts/") and body.exists() else None}
