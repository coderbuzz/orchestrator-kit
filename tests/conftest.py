"""Test doubles live here only. Production code never simulates a harness or an external integration."""
import http.server
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companyos.harness import CallResult  # noqa: E402

SAMPLE = {"list": ["https://a.example/1", "https://b.example/2"], "string": "ok", "object": {"value": 1}, "bool": True}


def evidence_for(rules: dict, art_type: str, cwd: str) -> dict:
    ev = {}
    for r in rules["types"][art_type]:
        if r.get("special") == "git_commit":
            ev[r["key"]] = fake_commit(cwd)
        elif "equals" in r:
            ev[r["key"]] = r["equals"]
        elif r["check"] in ("number", "int"):
            ev[r["key"]] = max(r.get("min", 0), 1) if r["check"] == "int" else 0.8
        else:
            ev[r["key"]] = SAMPLE[r["check"]]
    if art_type == "commit":
        ev["repository"] = "."
    return ev


def fake_commit(cwd: str) -> str:
    run = lambda *a: subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, check=True)  # noqa: E731
    if not (Path(cwd) / ".git").exists():
        run("init", "-q")
    f = Path(cwd) / f"change-{time.time_ns()}.txt"
    f.write_text("work\n")
    run("add", f.name)
    run("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "work")
    return run("rev-parse", "HEAD").stdout.strip()


class FakeHarness:
    """Scripted stand-in for a model harness. `script(task, orch)` may return overrides for the auto result,
    a full result (key 'status'), or a CallResult. Records every call."""

    def __init__(self, script=None, cost=0.01, delay=0.0):
        self.script, self.cost, self.delay = script or (lambda t, o: None), cost, delay
        self.calls, self.orch, self.active, self.max_active = [], None, 0, 0
        self.lock = threading.Lock()

    def __call__(self, hdef, model, req, schema):
        tid = re.search(r'"task_id": "([^"]+)"', req.prompt).group(1)
        task = self.orch.store.get("tasks", tid)
        with self.lock:
            self.calls.append({"task": tid, "agent": task["assigned_agent"], "step": task["step_id"], "kind": task["kind"], "model": model, "harness": hdef["id"],
                               "provider": req.provider, "effort": req.effort})
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            custom = self.script(task, self.orch)
            if isinstance(custom, CallResult):
                return custom
            out = custom if isinstance(custom, dict) and "status" in custom else {**self.auto(task, req), **(custom or {})}
            return CallResult(True, hdef["id"], model, output=out, cost_usd=self.cost)
        finally:
            with self.lock:
                self.active -= 1

    def auto(self, task, req):
        rules = self.orch.reg.evidence_rules
        out = {"status": "completed", "summary": f"{task['assigned_agent']} did: {task['objective'][:80]}", "artifacts": []}
        for at in task["required_artifacts"]:
            if at == "deployment":
                env = "production" if "production" in task["objective"].lower() else "staging"
                out["actions"] = [{"capability": f"deploy_{env}", "params": {}, "reason": f"deploy to {env}"}]
                continue
            out["artifacts"].append({"type": at, "title": at, "content": "body", "evidence": evidence_for(rules, at, req.cwd)})
        if task["decision_options"]:
            out["decision"] = {"selected_option": task["decision_options"][0], "reasoning_summary": "auto", "confidence": 0.7, "parameters": {}}
        if task["kind"] in ("verification", "approval_review"):
            out["verdict"] = "PASS"
        if task["kind"] == "escalation":
            out["decision"]["selected_option"] = "STOP_RUN"
        return out

    def calls_for(self, **kw):
        return [c for c in self.calls if all(c.get(k) == v for k, v in kw.items())]


@pytest.fixture
def fake_dsh(tmp_path):
    """A scripted `dsh` executable: answers --version, records its other argv in argv.json, prints `stdout`, exits with `code`."""
    from companyos.registry import Registry

    def make(stdout="", code=0, version="0.1.6"):
        out, argv, script = tmp_path / "out.txt", tmp_path / "argv.json", tmp_path / "dsh"
        out.write_text(stdout)
        script.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                          f"if '--version' in sys.argv: print({version!r}); sys.exit(0)\n"
                          f"open({str(argv)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                          f"sys.stdout.write(open({str(out)!r}).read()); sys.exit({code})\n")
        script.chmod(0o755)
        h = Registry().harnesses["dsh"]
        return {**h, "invocation": {**h["invocation"], "command": str(script), "prefix": []}}, lambda: json.loads(argv.read_text())
    return make
