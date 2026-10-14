import json
import subprocess
import tempfile
import time

import pytest

from orchestrator_kit import harness
from orchestrator_kit.mcp import Server


def run(argv, timeout, progress=None):
    req = harness.Request("", "", ".", [], None, timeout, on_progress=progress)
    with tempfile.TemporaryFile("w+") as o, tempfile.TemporaryFile("w+") as e:
        return harness.run_worker(argv, req, o, e, poll=0.1)


def test_wall_limit_kills_the_whole_tree_and_returns(monkeypatch):
    # the grandchild inherits stderr and outlives its parent: subprocess.run(timeout=) used to hang on it
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        run(["sh", "-c", "sleep 60 & sleep 60"], timeout=1)
    assert time.time() - t0 < 10
    assert subprocess.run(["pgrep", "-f", "^sleep 60$"], capture_output=True).returncode == 1, "grandchild survived"


def test_progress_reports_liveness_and_stall(monkeypatch):
    monkeypatch.setattr(harness, "PROGRESS_SECONDS", 0.2)
    monkeypatch.setattr(harness, "STALL_SECONDS", 0.5)
    got = []
    assert run(["sh", "-c", "sleep 2"], timeout=30, progress=got.append) == 0
    assert got and got[0]["processes"] >= 1 and got[0]["limit_s"] == 30
    assert not got[0]["stalled"] and got[-1]["stalled"], "an idle (sleeping) tree is reported stalled, never killed"


def test_dispatch_emits_mcp_progress(monkeypatch, capsys, tmp_path):
    srv = Server(tmp_path)
    srv.local.token = "tok"
    srv._progress({"elapsed_s": 40, "limit_s": 1800, "output_bytes": 12, "processes": 3, "idle_cpu_s": 0, "stalled": False})
    m = json.loads(capsys.readouterr().out)
    assert m["method"] == "notifications/progress" and m["params"]["progressToken"] == "tok" and m["params"]["progress"] == 40
    srv.local.token = None
    srv._progress({})  # no token from the client: silent
    assert capsys.readouterr().out == ""


def test_a_long_tool_call_does_not_block_the_server(monkeypatch, capsys, tmp_path):
    import io

    from orchestrator_kit import mcp
    srv = Server(tmp_path, handlers={"slow": lambda s, a: time.sleep(1.5) or "done"}, tools=[{"name": "slow", "inputSchema": {"required": []}}])
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "slow", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(map(json.dumps, reqs)) + "\n"))
    mcp.serve(srv)
    time.sleep(2)
    assert [json.loads(x)["id"] for x in capsys.readouterr().out.splitlines()] == [2, 1], "ping answered while slow ran"


def orphan(tmp_path, alive_for):
    from conftest import FakeHarness
    (tmp_path / "p").mkdir(exist_ok=True)
    s = Server(tmp_path, invoke=FakeHarness())
    s.call("project_init", {"project": "p", "name": "P", "daily_budget_usd": 5})
    c = s.company("p")
    agent = next(a for a in c.reg.enabled_agents if c.reg.agents[a]["type"] != "orchestrator")
    tid = s.call("task_add", {"project": "p", "agent": agent, "objective": "o", "criteria": ["c"], "ai": {"harness": "zcode"}})
    c.start(tid)
    w = subprocess.Popen(["sleep", str(alive_for)], start_new_session=True)
    (c.store.path / "runs").mkdir(exist_ok=True)
    h = next(iter(c.reg.harnesses))
    c._run_file(tid).write_text(json.dumps({"pid": w.pid, "owner": 2 ** 22 + 1, "started": time.time(), "limit": 30, "done": False,
                                            "harness": h, "model": "m", "provider": None, "effort": None}))
    return c, tid, w


def test_reconcile_settles_a_task_whose_worker_was_orphaned(tmp_path):
    c, tid, w = orphan(tmp_path, 1)
    assert c.reconcile() == [tid]
    w.wait()
    for _ in range(100):
        if c.task(tid)["status"] != "RUNNING":
            break
        time.sleep(0.2)
    t = c.task(tid)
    assert t["status"] == "PLANNED" and "worker lost" in t["inputs"]["previous_attempt_feedback"], t["status"]
    assert c.reconcile() == [], "settled once"


def test_reconcile_kills_an_orphan_past_its_wall_limit(tmp_path):
    c, tid, w = orphan(tmp_path, 60)
    rec = json.loads(c._run_file(tid).read_text())
    c._run_file(tid).write_text(json.dumps({**rec, "started": time.time() - 100}))
    c.reconcile()
    assert w.wait(15) is not None
    for _ in range(50):
        if c.task(tid)["status"] != "RUNNING":
            break
        time.sleep(0.2)
    assert c.task(tid)["status"] != "RUNNING"
