"""Deterministic evidence verification. "Done." is never evidence.

verify_task() checks: required artifact types present, every artifact's evidence against
protocols/artifacts/evidence-requirements.yaml (with special checks that look at the real world: git commit exists,
URL answers 2xx, sources are URLs), and a decision that picks one of the allowed options.
Independent (LLM) verification is a separate task the orchestrator session gives another agent.
"""
import subprocess
import urllib.request
from pathlib import Path

TYPES = {"list": list, "number": (int, float), "int": int, "string": str, "object": dict, "bool": bool}


def _sources(key: str, value, evidence: dict, workspace: Path | None, known_ids: set) -> str | None:
    bad = [s for s in value if not (isinstance(s, str) and (s.startswith(("http://", "https://")) or s in known_ids))
           and not (isinstance(s, dict) and str(s.get("url", "")).startswith(("http://", "https://")))]
    return f"{key}: not URLs or artifact ids: {bad[:3]}" if bad else None


def _git_commit(key: str, value, evidence: dict, workspace: Path | None, known_ids: set) -> str | None:
    if workspace is None:
        return "commit: no workspace to check the sha in"
    repo = (workspace / str(evidence.get("repository") or ".")).resolve()
    if not str(repo).startswith(str(workspace.resolve())):
        return "commit: repository is outside the task workspace"
    r = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{value}^{{commit}}"], capture_output=True)
    return None if r.returncode == 0 else f"commit: sha {value} not found in {evidence.get('repository')}"


def _http_ok(key: str, value, evidence: dict, workspace: Path | None, known_ids: set) -> str | None:
    try:
        with urllib.request.urlopen(str(value), timeout=10) as resp:
            return None if 200 <= resp.status < 300 else f"{key}: HTTP {resp.status}"
    except Exception as e:  # noqa: BLE001 - any failure is a failed health check
        return f"{key}: health check failed ({type(e).__name__})"


# `special` checks: fn(key, value, evidence, workspace, known_ids) -> problem or None. A pack adds its own through
# Runtime.evidence_checks; a rule naming a check nobody registered fails (never passes unchecked).
CHECKS = {"sources": _sources, "git_commit": _git_commit, "http_ok": _http_ok}


def check_evidence(rules: dict, art_type: str, evidence: dict, workspace: Path | None = None, known_ids: set = frozenset(),
                   checks: dict | None = None) -> list[str]:
    checks = {**CHECKS, **(checks or {})}
    spec = rules["types"].get(art_type)
    if spec is None:
        return [f"unknown artifact type '{art_type}'"]
    problems = []
    for r in spec:
        key, val = r["key"], (evidence or {}).get(r["key"])
        if val is None:
            problems.append(f"{art_type}: missing evidence '{key}'")
            continue
        want = TYPES[r["check"]]
        if not isinstance(val, want) or (r["check"] in ("int", "number") and isinstance(val, bool)):
            problems.append(f"{art_type}: evidence '{key}' must be {r['check']}")
            continue
        if "min_items" in r and len(val) < r["min_items"]:
            problems.append(f"{art_type}: '{key}' needs at least {r['min_items']} items")
        if "equals" in r and val != r["equals"]:
            problems.append(f"{art_type}: '{key}' is {val!r}, expected {r['equals']!r}")
        if "min" in r and val < r["min"] or "max" in r and val > r["max"]:
            problems.append(f"{art_type}: '{key}'={val} outside [{r.get('min')}, {r.get('max')}]")
        if "special" in r and not problems:
            fn = checks.get(r["special"])
            p = fn(key, val, evidence, workspace, known_ids) if fn else f"{art_type}: no check registered for special '{r['special']}'"
            if p:
                problems.append(p)
    return problems


def verify_task(rules: dict, task: dict, artifacts: list[dict], result: dict, workspace: Path | None,
                known_ids: set = frozenset(), checks: dict | None = None) -> tuple[str, list[str]]:
    """-> ('PASS'|'FAIL', notes)."""
    notes = []
    if not artifacts:
        notes.append("no artifacts: a summary is not evidence")
    present = {a["type"] for a in artifacts}
    notes += [f"required artifact '{t}' missing" for t in task.get("required_artifacts", []) if t not in present]
    for a in artifacts:
        notes += check_evidence(rules, a["type"], a["evidence"], workspace, known_ids, checks)
    opts = task.get("decision_options")
    if opts:
        sel = (result.get("decision") or {}).get("selected_option")
        if sel not in opts:
            notes.append(f"decision must select one of {opts}, got {sel!r}")
    return ("FAIL" if notes else "PASS"), notes
