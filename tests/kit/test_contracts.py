import copy
import re
import subprocess

import pytest

from orchestrator_kit import validate as v

NOW = "2026-10-07T00:00:00Z"
TASK = {"task_id": "task-001", "parent_task_id": None, "objective": "o", "assigned_agent": "example-agent", "created_by": "board",
        "priority": "normal", "status": "PENDING", "dependencies": [], "acceptance_criteria": ["c"], "created_at": NOW, "updated_at": NOW}
ARTIFACT = {"artifact_id": "art-001", "type": "test_result", "location": "repo://x", "created_by": "example-agent", "created_at": NOW,
            "version": "1", "status": "accepted", "evidence": {"sha": "abc"}}
APPROVAL = {"approval_id": "apr-001", "task_id": "task-001", "requested_by": "example-agent", "level": 2, "action": "a",
            "justification": "j", "status": "pending", "requested_at": NOW}


@pytest.mark.parametrize("kind,obj", [("task", TASK), ("artifact", ARTIFACT), ("approval", APPROVAL)])
def test_valid_instances(kind, obj):
    assert v.validate_instance(kind, obj) == []


@pytest.mark.parametrize("kind,obj,field", [
    ("task", TASK, "status"),
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
    lc = v.load(v.schema_file("protocols/tasks/lifecycle.yaml"))
    states = set(v.load(v.schema_file("protocols/tasks/task.schema.json"))["properties"]["status"]["enum"])
    assert set(lc["transitions"]) == states
    assert all(t in states for ts in lc["transitions"].values() for t in ts)
    assert all(lc["transitions"][s] == [] for s in lc["terminal"])
    assert lc["initial"] in states



def test_artifact_type_is_open_but_shaped():
    assert v.validate_instance("artifact", {**ARTIFACT, "type": "claim_check"}) == []
    assert v.validate_instance("artifact", {**ARTIFACT, "type": "Not A Type"})
