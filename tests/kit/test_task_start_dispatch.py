"""task_start is the native-subagent path only: a task on a worker harness (zcode, ...) is refused with a pointer to dispatch."""
import pytest

from orchestrator_kit.mcp import Server
from orchestrator_kit.runtime import Refused


def test_task_start_refuses_a_worker_harness_task_and_leaves_it_pending(tmp_path):
    (tmp_path / "p").mkdir()
    s = Server(tmp_path)
    s.call("project_init", {"project": "p", "name": "P", "daily_budget_usd": 5})
    c = s.company("p")
    agent = next(a for a in c.reg.enabled_agents if c.reg.agents[a]["type"] != "orchestrator")
    t = s.call("task_add", {"project": "p", "agent": agent, "objective": "o", "criteria": ["c"], "ai": {"harness": "zcode"}})
    with pytest.raises(Refused, match="dispatch"):
        s.call("task_start", {"project": "p", "task_id": t})
    assert s.call("task_show", {"project": "p", "task_id": t})["status"] == "PENDING"
