"""Validate every definition in the repo against its schema, then check cross-references.

Usage: python -m companyos.validate     (exit 1 on any error)
Kind is inferred from the file suffix, so adding a definition needs no registry edit.
"""
import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".git", ".venv", "tests", "state", "__pycache__"}

# suffix -> schema path (relative to ROOT)
KINDS = {
    ".agent.yaml": "agents/agent.schema.json",
    ".skill.yaml": "skills/skill.schema.json",
    ".policy.yaml": "policies/policy.schema.json",
    ".workflow.yaml": "workflows/workflow.schema.json",
    ".capabilities.yaml": "tools/capabilities/capability.schema.json",
    ".model-policy.yaml": "model-routing/model-policy.schema.json",
    ".harness.yaml": "model-routing/providers/harness.schema.json",
}
# protocol instances (used by tests and runtime), keyed by name
PROTOCOLS = {
    "task": "protocols/tasks/task.schema.json",
    "event": "protocols/events/event.schema.json",
    "decision": "protocols/decisions/decision.schema.json",
    "artifact": "protocols/artifacts/artifact.schema.json",
    "approval": "protocols/approvals/approval.schema.json",
    "company-state": "company/state.schema.json",
}
CONFIG_DIRS = {"company": "company", "models": "models", "agents": "agents", "policies": "policies", "environments": "environment"}


def load(path: Path):
    text = path.read_text()
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def schema_validator(rel: str, fragment: str = "") -> Draft202012Validator:
    schema = load(ROOT / rel)
    if fragment:  # config.schema.json#/$defs/<name>
        schema = {"$ref": f"#/$defs/{fragment}", "$defs": schema["$defs"]}
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def errors_for(validator: Draft202012Validator, data) -> list[str]:
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in validator.iter_errors(data)]


def validate_instance(kind: str, data) -> list[str]:
    """Validate a protocol instance (task, event, ...) by kind name."""
    return errors_for(schema_validator(PROTOCOLS[kind]), data)


def _files():
    for p in sorted(ROOT.rglob("*")):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP):
            yield p


def discover() -> dict[str, list[tuple[Path, object]]]:
    """Return {kind: [(path, parsed)]} for all definitions and config files."""
    found: dict[str, list] = {}
    for p in _files():
        rel = p.relative_to(ROOT)
        name = p.name
        kind = next((k for k in KINDS if name.endswith(k)), None)
        if kind is None and rel.parts[0] == "config" and len(rel.parts) == 3 and p.suffix == ".yaml":
            kind = f"config:{rel.parts[1]}"
        if kind:
            found.setdefault(kind, []).append((p, load(p)))
    return found


def check() -> list[str]:
    errs: list[str] = []
    found = discover()
    for kind, items in found.items():
        if kind.startswith("config:"):
            v = schema_validator("config/config.schema.json", CONFIG_DIRS[kind.split(":")[1]])
        else:
            v = schema_validator(KINDS[kind])
        for path, data in items:
            errs += [f"{path.relative_to(ROOT)}: {e}" for e in errors_for(v, data)]
    if errs:
        return errs  # cross-refs are meaningless on malformed files

    def ids(kind):
        return {d["id"] for _, d in found.get(kind, [])}

    agents, skills, policies = ids(".agent.yaml"), ids(".skill.yaml"), ids(".policy.yaml")
    mpols, harnesses, workflows = ids(".model-policy.yaml"), ids(".harness.yaml"), ids(".workflow.yaml")
    caps = {c["id"] for _, d in found.get(".capabilities.yaml", []) for c in d["capabilities"]}

    for kind in (".agent.yaml", ".skill.yaml", ".policy.yaml", ".workflow.yaml", ".model-policy.yaml", ".harness.yaml"):
        seen: set = set()
        for path, d in found.get(kind, []):
            if d["id"] in seen:
                errs.append(f"{path.relative_to(ROOT)}: duplicate {kind} id '{d['id']}'")
            seen.add(d["id"])
    cap_ids = [c["id"] for _, d in found.get(".capabilities.yaml", []) for c in d["capabilities"]]
    errs += [f"duplicate capability id '{c}'" for c in set(cap_ids) if cap_ids.count(c) > 1]

    def need(path, what, vals, known):
        errs.extend(f"{path.relative_to(ROOT)}: unknown {what} '{v}'" for v in vals if v not in known)

    for path, a in found.get(".agent.yaml", []):
        need(path, "skill", [s["id"] for s in a["skills"]], skills)
        need(path, "tool/capability", a["tools"], caps)
        need(path, "policy", [p["id"] for p in a["policies"]], policies)
        need(path, "model_policy", [a["model_policy"]], mpols)
        need(path, "delegate", a["authority"]["delegates_to"], agents)
        if a["authority"]["reports_to"] != "board":
            need(path, "reports_to", [a["authority"]["reports_to"]], agents)
        if "prompt_file" in a and not (ROOT / a["prompt_file"]).is_file():
            errs.append(f"{path.relative_to(ROOT)}: prompt_file '{a['prompt_file']}' not found")
    for path, s in found.get(".skill.yaml", []):
        need(path, "capability", s["required_capabilities"], caps)
    for path, p in found.get(".policy.yaml", []):
        if path.parent.name != p["domain"]:
            errs.append(f"{path.relative_to(ROOT)}: domain '{p['domain']}' != directory '{path.parent.name}'")
    for path, m in found.get(".model-policy.yaml", []):
        need(path, "harness", [c["harness"] for c in m["candidates"]], harnesses)
    for path, w in found.get(".workflow.yaml", []):
        need(path, "agent", w["agents"] + [s["agent"] for s in w["steps"]], agents)
        step_ids = {s["id"] for s in w["steps"]}
        need(path, "step dependency", [d for s in w["steps"] for d in s.get("depends_on", [])], step_ids)
        need(path, "skill", [s["skill"] for s in w["steps"] if "skill" in s], skills)
    for path, c in found.get("config:agents", []):
        need(path, "agent", c["enabled"], agents)
    for path, c in found.get("config:policies", []):
        need(path, "policy", c["enabled"], policies)
    for path, c in found.get("config:models", []):
        need(path, "harness", [h["id"] for h in c["harnesses"]], harnesses)
        need(path, "model policy", [c["default_model_policy"]], mpols)
    return errs


if __name__ == "__main__":
    problems = check()
    print("\n".join(problems) if problems else "OK: all definitions valid")
    sys.exit(1 if problems else 0)
