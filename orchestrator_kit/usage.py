"""SubagentStop hook: record what a native subagent consumed on the task it worked on (never charged; data for projections).

Claude Code passes the subagent's transcript path; its assistant messages carry the model and the exact token split (input, cache
write, cache read, output), so the equivalent cost is priced from the claude-code descriptor's list prices. The task is the one
named in the subagent's prompt (the `task_start` contract); a subagent of another plugin, or one without a task, is ignored.
"""
import json
import re
from datetime import datetime
from pathlib import Path

TASK_RE = re.compile(r'task_id\\?"\s*:\s*\\?"(task-[0-9a-f]+)')


def transcript_usage(path: str | Path) -> dict | None:
    """{task_id, model, tokens, duration_s} from a subagent transcript (JSONL); None when unreadable or empty."""
    task, msgs, stamps = None, {}, []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("timestamp"):
            stamps.append(e["timestamp"])
        if task is None and e.get("type") == "user" and (m := TASK_RE.search(line)):
            task = m.group(1)
        msg = e.get("message") or {}
        if e.get("type") == "assistant" and msg.get("usage") and msg.get("id"):
            msgs[msg["id"]] = msg  # one API message is written once per content block: count it once
    if not msgs:
        return None
    s = lambda k: sum(m["usage"].get(k) or 0 for m in msgs.values())  # noqa: E731
    write, read = s("cache_creation_input_tokens"), s("cache_read_input_tokens")
    tokens = {"inputTokens": s("input_tokens") + write + read, "cacheWriteTokens": write, "cacheReadTokens": read, "outputTokens": s("output_tokens")}
    try:
        span = (datetime.fromisoformat(stamps[-1].replace("Z", "+00:00")) - datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))).total_seconds()
    except (IndexError, ValueError):
        span = 0.0
    return {"task_id": task, "model": list(msgs.values())[-1].get("model"), "tokens": tokens, "duration_s": round(span, 1)}


def record(event: dict, root: Path, runtime_cls=None, ws: Path | None = None) -> str | None:
    """Record a SubagentStop event on its task; returns the project, or None when nothing was recorded."""
    from . import harness
    from .mcp import workspace
    from .pack import Pack
    from .runtime import Runtime, projects
    pack = Pack.load(root)
    if not str(event.get("agent_type", "")).startswith(f"{pack.name}:") or not event.get("agent_transcript_path"):
        return None
    u = transcript_usage(event["agent_transcript_path"])
    if not u or not u["task_id"]:
        return None
    for name, d in projects(ws or workspace(), pack.project_file).items():
        if (d / pack.state_dir / "state" / "tasks" / f"{u['task_id']}.json").exists():
            c = (runtime_cls or Runtime).for_project(d, root=root)
            price = harness.price_for(c.reg.harnesses.get("claude-code") or {}, "anthropic", u["model"] or "")
            c.record_native_usage(u["task_id"], model=u["model"], tokens=u["tokens"], duration_s=u["duration_s"],
                                  equivalent=harness.token_cost(price, u["tokens"]), key=event.get("agent_id"))
            return name
    return None


def hook_main(root: Path, runtime_cls=None, stdin=None) -> int:
    """Hook entry: never blocks or fails the session (a SubagentStop hook's output could keep the subagent running)."""
    import sys
    try:
        record(json.load(stdin or sys.stdin), Path(root), runtime_cls)
    except Exception as e:  # noqa: BLE001 - usage is data; losing one record must not break the session
        print(f"usage hook: {type(e).__name__}: {e}", file=sys.stderr)
    return 0
