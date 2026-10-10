"""Company tools: the deterministic guardrails an orchestrator session (Claude Code or zh) works through.

The orchestrator decides what happens: the plan, who does what, the order, retries, when to stop.
These operations refuse what must not happen: an invalid task transition, completion without evidence, an action outside
an agent's capabilities or above its authority, a side effect repeated after a crash, spend over the daily cap, a state
write outside an agent's scope, an invalid product stage change. A refusal raises `Refused` with the reason.

Work is done either by the orchestrator's own subagents (native/: `task start`, then `task submit` with the agent-result
JSON) or by a worker harness (`dispatch`: claude -p / zh -p with the task's AI selection, then submit). Exposed to sessions by the MCP server in companyos.mcp.
"""
import json
from pathlib import Path

import yaml

from . import harness, verify
from .adapters import ADAPTERS
from .policy import Policy
from .registry import Registry
from .store import InvalidRecord, Store, new_id, now
from .validate import AI_SCHEMA, ROOT, errors_for, load, validate_instance, validator_for

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


class Refused(Exception):
    """A guardrail said no. The message is the reason."""


PROJECT_FILE = "company.yaml"  # <workspace>/<project>/company.yaml; state in <workspace>/<project>/.company/


def projects(workspace: Path) -> dict[str, Path]:
    """Workspace projects the company runs: folders holding a company.yaml."""
    return {p.parent.name: p.parent for p in sorted(Path(workspace).glob(f"*/{PROJECT_FILE}"))}


class Company:
    @classmethod
    def for_project(cls, project_dir: Path, **kw) -> "Company":
        """The company as configured for one workspace project: its own state, budget, agents and default AI."""
        project_dir = Path(project_dir)
        cfg = yaml.safe_load((project_dir / PROJECT_FILE).read_text()) or {}
        if errs := errors_for(validator_for(load(ROOT / "company/project.schema.json")), cfg):
            raise Refused(f"{project_dir / PROJECT_FILE}: {errs[:3]}")
        c = cls(state_dir=project_dir / ".company" / "state", **kw)
        c.project_dir = project_dir
        conf = c.reg.config
        spending = {**(conf["policies"].get("parameters") or {}).get("spending", {}),
                    **({"max_cost_usd_per_day": cfg["daily_budget_usd"]} if "daily_budget_usd" in cfg else {}),
                    **({"max_cost_usd_per_task": cfg["task_budget_usd"]} if "task_budget_usd" in cfg else {})}
        conf["policies"] = {**conf["policies"], "parameters": {**(conf["policies"].get("parameters") or {}), "spending": spending}}
        if "agents" in cfg:
            unknown = set(cfg["agents"]) - set(c.reg.agents)
            if unknown:
                raise Refused(f"{project_dir / PROJECT_FILE}: unknown agents {sorted(unknown)}")
            conf["agents"] = {**conf["agents"], "enabled": [a for a in conf["agents"].get("enabled", []) if a in cfg["agents"]]}
        if "default_ai" in cfg:
            conf["models"] = {**conf["models"], "default_ai": cfg["default_ai"]}
        return c

    def __init__(self, root: Path | None = None, state_dir: Path | None = None, invoke=None, adapters=None):
        self.project_dir: Path | None = None
        self.reg = Registry(root)
        self.store = Store(state_dir or self.reg.state_dir)
        self.workspaces_dir = (Path(state_dir) / "workspaces") if state_dir else self.reg.workspaces_dir
        self.adapters = {**ADAPTERS, **(adapters or {})}
        self.policy = Policy(self.reg, set(adapters or {}))
        self.router = harness.Router(self.reg, self.store.get("runtime", "health") or {}, check_installed=invoke is None)
        self.invoke = invoke or harness.invoke
        if not self.store.company():
            mission = (self.reg.root / "company/mission/mission.md").read_text()
            self.store.save_company({"schema_version": 2, "mission": mission.strip(), "products": [], "decisions": []})

    # ------------------------------------------------------------------ records
    def audit(self, actor: str, action: str, why: str, task: dict | None = None, **extra):
        self.store.append("audit.jsonl", {"at": now(), "actor": actor, "action": action, "why": why[:1000],
                                          "task_id": (task or {}).get("task_id"), **extra})

    def board_message(self, sender: str, message: str, task_id: str | None = None, kind: str = "info"):
        self.store.append("board/inbox.jsonl", {"at": now(), "from": sender, "kind": kind, "message": message[:4000], "task_id": task_id})

    def task(self, task_id: str) -> dict:
        t = self.store.get("tasks", task_id)
        if t is None:
            raise Refused(f"no task {task_id}")
        return t

    def _set(self, t: dict, status: str, why: str, actor: str = "system") -> dict:
        if status not in self.reg.task_lifecycle["transitions"][t["status"]]:
            raise Refused(f"{t['task_id']}: {t['status']} -> {status} is not an allowed transition")
        t["status"], t["updated_at"] = status, now()
        self.store.put("tasks", t)
        self.audit(actor, f"task {status}", why, t)
        return t

    # ------------------------------------------------------------------ tasks
    def add_task(self, agent: str, objective: str, criteria: list[str], *, skill: str | None = None, artifacts: list[str] | None = None,
                 after: list[str] = (), budget_usd: float | None = None, ai: dict | None = None, parent: str | None = None,
                 level: int = 0, decision_options: list[str] | None = None, inputs: dict | None = None, by: str = "ceo") -> dict:
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
             "budget": {"max_cost_usd": budget_usd if budget_usd is not None else cap, "max_tokens": None, "max_wall_seconds": 1800},
             "deadline": None, "inputs": inputs or {}, "required_artifacts": required, "acceptance_criteria": list(criteria),
             "approval_level": level, "evidence": [], "result": None, "created_at": now(), "updated_at": now(),
             "workflow_run_id": None, "step_id": None, "skill": skill, "model_policy": None, "ai": ai, "attempts": 0,
             "max_attempts": self.reg.policy_params("autonomy-limits").get("max_task_attempts", 2), "tried_agents": [agent],
             "cost_usd": 0.0, "blocked_reason": None, "verification": None, "verification_mode": "deterministic", "verifier_agent": None,
             "decision_options": decision_options or [], "approval_id": None, "kind": "work", "awaiting_task": None, "waived": False,
             "iteration": 0, "optional": False}
        self.store.put("tasks", t)
        self.audit(by, "task add", objective[:300], t)
        return t

    def _check_ai(self, ai: dict):
        if errs := errors_for(validator_for(load(ROOT / AI_SCHEMA)), ai):
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

    def cancel(self, task_id: str, why: str, actor: str = "ceo") -> dict:
        with self.store.tx():
            return self._set(self.task(task_id), "CANCELLED", why, actor)

    def fail(self, task_id: str, why: str, actor: str = "ceo") -> dict:
        with self.store.tx():
            t = self.task(task_id)
            t["blocked_reason"] = why[:500]
            return self._set(t, "FAILED", why, actor)

    def submit(self, task_id: str, result: dict) -> dict:
        """Process an agent-result for a RUNNING task: policy filter, approval gate, actions (exactly once), evidence
        verification, then COMPLETED, or back to PLANNED with feedback (bounded by attempts), or FAILED / BLOCKED.
        Returns {"status", "notes"}."""
        if errs := validate_instance("agent-result", result):
            raise Refused(f"result violates the agent-result contract: {errs[:3]}")
        with self.store.tx():
            t = self.task(task_id)
            if t["status"] != "RUNNING":
                raise Refused(f"task {task_id} is {t['status']}, not RUNNING")
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
            self.board_message(agent, f"Level-3 action proceeding: {t['objective'][:200]} -> {res['summary'][:400]}", t["task_id"], "level-3")
        failed = self._execute_actions(t, actions)
        if failed:
            t["blocked_reason"] = failed
            self._set(t, "FAILED", failed + " (no automatic retry)", "system")
            self.board_message("system", f"Irreversible action failed: {failed}", t["task_id"], "escalation")
            return {"status": "FAILED", "notes": [failed]}
        arts = [self.store.get("artifacts", a) for a in self._artifact_ids(t)]
        known = {a["artifact_id"] for a in self.store.all("artifacts")}
        verdict, notes = verify.verify_task(self.reg.evidence_rules, t, arts, res, self._workspace(t), known)
        t["verification"] = {"verdict": verdict, "notes": notes[:20], "verifier": "deterministic"}
        self.store.put("tasks", t)
        if verdict == "FAIL":
            return self._retry_or_fail(t, "verification failed: " + "; ".join(notes)[:800])
        try:
            self._apply_state(t, res)
        except InvalidRecord as e:  # nothing was written: company state is validated before the atomic write
            return self._retry_or_fail(t, f"state update rejected: {e}")
        for a in arts:
            self.store.put("artifacts", {**a, "status": "accepted"})
        t["evidence"] = [a["artifact_id"] for a in arts]
        if res.get("decision"):
            self._record_decision(t, res["decision"])
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
        ws = self.workspaces_dir / (t["inputs"].get("product_id") or t["parent_task_id"] or t["task_id"])
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
        agent, tools, dropped = t["assigned_agent"], self.reg.agents[t["assigned_agent"]]["tools"], []
        kept = []
        for a in res.get("actions") or []:
            why = self.policy.check_capability(agent, a["capability"])
            (dropped.append(f"action: {why}") if why else kept.append(a))
        res["actions"] = kept
        kept = []
        for s in res.get("state_updates") or []:
            ok = self.policy.can_write(agent, f"company/{s['section']}") and "update_company_state" in tools
            (kept.append(s) if ok else dropped.append(f"state: may not write company/{s['section']}"))
        res["state_updates"] = kept
        dept = self.reg.agents[agent]["department"]
        if res.get("memory_notes") and not self.policy.can_write(agent, f"department/{dept}"):
            dropped.append(f"memory: may not write department/{dept}")
            res["memory_notes"] = []
        if res.get("decision") and "record_decision" not in tools and not t["decision_options"]:
            dropped.append("decision: agent lacks record_decision")
            res.pop("decision")
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
                gate = self.policy.deployment_gate("production", self._family_artifacts(t)) if cap == "deploy_production" else None
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

    def _family_artifacts(self, t: dict) -> list[dict]:
        """Artifacts of tasks that share this task's root (the initiative) — what the deployment gate looks at."""
        root = lambda x: root(self.task(x["parent_task_id"])) if x.get("parent_task_id") else x["task_id"]  # noqa: E731
        mine = root(t)
        return [a for a in self.store.all("artifacts") if (o := self.store.get("tasks", a["task_id"])) and root(o) == mine]

    def _apply_state(self, t: dict, res: dict):
        if res.get("state_updates"):
            state = self.store.company()
            for su in res["state_updates"]:
                sec, val = su["section"], su["value"]
                if su["op"] == "append":
                    state[sec] = (state.get(sec) or []) + (val if isinstance(val, list) else [val])
                elif su["op"] == "merge" and isinstance(state.get(sec), dict) and isinstance(val, dict):
                    state[sec].update(val)
                else:
                    state[sec] = val
            self.store.save_company(state)  # schema-validated; an invalid update raises and nothing is written
        dept = self.reg.agents[t["assigned_agent"]]["department"]
        for n in res.get("memory_notes") or []:
            self.store.append(f"memory/{dept}.jsonl", {"at": now(), "agent": t["assigned_agent"], "task_id": t["task_id"], "note": n})

    def _record_decision(self, t: dict, d: dict):
        if t["decision_options"] and d["selected_option"] not in t["decision_options"]:
            return  # verify already failed this; defensive
        opts = d.get("options_considered") or [{"id": o, "summary": o} for o in t["decision_options"]]
        opts = opts if len(opts) >= 2 else opts + [{"id": "status-quo", "summary": "Do nothing"}]
        if not any(o["id"] == d["selected_option"] for o in opts):
            opts.append({"id": d["selected_option"], "summary": "selected"})
        rec = {"decision_id": f"dec-{t['task_id']}", "decision": f"{d['selected_option']}: {t['objective'][:300]}",
               "decision_type": "strategic" if t["assigned_agent"] == "ceo" else "operational", "owner": t["assigned_agent"],
               "objective": t["objective"], "options": opts, "selected_option": d["selected_option"],
               "reasoning_summary": d["reasoning_summary"][:1500], "evidence": self._artifact_ids(t), "confidence": d["confidence"],
               "impact": d.get("impact", "medium"), "cost": {"estimate_usd": d.get("cost_estimate_usd")}, "risk": d.get("risk", "medium"),
               "reversibility": d.get("reversibility", "costly"), "approval_required": bool(t.get("approval_id")),
               "approval_status": "granted" if t.get("approval_id") else "not_required", "created_at": now()}
        self.store.put("decisions", rec)
        state = self.store.company()
        state.setdefault("decisions", []).append(rec["decision_id"])
        self.store.save_company(state)

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
        if approver == "board":
            self.board_message(t["assigned_agent"], f"Approval needed (level {level}) [{a['approval_id']}]: {action[:500]} — {justification[:600]}",
                               t["task_id"], "approval")
        return a

    def resolve_approval(self, approval_id: str, granted: bool, by: str, note: str = "") -> dict:
        """`by` is 'board' or an agent id. Agents may not approve their own request or above their authority;
        level 4 is the Board's alone. A granted approval resumes the task with its saved result."""
        with self.store.tx():
            a = self.store.get("approvals", approval_id)
            if a is None or a["status"] != "pending":
                raise Refused(f"approval {approval_id} is not pending")
            if by != "board":
                if by not in self.reg.agents:
                    raise Refused(f"unknown approver '{by}'")
                if by == a["requested_by"]:
                    raise Refused("approval-levels/no-self-escalation: a requester cannot approve its own request")
                if a["approver"] == "board" or self.reg.agents[by]["authority"]["max_approval_level"] < a["level"]:
                    raise Refused(f"{by} cannot approve level {a['level']} (approver: {a['approver']})")
            a.update(status="granted" if granted else "rejected", resolved_by=by, resolved_at=now(), resolution_note=note[:1000])
            self.store.put("approvals", a)
            t = self.task(a["task_id"])
            self.audit(by, "approve" if granted else "reject", note or "-", t, approval_id=approval_id)
            if t["status"] != "WAITING_APPROVAL":
                return {"status": t["status"], "notes": []}
            if not granted:
                t["blocked_reason"] = f"approval rejected by {by}: {note[:300]}"
                self._set(t, "FAILED", t["blocked_reason"], by)
                return {"status": "FAILED", "notes": [t["blocked_reason"]]}
            self._set(t, "RUNNING", f"approval {approval_id} granted by {by}", by)
            return self._process(t)

    # ------------------------------------------------------------------ products (lifecycle-guarded)
    def add_product(self, name: str, by: str = "ceo") -> dict:
        with self.store.tx():
            state = self.store.company()
            pid = "prod-" + "".join(c if c.isalnum() else "-" for c in name.lower())[:40].strip("-") + "-" + new_id("x")[2:6]
            prod = {"id": pid, "name": name, "stage": self.reg.product_lifecycle["initial"], "priority": "normal", "runs": [],
                    "workspace": str(self.workspaces_dir / pid), "brief": ""}
            state.setdefault("products", []).append(prod)
            self.store.save_company(state)
            self.audit(by, "product add", name, product_id=pid)
            return prod

    def product_stage(self, product_id: str, target: str, why: str, by: str = "ceo") -> dict:
        with self.store.tx():
            state = self.store.company()
            prod = next((p for p in state.get("products", []) if p["id"] == product_id), None)
            if prod is None:
                raise Refused(f"no product {product_id}")
            if target not in self.reg.product_lifecycle["transitions"][prod["stage"]]:
                raise Refused(f"product {product_id}: {prod['stage']} -> {target} is not an allowed transition")
            if by != "board" and self.policy.effect_level({"product_transition": target}, prod["stage"]) >= 4:
                raise Refused(f"decision-rights/board-reserved: only the Board may move a live product to {target}")
            prod.setdefault("stage_history", []).append({"from": prod["stage"], "to": target, "at": now(), "why": why[:300]})
            prod["stage"] = target
            self.store.save_company(state)
            self.audit(by, f"product {target}", why, product_id=product_id)
            return prod

    # ------------------------------------------------------------------ worker harness dispatch
    def daily_spend(self) -> float:
        today = now()[:10]
        return sum(u["cost_usd"] for u in self.store.read_log("usage.jsonl") if u["at"][:10] == today)

    def dispatch(self, task_id: str) -> dict:
        """Run the task on a worker harness (its AI selection first, model policy candidates as fallback), charge the
        cost, and submit the result. Refused when the daily cap is reached."""
        cap = self.reg.policy_params("spending").get("max_cost_usd_per_day")
        if cap is not None and self.daily_spend() >= cap:
            raise Refused(f"budget-control/daily-cap: {self.daily_spend():.2f} >= {cap} USD today")
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
        req = harness.Request(system, prompt, str(self._workspace(t)), caps, max(budget, 0.01) if budget is not None else None,
                              t["budget"]["max_wall_seconds"] or 1800)
        ai = self.reg.ai_for(agent["id"], skill, t.get("ai"))
        policy_id = self.reg.model_policy_for(agent["id"], skill, t.get("model_policy"))
        try:
            attempts = self.router.call_with_fallback(policy_id, req, self.invoke, ai=ai)
        except harness.Unavailable as e:
            with self.store.tx():
                t = self.task(task_id)
                t["attempts"] -= 1
                t["blocked_reason"] = f"harness unavailable: {e}"
                self._set(t, "BLOCKED", t["blocked_reason"])
            return {"status": "BLOCKED", "notes": [t["blocked_reason"]]}
        with self.store.tx():
            self.store.put("runtime", self.router.health, key="health")
            t = self.task(task_id)
            for a in attempts:
                self.store.append("usage.jsonl", {"at": now(), "task_id": task_id, "agent": t["assigned_agent"], "harness": a.harness,
                                                  "model": a.model, "effort": ai.get("effort"), "cost_usd": a.cost_usd, "estimated": a.cost_estimated,
                                                  "ok": a.ok, "duration_s": round(a.duration_s, 1), "error": a.error[:300]})
                t["cost_usd"] = round(t["cost_usd"] + a.cost_usd, 6)
            self.store.put("tasks", t)
            last = attempts[-1]
            if not last.ok:
                return self._retry_or_fail(t, f"all model candidates failed; last: {last.harness}/{last.model}: {last.error}")
        return self.submit(task_id, last.output)

    def prompt(self, t: dict, agent: dict, skill: dict | None) -> tuple[str, str]:
        """System prompt (role, policies, skill) and the task contract for a worker harness."""
        caps = {c: ("available" if self.policy.available(c) else "UNAVAILABLE (requires configuration)") for c in agent["tools"]}
        definition = {k: agent[k] for k in ("id", "name", "role", "mission", "responsibilities", "non_responsibilities", "authority",
                                            "success_criteria", "escalation_rules") if k in agent}
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
                    "attempt": t["attempts"], "inputs": t["inputs"]}
        deps = [{"task": d["objective"], "agent": d["assigned_agent"], "summary": (d.get("result") or {}).get("summary"),
                 "artifacts": [self._artifact_view(a) for a in d["evidence"]]} for d in map(self.task, t["dependencies"])]
        state = self.store.company()
        visible = {s.split("/", 1)[1]: state.get(s.split("/", 1)[1]) for s in agent["memory_scope"]["reads"]
                   if s.startswith("company/") and s != "company/context"}
        user = "\n\n".join([
            "# Task contract\n```json\n" + json.dumps(contract, indent=1, default=str)[:20000] + "\n```",
            "# Results of the tasks this one depends on\n```json\n" + json.dumps(deps, indent=1, default=str)[:60000] + "\n```",
            "# Company state (your read scope)\n```json\n" + json.dumps(visible, indent=1, default=str)[:20000] + "\n```",
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
