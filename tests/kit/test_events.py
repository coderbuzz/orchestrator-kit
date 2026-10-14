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
    assert kinds.index(("task.start", first)) < kinds.index(("run.end", first)) < kinds.index(("task.end", first))
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
