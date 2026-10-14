"""`orchestrator-kit tail <project dir> [task_id] [-n N] [-f]`: watch a worker's activity log from a terminal. Stdlib only (cli fast path)."""
import glob
import json
import os
import sys
import time


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("-")]
    n = int(argv[argv.index("-n") + 1]) if "-n" in argv else 30
    args = [a for a in args if a != str(n)] if "-n" in argv else args
    follow = "-f" in argv
    if not args:
        print("usage: orchestrator-kit tail <project dir> [task_id] [-n N] [-f]", file=sys.stderr)
        return 2
    name = f"{args[1] if len(args) > 1 else '*'}.activity.log"
    logs = [f for d in (".*", "*") for f in glob.glob(os.path.join(args[0], d, "state", "runs", name))]  # state dir is e.g. .company
    if not logs:
        print("no activity log found", file=sys.stderr)
        return 1
    log = max(logs, key=os.path.getmtime)
    with open(log) as f:
        print(f"== {log}")
        print("".join(f.readlines()[-n:]), end="")
        while follow:
            line = f.readline()
            if line:
                print(line, end="", flush=True)
                continue
            rec = log.replace(".activity.log", ".json")
            if os.path.exists(rec) and json.load(open(rec)).get("done"):
                return 0
            time.sleep(1)
    return 0
