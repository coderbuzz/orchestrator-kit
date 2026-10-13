"""The second pack (examples/review-pipeline) on the kit alone: validate, generate its plugin, run a full review through its
MCP server with a scripted harness, and reject findings a model made up. The kit has no code for this pack."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FakeHarness
from orchestrator_kit.harness import CallResult
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


def test_project_hires_its_team_from_the_catalog(tmp_path):
    (tmp_path / "ws/pr-9").mkdir(parents=True)
    s = rp.server(tmp_path / "ws", invoke=FakeHarness())
    with pytest.raises(Refused, match="not in the company catalog"):
        s.call("project_init", {"project": "pr-9", "name": "PR 9", "daily_budget_usd": 1, "roster": ["copywriter"]})
    info = s.call("project_init", {"project": "pr-9", "name": "PR 9", "daily_budget_usd": 1, "goal": "Ship PR 9 without security defects",
                                   "roster": ["security-reviewer"]})
    assert info["roster"] == ["review-lead", "security-reviewer"], "the orchestrator is always on the roster"
    with pytest.raises(Refused, match=r"'bug-reviewer' is not on this project's roster \(use maintainer_staff\)"):
        s.call("task_add", {"project": "pr-9", "agent": "bug-reviewer", "objective": "x", "criteria": ["x"]})
    info = s.call("maintainer_staff", {"project": "pr-9", "hire": ["bug-reviewer"], "release": ["security-reviewer"]})
    assert info["roster"] == ["bug-reviewer", "review-lead"]
    with pytest.raises(Refused, match="orchestrator stays"):
        s.call("maintainer_staff", {"project": "pr-9", "release": ["review-lead"]})
    t = s.call("task_add", {"project": "pr-9", "agent": "bug-reviewer", "objective": "x", "criteria": ["x"]})
    c = s.company("pr-9")
    assert '"project_goal": "Ship PR 9 without security defects"' in c.prompt(c.task(t), c.reg.agents["bug-reviewer"], None)[1]
    assert any(a["action"] == "roster" for a in c.store.read_log("audit.jsonl"))
    report = s.call("status", {"project": "pr-9"})
    assert "- goal: Ship PR 9 without security defects" in report and "- roster: bug-reviewer, review-lead" in report
    with pytest.raises(Refused, match="unknown or disabled agent 'nobody'"):
        s.call("task_add", {"project": "pr-9", "agent": "nobody", "objective": "x", "criteria": ["x"]})


def test_human_is_told_at_80_percent_of_the_daily_budget(review):
    s, answers, base, _, _ = review   # daily budget 5 USD; each fake call costs 2 USD
    c = s.company("pr-7")
    c.invoke.cost = 2.0
    answers["simplicity-reviewer"] = [result(scope(base))] * 3
    for _ in range(2):
        t = s.call("task_add", {"project": "pr-7", "agent": "simplicity-reviewer", "objective": "r", "criteria": ["x"],
                                "skill": "simplicity-review", "inputs": {"workdir": "repo"}})
        s.call("dispatch", {"project": "pr-7", "task_id": t})
    warnings = [m for m in c.store.read_log("maintainer/inbox.jsonl") if m["kind"] == "budget"]
    assert len(warnings) == 1 and "80%" in warnings[0]["message"]


def test_native_subagent_gets_the_contract_and_a_contract_violation_uses_the_attempt(review):
    s, _, base, _, _ = review
    t = s.call("task_add", {"project": "pr-7", "agent": "security-reviewer", "objective": "review", "criteria": ["in diff"],
                            "skill": "security-review", "inputs": {"workdir": "repo"}})
    first = s.call("task_start", {"project": "pr-7", "task_id": t})
    prompt = first["subagent_prompt"]
    assert "Valid example:" in prompt and '"reversibility": {"enum": ["easy", "costly", "irreversible"]}' in prompt
    assert '"$ref": "../ai/1"' not in prompt, "the schema is self-contained"
    bad = {"status": "completed", "summary": "s", "state_updates": {}, "artifacts": [{"kind": "review_scope", "title": "t", "evidence": {}}]}
    out = s.call("task_submit", {"project": "pr-7", "task_id": t, "result": bad})
    assert out["status"] == "PLANNED" and "state_updates" in out["notes"][0], "rejected with feedback, not left RUNNING"
    again = s.call("task_start", {"project": "pr-7", "task_id": t})
    assert again["attempt"] == 2 and "previous_attempt_feedback" in again["subagent_prompt"] and "kind" in again["subagent_prompt"]
    assert s.call("task_submit", {"project": "pr-7", "task_id": t, "result": bad})["status"] == "FAILED"
    with pytest.raises(Refused, match="attempts exhausted|is FAILED"):
        s.call("task_start", {"project": "pr-7", "task_id": t})
    t2 = s.call("task_add", {"project": "pr-7", "agent": "security-reviewer", "objective": "review", "criteria": ["in diff"],
                             "skill": "security-review", "inputs": {"workdir": "repo"}})
    s.call("task_start", {"project": "pr-7", "task_id": t2})
    assert s.call("task_submit", {"project": "pr-7", "task_id": t2, "result": result(scope(base))})["status"] == "COMPLETED"


def test_flat_plan_calls_do_not_count_against_the_budget_but_are_kept_as_data(review):
    s, answers, base, _, _ = review   # daily budget 5 USD; each call is worth 3 USD at list price on a flat plan
    answers["simplicity-reviewer"] = [CallResult(True, "zcode", "m", output=result(scope(base)), cost_usd=0.0, equivalent_cost_usd=3.0,
                                                 tokens={"inputTokens": 10})] * 2
    for _ in range(2):
        t = s.call("task_add", {"project": "pr-7", "agent": "simplicity-reviewer", "objective": "r", "criteria": ["x"],
                                "skill": "simplicity-review", "inputs": {"workdir": "repo"}})
        assert s.call("dispatch", {"project": "pr-7", "task_id": t})["status"] == "COMPLETED"
    u = s.call("usage", {"project": "pr-7"})
    assert (u["today_usd"], u["today_equivalent_usd"], u["total_equivalent_usd"], u["unpriced_calls"]) == (0, 6.0, 6.0, 0)
    assert u["calls"][-1]["equivalent_cost_usd"] == 3.0 and u["calls"][-1]["tokens"] == {"inputTokens": 10}
    assert not [m for m in s.company("pr-7").store.read_log("maintainer/inbox.jsonl") if m["kind"] == "budget"], "no 80% warning"


def test_native_subagent_usage_is_recorded_as_data_never_charged(review):
    s, _, base, _, _ = review
    t = s.call("task_add", {"project": "pr-7", "agent": "security-reviewer", "objective": "review", "criteria": ["in diff"],
                            "skill": "security-review", "inputs": {"workdir": "repo"}})
    s.call("task_start", {"project": "pr-7", "task_id": t})
    usage = {"model": "opus", "total_tokens": 28840, "tool_uses": 1, "duration_ms": 5400}
    assert s.call("task_submit", {"project": "pr-7", "task_id": t, "result": result(scope(base)), "usage": usage})["status"] == "COMPLETED"
    u = s.call("usage", {"project": "pr-7"})
    [call] = u["calls"]
    assert (call["harness"], call["model"], call["tokens"], call["cost_usd"], call["equivalent_cost_usd"], call["duration_s"]) == \
           ("native", "opus", {"totalTokens": 28840}, 0.0, None, 5.4)
    assert (u["today_usd"], u["today_tokens"], u["unpriced_calls"]) == (0, 28840, 1)
    assert "usage" in [x for x in s.tools if x["name"] == "task_submit"][0]["inputSchema"]["properties"]
