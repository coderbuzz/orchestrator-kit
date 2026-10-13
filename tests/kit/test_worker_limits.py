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
    srv = Server.__new__(Server)
    srv.token = "tok"
    srv._progress({"elapsed_s": 40, "limit_s": 1800, "output_bytes": 12, "processes": 3, "idle_cpu_s": 0, "stalled": False})
    m = json.loads(capsys.readouterr().out)
    assert m["method"] == "notifications/progress" and m["params"]["progressToken"] == "tok" and m["params"]["progress"] == 40
    srv.token = None
    srv._progress({})  # no token from the client: silent
    assert capsys.readouterr().out == ""
