"""Read-only reader of a project's state dir for the office dashboard (docs/dashboard-events.md §3).

Never takes the store lock and never writes. events.jsonl is read from a byte offset; a trailing partial line is left for the
next call, a torn or invalid JSON line is skipped, and events leave through a field allowlist. Stdlib only at import.
"""
import bisect
import calendar
import glob
import json
import os
import sys
import time
from pathlib import Path

from .supervisor import clean

ALLOW = frozenset("""v seq ts project run task attempt agent kind state harness model provider effort tool target model_s msg summary
elapsed_s cpu_idle_s calls last_alive_age_s tokens stop last_call prior_attempt workdir ok error cost_usd equivalent_cost_usd duration_s
gate action decision by objective status next counts from to why wait_on text source""".split())
ACTIVE = ("RUNNING", "WAITING_APPROVAL", "BLOCKED")
TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")
PRIORITY = {"critical": 0, "high": 1, "normal": 2, "low": 3}
URGENT = ("stuck", "stopped", "waiting", "slow", "working")
_LOGS: dict[str, "_Log"] = {}


def _ts(s) -> float | None:
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _alive(pid) -> bool:
    # ponytail: checks the session leader only; harness.tree_alive scans /proc if a leader exits before its children
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class _Log:
    """Incremental reader of one events.jsonl: line offsets by seq plus a per-run fold of the latest facts."""

    def __init__(self, path: Path):
        self.path, self.ino, self.off = path, None, 0
        self.reset()

    def reset(self):
        self.off, self.seqs, self.offs, self.runs = 0, [], [], {}
        self.last_ts, self.last_seq, self.project = None, 0, None

    def refresh(self):
        try:
            st = self.path.stat()
        except OSError:
            self.ino = None
            return self.reset()
        if st.st_ino != self.ino or st.st_size < self.off:
            self.ino = st.st_ino
            self.reset()
        try:
            with open(self.path, "rb") as f:
                f.seek(self.off)
                data = f.read()
        except OSError:
            return
        *lines, _partial = data.split(b"\n")  # the chunk after the last newline is not a whole line yet
        for line in lines:
            start, self.off = self.off, self.off + len(line) + 1
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                self._fold(e, start)

    def _fold(self, e: dict, start: int):
        seq = e.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            self.seqs.append(seq)
            self.offs.append(start)
            self.last_seq = seq
        self.last_ts = _ts(e.get("ts")) or self.last_ts
        self.project = e.get("project") or self.project
        run, kind = e.get("run"), e.get("kind")
        if not isinstance(run, str):
            return
        r = self.runs.setdefault(run, {})
        r["last_ts"] = _ts(e.get("ts")) or r.get("last_ts")
        pick = lambda *ks: {k: e.get(k) for k in ks}  # noqa: E731
        if kind == "run.start":
            r["start"] = pick("harness", "model", "provider", "effort")
        elif kind == "run.tick":
            r["tick"] = pick("state", "elapsed_s", "calls", "tokens", "cpu_idle_s", "last_alive_age_s")
        elif kind == "run.stop":
            r["stop"] = {"stop": e.get("stop"), "last_call": e.get("last_call")}
        elif kind == "tool.call":
            r["call"] = {**pick("tool", "target"), "ts": _ts(e.get("ts"))}
        elif kind == "tool.error":
            r["error"] = {**pick("tool", "msg"), "ts": _ts(e.get("ts"))}
        elif kind == "task.start":
            r["task_start_ts"] = _ts(e.get("ts"))


def _log(state_dir) -> _Log:
    p = Path(state_dir).resolve() / "events.jsonl"
    lg = _LOGS.setdefault(str(p), _Log(p))
    lg.refresh()
    return lg


def events(state_dir, after_seq: int = 0, limit: int = 200) -> dict:
    """Events with seq > after_seq, ascending, at most `limit` (1..1000), through the allowlist; last_seq is the log's last seq."""
    lg, limit = _log(state_dir), max(1, min(int(limit), 1000))
    i, out = bisect.bisect_right(lg.seqs, after_seq), []
    if i < len(lg.seqs):
        try:
            with open(lg.path, "rb") as f:
                f.seek(lg.offs[i])
                while len(out) < limit and f.tell() < lg.off:
                    line = f.readline()
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and isinstance(e.get("seq"), int) and e["seq"] > after_seq:
                        out.append({k: e[k] for k in e if k in ALLOW})
        except OSError:
            pass
    return {"events": out, "last_seq": lg.last_seq}


def _age(now, t):
    return None if t is None else max(0, int(now - t))


def _card(state_dir, t: dict, fold: dict, now: float) -> dict:
    tid, st = t["task_id"], t["status"]
    r = fold.get(f"{tid}#{t.get('attempts')}", {})
    stop = (r.get("stop") or {}).get("stop") or {}
    ai = t.get("ai") or {}
    s0 = r.get("start") or {k: ai.get(k) for k in ("harness", "model", "provider", "effort")}
    tick, call, err, t0 = r.get("tick") or {}, r.get("call"), r.get("error"), r.get("task_start_ts")
    hard = stop.get("kind") in ("stuck", "loop")
    if st == "WAITING_APPROVAL":
        state = "waiting"
    elif st == "BLOCKED":
        state = "stuck" if hard else "stopped"
    else:
        state = ("stuck" if hard else "stopped") if r.get("stop") else tick.get("state") or "working"
    runner = None
    if st == "RUNNING":
        rec = _read(Path(state_dir) / "runs" / f"{tid}.json")
        if rec and not rec.get("done"):
            runner = "alive" if _alive(rec.get("owner")) else ("orphaned" if _alive(rec.get("pid")) else "dead")
    return {"task": tid, "run": f"{tid}#{t.get('attempts')}", "attempt": t.get("attempts"), "status": st, "state": state,
            "objective": clean(t.get("objective", ""), 200), "priority": t.get("priority"), **s0,
            "started_at": _iso(t0) if t0 else None,
            "elapsed_s": tick["elapsed_s"] if tick.get("elapsed_s") is not None else (_age(now, t0)),
            "calls": tick.get("calls"), "tokens": tick.get("tokens"), "cost_usd": t.get("cost_usd"),
            "last_call": f"{call['tool']} {call['target'] or ''}".strip() if call else None,
            "last_error": {"tool": err["tool"], "msg": err["msg"]} if err and err["ts"] is not None and (not call or call["ts"] is None or err["ts"] >= call["ts"]) else None,
            "stop": stop or None,
            "wait_on": [f"approval:{t['approval_id']}"] if st == "WAITING_APPROVAL" and t.get("approval_id") else [],
            "blocked_reason": clean(t.get("blocked_reason") or "", 300) if st == "BLOCKED" else None,
            "last_event_age_s": _age(now, r.get("last_ts")), "runner": runner}


def _desk(state_dir, agent, dept, on_roster, tasks, byid, fold, now) -> dict:
    """One desk. state: (1) any run card -> the most urgent card state (stuck > stopped > waiting > slow > working); (2) else a
    non-empty queue -> waiting if every item waits on something, else idle; (3) else the latest terminal task by updated_at:
    COMPLETED done, FAILED failed, CANCELLED idle; (4) else idle. `last` is that latest terminal task."""
    key = lambda t: t.get("updated_at") or ""  # noqa: E731
    runs = [_card(state_dir, t, fold, now) for t in sorted((t for t in tasks if t.get("status") in ACTIVE), key=key)]
    q = sorted((t for t in tasks if t.get("status") in ("PENDING", "PLANNED")), key=lambda t: (PRIORITY.get(t.get("priority"), 9), t.get("created_at") or ""))
    queue = [{"task": t["task_id"], "objective": clean(t.get("objective", ""), 120), "priority": t.get("priority"), "status": t["status"],
              "wait_on": [f"task:{d}" for d in t.get("dependencies") or [] if (byid.get(d) or {}).get("status") != "COMPLETED"]} for t in q]
    term = max((t for t in tasks if t.get("status") in TERMINAL), key=key, default=None)
    if runs:
        state = min((c["state"] for c in runs), key=URGENT.index)
    elif queue:
        state = "waiting" if all(x["wait_on"] for x in queue) else "idle"
    else:
        state = {"COMPLETED": "done", "FAILED": "failed"}.get(term and term["status"], "idle")
    return {"agent": agent, "department": dept, "on_roster": on_roster, "state": state, "runs": runs, "queue": queue,
            "last": {"task": term["task_id"], "status": term["status"]} if term else None}


def snapshot(state_dir, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    sd = Path(state_dir)
    lg = _log(sd)
    rd = _read(sd / "roster.json")
    roster = rd.get("agents") if isinstance(rd, dict) and isinstance(rd.get("agents"), list) else None
    load = lambda coll: [x for x in (_read(p) for p in sorted(glob.glob(str(sd / coll / "*.json")))) if isinstance(x, dict)]  # noqa: E731
    tasks = [t for t in load("tasks") if "task_id" in t and "status" in t]
    byid, by_agent, counts = {t["task_id"]: t for t in tasks}, {}, {}
    for t in tasks:
        by_agent.setdefault(t.get("assigned_agent"), []).append(t)
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    desks = [_desk(sd, a["id"], a.get("department"), True, by_agent.get(a["id"], []), byid, lg.runs, now) for a in roster or [] if isinstance(a, dict) and "id" in a]
    seen = {d["agent"] for d in desks}
    desks += [_desk(sd, a, None, False if roster is not None else None, by_agent[a], byid, lg.runs, now) for a in sorted(x for x in by_agent if x and x not in seen)]
    approvals = [{"approval_id": a.get("approval_id"), "task": a.get("task_id"), "agent": a.get("requested_by"), "level": a.get("level"),
                  "action": clean(a.get("action") or "", 200), "approver": a.get("approver"), "requested_at": a.get("requested_at"),
                  "age_s": _age(now, _ts(a.get("requested_at")))}
                 for a in sorted((a for a in load("approvals") if a.get("status") == "pending"), key=lambda a: a.get("requested_at") or "")]
    done = sorted((t for t in tasks if t["status"] in ("COMPLETED", "FAILED")), key=lambda t: t.get("updated_at") or "", reverse=True)[:5]
    recent = [{"task": t["task_id"], "agent": t.get("assigned_agent"), "status": t["status"], "state": "done" if t["status"] == "COMPLETED" else "failed",
               "summary": clean(s, 200) if (s := (t.get("result") or {}).get("summary") if t["status"] == "COMPLETED" else t.get("blocked_reason")) else None,
               "cost_usd": t.get("cost_usd")} for t in done]
    return {"v": 1, "generated_at": _iso(now), "project": (rd.get("project") if isinstance(rd, dict) else None) or lg.project, "last_seq": lg.last_seq,
            "last_event_age_s": _age(now, lg.last_ts), "counts": counts, "cost_usd": round(sum(t.get("cost_usd") or 0 for t in tasks), 6),
            "desks": desks, "approvals": approvals, "recent": recent}


def _state_dir(p: str) -> Path | None:
    d = Path(p)
    if (d / "tasks").is_dir() or (d / "events.jsonl").exists():
        return d
    found = [f for g in (".*", "*") for f in sorted(glob.glob(str(d / g / "state")))]
    return Path(found[0]) if found else None


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="orchestrator-kit office", description="state of every desk of a project")
    ap.add_argument("dir")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--after", type=int)
    ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args(argv)
    sd = _state_dir(a.dir)
    if sd is None:
        print("no state dir found", file=sys.stderr)
        return 1
    if a.after is not None:
        print(json.dumps(events(sd, a.after, a.limit)))
        return 0
    s = snapshot(sd)
    if a.json:
        print(json.dumps(s, indent=1))
        return 0
    age = s["last_event_age_s"]
    print(f"{s['project']}  {s['last_seq']}  last event {'-' if age is None else f'{age}s'} ago")
    for d in s["desks"]:
        c = d["runs"][0] if d["runs"] else None
        print(f"{d['agent']}  {d['state']}  " + (f"[{c['task']} {c['objective'][:50]}]  {c['last_call'] or ''}".rstrip() if c else f"queue:{len(d['queue'])}"))
    print(f"approvals pending: {len(s['approvals'])}")
    return 0
