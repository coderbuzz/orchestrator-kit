"""Progress supervisor: detector on recorded and synthetic logs, zcode log reader, worker kill, dispatch integration. No model calls."""
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from orchestrator_kit import harness, supervisor
from orchestrator_kit.mcp import Server
from orchestrator_kit.supervisor import Detector, Limits, ZcodeLog

FIXTURE = Path(__file__).parent.parent / "fixtures" / "zcode_rollout_slow_progress.jsonl"
LIM = Limits()


def call(sig, tool="Bash"):
    return {"kind": "call", "ts": 0, "tool": tool, "target": sig, "sig": sig, "model_s": 1}


def err(sig, tool="Bash"):
    return {"kind": "error", "ts": 0, "tool": tool, "msg": sig, "sig": sig}


def run_events(events, step, lim=LIM, wd_every=None, cpu=0):
    """Feed one event per `step` seconds; the workdir changes every `wd_every` events. -> first (kind, reason) or None."""
    d, now = Detector(lim, 0), 0.0
    for i, e in enumerate(events, 1):
        now += step
        d.feed(now, [e], wd_changed=bool(wd_every and i % wd_every == 0), ticks=int(now * cpu))
        if v := d.verdict(now):
            return v
    return None


# ------------------------------------------------------------------ the detector
def test_recorded_slow_but_progressing_run_is_never_stopped():
    """A real GLM-5.3-Flash run (sirkel, 94 tool calls in 29 min, model calls up to 230 s, files written) replayed at its own pace."""
    d, w = Detector(LIM, 0), None
    rows = [json.loads(line) for line in FIXTURE.read_text().splitlines()]
    assert max(r["durationMs"] for r in rows) > 200_000, "the fixture holds the slow calls"
    log = ZcodeLog("[run-id:task-fixture]", 0, pattern=str(FIXTURE))
    events = log.read()
    t0 = supervisor._epoch(rows[0]["startedAt"])
    seen = 0
    for e in events:
        now = e["ts"] - t0
        mutating = e["kind"] == "call" and e["tool"] in ("Write", "Edit")
        d.feed(now, [e], wd_changed=mutating, ticks=int(now * 50))
        w = w or d.verdict(now + 1)
        seen += e["kind"] == "call"
    assert seen == 94 and w is None, w
    # and its slow stretches alone (no event for the length of the slowest call) are far below the stuck threshold
    assert d.verdict(e["ts"] - t0 + 230) is None


def test_same_call_repeated_is_a_loop_unless_the_workdir_changes():
    v = run_events([call("pnpm test")] * 8, 60)
    assert v and v[0] == "loop" and "same call" in v[1]
    assert run_events([call("pnpm test")] * 12, 60, wd_every=3) is None, "edits in between: not a loop"
    assert run_events([call("pnpm test")] * 5, 60) is None, "below the repeat threshold"


def test_cycles_are_loops():
    ab = [call("read a"), call("edit a")] * 8
    v = run_events(ab, 30)
    assert v and v[0] == "loop" and "cycle of 2" in v[1]
    v = run_events([call("x"), call("y"), call("z")] * 8, 30)
    assert v and v[0] == "loop" and "cycle of 3" in v[1]


def test_the_same_error_repeated_is_a_loop():
    evs = [x for _ in range(6) for x in (call("edit f"), err("old_string not found"))]
    evs = [call(f"edit f{i}") if e["kind"] == "call" else e for i, e in enumerate(evs)]  # different calls, same error
    v = run_events(evs, 20)
    assert v and v[0] == "loop" and "same tool error" in v[1]
    assert run_events([err("e1"), err("e2")] * 6, 20) is None, "different errors are not a loop"


def test_calls_that_are_all_repeats_without_a_workdir_change_are_a_loop():
    pool = [call(f"read {i}") for i in range(5)]
    order = [pool[(i * 3) % 5] for i in range(60)]  # 5-call cycle: past the cycle rule, caught by "nothing new"
    v = run_events(order, 30)  # 30 s apart: 15 min in
    assert v and v[0] == "loop" and "none is new" in v[1]
    new = [call(f"read {i}") for i in range(60)]
    assert run_events(new, 30) is None, "every call is new: exploring is progress"


def test_stuck_means_no_sign_of_life_and_no_cpu():
    d = Detector(LIM, 0)
    d.feed(0, [call("a")], ticks=0)
    for now in range(60, 700, 60):
        d.feed(now, [], ticks=0)
    assert d.verdict(700)[0] == "stuck" and d.verdict(500) is None, "10 minutes without any signal"
    busy = Detector(LIM, 0)  # a long build: no tool calls, but the tree uses CPU
    busy.feed(0, [call("a")], ticks=0)
    for now in range(60, 1900, 60):
        busy.feed(now, [], ticks=now * 100)
    assert busy.verdict(1900) is None
    out = Detector(LIM, 0)  # output still growing counts as alive
    for now in range(60, 1900, 60):
        out.feed(now, [], out_grew=True, ticks=0)
    assert out.verdict(1900) is None


def test_thresholds_come_from_config():
    assert Limits.from_config({"stuck_minutes": 3, "loop_repeats": 4, "junk": 1}) == Limits(stuck_minutes=3, loop_repeats=4)
    v = run_events([call("x")] * 5, 10, lim=Limits(loop_repeats=4))
    assert v and v[0] == "loop"


def test_activity_lines_never_carry_secrets():
    assert "ghp_" not in supervisor.clean("git push https://x:ghp_" + "a" * 30 + "@github.com/o/r")
    assert "hunter2" not in supervisor.clean("curl -H 'Authorization: Bearer hunter2' x") and "hunter2" not in supervisor.clean("API_TOKEN=hunter2 pnpm build")
    assert "hunter2" not in supervisor.clean("login --password hunter2")
    assert supervisor.target_of({"file_path": "apps/web/src/a.ts", "content": "SECRET FILE BODY"}) == "apps/web/src/a.ts"


# ------------------------------------------------------------------ the zcode log reader
def rollout(path: Path, calls):
    """Append rollout records the way zcode writes them (one JSON line per model call)."""
    with open(path, "a") as f:
        for i, (tool, inp) in enumerate(calls):
            f.write(json.dumps({"completedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "durationMs": 1500,
                                "request": {"messages": [{"role": "user", "content": "[run-id:t1]"}] if not f.tell() else []},
                                "response": {"toolCalls": [{"id": f"c{time.time_ns()}{i}", "name": tool, "input": inp}],
                                             "usage": {"inputTokens": 1000, "outputTokens": 50, "cacheReadTokens": 900}}}) + "\n")


def test_reader_finds_the_run_by_marker_and_reads_incrementally(tmp_path):
    other = tmp_path / "model-io-sess_other.jsonl"
    other.write_text(json.dumps({"request": {"messages": [{"role": "user", "content": "[run-id:other]"}]}, "response": {}}) + "\n")
    mine = tmp_path / "model-io-sess_mine.jsonl"
    rollout(mine, [("Edit", {"file_path": "a.ts", "old_string": "x", "new_string": "y"})])
    r = ZcodeLog("[run-id:t1]", time.time() - 60, pattern=str(tmp_path / "model-io-sess_*.jsonl"))
    ev = r.read()
    assert [(e["tool"], e["target"]) for e in ev] == [("Edit", "a.ts")] and r.path == str(mine)
    assert r.read() == []
    rollout(mine, [("Bash", {"command": "pnpm test"})])
    assert [e["target"] for e in r.read()] == ["pnpm test"]
    assert r.tokens == {"inputTokens": 2000, "outputTokens": 100, "cacheReadTokens": 1800, "cacheWriteTokens": 0}


# ------------------------------------------------------------------ the worker kill (real processes)
WORKER = textwrap.dedent("""
    import json, sys, time
    path, mode = sys.argv[1], sys.argv[2]
    def rec(tool, inp, first=False):
        with open(path, "a") as f:
            f.write(json.dumps({"completedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "durationMs": 800,
                "request": {"messages": [{"role": "user", "content": sys.argv[3]}] if first else []},
                "response": {"toolCalls": [{"id": str(time.time_ns()), "name": tool, "input": inp}],
                             "usage": {"inputTokens": 1000, "outputTokens": 50, "cacheReadTokens": 900}}}) + "\\n")
    if mode == "loop":
        rec("Bash", {"command": "pnpm test"}, True)
        for _ in range(100):
            rec("Bash", {"command": "pnpm test"}); time.sleep(0.15)
    elif mode == "progress":
        for i in range(12):
            rec("Write", {"file_path": f"f{i}.txt"}, i == 0); open(f"f{i}.txt", "w").write(str(i)); time.sleep(0.4)
        print(json.dumps({"response": json.dumps({"status": "completed", "summary": "ok"}), "usage": {"inputTokens": 1, "outputTokens": 1}}))
    elif mode == "sleep":
        rec("Read", {"file_path": "a"}, True); time.sleep(60)
""")


@pytest.fixture
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "SUPERVISE_SECONDS", 0.2)
    monkeypatch.setattr(supervisor, "ROLLOUT_GLOB", str(tmp_path / "rollout" / "*.jsonl"))
    (tmp_path / "rollout").mkdir()
    (tmp_path / "worker.py").write_text(WORKER)
    return tmp_path


def sup_for(base, cwd, lim):
    return supervisor.Supervisor(base, str(cwd), "zcode", lim, time.time(), "[run-id:t1]")


def run(fast, mode, lim, timeout=30):
    work = fast / "work"
    work.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    argv = [sys.executable, str(fast / "worker.py"), str(fast / "rollout" / "model-io-sess_x.jsonl"), mode, "[run-id:t1]"]
    sup = sup_for(fast / "runs" / "t1", work, lim)
    (fast / "runs").mkdir(exist_ok=True)
    req = harness.Request("", "", str(work), [], None, timeout, limits=lim)
    with open(fast / "o", "w+") as o, open(fast / "e", "w+") as e:
        return sup, harness.run_worker(argv, req, o, e, poll=0.1, sup=sup)


def test_a_looping_worker_is_killed_with_its_tree(fast):
    t0 = time.time()
    with pytest.raises(harness.Stopped) as ei:
        run(fast, "loop", Limits(loop_repeats=4))
    assert ei.value.kind == "loop" and time.time() - t0 < 15
    assert subprocess.run(["pgrep", "-f", "worker.py"], capture_output=True).returncode == 1, "worker survived"


def test_a_worker_that_makes_progress_is_left_alone_even_with_tight_thresholds(fast):
    sup, rc = run(fast, "progress", Limits(stuck_minutes=0.05, loop_repeats=4, no_progress_minutes=0.05))
    assert rc == 0
    lines = sup.tail(50)
    assert any("Write      f3.txt" in ln for ln in lines) and any(ln.split()[1] == "workdir" for ln in lines), lines
    assert sup.tokens["inputTokens"] == 12000


def test_a_silent_worker_with_no_cpu_is_stuck(fast):
    t0 = time.time()
    with pytest.raises(harness.Stopped) as ei:
        run(fast, "sleep", Limits(stuck_minutes=0.05))  # 3 s
    assert ei.value.kind == "stuck" and 2 < time.time() - t0 < 20


def test_the_ceiling_still_applies(fast):
    with pytest.raises(subprocess.TimeoutExpired):
        run(fast, "sleep", Limits(stuck_minutes=60), timeout=2)


# ------------------------------------------------------------------ dispatch end to end
def setup_dispatch(fast, monkeypatch, mode, lim):
    """A project whose task runs on `zcode`, with the zcode builder swapped for the scripted worker (the rest is the real path)."""
    ws = fast / "ws"
    (ws / "p").mkdir(parents=True)
    srv = Server(ws, invoke=harness.invoke)
    srv.call("project_init", {"project": "p", "name": "P", "daily_budget_usd": 5})
    c = srv.company("p")
    for h in c.reg.config["models"]["harnesses"]:
        h["enabled"] = True
    c.limits = lambda: lim
    monkeypatch.setattr(harness, "BUILDERS", {**harness.BUILDERS, "zcode": lambda hdef, model, req, schema: [
        sys.executable, str(fast / "worker.py"), str(fast / "rollout" / "model-io-sess_x.jsonl"), mode, harness.run_marker(req.run_id)]})
    monkeypatch.setattr(harness, "installed_version", lambda cmd: "")
    agent = next(a for a in c.reg.enabled_agents if c.reg.agents[a]["type"] != "orchestrator")
    tid = srv.call("task_add", {"project": "p", "agent": agent, "objective": "o", "criteria": ["c"], "ai": {"harness": "zcode"}})
    monkeypatch.setattr(supervisor, "ROLLOUT_GLOB", str(fast / "rollout" / "*.jsonl"))
    return srv, c, tid


def test_dispatch_stops_a_loop_blocks_the_task_and_does_not_reroute(fast, monkeypatch):
    srv, c, tid = setup_dispatch(fast, monkeypatch, "loop", Limits(loop_repeats=4))
    res = srv.call("dispatch", {"project": "p", "task_id": tid, "wait_seconds": 120})
    assert res["status"] == "BLOCKED" and res["stopped"] == "loop", res
    t = c.task(tid)
    assert t["status"] == "BLOCKED" and "stopped by supervisor (loop)" in t["blocked_reason"] and "Last tool call: Bash pnpm test" in t["blocked_reason"]
    usage = c.store.read_log("usage.jsonl")
    assert len(usage) == 1, "one attempt: no other route was tried"
    assert usage[0]["ok"] is False and usage[0]["tokens"]["inputTokens"] > 0, "tokens come from the worker's log although it was killed"
    log = srv.call("run_log", {"project": "p", "task_id": tid, "lines": 5})
    assert log["running"] is False and log["stopped"] == "loop" and any("pnpm test" in ln for ln in log["lines"])
    assert subprocess.run(["pgrep", "-f", "worker.py"], capture_output=True).returncode == 1


def test_run_stop_kills_a_running_worker_and_needs_a_reason(fast, monkeypatch):
    srv, c, tid = setup_dispatch(fast, monkeypatch, "sleep", Limits(stuck_minutes=60))
    out = {}
    th = threading.Thread(target=lambda: out.update(srv.call("dispatch", {"project": "p", "task_id": tid, "wait_seconds": 120})))
    th.start()
    for _ in range(100):
        if (c._run_file(tid)).exists() and srv.call("run_log", {"project": "p", "task_id": tid})["running"]:
            break
        time.sleep(0.1)
    from orchestrator_kit.runtime import Refused
    with pytest.raises(Refused, match="reason is required"):
        srv.call("run_stop", {"project": "p", "task_id": tid, "reason": "  "})
    assert srv.call("run_stop", {"project": "p", "task_id": tid, "reason": "the Board changed the plan"})["stop_requested"]
    th.join(20)
    assert out["status"] == "BLOCKED" and out["stopped"] == "manual" and "the Board changed the plan" in c.task(tid)["blocked_reason"]
    assert any(a["action"] == "run stop" for a in c.store.read_log("audit.jsonl"))
    with pytest.raises(Refused, match="no running worker"):
        srv.call("run_stop", {"project": "p", "task_id": tid, "reason": "again"})


def test_a_progressing_dispatch_completes_under_tight_thresholds(fast, monkeypatch):
    srv, c, tid = setup_dispatch(fast, monkeypatch, "progress", Limits(stuck_minutes=0.05, loop_repeats=4, no_progress_minutes=0.05))
    res = srv.call("dispatch", {"project": "p", "task_id": tid, "wait_seconds": 120})
    assert res.get("stopped") is None and c.task(tid)["status"] != "BLOCKED", res


def test_progress_notification_carries_the_latest_activity(capsys, tmp_path):
    srv = Server(tmp_path)
    srv.local.token = "tok"
    srv._progress({"elapsed_s": 40, "limit_s": 10800, "output_bytes": 12, "processes": 3, "idle_cpu_s": 0, "stalled": False,
                   "activity": ["10:00:01  Edit       apps/web/src/a.ts  (model 12.3s)", "10:00:20  Bash       pnpm test  (model 4.0s)"]})
    msg = json.loads(capsys.readouterr().out)["params"]["message"]
    assert "Edit       apps/web/src/a.ts" in msg and msg.count("\n") == 2 and "ceiling 10800s" in msg


def test_tail_cli_prints_the_latest_log(tmp_path, capsys):
    from orchestrator_kit import tail
    runs = tmp_path / "proj" / ".company" / "state" / "runs"
    runs.mkdir(parents=True)
    (runs / "task-1.activity.log").write_text("\n".join(f"10:00:{i:02d}  Bash  step {i}" for i in range(50)) + "\n")
    assert tail.main([str(tmp_path / "proj"), "-n", "3"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1].endswith("step 49") and len(out) == 4
    assert tail.main([str(tmp_path / "none")]) == 1


def test_dispatch_returns_running_at_once_and_the_task_settles_by_itself(fast, monkeypatch):
    srv, c, tid = setup_dispatch(fast, monkeypatch, "sleep", Limits(stuck_minutes=60))
    t0 = time.time()
    res = srv.call("dispatch", {"project": "p", "task_id": tid, "wait_seconds": 0})
    assert res["status"] == "RUNNING" and res["started"] and time.time() - t0 < 5
    from orchestrator_kit.runtime import Refused
    with pytest.raises(Refused, match="already has a worker"):
        srv.call("dispatch", {"project": "p", "task_id": tid})
    assert srv.call("run_log", {"project": "p", "task_id": tid})["running"]  # answered while the worker runs
    srv.call("run_stop", {"project": "p", "task_id": tid, "reason": "test over"})
    for _ in range(150):
        if c.task(tid)["status"] == "BLOCKED":
            break
        time.sleep(0.2)
    t = c.task(tid)
    assert t["status"] == "BLOCKED" and "test over" in t["blocked_reason"], "the background thread settled the task"
    assert not c._active


def test_a_dependency_that_will_never_complete_can_be_released_with_a_reason(tmp_path):
    from conftest import FakeHarness
    from orchestrator_kit.runtime import Refused
    (tmp_path / "p").mkdir()
    s = Server(tmp_path, invoke=FakeHarness())
    s.call("project_init", {"project": "p", "name": "P", "daily_budget_usd": 5})
    c = s.company("p")
    agent = next(a for a in c.reg.enabled_agents if c.reg.agents[a]["type"] != "orchestrator")
    mk = lambda **k: s.call("task_add", {"project": "p", "agent": agent, "objective": "o", "criteria": ["c"], **k})  # noqa: E731
    dep = mk()
    t = mk(after=[dep])
    rel = lambda reason="work proven in task X": s.call("task_release_dependency", {"project": "p", "task_id": t, "dependency": dep, "reason": reason})  # noqa: E731
    with pytest.raises(Refused, match="only a CANCELLED or FAILED"):
        rel()
    c.cancel(dep, "superseded")
    with pytest.raises(Refused, match="reason is required"):
        rel("  ")
    assert rel()["dependencies"] == []
    assert any(a["action"] == "dependency released" and dep in a["why"] for a in c.store.read_log("audit.jsonl"))
    c.start(t)  # the task can start now
