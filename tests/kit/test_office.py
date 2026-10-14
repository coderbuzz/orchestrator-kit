"""docs/dashboard-events.md §3: the read-only office reader (snapshot, events, CLI) over a state dir."""
import json
import os
import subprocess
import sys
import time

from conftest import FakeHarness
from orchestrator_kit import cli, office
from orchestrator_kit.mcp import Server

NOW = 1_800_000_000.0
Z = lambda dt: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + dt))  # noqa: E731


def put(sd, rel, obj):
    p = sd / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def task(sd, tid, agent, status, **kw):
    put(sd, f"tasks/{tid}.json", {"task_id": tid, "objective": f"do {tid}", "assigned_agent": agent, "priority": "normal", "status": status,
                                 "dependencies": [], "created_at": Z(-500), "updated_at": Z(-100), "attempts": 1, "cost_usd": 0.5,
                                 "result": None, "blocked_reason": None, "approval_id": None, "ai": None, **kw})


def ev(sd, seq, kind, run="t1#1", dt=-50, **kw):
    e = {"v": 1, "seq": seq, "ts": Z(dt), "project": "P", "run": run, "task": run.split("#")[0], "attempt": 1, "agent": "a", "kind": kind, "state": "working", **kw}
    with open(sd / "events.jsonl", "a") as f:
        f.write(json.dumps(e) + "\n")


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def fixture(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir(parents=True)
    put(sd, "roster.json", {"project": "P", "agents": [{"id": "dev", "department": "eng"}, {"id": "cfo", "department": "fin"}, {"id": "qa", "department": "eng"}]})
    task(sd, "t1", "dev", "RUNNING", ai={"harness": "zcode", "model": "m0"})
    put(sd, "runs/t1.json", {"pid": os.getpid(), "owner": os.getpid(), "done": False})
    task(sd, "t2", "cfo", "WAITING_APPROVAL", approval_id="apr-1")
    put(sd, "approvals/apr-1.json", {"approval_id": "apr-1", "task_id": "t2", "requested_by": "cfo", "level": 2, "action": "spend", "status": "pending",
                                    "approver": "board", "requested_at": Z(-60)})
    put(sd, "approvals/apr-0.json", {"approval_id": "apr-0", "status": "granted"})
    task(sd, "t3", "dev", "PENDING", priority="low", created_at=Z(-400))
    task(sd, "t4", "dev", "PLANNED", priority="high", dependencies=["t1"], created_at=Z(-300))
    task(sd, "t5", "dev", "PENDING", priority="high", created_at=Z(-200))
    task(sd, "t6", "ghost", "COMPLETED", result={"summary": "all done"}, updated_at=Z(-10))
    task(sd, "t7", "ghost", "FAILED", blocked_reason="boom", updated_at=Z(-20))
    ev(sd, 1, "task.start")
    ev(sd, 2, "run.start", harness="claude-code", model="opus", provider="anthropic", effort="high")
    ev(sd, 3, "tool.call", tool="Edit", target="a.py", dt=-40)
    ev(sd, 4, "tool.error", tool="Bash", msg="exit 1", dt=-30)
    ev(sd, 5, "run.tick", state="slow", elapsed_s=90, calls=3, tokens={"in": 5, "out": 6}, dt=-20)
    ev(sd, 6, "tool.call", run="t9#1", tool="Read", target="z")
    return sd


def card(snap, agent):
    d = next(d for d in snap["desks"] if d["agent"] == agent)
    return d, (d["runs"][0] if d["runs"] else None)


def test_the_reader_is_stdlib_only_at_import():
    out = subprocess.run([sys.executable, "-c", "import sys, orchestrator_kit.office; print(sorted({'yaml', 'jsonschema'} & set(sys.modules)))"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "[]"


def test_snapshot_of_a_replayed_project(tmp_path):
    sd = fixture(tmp_path)
    s = office.snapshot(sd, NOW)
    assert s["project"] == "P" and s["last_seq"] == 6 and s["last_event_age_s"] == 50
    assert [d["agent"] for d in s["desks"]] == ["dev", "cfo", "qa", "ghost"]
    d, c = card(s, "dev")
    assert d["state"] == "slow" and d["on_roster"] is True and d["department"] == "eng"
    assert (c["harness"], c["model"], c["effort"]) == ("claude-code", "opus", "high") and c["run"] == "t1#1"
    assert c["last_call"] == "Edit a.py" and c["calls"] == 3 and c["tokens"] == {"in": 5, "out": 6} and c["elapsed_s"] == 90
    assert c["last_error"] == {"tool": "Bash", "msg": "exit 1"}
    assert c["runner"] == "alive" and c["state"] == "slow" and c["started_at"] == Z(-50)
    assert [q["task"] for q in d["queue"]] == ["t4", "t5", "t3"]
    assert d["queue"][0]["wait_on"] == ["task:t1"] and d["queue"][1]["wait_on"] == []
    d, c = card(s, "cfo")
    assert d["state"] == "waiting" and c["wait_on"] == ["approval:apr-1"]
    assert card(s, "qa")[0]["state"] == "idle" and card(s, "qa")[0]["last"] is None
    g = card(s, "ghost")[0]
    assert g["on_roster"] is False and g["state"] == "done" and g["last"] == {"task": "t6", "status": "COMPLETED"}
    assert [a["approval_id"] for a in s["approvals"]] == ["apr-1"] and s["approvals"][0]["age_s"] == 60
    assert [r["task"] for r in s["recent"]] == ["t6", "t7"] and s["recent"][0]["summary"] == "all done" and s["recent"][1]["summary"] == "boom"
    assert s["counts"]["PENDING"] == 2 and s["cost_usd"] == 3.5


def test_stuck_stopped_and_precedence(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    put(sd, "roster.json", {"project": "P", "agents": [{"id": "a", "department": None}, {"id": "b", "department": None}, {"id": "c", "department": None}]})
    task(sd, "t1", "a", "BLOCKED", blocked_reason="looping")
    ev(sd, 1, "run.stop", stop={"kind": "loop", "reason": "x"}, last_call="Edit f")
    task(sd, "t2", "b", "BLOCKED")
    task(sd, "t3", "c", "RUNNING")
    task(sd, "t4", "c", "BLOCKED")
    ev(sd, 2, "run.stop", run="t4#1", stop={"kind": "stuck"})
    ev(sd, 3, "run.tick", run="t3#1", state="working")
    s = office.snapshot(sd, NOW)
    assert card(s, "a")[0]["state"] == "stuck" and card(s, "a")[1]["blocked_reason"] == "looping" and card(s, "a")[1]["stop"]["kind"] == "loop"
    assert card(s, "b")[0]["state"] == "stopped"
    d = card(s, "c")[0]
    assert [c["state"] for c in d["runs"]] == ["working", "stuck"] and d["state"] == "stuck"


def test_runner_states(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    task(sd, "t1", "a", "RUNNING")
    dead = dead_pid()
    get = lambda: office.snapshot(sd, NOW)["desks"][0]["runs"][0]["runner"]  # noqa: E731
    assert get() is None
    put(sd, "runs/t1.json", {"pid": dead, "owner": dead, "done": False})
    assert get() == "dead"
    put(sd, "runs/t1.json", {"pid": os.getpid(), "owner": dead, "done": False})
    assert get() == "orphaned"
    put(sd, "runs/t1.json", {"pid": dead, "owner": dead, "done": True})
    assert get() is None


def test_partial_and_torn_lines_and_the_allowlist(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    ev(sd, 1, "task.start")
    with open(sd / "events.jsonl", "a") as f:
        f.write("{not json\n")
    ev(sd, 2, "tool.call", tool="Edit", target="x", secret_field="s3cret")
    whole = json.dumps({"v": 1, "seq": 3, "ts": Z(0), "project": "P", "run": "t1#1", "task": "t1", "kind": "tool.call", "tool": "Read", "target": "y"})
    with open(sd / "events.jsonl", "a") as f:
        f.write(whole[:30])
    r = office.events(sd, 0)
    assert [e["seq"] for e in r["events"]] == [1, 2] and r["last_seq"] == 2
    assert "secret_field" not in r["events"][1] and r["events"][1]["tool"] == "Edit"
    assert office._log(sd).runs["t1#1"].get("call", {}).get("tool") == "Edit", "the partial line is not folded"
    with open(sd / "events.jsonl", "a") as f:
        f.write(whole[30:] + "\n")
    r = office.events(sd, 0)
    assert [e["seq"] for e in r["events"]] == [1, 2, 3] and r["last_seq"] == 3 and office._log(sd).runs["t1#1"]["call"]["tool"] == "Read"


def test_paging_across_a_seq_gap_and_replacement(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    for n in (1, 2, 5, 6):
        ev(sd, n, "tool.call", tool="T", target=str(n))
    assert [e["seq"] for e in office.events(sd, 0, 2)["events"]] == [1, 2]
    assert [e["seq"] for e in office.events(sd, 2)["events"]] == [5, 6]
    assert [e["seq"] for e in office.events(sd, 3)["events"]] == [5, 6]
    r = office.events(sd, 5)
    assert [e["seq"] for e in r["events"]] == [6] and r["last_seq"] == 6 and office.events(sd, 6)["events"] == []
    tmp = sd / "events.new"
    tmp.write_text(json.dumps({"v": 1, "seq": 1, "ts": Z(0), "kind": "task.start", "run": "n#1"}) + "\n")
    os.replace(tmp, sd / "events.jsonl")
    r = office.events(sd, 0)
    assert [e["run"] for e in r["events"]] == ["n#1"] and r["last_seq"] == 1


def project(tmp_path):
    (tmp_path / "p").mkdir()
    fake = FakeHarness()
    s = Server(tmp_path, invoke=fake)
    s.call("project_init", {"project": "p", "name": "P", "daily_budget_usd": 5})
    c = fake.orch = s.company("p")
    agent = next(a for a in c.reg.enabled_agents if c.reg.agents[a]["type"] != "orchestrator")
    return s, c, agent


def test_a_real_run_shows_done_and_every_other_desk_idle(tmp_path):
    s, c, agent = project(tmp_path)
    tid = s.call("task_add", {"project": "p", "agent": agent, "objective": "job", "criteria": ["c"], "artifacts": ["other"], "ai": {"harness": "zcode"}})
    s.call("task_start", {"project": "p", "task_id": tid})
    for _ in range(100):
        if c.task(tid)["status"] == "COMPLETED":
            break
        time.sleep(0.1)
    time.sleep(0.3)
    roster = json.loads((c.store.path / "roster.json").read_text())
    assert roster["project"] == "P" and all("department" in a for a in roster["agents"]) and agent in [a["id"] for a in roster["agents"]]
    snap = office.snapshot(c.store.path)
    log = c.store.read_log("events.jsonl")
    d = next(d for d in snap["desks"] if d["agent"] == agent)
    assert d["state"] == "done" and d["last"]["status"] == "COMPLETED"
    assert all(x["state"] == "idle" for x in snap["desks"] if x["agent"] != agent)
    assert snap["last_seq"] == log[-1]["seq"]
    assert [e["seq"] for e in office.events(c.store.path, 0, 1000)["events"]] == [e["seq"] for e in log]


def test_cli(tmp_path, capsys):
    sd = fixture(tmp_path / "proj" / ".co")
    assert cli.main(["office", str(tmp_path / "proj"), "--json"]) == 0
    assert "desks" in json.loads(capsys.readouterr().out)
    assert cli.main(["office", str(tmp_path / "proj"), "--after", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"events", "last_seq"} and out["last_seq"] == 6
    assert cli.main(["office", str(sd)]) == 0
    text = capsys.readouterr().out
    assert text.startswith("P  6  last event") and "approvals pending: 1" in text and "dev  slow  [t1 do t1]  Edit a.py" in text
    (tmp_path / "empty").mkdir()
    assert cli.main(["office", str(tmp_path / "empty")]) == 1
