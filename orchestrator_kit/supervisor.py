"""Progress supervisor for a worker run: replaces the hard per-route time limit.

A worker is stopped only when it is stuck (no sign of life) or looping (it keeps calling tools but nothing new happens);
while it makes progress it is left alone. Signals that need no model:
  - tool calls in the worker's own log (zcode: ~/.zcode/cli/rollout/model-io-sess_*.jsonl): name + arguments, token usage, errors;
  - the workdir (git status fingerprint: files changed, new commit);
  - CPU of the worker's process tree, bytes written to its stdout.
Harnesses without a readable tool-call log (claude-code, agy, dsh today) are supervised on the last three only: "stuck" works,
loop detection does not (the activity log then shows workdir changes only).

Verdicts (`Supervisor.poll` -> reason string or None):
  stuck  no sign of life for `stuck_minutes` AND the process tree used no CPU in that window
  loop   the same call (or an A-B / A-B-C cycle) repeated `loop_repeats` times with the workdir unchanged, OR the same error
         `error_repeats` times in a row with the workdir unchanged, OR calls keep arriving but none is new and the workdir is unchanged for `no_progress_minutes`
  manual a stop request in <run>.stop (the `run_stop` MCP tool: an orchestrator decision, reason mandatory)
A slow model call (25-230 s is normal for GLM-5.3-Flash high) is neither: the thresholds are minutes, not seconds.
Stdlib only.
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .store import redact

DEFAULTS = {"stuck_minutes": 10, "loop_repeats": 6, "error_repeats": 5, "no_progress_minutes": 15, "max_wall_seconds": 10800}
TICK_SECONDS = 30  # at most one `run.tick` event per run this often
MIN_CPU_TICKS = 100  # clock ticks (1 s) of CPU in the stuck window: a sleeping tree ticks far less
ROLLOUT_GLOB = "~/.zcode/cli/rollout/model-io-sess_*.jsonl"
_KEYS = ("file_path", "path", "command", "pattern", "url", "query", "description", "prompt")
_SECRETS = re.compile(r"(?i)((?:token|secret|passw(?:or)?d|api[_-]?key|authorization|bearer|cookie)\w*[=: ]+(?:(?:bearer|basic|token)\s+)?)\S+")
_ENVSET = re.compile(r"\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS)[A-Z0-9_]*)=\S+")


@dataclass
class Limits:
    stuck_minutes: float = DEFAULTS["stuck_minutes"]
    loop_repeats: int = DEFAULTS["loop_repeats"]
    error_repeats: int = DEFAULTS["error_repeats"]
    no_progress_minutes: float = DEFAULTS["no_progress_minutes"]
    max_wall_seconds: int = DEFAULTS["max_wall_seconds"]

    @classmethod
    def from_config(cls, cfg: dict | None) -> "Limits":
        known = {k: v for k, v in (cfg or {}).items() if k in DEFAULTS}
        return cls(**known)


def clean(text: str, n: int = 100) -> str:
    """One readable line without secrets: values after token/secret/password/key words, KEY=value env assignments and known key shapes are masked."""
    text = _ENVSET.sub(r"\1=[REDACTED]", _SECRETS.sub(r"\1[REDACTED]", redact(" ".join(str(text).split()))))
    return text if len(text) <= n else text[:n - 1] + "…"


def target_of(inp) -> str:
    """Short, secret-free description of a tool call's argument (never file contents)."""
    if not isinstance(inp, dict):
        return ""
    for k in _KEYS:
        if inp.get(k):
            return clean(inp[k])
    return ""


def signature(tool: str, inp) -> str:
    return hashlib.sha1((tool + json.dumps(inp, sort_keys=True, default=str)).encode()).hexdigest()[:16]


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------------------------------------- zcode log
class ZcodeLog:
    """Incremental reader of the worker's rollout file (one JSON record per model call). The file is found by the run marker the
    prompt carries (`[run-id:<id>]`) among rollout files modified since the worker started. Yields events:
    {"kind": "call", ts, tool, target, sig, model_s} and {"kind": "error", ts, tool, msg}; `tokens` sums the usage of all calls."""

    def __init__(self, marker: str, since: float, pattern: str | None = None):
        self.marker, self.since, self.pattern = marker, since, os.path.expanduser(pattern or ROLLOUT_GLOB)
        self.path: str | None = None
        self.offset, self.seen, self.checked, self.calls = 0, set(), {}, 0
        self.tokens = {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0}

    def _find(self):
        for f in glob.glob(self.pattern):
            try:
                m = os.stat(f).st_mtime
            except OSError:
                continue
            if m < self.since - 5 or self.checked.get(f) == m:
                continue
            self.checked[f] = m
            try:
                with open(f, "rb") as fh:
                    head = fh.readline(8_000_000)
            except OSError:
                continue
            if self.marker.encode() in head:
                self.path = f
                return

    def read(self) -> list[dict]:
        if self.path is None:
            self._find()
        if self.path is None:
            return []
        out = []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                while (line := fh.readline()).endswith(b"\n"):
                    self.offset += len(line)
                    try:
                        out += self._record(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out

    def _record(self, r: dict) -> list[dict]:
        ev = []
        for m in (r.get("request") or {}).get("messages") or []:  # tool results since the last call: errors feed the repeated-error rule
            if m.get("role") == "tool" and m.get("isError") and m.get("toolCallId") not in self.seen:
                self.seen.add(m.get("toolCallId"))
                body = m.get("content")
                body = body if isinstance(body, str) else json.dumps(body, default=str)
                ev.append({"kind": "error", "ts": _epoch(r["startedAt"]) if r.get("startedAt") else time.time(), "tool": m.get("toolName", "?"),
                           "msg": clean(body, 160), "sig": signature(m.get("toolName", "?"), clean(body, 200))})
        resp = r.get("response") or {}
        u = resp.get("usage") or {}
        for k, src in (("inputTokens", "inputTokens"), ("outputTokens", "outputTokens"), ("cacheReadTokens", "cacheReadTokens"), ("cacheWriteTokens", "cacheWriteTokens")):
            self.tokens[k] += u.get(src) or 0
        self.calls += 1
        ts = _epoch(r["completedAt"]) if r.get("completedAt") else time.time()
        for tc in resp.get("toolCalls") or []:
            ev.append({"kind": "call", "ts": ts, "tool": tc.get("name", "?"), "target": target_of(tc.get("input")),
                       "sig": signature(tc.get("name", "?"), tc.get("input")), "model_s": round((r.get("durationMs") or 0) / 1000, 1)})
        return ev


READERS = {"zcode": ZcodeLog}


# ---------------------------------------------------------------------------------------------- workdir
def workdir_fingerprint(cwd: str) -> str | None:
    """Hash of git HEAD + the dirty files (path, size, mtime). Changes when the worker edits or commits; None = not a git repo."""
    git = lambda *a: subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)  # noqa: E731
    try:
        head, st = git("rev-parse", "HEAD"), git("status", "--porcelain", "-z", "-uall")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if st.returncode:
        return None
    h = hashlib.sha1(head.stdout.encode())
    for ent in st.stdout.split("\0")[:400]:
        if len(ent) > 3:
            try:
                s = os.stat(os.path.join(cwd, ent[3:]))
                h.update(f"{ent}{s.st_size}{s.st_mtime_ns}".encode())
            except OSError:
                h.update(ent.encode())
    return h.hexdigest()


def workdir_summary(cwd: str) -> str:
    try:
        r = subprocess.run(["git", "-C", cwd, "status", "--short"], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = r.stdout.splitlines() if not r.returncode else []
    return f"{len(lines)} changed file(s): " + clean(", ".join(l[3:] for l in lines[:8]), 200) if lines else ""


# ---------------------------------------------------------------------------------------------- detector
class Detector:
    """Pure judgement over events; no I/O, so it is tested with recorded and synthetic logs."""

    def __init__(self, lim: Limits, now: float):
        self.lim = lim
        self.calls: list[str] = []      # signatures of tool calls, in order
        self.errors: list[tuple[str, int]] = []  # (signature, workdir epoch) of tool errors, in order
        self.epoch = 0                  # counts workdir changes
        self.known: set[str] = set()
        self.last_alive = self.last_novel = now
        self.since_change = 0           # calls since the workdir last changed
        self.cpu: list[tuple[float, int]] = []

    def feed(self, now: float, events: list[dict], wd_changed: bool = False, out_grew: bool = False, ticks: int | None = None):
        if ticks is not None:
            self.cpu.append((now, ticks))
            self.cpu = [c for c in self.cpu if now - c[0] <= self.lim.stuck_minutes * 60 + 120]
        if wd_changed:
            self.last_alive = self.last_novel = now
            self.since_change, self.epoch = 0, self.epoch + 1
        if out_grew or events:
            self.last_alive = now
        for e in events:
            if e["kind"] == "error":
                self.errors.append((e["sig"], self.epoch))
                continue
            self.calls.append(e["sig"])
            self.since_change += 1
            if e["sig"] not in self.known:
                self.known.add(e["sig"])
                self.last_novel = now

    def _cpu_active(self, now: float) -> bool:
        window = [c for c in self.cpu if now - c[0] <= self.lim.stuck_minutes * 60]
        return len(window) >= 2 and window[-1][1] - window[0][1] >= MIN_CPU_TICKS

    def verdict(self, now: float) -> tuple[str, str] | None:
        lim, k = self.lim, self.lim.loop_repeats
        run = self.calls[-self.since_change:] if self.since_change else []  # calls made with the workdir unchanged
        for p in (1, 2, 3):
            tail = run[-p * k:]
            if len(tail) == p * k and all(tail[i] == tail[i % p] for i in range(len(tail))) and (p == 1 or len(set(tail)) == p):
                what = "the same call" if p == 1 else f"a cycle of {p} calls"
                return "loop", f"{what} repeated {k} times with no change in the workdir"
        ek = lim.error_repeats
        if len(self.errors) >= ek and len(set(self.errors[-ek:])) == 1:  # same error, same epoch: the worker changed nothing between the tries
            return "loop", f"the same tool error {ek} times in a row with no change in the workdir"
        if self.calls and now - max(self.last_novel, 0) >= lim.no_progress_minutes * 60 and now - self.last_alive < lim.stuck_minutes * 60:
            return "loop", f"calls keep arriving but none is new and the workdir is unchanged for {lim.no_progress_minutes:g} min"
        if now - self.last_alive >= lim.stuck_minutes * 60 and not self._cpu_active(now):
            return "stuck", f"no sign of life (tool calls, workdir, output) for {lim.stuck_minutes:g} min and no CPU in the process tree"
        return None


# ---------------------------------------------------------------------------------------------- supervisor
class Supervisor:
    """One per worker run. `poll(now, ticks, out_bytes)` -> None to keep going, or (kind, reason) to stop the worker.
    Writes the filtered activity log `<base>.activity.log` (one line per tool call) and keeps what a stop report needs.
    With `emit` (the runtime's `kit-event/1` writer, docs/dashboard-events.md layer B) the same signals also go to events.jsonl:
    tool.call, tool.error, workdir.change, run.tick (every TICK_SECONDS) and run.stop (from `harness.halted`)."""

    def __init__(self, base: Path, cwd: str, harness_id: str, limits: Limits, started: float | None = None, marker: str = "",
                 emit=None):
        self.base, self.cwd, self.lim, self.harness, self.emit = Path(base), cwd, limits, harness_id, emit
        self.started = started or time.time()
        self.ticks, self.cpu_at, self.last_tick = None, self.started, self.started
        self.det = Detector(limits, self.started)
        reader = READERS.get(harness_id)
        self.reader = reader(marker, self.started) if reader and marker else None
        self.log = self.base.with_suffix(".activity.log")
        self.stop_file = self.base.with_suffix(".stop")
        self.fp, self.out_bytes, self.last = workdir_fingerprint(cwd), 0, "none"
        self.verdict: tuple[str, str] | None = None

    @property
    def tokens(self) -> dict:
        t = self.reader.tokens if self.reader else {}
        return dict(t) if any(t.values()) else {}

    def note(self, text: str, ts: float | None = None):
        with open(self.log, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S', time.localtime(ts or time.time()))}  {text}\n")

    def event(self, kind: str, **fields):
        """One structured event beside the activity log; fields that are None are left out (absent = not measured)."""
        if self.emit:
            try:
                self.emit(kind, harness=self.harness, **{k: v for k, v in fields.items() if v is not None})
            except Exception:  # noqa: BLE001  observability never breaks supervision: an exception here would leave the worker unwatched
                pass

    def state(self, now: float) -> str:
        """`slow` once the run is quiet for half of stuck_minutes (a warning; only the verdict calls it stuck)."""
        return "slow" if now - self.det.last_alive >= self.lim.stuck_minutes * 30 else "working"

    def poll(self, now: float, ticks: int | None = None, out_bytes: int = 0) -> tuple[str, str] | None:
        if self.stop_file.exists():
            return "manual", "stopped on request: " + clean(self.stop_file.read_text(), 300)
        events = self.reader.read() if self.reader else []
        for e in events:
            if e["kind"] == "call":
                self.note(f"{e['tool']:<10} {e['target']}  (model {e['model_s']}s)", e["ts"])
                self.last = f"{e['tool']} {e['target']}".strip()
                self.event("tool.call", state="working", tool=e["tool"], target=e["target"], model_s=e["model_s"])
            else:
                self.note(f"ERROR      {e['tool']}: {e['msg']}", e["ts"])
                self.event("tool.error", state="working", tool=e["tool"], msg=e["msg"])
        fp = workdir_fingerprint(self.cwd)
        changed = fp is not None and fp != self.fp
        if changed:
            summary = workdir_summary(self.cwd) or "changed"
            self.note("workdir    " + summary)
            self.event("workdir.change", state="working", summary=summary)
        self.fp = fp if fp is not None else self.fp
        self.det.feed(now, events, changed, out_bytes > self.out_bytes, ticks)
        self.out_bytes = out_bytes
        if ticks is not None and ticks != self.ticks:
            self.ticks, self.cpu_at = ticks, now
        if now - self.last_tick >= TICK_SECONDS:
            self.last_tick = now
            self.event("run.tick", state=self.state(now), elapsed_s=int(now - self.started), cpu_idle_s=int(now - self.cpu_at),
                       calls=len(self.det.calls), last_alive_age_s=int(now - self.det.last_alive), tokens=self.tokens or None)
        self.verdict = self.det.verdict(now)
        return self.verdict

    def stop_report(self, kind: str, reason: str) -> str:
        """The full reason for the run record and the task result: what tripped, the last signal, how long it ran, what the workdir holds."""
        d = self.det
        age = lambda t: f"{int(time.time() - t)}s ago"  # noqa: E731
        return (f"stopped by supervisor ({kind}): {reason}. Last tool call: {self.last} ({age(d.last_alive)} was the last sign of life; "
                f"last new call/workdir change {age(d.last_novel)}). Ran {int(time.time() - self.started)}s, {len(d.calls)} tool calls seen "
                f"({len(d.known)} distinct). Workdir: {workdir_summary(self.cwd) or 'unchanged'}. Activity log: {self.log}")

    def tail(self, n: int = 20) -> list[str]:
        return tail_lines(self.log, n)


def tail_lines(path: Path, n: int = 20) -> list[str]:
    try:
        return Path(path).read_text().splitlines()[-n:]
    except OSError:
        return []
