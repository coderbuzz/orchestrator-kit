"""Quota guard: skip a route whose subscription is nearly spent instead of trying it and stalling, and name limit errors.

A skipped route is a failed attempt with the reason (never a task failure on its own: the next route runs); nothing waits for a reset.
  agy          `agy -p /usage` (Gemini and Claude/GPT groups): skip below 15% of the 5-hour window or 20% of the weekly one
  claude-code  one cheap probe call (haiku, stream-json) reads the `rate_limit_event` utilization: skip at >= 85% (5 h) or >= 80% (7 d),
               when the plan does not say "allowed", or when the probe itself fails
  others       no readable quota (GLM coding plan, dsh credit): only the error classification below applies
Readings are cached for CACHE_SECONDS. Stdlib only.
"""
import json
import re
import subprocess
import time

CACHE_SECONDS = 600
AGY_MIN = {"five": 15, "weekly": 20}  # percent remaining
CLAUDE_MAX = {"five_hour": 0.85, "seven_day": 0.80}  # utilization
LIMIT_RE = re.compile(r"rate[ _-]?limit|quota|usage limit|limit (?:reached|exceeded)|resource[_ ]exhausted|\b429\b|too many requests|insufficient (?:balance|credit)|out of credit", re.I)
_cache: dict[str, tuple[float, str | None]] = {}


def classify(error: str) -> str:
    """Prefix a limit/quota error so the log and the task feedback say the route is spent, not broken."""
    return error if not error or error.startswith("route exhausted") or not LIMIT_RE.search(error) else f"route exhausted (quota/rate limit): {error}"


def agy_reason(text: str, model: str) -> str | None:
    group = "Claude and GPT" if model.startswith(("claude", "gpt")) else "Gemini"
    for line in text.splitlines():
        m = re.match(rf"{group} Models?\s+(Weekly|Five Hour) Limit Remaining\s+(\d+)%", line.strip()) or re.match(rf"{group}.*?\s(Weekly|Five Hour) Limit Remaining\s+(\d+)%", line.strip())
        if m and int(m[2]) < AGY_MIN["weekly" if m[1] == "Weekly" else "five"]:
            return f"agy {group} {m[1].lower()} quota at {m[2]}% (needs >= {AGY_MIN['weekly' if m[1] == 'Weekly' else 'five']}%)"
    return None


def claude_reason(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "rate_limit_event":
            info = e.get("rate_limit_info") or {}
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                return f"Claude plan status {info['status']}"
            for w, cap in CLAUDE_MAX.items():
                u = ((info.get("unifiedWindows") or {}).get(w) or {}).get("utilization")
                if u is not None and u >= cap:
                    return f"Claude plan {w} window at {u:.0%} (skips at {cap:.0%})"
    return None


def _run(argv, timeout, cwd=None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None


def skip_reason(hdef: dict, model: str, run=_run) -> str | None:
    """Why this route should not be tried now (None = go). Cached; `run` is injectable for tests."""
    hid = hdef["id"]
    if hid not in ("agy", "claude-code"):
        return None
    key = f"{hid}:{model if hid == 'agy' and model.startswith(('claude', 'gpt')) else ''}"
    if key in _cache and time.time() - _cache[key][0] < CACHE_SECONDS:
        return _cache[key][1]
    cmd = hdef["invocation"]["command"]
    if hid == "agy":
        r = run([cmd, "-p", "/usage"], 60)
        why = (f"cannot read agy /usage ({'timeout' if r is None else 'exit ' + str(r.returncode)})" if r is None or r.returncode else agy_reason(r.stdout, model))
    else:
        r = run([cmd, "-p", "ok", "--model", "claude-haiku-5-5", "--output-format", "stream-json", "--verbose", "--tools", ""], 90, cwd="/tmp")
        why = "Claude quota probe failed (limit reached or unreachable)" if r is None or r.returncode else claude_reason(r.stdout)
    _cache[key] = (time.time(), why)
    return why
