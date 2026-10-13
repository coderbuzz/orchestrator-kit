"""Generic PreToolUse guard for a pack's plugin. It runs on every Bash/Edit/Write call of every session, so it is stdlib-only
and imports nothing else from the package.

python -m orchestrator_kit.hook <state_dir> <human_tool_prefix>     (hook event JSON on stdin)

Tools named <human_tool_prefix>* always ask the human; the shell may not touch <state_dir>/state and edit tools may not
write under /<state_dir>/. A speed bump for an honest session, not a sandbox.
"""
import json
import re
import sys

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def guard(event: dict, state_dir: str, human_prefix: str) -> dict:
    name, args = event.get("tool_name", ""), event.get("tool_input") or {}
    decide = lambda d, why: {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": d, "permissionDecisionReason": why}}  # noqa: E731
    if re.search(rf"__{re.escape(human_prefix)}[a-z_]+$", name):
        return decide("ask", "Reserved to the human: confirm only if you decided this.")
    if name == "Bash" and re.search(re.escape(state_dir) + r"(/|\\/)state", args.get("command", "")):
        return decide("deny", "Project state goes through the pack's tools, not the shell.")
    if name in EDIT_TOOLS and f"/{state_dir}/" in str(args.get("file_path", "")):
        return decide("deny", "Project state is written only by the pack's tools.")
    return {}


def main(argv: list[str]) -> int:
    out = guard(json.load(sys.stdin), *argv[:2])
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
