"""task_start on a worker-harness task (zcode, ...) runs it through dispatch instead of handing out a native subagent prompt."""
from conftest import FakeHarness
from orchestrator_kit.mcp import Server


def test_task_start_dispatches_a_worker_harness_task(tmp_path):
    (tmp_path / "p").mkdir()
    fake = FakeHarness()
    s = Server(tmp_path, invoke=fake)
    s.call("project_init", {"project": "p", "name": "P", "daily_budget_usd": 5})
    c = fake.orch = s.company("p")
    agent = next(a for a in c.reg.enabled_agents if c.reg.agents[a]["type"] != "orchestrator")
    t = s.call("task_add", {"project": "p", "agent": agent, "objective": "o", "criteria": ["c"], "ai": {"harness": "zcode"}})
    out = s.call("task_start", {"project": "p", "task_id": t})
    assert out["dispatched"] is True and "subagent_prompt" not in out and "status" in out
    assert len(fake.calls) == 1, "the worker ran (which harness answers follows the enabled harnesses and the mapping fallbacks)"
    assert s.call("task_show", {"project": "p", "task_id": t})["attempts"] == 1
