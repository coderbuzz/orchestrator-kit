"""Layer A of docs/dashboard-events.md: the runtime writes task.start / run.end / task.end / project.done to events.jsonl by itself."""
import time

from conftest import FakeHarness
from orchestrator_kit.mcp import Server


def project(tmp_path, fake=None):
    (tmp_path / "p").mkdir()
    fake = fake or FakeHarness()
    s = Server(tmp_path, invoke=fake)
    s.call("project_init", {"project": "p", "name": "P", "daily_budget_usd": 5})
    c = fake.orch = s.company("p")
    agent = next(a for a in c.reg.enabled_agents if c.reg.agents[a]["type"] != "orchestrator")
    return s, c, agent


def test_a_task_that_completes_emits_start_run_end_then_end_with_next_and_project_done(tmp_path):
    s, c, agent = project(tmp_path)
    mk = lambda obj, **k: s.call("task_add", {"project": "p", "agent": agent, "objective": obj, "criteria": ["c"], "artifacts": ["other"], "ai": {"harness": "zcode"}, **k})  # noqa: E731
    first = mk("first job")
    second = mk("second job", after=[first])
    s.call("task_start", {"project": "p", "task_id": first})
    for _ in range(100):
        if c.task(first)["status"] == "COMPLETED":
            break
        time.sleep(0.1)
    ev = c.store.read_log("events.jsonl")
    assert c.task(first)["status"] == "COMPLETED", (c.task(first)["status"], [a["why"] for a in c.store.read_log("audit.jsonl")][-3:], [(e["kind"], e.get("status")) for e in ev])
    assert [e["seq"] for e in ev] == list(range(1, len(ev) + 1)), "seq is a gap-free counter a reader can resume from"
    kinds = [(e["kind"], e["task"]) for e in ev]
    assert kinds.index(("task.start", first)) < kinds.index(("run.start", first)) < kinds.index(("run.end", first)) < kinds.index(("task.end", first))
    start = next(e for e in ev if e["kind"] == "task.start")
    assert start["objective"] == "first job" and start["state"] == "working" and start["project"] == "P"
    end = next(e for e in ev if e["kind"] == "task.end")
    assert end["status"] == "COMPLETED" and end["state"] == "done"
    assert [n["task"] for n in end["next"]] == [second], "the task this one unblocks"
    run = next(e for e in ev if e["kind"] == "run.end")
    assert run["ok"] is True and run["harness"] and "error" in run
    assert not any(e["kind"] == "project.done" for e in ev), "the second task is still open"
    c.cancel(second, "not needed")
    last = c.store.read_log("events.jsonl")[-1]
    assert last["kind"] == "project.done" and last["counts"] == {"COMPLETED": 1, "CANCELLED": 1}


def test_a_failed_attempt_carries_its_cause_and_secrets_never_reach_the_log(tmp_path):
    from orchestrator_kit.harness import CallResult
    fake = FakeHarness(script=lambda t, o: CallResult(False, "zcode", "m", error="exit 1: boom ghp_" + "a" * 30))
    s, c, agent = project(tmp_path, fake)
    tid = s.call("task_add", {"project": "p", "agent": agent, "objective": "o", "criteria": ["c"], "ai": {"harness": "zcode"}})
    s.call("task_start", {"project": "p", "task_id": tid})
    for _ in range(100):
        if any(e["kind"] == "run.end" for e in c.store.read_log("events.jsonl")):
            break
        time.sleep(0.1)
    raw = (c.store.path / "events.jsonl").read_text()
    run = next(e for e in c.store.read_log("events.jsonl") if e["kind"] == "run.end")
    assert run["ok"] is False and run["error"].startswith("exit 1: boom") and run["state"] == "failed"
    assert "ghp_" not in raw


def wait_for(c, pred, n=100):
    for _ in range(n):
        if pred(c.store.read_log("events.jsonl")):
            break
        time.sleep(0.1)
    return c.store.read_log("events.jsonl")


def test_restart_after_blocked_emits_task_start_again_but_resuming_after_approval_does_not(tmp_path):
    s, c, agent = project(tmp_path)
    tid = s.call("task_add", {"project": "p", "agent": agent, "objective": "o", "criteria": ["c"]})
    c.start(tid)
    c._set(c.task(tid), "BLOCKED", "waiting for input")
    c.start(tid)
    starts = [e for e in c.store.read_log("events.jsonl") if e["kind"] == "task.start"]
    assert [e["attempt"] for e in starts] == [1, 2] and starts[1]["run"] == f"{tid}#2"
    c._set(c.task(tid), "WAITING_APPROVAL", "level 2")
    c._set(c.task(tid), "RUNNING", "approved")
    ev = c.store.read_log("events.jsonl")
    assert len([e for e in ev if e["kind"] == "task.start"]) == 2 and ev[-1]["kind"] == "task.status" and ev[-1]["to"] == "RUNNING"


def test_next_lists_dependents_only_when_the_task_completed_and_they_are_ready(tmp_path):
    s, c, agent = project(tmp_path)
    mk = lambda obj, **k: s.call("task_add", {"project": "p", "agent": agent, "objective": obj, "criteria": ["c"], **k})  # noqa: E731
    nxt = lambda tid: [n["task"] for e in c.store.read_log("events.jsonl") if e["kind"] == "task.end" and e["task"] == tid for n in e["next"]]  # noqa: E731
    first, other = mk("first"), mk("other")
    dep = mk("needs first", after=[first])
    c.cancel(first, "not needed")
    assert other in nxt(first), "a cancelled task unblocks nothing: next falls back to the agent's own open tasks"
    assert dep not in nxt(first), "a task waiting on a cancelled one can never run, so it is not next either"
    ok, blocker = mk("ok"), mk("blocker")
    d1, d2 = mk("d1", after=[ok]), mk("d2", after=[ok, blocker])
    c.start(ok)
    c._set({**c.task(ok), "evidence": ["e"]}, "COMPLETED", "done")
    assert nxt(ok) == [d1], "d2 still waits for blocker; only a ready dependent is next"


def test_an_event_write_failure_never_breaks_a_task_transition(tmp_path, monkeypatch):
    from orchestrator_kit.store import Store
    s, c, agent = project(tmp_path)
    tid = s.call("task_add", {"project": "p", "agent": agent, "objective": "o", "criteria": ["c"]})
    real = Store.append

    def append(self, log, rec):
        if log == "events.jsonl":
            raise OSError("disk full")
        return real(self, log, rec)
    monkeypatch.setattr(Store, "append", append)
    c.start(tid)
    assert c.task(tid)["status"] == "RUNNING"
    c._set(c.task(tid), "BLOCKED", "x")
    assert c.task(tid)["status"] == "BLOCKED" and not (c.store.path / "events.jsonl").exists()


def test_every_event_carries_run_and_a_gate_resolution_has_its_own_state(tmp_path, monkeypatch):
    s, c, agent = project(tmp_path)
    monkeypatch.setattr(c, "_process", lambda t: {"status": t["status"]})
    states = {}
    for granted in (True, False):
        tid = s.call("task_add", {"project": "p", "agent": agent, "objective": "o", "criteria": ["c"]})
        c.start(tid)
        apr = c._request_approval(c.task(tid), 2, "do it", "because")
        c.resolve_approval(apr["approval_id"], granted, c.reg.pack.human_id)
        states[granted] = [e["state"] for e in c.store.read_log("events.jsonl") if e["kind"] == "gate" and e["task"] == tid]
    assert states == {True: ["waiting", "working"], False: ["waiting", "failed"]}
    ev = c.store.read_log("events.jsonl")
    assert all(e["run"] == f"{e['task']}#{e['attempt']}" for e in ev)


def test_attempts_are_visible_live_run_start_with_prior_attempt_then_run_end_before_task_end(tmp_path):
    from orchestrator_kit.harness import CallResult
    n = []

    def script(t, o):
        n.append(1)
        if len(n) == 1:
            return CallResult(False, "zcode", "m", error="exit 1: provider error")
    s, c, agent = project(tmp_path, FakeHarness(script=script))
    tid = s.call("task_add", {"project": "p", "agent": agent, "objective": "o", "criteria": ["c"], "artifacts": ["other"], "ai": {"harness": "zcode"}})
    s.call("task_start", {"project": "p", "task_id": tid})
    ev = wait_for(c, lambda ev: any(e["kind"] == "task.end" for e in ev))
    run = [(e["kind"], e.get("ok")) for e in ev if e["kind"].startswith("run.") or e["kind"] == "task.end"]
    assert run == [("run.start", None), ("run.end", False), ("run.start", None), ("run.end", True), ("task.end", None)], run
    first, second = [e for e in ev if e["kind"] == "run.start"]
    assert "prior_attempt" not in first and first["run"] == f"{tid}#1" and first["state"] == "working"
    assert second["prior_attempt"]["error"].startswith("exit 1: provider error") and second["prior_attempt"]["harness"] == "zcode"


def test_call_with_fallback_reports_each_attempt_to_on_attempt():
    from orchestrator_kit import harness
    from orchestrator_kit.registry import Registry
    r = harness.Router(Registry(), {}, check_installed=False)
    routes = r.route("default")[:2]
    seen = []
    fn = lambda h, m, rq, sc: harness.CallResult(len(seen) > 2, h["id"], m, error="" if len(seen) > 2 else "exit 1: boom", output={"status": "completed", "summary": "s"})  # noqa: E731
    out = r.call_with_fallback("default", harness.Request("s", "t", ".", [], None, 100), fn, which=routes, on_attempt=lambda k, x: seen.append((k, x)))
    assert [k for k, _ in seen] == ["start", "end", "start", "end"] and [a.ok for a in out] == [False, True]
    assert seen[0][1]["prior"] is None and seen[2][1]["prior"] is out[0] and seen[2][1]["harness"] == routes[1].hdef["id"]
    assert seen[1][1] is out[0] and seen[3][1] is out[1] and set(seen[0][1]) == {"harness", "model", "provider", "effort", "prior"}
