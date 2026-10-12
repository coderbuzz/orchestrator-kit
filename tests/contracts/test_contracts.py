import copy
import re
import subprocess

import pytest
import yaml

from companyos import validate as v

NOW = "2026-10-07T00:00:00Z"
TASK = {"task_id": "task-001", "parent_task_id": None, "objective": "o", "assigned_agent": "example-agent", "created_by": "board",
        "priority": "normal", "status": "PENDING", "dependencies": [], "acceptance_criteria": ["c"], "created_at": NOW, "updated_at": NOW}
DECISION = {"decision_id": "dec-001", "decision": "d", "decision_type": "product", "owner": "example-agent", "objective": "o",
            "options": [{"id": "a", "summary": "A"}, {"id": "b", "summary": "B"}], "selected_option": "a", "reasoning_summary": "r",
            "evidence": [], "confidence": 0.7, "impact": "low", "cost": {"estimate_usd": None}, "risk": "low", "reversibility": "easy",
            "approval_required": False, "approval_status": "not_required", "created_at": NOW}
ARTIFACT = {"artifact_id": "art-001", "type": "test_result", "location": "repo://x", "created_by": "example-agent", "created_at": NOW,
            "version": "1", "status": "accepted", "evidence": {"sha": "abc"}}
APPROVAL = {"approval_id": "apr-001", "task_id": "task-001", "requested_by": "example-agent", "level": 2, "action": "a",
            "justification": "j", "status": "pending", "requested_at": NOW}
STATE = {"schema_version": 1, "as_of": NOW, "mission": "m"}


@pytest.mark.parametrize("kind,obj", [("task", TASK), ("decision", DECISION), ("artifact", ARTIFACT),
                                      ("approval", APPROVAL), ("company-state", STATE)])
def test_valid_instances(kind, obj):
    assert v.validate_instance(kind, obj) == []


@pytest.mark.parametrize("kind,obj,field", [
    ("task", TASK, "status"), ("decision", DECISION, "confidence"),
    ("artifact", ARTIFACT, "status"), ("approval", APPROVAL, "level")])
def test_invalid_instances(kind, obj, field):
    bad = copy.deepcopy(obj)
    bad[field] = "nonsense" if field != "confidence" and field != "level" else 9
    assert v.validate_instance(kind, bad)


def test_completed_task_requires_evidence():
    t = {**TASK, "status": "COMPLETED"}
    assert v.validate_instance("task", t)
    assert v.validate_instance("task", {**t, "evidence": ["art-001"]}) == []


def test_submitted_artifact_requires_evidence():
    assert v.validate_instance("artifact", {**ARTIFACT, "evidence": {}})


def test_lifecycle_matches_schema():
    lc = yaml.safe_load((v.ROOT / "protocols/tasks/lifecycle.yaml").read_text())
    states = set(v.load(v.ROOT / "protocols/tasks/task.schema.json")["properties"]["status"]["enum"])
    assert set(lc["transitions"]) == states
    assert all(t in states for ts in lc["transitions"].values() for t in ts)
    assert all(lc["transitions"][s] == [] for s in lc["terminal"])
    assert lc["initial"] in states



def test_artifact_type_is_open_but_shaped():
    assert v.validate_instance("artifact", {**ARTIFACT, "type": "claim_check"}) == []
    assert v.validate_instance("artifact", {**ARTIFACT, "type": "Not A Type"})
