"""Validate every definition in the repo against its schema, then check cross-references.

Usage: python -m companyos.validate     (exit 1 on any error)
Kind is inferred from the file suffix, so adding a definition needs no registry edit.
"""
import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".git", ".venv", "tests", "state", "__pycache__", "archive", "plugin", ".claude-plugin"}

# suffix -> schema path (relative to ROOT)
KINDS = {
    ".agent.yaml": "agents/agent.schema.json",
    ".skill.yaml": "skills/skill.schema.json",
    ".policy.yaml": "policies/policy.schema.json",
    ".capabilities.yaml": "tools/capabilities/capability.schema.json",
    ".model-policy.yaml": "model-routing/model-policy.schema.json",
    ".harness.yaml": "model-routing/providers/harness.schema.json",
    "agent-mapping.yaml": "model-routing/mappings/agent-mapping.schema.json",
}
# protocol instances (used by tests and runtime), keyed by name
PROTOCOLS = {
    "task": "protocols/tasks/task.schema.json",
    "decision": "protocols/decisions/decision.schema.json",
    "artifact": "protocols/artifacts/artifact.schema.json",
    "approval": "protocols/approvals/approval.schema.json",
    "company-state": "company/state.schema.json",
    "agent-result": "protocols/results/agent-result.schema.json",
    "board-report": "protocols/reports/board-report.schema.json",
}
CONFIG_DIRS = {"company": "company", "models": "models", "agents": "agents", "policies": "policies", "environments": "environment"}


def load(path: Path):
    text = path.read_text()
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


AI_SCHEMA = "protocols/ai/ai.schema.json"  # shared; other schemas reference it as "../ai/1"


def schema_validator(rel: str, fragment: str = "") -> Draft202012Validator:
    schema = load(ROOT / rel)
    if fragment:  # config.schema.json#/$defs/<name>
        schema = {"$id": schema["$id"], "$ref": f"#/$defs/{fragment}", "$defs": schema["$defs"]}
    return validator_for(schema)


def validator_for(schema: dict) -> Draft202012Validator:
    ai = load(ROOT / AI_SCHEMA)
    registry = Registry().with_resource(ai["$id"], Resource.from_contents(ai))
    return Draft202012Validator(schema, registry=registry, format_checker=Draft202012Validator.FORMAT_CHECKER)


def errors_for(validator: Draft202012Validator, data) -> list[str]:
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in validator.iter_errors(data)]


def validate_instance(kind: str, data) -> list[str]:
    """Validate a protocol instance (task, approval, ...) by kind name."""
    return errors_for(schema_validator(PROTOCOLS[kind]), data)


def _files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and not (set(p.relative_to(root).parts) & SKIP):
            yield p


def discover(root: Path | None = None) -> dict[str, list[tuple[Path, object]]]:
    """Return {kind: [(path, parsed)]} for all definitions and config files."""
    root = root or ROOT
    found: dict[str, list] = {}
    for p in _files(root):
        rel = p.relative_to(root)
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
    mpols, harnesses = ids(".model-policy.yaml"), ids(".harness.yaml")
    caps = {c["id"] for _, d in found.get(".capabilities.yaml", []) for c in d["capabilities"]}

    for kind in (".agent.yaml", ".skill.yaml", ".policy.yaml", ".model-policy.yaml", ".harness.yaml"):
        seen: set = set()
        for path, d in found.get(kind, []):
            key = (d["id"], d["version"]) if kind in (".skill.yaml", ".policy.yaml") else d["id"]  # skills/policies may keep old versions
            if key in seen:
                errs.append(f"{path.relative_to(ROOT)}: duplicate {kind} id '{d['id']}'")
            seen.add(key)
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
    for path, c in found.get("config:agents", []):
        need(path, "agent", c["enabled"], agents)
    for path, c in found.get("config:policies", []):
        need(path, "policy", c["enabled"], policies)
    for path, c in found.get("config:models", []):
        need(path, "harness", [h["id"] for h in c["harnesses"]], harnesses)
        need(path, "model policy", [c["default_model_policy"]], mpols)
    errs += _semantic_checks(found)
    return errs


def _semantic_checks(found) -> list[str]:
    """Checks that need resolved references: versions, capability coverage, memory scopes, AI selections."""
    from .registry import Registry, SECTION_RE
    errs: list[str] = []
    try:
        reg = Registry(ROOT)
    except Exception as e:  # noqa: BLE001
        return [f"registry: {e}"]
    rel = lambda p: p.relative_to(ROOT)  # noqa: E731
    sections = set(load(ROOT / "company/state.schema.json")["properties"])
    depts = set(load(ROOT / "agents/agent.schema.json")["properties"]["department"]["enum"])
    art_types = set(reg.evidence_rules["types"])
    for path, a in found.get(".agent.yaml", []):
        for ref in a["skills"]:
            try:
                s = reg.skill(ref)
            except KeyError as e:
                errs.append(f"{rel(path)}: {e}")
                continue
            missing = [c for c in s["required_capabilities"] if c not in a["tools"]]
            if missing:
                errs.append(f"{rel(path)}: skill {s['id']} requires capabilities {missing} the agent lacks")
        for ref in a["policies"]:
            try:
                reg.policy(ref)
            except KeyError as e:
                errs.append(f"{rel(path)}: {e}")
        for scope in a["memory_scope"]["reads"] + a["memory_scope"]["writes"]:
            kind, _, name = scope.partition("/")
            ok = scope == "company/context" or (SECTION_RE.match(scope) and (name in sections if kind == "company" else name in depts))
            if not ok:
                errs.append(f"{rel(path)}: unknown memory scope '{scope}'")
        errs += [f"{rel(path)}: unknown output artifact type '{o}'" for o in a["outputs"] if o not in art_types]
    for path, s in found.get(".skill.yaml", []):
        if s.get("model_policy") and s["model_policy"] not in reg.model_policies:
            errs.append(f"{rel(path)}: unknown model_policy '{s['model_policy']}'")
        errs += [f"{rel(path)}: unknown output artifact type '{o}'" for o in s["outputs"] if o not in art_types]
    for path, h in found.get(".harness.yaml", []):
        errs += [f"{rel(path)}: capability_tools names unknown capability '{c}'" for c in (h.get("capability_tools") or {}) if c not in reg.capabilities]
    errs += _ai_checks(reg) + _candidate_checks(reg)
    return errs


def _candidate_checks(reg) -> list[str]:
    """Model policy candidates must name a provider and model their harness really offers (zh: `zh providers`)."""
    errs = []
    for pid, m in reg.model_policies.items():
        for c in m["candidates"]:
            h = reg.harnesses.get(c["harness"])
            if h is None:
                continue  # unknown harness is reported elsewhere
            if c.get("provider") and c["provider"] not in {p["id"] for p in h["providers"]}:
                errs.append(f"model policy {pid}: harness {h['id']} does not offer provider '{c['provider']}'")
            if h.get("models") and c["model"] not in h["models"]:
                errs.append(f"model policy {pid}: harness {h['id']} has no verified model '{c['model']}'")
            if h["invocation"].get("model_args") and any("{provider}" in a for a in h["invocation"]["model_args"]) \
                    and c["model"] != "configured" and not c.get("provider"):
                errs.append(f"model policy {pid}: {h['id']} candidate '{c['model']}' needs a provider")
    return errs


def _ai_checks(reg) -> list[str]:
    """Every AI selection in definitions and config must be one its harness can honour (protocols/ai)."""
    from .harness import unsupported
    errs_extra: list[str] = []
    sels = [(f"agent {a}", d.get("ai")) for a, d in reg.agents.items()]
    sels += [(f"skill {s['id']}@{s['version']}", s.get("ai")) for lst in reg._skills.values() for s in lst]
    sels += [("config models.default_ai", reg.config["models"].get("default_ai"))]
    for e in reg.mapping.get("entries", []):
        who = f"mapping {e['agent']}" + (f"/{e['skill']}" if e.get("skill") else "")
        sels += [(who, e["ai"])] + [(f"{who} fallback", f["ai"]) for f in e["fallbacks"]]
        if e["agent"] not in reg.agents:
            errs_extra.append(f"{who}: unknown agent")
        elif e.get("skill") and not any(s["id"] == e["skill"] for s in reg.agents[e["agent"]]["skills"]):
            errs_extra.append(f"{who}: agent does not hold skill '{e['skill']}'")
    sels += [(f"config agents.overrides.{a}", (o or {}).get("ai")) for a, o in (reg.config["agents"].get("overrides") or {}).items()]
    errs = errs_extra
    for where, ai in sels:
        if not ai:
            continue
        if ai.get("harness") is None:
            if ai.get("model") or ai.get("provider"):
                errs.append(f"{where}: ai.model/provider need ai.harness")
            continue
        hdef = reg.harnesses.get(ai["harness"])
        why = f"unknown harness '{ai['harness']}'" if hdef is None else unsupported(hdef, ai.get("model"), ai.get("provider"), ai.get("effort"))
        if why:
            errs.append(f"{where}: {why}")
    return errs


if __name__ == "__main__":
    problems = check()
    print("\n".join(problems) if problems else "OK: all definitions valid")
    sys.exit(1 if problems else 0)
