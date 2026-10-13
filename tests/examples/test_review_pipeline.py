"""The second pack (examples/review-pipeline) on the kit alone: validate, generate its plugin, run a full review through its
MCP server with a scripted harness, and reject findings a model made up. The kit has no code for this pack."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FakeHarness
from orchestrator_kit import hook, validate
from orchestrator_kit.plugin import generate
from orchestrator_kit.registry import Registry
from orchestrator_kit.runtime import Refused

PACK = Path(__file__).resolve().parents[2] / "examples/review-pipeline"
sys.path.insert(0, str(PACK))
import review_pipeline as rp  # noqa: E402

BASE_SRC = "def load(path):\n    return open(path).read()\n"
CHANGED_SRC = "import os\n\ndef load(path):\n    os.system('cat ' + path)\n    return open(path).read()\n"
INJECTION = "    os.system('cat ' + path)"  # line 4 of CHANGED_SRC


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *a],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def review(tmp_path):
    """A workspace with one review project `pr-7` whose repo has a base commit and the change under review."""
    repo = tmp_path / "ws/pr-7/repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "app.py").write_text(BASE_SRC)
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text(CHANGED_SRC)
    git(repo, "commit", "-qam", "change")
    posted = []
    fake_gh = lambda orch, task, p: (posted.append(p), {"ok": True, "detail": "posted", "artifact": {  # noqa: E731
        "type": "pr_comment", "title": "comment", "evidence": {"url": "https://example.test/pr/7#c1"}}})[1]
    answers = {}
    fake = FakeHarness(lambda task, orch: answers[task["assigned_agent"]].pop(0))
    s = rp.server(tmp_path / "ws", invoke=fake, adapters={"post_pr_comment": fake_gh})
    s.call("project_init", {"project": "pr-7", "name": "PR 7", "daily_budget_usd": 5})
    fake.orch = s.company("pr-7")
    return s, answers, base, posted, tmp_path


def scope(base):
    return {"type": "review_scope", "title": "scope", "evidence": {"diff_base": base, "files_reviewed": ["app.py"]}}


def finding(base, line, excerpt, file="app.py"):
    return {"type": "review_finding", "title": f"{file}:{line}", "content": "shell injection",
            "evidence": {"file": file, "line": line, "severity": "high", "diff_base": base, "evidence_excerpt": excerpt}}


def result(*artifacts, **extra):
    return {"status": "completed", "summary": "reviewed", "artifacts": list(artifacts), **extra}


def test_pack_validates_and_its_vocabulary_is_enforced(tmp_path):
    assert validate.check(PACK) == []
    copy = tmp_path / "pack"
    shutil.copytree(PACK, copy, ignore=shutil.ignore_patterns("__pycache__"))
    a = copy / "agents/verifier.agent.yaml"
    a.write_text(a.read_text().replace("department: review", "department: marketing"))
    assert any("unknown department 'marketing'" in e for e in validate.check(copy))


def test_plugin_generates_from_the_pack_alone():
    files = generate(Registry(PACK), rp.ReviewGenerator())
    assert json.loads(files[".claude-plugin/plugin.json"])["name"] == "review-pipeline"
    assert json.loads(files[".mcp.json"]) == {"review": {"command": "review-pipeline-mcp"}}
    pre = json.loads(files["hooks/hooks.json"])["hooks"]["PreToolUse"][0]
    assert pre["matcher"].startswith("mcp__plugin_review-pipeline_review__maintainer_.*|Bash")
    assert "model: opus" in files["agents/security-reviewer.md"] and "model: haiku" in files["agents/bug-reviewer.md"]
    lead = files["skills/review-lead/SKILL.md"]
    assert "`maintainer_decide`" in lead and "security-reviewer" in lead and "### review-synthesis" not in lead
    assert not [f for f, text in files.items() if any(w in text for w in ("QUMATIC", "CEO", "Board", "board_", ".company"))]


def test_full_review_flow_with_maintainer_approval(review):
    s, answers, base, posted, tmp = review
    answers.update({
        "security-reviewer": [result(scope(base), finding(base, 4, INJECTION))],
        "simplicity-reviewer": [result(scope(base))],   # nothing to report is a valid outcome
        "bug-reviewer": [result(scope(base), finding(base, 99, "return None")),   # made up: rejected, retried with feedback
                         result(scope(base))],
    })
    tid = {}
    for agent in ("security-reviewer", "simplicity-reviewer", "bug-reviewer"):
        tid[agent] = s.call("task_add", {"project": "pr-7", "agent": agent, "objective": f"{agent} of PR 7", "criteria": ["in diff"],
                                         "skill": agent.replace("-reviewer", "-review"), "inputs": {"workdir": "repo"}})
    assert s.call("dispatch", {"project": "pr-7", "task_id": tid["security-reviewer"]})["status"] == "COMPLETED"
    assert s.call("dispatch", {"project": "pr-7", "task_id": tid["simplicity-reviewer"]})["status"] == "COMPLETED"
    bug = s.call("dispatch", {"project": "pr-7", "task_id": tid["bug-reviewer"]})
    assert bug["status"] == "PLANNED" and "app.py:99 is not a changed line" in bug["notes"][0]
    assert s.call("dispatch", {"project": "pr-7", "task_id": tid["bug-reviewer"]})["status"] == "COMPLETED"

    body = "changes_requested: app.py:4 shell injection"
    answers["review-lead"] = [result({"type": "review_summary", "title": "review", "content": body,
                                      "evidence": {"verdict": "changes_requested", "findings": ["app.py:4"]}},
                                     actions=[{"capability": "post_pr_comment", "params": {"pr": 7, "body": body}, "reason": "publish the review"}])]
    lead = s.call("task_add", {"project": "pr-7", "agent": "review-lead", "objective": "synthesize PR 7", "criteria": ["one review"],
                               "skill": "review-synthesis", "after": list(tid.values()), "inputs": {"workdir": "repo"}})
    out = s.call("dispatch", {"project": "pr-7", "task_id": lead})
    assert out["status"] == "WAITING_APPROVAL" and not posted, "nothing leaves the repo before the maintainer decides"
    [apr] = s.call("approvals", {"project": "pr-7"})
    assert apr["approver"] == "maintainer" and apr["maintainer"] is True
    with pytest.raises(Refused, match="maintainer_decide"):
        s.call("review", {"project": "pr-7", "approval_id": apr["approval_id"], "grant": True, "as_agent": "maintainer"})
    done = s.call("maintainer_decide", {"project": "pr-7", "approval_id": apr["approval_id"], "approve": True})
    assert done["status"] == "COMPLETED" and posted == [{"pr": 7, "body": body}]
    c = s.company("pr-7")
    types = {a["type"] for a in c.store.all("artifacts") if a["status"] == "accepted"}
    assert types >= {"review_scope", "review_finding", "review_summary", "pr_comment"}
    assert c.store.read_log("maintainer/inbox.jsonl"), "the maintainer was asked through the human inbox"
    assert (tmp / "ws/pr-7/.review/state/tasks").is_dir()
    assert "Status" in s.call("status", {"project": "pr-7"})


@pytest.mark.parametrize("line,excerpt,file,why", [
    (99, "return None", "app.py", "not a changed line"),               # line that does not exist
    (3, "def load(path):", "app.py", "not a changed line"),            # real line, but the change did not touch it
    (4, "eval(user_input)", "app.py", "does not contain the quoted"),  # right line, invented quote
    (4, INJECTION, "../outside.py", "not a file in the workspace"),    # path escaping the workspace
])
def test_made_up_findings_are_rejected(review, line, excerpt, file, why):
    s, answers, base, _, _ = review
    answers["security-reviewer"] = [result(scope(base), finding(base, line, excerpt, file))] * 2
    t = s.call("task_add", {"project": "pr-7", "agent": "security-reviewer", "objective": "review", "criteria": ["in diff"],
                            "skill": "security-review", "inputs": {"workdir": "repo"}})
    first = s.call("dispatch", {"project": "pr-7", "task_id": t})
    assert first["status"] == "PLANNED" and why in first["notes"][0]
    assert s.call("dispatch", {"project": "pr-7", "task_id": t})["status"] == "FAILED"


def test_unregistered_special_check_fails_closed():
    from orchestrator_kit.verify import check_evidence
    rules = {"types": {"x": [{"key": "a", "check": "string", "special": "nobody_registered_this"}]}}
    assert "no check registered" in check_evidence(rules, "x", {"a": "v"})[0]


def test_kit_hook_guards_the_packs_state_and_human_tools():
    ask = hook.guard({"tool_name": "mcp__plugin_review-pipeline_review__maintainer_decide"}, ".review", "maintainer_")
    assert ask["hookSpecificOutput"]["permissionDecision"] == "ask"
    edit = hook.guard({"tool_name": "Write", "tool_input": {"file_path": "/w/pr-7/.review/state/x.json"}}, ".review", "maintainer_")
    assert edit["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert hook.guard({"tool_name": "mcp__plugin_review-pipeline_review__status"}, ".review", "maintainer_") == {}
    loaded = subprocess.run([sys.executable, "-c", "import sys, orchestrator_kit.hook; print(sorted({'yaml', 'jsonschema'} & set(sys.modules)))"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert loaded == "[]", "the hook runs on every tool call: stdlib only"
