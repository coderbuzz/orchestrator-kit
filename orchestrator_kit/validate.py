"""Validate every definition in the repo against its schema, then check cross-references.

Usage: python -m orchestrator_kit.validate [pack root]     (exit 1 on any error)
Kind is inferred from the file suffix, so adding a definition needs no registry edit.
Core schemas ship with the kit (package data, schemas/); a pack keeps its own (e.g. its state schema) at the same relative
path in its root, and a pack file there takes precedence.
"""
import importlib.resources
import json
import os
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .pack import Pack

# Default pack root when a caller passes none: $ORCHESTRATOR_KIT_PACK, else the current directory. Every function below takes an
# explicit `root`; a pack's own entry points (e.g. companyos) always pass theirs.
ROOT = Path(os.environ.get("ORCHESTRATOR_KIT_PACK") or Path.cwd())
KIT_SCHEMAS = importlib.resources.files(__package__) / "schemas"
SECTION_RE = re.compile(r"^(company|department)/[a-z_]+$")  # memory scopes: company/<state section> or department/<department>
SKIP = {".git", ".venv", "tests", "state", "__pycache__", "archive", "plugin", ".claude-plugin", "orchestrator_kit", "examples"}

# suffix -> schema path (relative to the pack root or the kit schemas, see schema_file)
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


def schema_file(rel: str, root: Path | None = None) -> Path:
    """A contract file (schema, task lifecycle): the pack's copy if it has one, else the kit's."""
    own = Path(root or ROOT) / rel
    return own if own.exists() else Path(str(KIT_SCHEMAS / rel))


AI_SCHEMA = "protocols/ai/ai.schema.json"  # shared; other schemas reference it as "../ai/1"


def schema_validator(rel: str, fragment: str = "", root: Path | None = None) -> Draft202012Validator:
    schema = load(schema_file(rel, root))
    if fragment:  # config.schema.json#/$defs/<name>
        schema = {"$id": schema["$id"], "$ref": f"#/$defs/{fragment}", "$defs": schema["$defs"]}
    return validator_for(schema, root)


def validator_for(schema: dict, root: Path | None = None) -> Draft202012Validator:
    ai = load(schema_file(AI_SCHEMA, root))
    registry = Registry().with_resource(ai["$id"], Resource.from_contents(ai))
    return Draft202012Validator(schema, registry=registry, format_checker=Draft202012Validator.FORMAT_CHECKER)


def errors_for(validator: Draft202012Validator, data) -> list[str]:
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in validator.iter_errors(data)]


def validate_instance(kind: str, data, root: Path | None = None) -> list[str]:
    """Validate a protocol instance (task, approval, ...) by kind name."""
    return errors_for(schema_validator(PROTOCOLS[kind], root=root), data)


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


def check(root: Path | None = None) -> list[str]:
    root = Path(root or ROOT)
    errs: list[str] = []
    found = discover(root)
    for kind, items in found.items():
        if kind.startswith("config:"):
            v = schema_validator("config/config.schema.json", CONFIG_DIRS[kind.split(":")[1]], root)
        else:
            v = schema_validator(KINDS[kind], root=root)
        for path, data in items:
            errs += [f"{path.relative_to(root)}: {e}" for e in errors_for(v, data)]
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
                errs.append(f"{path.relative_to(root)}: duplicate {kind} id '{d['id']}'")
            seen.add(key)
    cap_ids = [c["id"] for _, d in found.get(".capabilities.yaml", []) for c in d["capabilities"]]
    errs += [f"duplicate capability id '{c}'" for c in set(cap_ids) if cap_ids.count(c) > 1]

    def need(path, what, vals, known):
        errs.extend(f"{path.relative_to(root)}: unknown {what} '{v}'" for v in vals if v not in known)

    pack = Pack.load(root)
    human, vocab = pack.human_id, pack.vocabulary
    for path, a in found.get(".agent.yaml", []):
        if "departments" in vocab:
            need(path, "department", [a["department"]], vocab["departments"])
        need(path, "skill", [s["id"] for s in a["skills"]], skills)
        need(path, "tool/capability", a["tools"], caps)
        need(path, "policy", [p["id"] for p in a["policies"]], policies)
        need(path, "model_policy", [a["model_policy"]], mpols)
        need(path, "delegate", a["authority"]["delegates_to"], agents)
        if a["authority"]["reports_to"] != human:
            need(path, "reports_to", [a["authority"]["reports_to"]], agents)
        if "prompt_file" in a and not (root / a["prompt_file"]).is_file():
            errs.append(f"{path.relative_to(root)}: prompt_file '{a['prompt_file']}' not found")
    for path, s in found.get(".skill.yaml", []):
        need(path, "capability", s["required_capabilities"], caps)
    for path, p in found.get(".policy.yaml", []):
        if "policy_domains" in vocab:
            need(path, "policy domain", [p["domain"]], vocab["policy_domains"])
        if path.parent.name != p["domain"]:
            errs.append(f"{path.relative_to(root)}: domain '{p['domain']}' != directory '{path.parent.name}'")
    for path, m in found.get(".model-policy.yaml", []):
        need(path, "harness", [c["harness"] for c in m["candidates"]], harnesses)
    for path, c in found.get("config:agents", []):
        need(path, "agent", c["enabled"], agents)
    for path, c in found.get("config:policies", []):
        need(path, "policy", c["enabled"], policies)
    for path, c in found.get("config:models", []):
        need(path, "harness", [h["id"] for h in c["harnesses"]], harnesses)
        need(path, "model policy", [c["default_model_policy"]], mpols)
    errs += _semantic_checks(found, root)
    return errs


def _semantic_checks(found, root: Path) -> list[str]:
    """Checks that need resolved references: versions, capability coverage, memory scopes, AI selections."""
    from .registry import Registry
    errs: list[str] = []
    try:
        reg = Registry(root)
    except Exception as e:  # noqa: BLE001
        return [f"registry: {e}"]
    rel = lambda p: p.relative_to(root)  # noqa: E731
    state = schema_file("company/state.schema.json", root)  # optional: a pack without shared state allows no company/<section> scope
    sections = set(load(state)["properties"]) if state.exists() else set()
    depts = set(reg.pack.vocabulary.get("departments") or [a["department"] for a in reg.agents.values()])
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
    native = reg.mapping.get("native") or {}
    claude_models = set((reg.harnesses.get("claude-code") or {}).get("models") or [])
    for kind, table in (("agents", native.get("agents") or {}), ("skills", native.get("skills") or {})):
        for k, m in table.items():
            if m not in claude_models:
                errs_extra.append(f"mapping native.{kind}.{k}: '{m}' is not a claude-code model {sorted(claude_models)}")
            if kind == "agents" and k not in reg.agents:
                errs_extra.append(f"mapping native.agents.{k}: unknown agent")
    if native.get("light_tasks") and native["light_tasks"] not in claude_models:
        errs_extra.append(f"mapping native.light_tasks: '{native['light_tasks']}' is not a claude-code model")
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


def main(argv: list[str]) -> int:
    problems = check(argv[0] if argv else None)
    print("\n".join(problems) if problems else "OK: all definitions valid")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
