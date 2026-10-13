"""MCP server (stdio) exposing a pack's runtime tools to an orchestrator session.

A pack supplies its Runtime class, the ordered tool list (core tools below plus its own), handlers for its own tools, and
optionally a status report. Projects are folders of $ORCHESTRATOR_KIT_WORKSPACE (default ~/workspace) holding the pack's project file.

Stdlib only: newline-delimited JSON-RPC 2.0 (MCP stdio transport), tools capability only.
"""
import json
import os
import sys
from pathlib import Path


from . import harness
from .pack import Pack
from .runtime import Refused, Runtime, projects, read_project, write_project
from .validate import ROOT

PROTOCOL = "2025-06-18"
S, I, N, B = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}
L = {"type": "array", "items": S}
P = {"project": {**S, "description": "Workspace project folder name (see `projects`)"}}


def tool(name, description, props=None, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": {**P, **(props or {})}, "required": ["project", *required]}}


def free(name, description, props=None, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(required)}}


def core_tools(pack: Pack, descriptions: dict | None = None) -> dict[str, dict]:
    """The runtime's tools by name. A pack words the descriptions for its own sessions (`descriptions`: name -> text) and orders them."""
    h, H = pack.human_id, pack.human_id.upper()
    d = {"projects": "List the workspace projects with open tasks, approvals waiting and spend today.",
         "project_init": f"Start running a workspace folder as a project: writes <project>/{pack.project_file} with its goal, budget and "
                         "the team hired from the company catalog (roster). The folder must already exist.",
         "status": "Report for a project: what runs, what is blocked or done, cost, results, risks, decisions needed.",
         "tasks": "List a project's tasks (optionally by status).",
         "task_show": "One task with its result, verification and evidence.",
         "task_add": "Create a task (checks agent, delegation, skill, artifact types, budget, AI selection).",
         "task_start": "Mark a task RUNNING (dependencies done, attempts left) and get the prompt and the name of the subagent that does it.",
         "task_submit": "Submit the subagent's agent-result JSON: policy filter, approval gate, actions once, evidence check. "
                        "Returns COMPLETED, PLANNED (retry with feedback), WAITING_APPROVAL, FAILED or BLOCKED. Pass the subagent's `usage` (recorded as data, never charged).",
         "task_close": "Cancel or fail a task with a reason.",
         "dispatch": "Run a task on a worker harness with its AI selection, charge the cost, submit the result.",
         "review": "Record an agent-level approval decision taken by the approver agent's review (never the requester's).",
         "approvals": f"Approvals waiting, across all projects or one; `{h}: true` ones need the {h} (the human).",
         "usage": "AI spend of a project: charged (counts against budgets; flat plans 0) and list-price equivalent, today and in total, with recent calls.",
         f"{pack.human_prefix}decide": f"{H} ONLY: record the human's decision on a {h} approval. Call only after the human decided it "
                                       "in this conversation; the human confirms the call.",
         f"{pack.human_prefix}staff": f"{H} ONLY: hire agents from the company catalog onto a project's roster, or release them, after the "
                                      "human decided it in this conversation; the human confirms the call.",
         **(descriptions or {})}
    return {
        "projects": free("projects", d["projects"]),
        "project_init": tool("project_init", d["project_init"], {"name": S, "daily_budget_usd": N, "description": S, "goal": S, "roster": L},
                             ["name", "daily_budget_usd"]),
        "status": tool("status", d["status"]),
        "tasks": tool("tasks", d["tasks"], {"status": S}),
        "task_show": tool("task_show", d["task_show"], {"task_id": S}, ["task_id"]),
        "task_add": tool("task_add", d["task_add"],
                         {"agent": S, "objective": S, "criteria": L, "skill": S, "artifacts": L, "after": L, "parent": S, "budget_usd": N,
                          "ai": {"type": "object", "description": "{harness, provider, model, effort}"}, "options": L, "level": I,
                          "inputs": {"type": "object", "description": "e.g. {\"workdir\": \"<repo folder>\"} for work inside a project repo"}},
                         ["agent", "objective", "criteria"]),
        "task_start": tool("task_start", d["task_start"], {"task_id": S}, ["task_id"]),
        "task_submit": tool("task_submit", d["task_submit"], {"task_id": S, "result": {"type": "object"}, "usage": {
            "type": "object", "description": "The subagent's usage as the Agent tool reports it: {model, total_tokens, tool_uses, duration_ms}"}},
            ["task_id", "result"]),
        "task_close": tool("task_close", d["task_close"], {"task_id": S, "outcome": {"enum": ["cancel", "fail"]}, "why": S},
                           ["task_id", "outcome", "why"]),
        "dispatch": tool("dispatch", d["dispatch"], {"task_id": S}, ["task_id"]),
        "review": tool("review", d["review"], {"approval_id": S, "grant": B, "as_agent": S, "note": S}, ["approval_id", "grant", "as_agent"]),
        "approvals": free("approvals", d["approvals"], {"project": S}),
        "usage": tool("usage", d["usage"]),
        f"{pack.human_prefix}decide": tool(f"{pack.human_prefix}decide", d[f"{pack.human_prefix}decide"], {"approval_id": S, "approve": B, "note": S},
                                           ["approval_id", "approve"]),
        f"{pack.human_prefix}staff": tool(f"{pack.human_prefix}staff", d[f"{pack.human_prefix}staff"], {"hire": L, "release": L}),
    }


def workspace() -> Path:
    return Path(os.environ.get("ORCHESTRATOR_KIT_WORKSPACE") or os.environ.get("COMPANYOS_WORKSPACE") or Path.home() / "workspace")


def default_report(c: Runtime) -> str:
    tasks = c.store.all("tasks")
    counts = {s: sum(t["status"] == s for t in tasks) for s in sorted({t["status"] for t in tasks})}
    pending = [a for a in c.store.all("approvals") if a["status"] == "pending"]
    return (f"# Status\n\n- tasks: {counts or 'none'}\n- approvals waiting: {len(pending)}\n- spend today: {c.daily_spend():.4f} USD\n")


class Server:
    """`tools`: the ordered tool list the pack exposes. `handlers`: name -> fn(server, args) for the pack's own tools."""

    def __init__(self, ws: Path | None = None, *, runtime=Runtime, tools=(), handlers=None, report=default_report, **runtime_kw):
        self.ws, self.kw, self.cache = Path(ws or workspace()), runtime_kw, {}
        self.pack = Pack.load(runtime_kw.get("root") or ROOT)
        self.runtime, self.tools, self.report = runtime, list(tools), report
        self.handlers = {"projects": self._projects, "approvals": self._approvals, "project_init": self._project_init, "status": self._status,
                         "tasks": self._tasks, "task_show": self._task_show, "task_add": self._task_add, "task_start": self._task_start,
                         "task_submit": self._task_submit, "task_close": self._task_close, "dispatch": self._dispatch, "review": self._review,
                         "usage": self._usage, f"{self.pack.human_prefix}decide": self._human_decide,
                         f"{self.pack.human_prefix}staff": self._human_staff, **(handlers or {})}

    def projects(self) -> dict[str, Path]:
        return projects(self.ws, self.pack.project_file)

    def company(self, project: str) -> Runtime:
        found = self.projects()
        if project not in found:
            raise Refused(f"no project '{project}' (projects: {sorted(found)}); use project_init for an existing folder")
        if project not in self.cache:
            self.cache[project] = self.runtime.for_project(found[project], **self.kw)
        return self.cache[project]

    def call(self, name: str, a: dict):
        h = self.handlers.get(name)
        if h is None:
            raise Refused(f"unknown tool {name}")
        return h(self, a)

    # ------------------------------------------------------------------ core handlers
    def _projects(self, _, a):
        return [self._summary(p) for p in self.projects()]

    def _approvals(self, _, a):
        names = [a["project"]] if a.get("project") else list(self.projects())
        return [{"project": p, **{k: x[k] for k in ("approval_id", "level", "approver", "requested_by", "action", "justification")},
                 self.pack.human_id: x["approver"] == self.pack.human_id}
                for p in names for x in self.company(p).store.all("approvals") if x["status"] == "pending"]

    def _project_init(self, _, a):
        cfg = {"name": a["name"], **{k: a[k] for k in ("description", "goal", "roster") if a.get(k)}, "daily_budget_usd": a["daily_budget_usd"]}
        write_project(self.ws / a["project"], self.pack.root, cfg, new=True)
        return self._summary(a["project"])

    def _status(self, _, a):
        c = self.company(a["project"])
        p = c.project
        head = (f"# Project {a['project']}: {p['name']}\n\n- goal: {p.get('goal') or 'not set'}\n- roster: {', '.join(sorted(c.reg.enabled_agents))}\n"
                f"- daily budget: {p.get('daily_budget_usd')} USD\n\n")
        return head + self.report(c)

    def _tasks(self, _, a):
        c = self.company(a["project"])
        return [{k: t[k] for k in ("task_id", "status", "assigned_agent", "objective", "attempts", "cost_usd", "dependencies")}
                for t in sorted(c.store.all("tasks"), key=lambda t: t["created_at"]) if not a.get("status") or t["status"] == a["status"]]

    def _task_show(self, _, a):
        return self.company(a["project"]).task(a["task_id"])

    def _task_add(self, _, a):
        return self.company(a["project"]).add_task(a["agent"], a["objective"], a["criteria"], skill=a.get("skill"), artifacts=a.get("artifacts"),
                                                   after=a.get("after") or [], parent=a.get("parent"), budget_usd=a.get("budget_usd"), ai=a.get("ai"),
                                                   level=a.get("level") or 0, decision_options=a.get("options"), inputs=a.get("inputs"))["task_id"]

    def _task_start(self, _, a):
        c = self.company(a["project"])
        t = c.start(a["task_id"], actor=c.actor)
        agent = c.reg.agents[t["assigned_agent"]]
        _, prompt = c.prompt(t, agent, c.reg.agent_skill(agent["id"], t["skill"]) if t.get("skill") else None)
        return {"task_id": t["task_id"], "subagent": c.subagent_name(agent["id"]), "attempt": t["attempts"],
                "ai": c.reg.ai_for(agent["id"], None, t.get("ai")),
                "subagent_prompt": prompt + "\n\n" + harness.contract_section(harness.standalone(c.reg.result_schema))}

    def _task_submit(self, _, a):
        return self.company(a["project"]).submit(a["task_id"], a["result"], a.get("usage"))

    def _task_close(self, _, a):
        c = self.company(a["project"])
        return (c.cancel if a["outcome"] == "cancel" else c.fail)(a["task_id"], a["why"])["status"]

    def _dispatch(self, _, a):
        return self.company(a["project"]).dispatch(a["task_id"])

    def _review(self, _, a):
        if a["as_agent"] == self.pack.human_id:
            raise Refused(f"the {self.pack.human_id.capitalize()} decides through {self.pack.human_prefix}decide")
        return self.company(a["project"]).resolve_approval(a["approval_id"], a["grant"], a["as_agent"], a.get("note", ""))

    def _usage(self, _, a):
        c = self.company(a["project"])
        return {**c.spend(), "calls": c.store.read_log("usage.jsonl", limit=20)}

    def _human_decide(self, _, a):
        return self.company(a["project"]).resolve_approval(a["approval_id"], a["approve"], self.pack.human_id, a.get("note", ""))

    def _human_staff(self, _, a):
        c = self.company(a["project"])
        cfg = read_project(c.project_dir, self.pack.root)
        orchestrators = {i for i, d in c.reg.agents.items() if d["type"] == "orchestrator"}
        if kept := orchestrators & set(a.get("release") or []):
            raise Refused(f"the orchestrator stays on every roster: {sorted(kept)}")
        roster = cfg.get("roster") or sorted(c.reg.enabled_agents - orchestrators)
        cfg["roster"] = [x for x in roster if x not in (a.get("release") or [])] + [x for x in a.get("hire") or [] if x not in roster]
        write_project(c.project_dir, self.pack.root, cfg)
        self.cache.pop(a["project"])
        c.audit(self.pack.human_id, "roster", f"hire {a.get('hire') or []} release {a.get('release') or []}")
        return self._summary(a["project"])

    def _summary(self, project: str) -> dict:
        c = self.company(project)
        tasks, apr = c.store.all("tasks"), [x for x in c.store.all("approvals") if x["status"] == "pending"]
        return {"project": project, "name": c.project["name"], "goal": c.project.get("goal"), "path": str(c.project_dir),
                "roster": sorted(c.reg.enabled_agents),
                "open_tasks": sum(t["status"] not in ("COMPLETED", "FAILED", "CANCELLED") for t in tasks),
                "approvals_waiting": len(apr), f"{self.pack.human_id}_approvals_waiting": sum(x["approver"] == self.pack.human_id for x in apr),
                "spend_today_usd": round(c.daily_spend(), 4)}

    def handle(self, msg: dict) -> dict | None:
        """One JSON-RPC message -> response (None for notifications)."""
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            return None
        if method == "initialize":
            res = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                   "serverInfo": {"name": self.pack.name, "version": "1.0.0"}, "instructions": self.pack.mcp["instructions"]}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": self.tools}
        elif method == "tools/call":
            try:
                out, err = self.call(params["name"], params.get("arguments") or {}), False
            except Refused as e:
                out, err = f"refused: {e}", True
            except Exception as e:  # noqa: BLE001 - report, never crash the server
                out, err = f"error: {type(e).__name__}: {e}", True
            text = out if isinstance(out, str) else json.dumps(out, indent=1, default=str, ensure_ascii=False)
            res = {"content": [{"type": "text", "text": text}], "isError": err}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}


def serve(server: Server) -> int:
    for line in sys.stdin:
        if line.strip():
            reply = server.handle(json.loads(line))
            if reply is not None:
                sys.stdout.write(json.dumps(reply, default=str) + "\n")
                sys.stdout.flush()
    return 0
