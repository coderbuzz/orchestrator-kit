"""Generators: a new company repo, hiring a role from the kit's role library into a company, a new project, the org chart.

The role library (orchestrator_kit/library) is itself a valid pack. Hiring COPIES a role and what it needs (skills, policies,
capabilities, model policies, harness descriptors, evidence types) into the company repo: the company owns its definitions
and reviews them as a change; a newer kit never changes a company's agents silently.
"""
import copy
import importlib.resources
import json
import re
import shutil
from pathlib import Path

import yaml

from . import validate as v
from .registry import Registry
from .runtime import Refused, write_project

LIB = Path(str(importlib.resources.files(__package__) / "library"))
BASE = ["agents/shared/preamble.md", "company/mission/mission.md", "policies", "tools", "model-routing", "protocols",
        "config/policies", "config/models"]


def dump(d) -> str:
    return yaml.safe_dump(d, sort_keys=False, allow_unicode=True, width=1000, default_flow_style=None)


class Changes:
    """Files written by one operation, restorable if the result does not validate."""

    def __init__(self, root: Path):
        self.root, self.before = root, {}

    def write(self, rel: str, text: str):
        p = self.root / rel
        self.before.setdefault(rel, p.read_text() if p.exists() else None)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def copy(self, src_root: Path, rel: str):
        self.write(rel, (src_root / rel).read_text())

    def rollback(self):
        for rel, text in self.before.items():
            p = self.root / rel
            p.unlink() if text is None else p.write_text(text)

    def check(self):
        if errs := v.check(self.root):
            self.rollback()
            raise Refused("the result does not validate, nothing was changed:\n" + "\n".join(errs[:10]))
        return sorted(self.before)


def _files(reg: Registry, kind: str) -> dict[str, Path]:
    """id -> definition file of one kind (skills/policies: every version)."""
    out = {}
    for p, d in v.discover(reg.root).get(kind, []):
        items = d["capabilities"] if kind == ".capabilities.yaml" else [d]
        for item in items:
            out.setdefault(item["id"], p)
    return out


def roles() -> list[dict]:
    return [{"id": a["id"], "name": a["name"], "department": a["department"], "type": a["type"], "role": a["role"]}
            for a in sorted(Registry(LIB).agents.values(), key=lambda a: (a["department"], a["id"]))]


def hire(root: Path, role: str, as_id: str | None = None, reports_to: str | None = None) -> list[str]:
    """Copy library role `role` (as `as_id`) and everything it needs into the company at `root`; validate or roll back."""
    root, lib, co = Path(root), Registry(LIB), Registry(root)
    if role not in lib.agents:
        raise Refused(f"no role '{role}' in the library (see `orchestrator-kit roles`)")
    a, aid = copy.deepcopy(lib.agents[role]), as_id or role
    if aid in co.agents:
        raise Refused(f"the company already has an agent '{aid}' (use --as to hire a second one under another id)")
    orchestrators = [i for i, d in co.agents.items() if d["type"] == "orchestrator"]
    if a["type"] == "orchestrator" and orchestrators:
        raise Refused(f"the company already has an orchestrator: {orchestrators}")
    if a["department"] not in co.pack.vocabulary.get("departments", [a["department"]]):
        raise Refused(f"department '{a['department']}' is not in pack.yaml vocabulary.departments; add it first (a company decision)")
    human = co.pack.human_id
    boss = reports_to or (a["authority"]["reports_to"] if a["authority"]["reports_to"] in co.agents else (orchestrators or [human])[0])
    if boss != human and boss not in co.agents:
        raise Refused(f"reports_to '{boss}' is not an agent of the company")
    ch = Changes(root)
    # a skill the company already has resolves to the company's version: the agent must hold what that version requires
    lib_skill_files, co_skill_files = _files(lib, ".skill.yaml"), _files(co, ".skill.yaml")
    skills = [co.skill(s) if s["id"] in co_skill_files else lib.agent_skill(role, s["id"]) for s in a["skills"]]
    a["tools"] = [*a["tools"], *sorted({c for s in skills for c in s["required_capabilities"]} - set(a["tools"]))]

    # the agent and its prompt
    tier = Path(lib.agents[role]["prompt_file"]).parent
    a.update(id=aid, prompt_file=str(tier / f"{aid}.prompt.md"))
    a["authority"]["reports_to"] = boss
    a["authority"]["delegates_to"] = [x for x in a["authority"]["delegates_to"] if x in co.agents]
    ch.write(str(tier / f"{aid}.agent.yaml"), dump(a))
    ch.write(a["prompt_file"], (LIB / lib.agents[role]["prompt_file"]).read_text())

    # what it needs, copied only when the company lacks it (the company's own version wins)
    for s in skills:
        if s["id"] not in co_skill_files:
            ch.copy(LIB, str(lib_skill_files[s["id"]].relative_to(LIB)))
    lib_pol, co_pol = _files(lib, ".policy.yaml"), _files(co, ".policy.yaml")
    for ref in a["policies"]:
        if ref["id"] not in co_pol:
            ch.copy(LIB, str(lib_pol[ref["id"]].relative_to(LIB)))
    lib_mp, co_mp = _files(lib, ".model-policy.yaml"), _files(co, ".model-policy.yaml")
    lib_h, co_h = _files(lib, ".harness.yaml"), _files(co, ".harness.yaml")
    for mp in {a["model_policy"], *(s.get("model_policy") for s in skills)} - {None}:
        if mp not in co_mp:
            ch.copy(LIB, str(lib_mp[mp].relative_to(LIB)))
            for h in {c["harness"] for c in lib.model_policies[mp]["candidates"]} - set(co_h):
                ch.copy(LIB, str(lib_h[h].relative_to(LIB)))
    caps = {*a["tools"], *(c for s in skills for c in s["required_capabilities"])} - set(co.capabilities)
    if caps:
        rel = "tools/capabilities/hired.capabilities.yaml"
        doc = yaml.safe_load((root / rel).read_text()) if (root / rel).exists() else {"version": "1.0.0", "capabilities": []}
        doc["capabilities"] += [lib.capabilities[c] for c in sorted(caps)]
        ch.write(rel, "# Capabilities added by `orchestrator-kit hire` (copied from the kit's library).\n" + dump(doc))
    types = {*a["outputs"], *(o for s in skills for o in s["outputs"])} - set(co.evidence_rules["types"])
    if types:
        rel = "protocols/artifacts/evidence-requirements.yaml"
        text = (root / rel).read_text()
        lines = text.splitlines()
        at = next((i for i, ln in enumerate(lines) if ln.rstrip() == "types:"), None)
        if at is None or any(ln and not ln.startswith((" ", "#")) for ln in lines[at + 1:]):
            raise Refused(f"{rel}: add evidence rules for {sorted(types)} by hand (types: is not the last block)")
        ch.write(rel, text.rstrip("\n") + "\n" + "".join(f"  {t}: {json.dumps(lib.evidence_rules['types'][t])}\n" for t in sorted(types)))

    # the org chart and the catalog
    if boss != human:
        bf = next(p for p, d in v.discover(root).get(".agent.yaml", []) if d["id"] == boss)
        text, rel = bf.read_text(), str(bf.relative_to(root))
        m = re.search(r"^(\s*delegates_to:\s*)\[(.*?)\]", text, re.M)
        if m:
            items = [x.strip() for x in m.group(2).split(",") if x.strip()] + [aid]
            ch.write(rel, text[:m.start()] + f"{m.group(1)}[{', '.join(items)}]" + text[m.end():])
        else:
            d = yaml.safe_load(text)
            d["authority"]["delegates_to"].append(aid)
            ch.write(rel, dump(d))
    _enable(ch, root, aid)
    return ch.check()


def _enable(ch: Changes, root: Path, aid: str):
    rel = "config/agents/example.yaml"
    text = (root / rel).read_text() if (root / rel).exists() else "enabled: []\n"
    head = "".join(ln + "\n" for ln in text.splitlines() if ln.startswith("#"))
    d = yaml.safe_load(text) or {}
    d["enabled"] = [*(d.get("enabled") or []), aid]
    ch.write(rel, head + dump(d))


def new_company(dest: Path, name: str, title: str, human: str = "board", mission: str | None = None, orchestrator: str = "ceo") -> list[str]:
    """A company repo from the library base plus its orchestrator, with its plugin generated."""
    dest = Path(dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        raise Refused(f"{dest} is not empty")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name) or not re.fullmatch(r"[a-z][a-z0-9-]*", human):
        raise Refused("name and human must be kebab-case ids")
    lib = Registry(LIB)
    for rel in BASE:
        src = LIB / rel
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest / rel) if src.is_dir() else shutil.copy(src, dest / rel)
    prefix = human.replace("-", "") + "_"
    pack = {"name": name, "server": "company", "project_file": "company.yaml", "state_dir": ".company",
            "human": {"id": human, "tool_prefix": prefix}, "preamble": "agents/shared/preamble.md", "def_keys": list(lib.pack.def_keys),
            "plugin": {"version": "0.1.0", "author": title, "description": f"{title}: talk to the orchestrator, who runs workspace projects with the company's agents."},
            "mcp": {"command": "orchestrator-kit", "args": ["mcp", str(dest)], "instructions": f"{title} company tools. The orchestrator skill describes how to use them."},
            "hook": {"command": f"orchestrator-kit hook .company {prefix}", "description": f"{prefix}* tools always ask the human; project state is off-limits to the shell."},
            "vocabulary": lib.pack.vocabulary}
    (dest / "pack.yaml").write_text("# Company manifest (orchestrator-kit). mcp.args holds this repo's path: run `orchestrator-kit plugin` after moving it.\n" + dump(pack))
    if mission:
        (dest / "company/mission/mission.md").write_text(f"# Mission\n\n{mission.strip()}\n")
    (dest / "config/agents").mkdir(parents=True, exist_ok=True)
    (dest / "config/agents/example.yaml").write_text("# Agents of the company (its catalog). Projects hire their team from these (project roster).\nenabled: []\n")
    (dest / ".gitignore").write_text("state/\n.company/\nconfig/**/local.*\n.env*\n__pycache__/\n")
    (dest / ".claude-plugin").mkdir()
    (dest / ".claude-plugin/marketplace.json").write_text(json.dumps(
        {"name": name, "description": f"{title} company plugin", "owner": {"name": title},
         "plugins": [{"name": name, "description": pack["plugin"]["description"], "source": "./plugin"}]}, indent=2) + "\n")
    (dest / "README.md").write_text(
        f"# {title}\n\nA company on orchestrator-kit. Agents, skills and policies here are the company's catalog; projects hire their team "
        "from it.\n\n```\norchestrator-kit roles                         # the role library\norchestrator-kit hire <role> --pack .           # add a role\n"
        "orchestrator-kit validate --pack .\norchestrator-kit plugin --pack .               # regenerate plugin/\n"
        "orchestrator-kit new-project ~/workspace/<p> --pack . --budget 5 --goal \"...\" --roster a,b\n"
        "claude plugin marketplace add <this repo> && claude plugin install " + f"{name}@{name}\n```\n")
    hire(dest, orchestrator, reports_to=human)
    write_plugin(dest)
    return sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())


def write_plugin(root: Path, check: bool = False) -> list[str]:
    """Generate plugin/ with the default generator (a pack with its own generator runs its own CLI). Returns drift when check."""
    from .plugin import drift, existing, generate
    reg = Registry(root)
    files, out = generate(reg), Path(root) / "plugin"
    if check:
        return drift(files, out)
    for f in existing(out) - files.keys():
        (out / f).unlink()
    for f, text in files.items():
        (out / f).parent.mkdir(parents=True, exist_ok=True)
        (out / f).write_text(text)
    return []


def new_project(project_dir: Path, root: Path, title: str, budget: float, goal: str | None = None, roster: list[str] | None = None) -> dict:
    created = not Path(project_dir).exists()
    Path(project_dir).mkdir(parents=True, exist_ok=True)
    cfg = {"name": title, **({"goal": goal} if goal else {}), **({"roster": roster} if roster else {}), "daily_budget_usd": budget}
    try:
        return write_project(project_dir, root, cfg, new=True)
    except Refused:
        if created:
            Path(project_dir).rmdir()
        raise


def org(root: Path) -> str:
    """The org chart from reports_to, as an indented tree under the human."""
    reg = Registry(root)
    kids: dict[str, list[str]] = {}
    for i, a in sorted(reg.agents.items()):
        kids.setdefault(a["authority"]["reports_to"], []).append(i)
    lines = []

    def walk(node: str, depth: int):
        for k in kids.get(node, []):
            a = reg.agents[k]
            lines.append(f"{'  ' * depth}- {k} ({a['name']}, {a['department']}){'' if k in reg.enabled_agents else ' [not enabled]'}")
            walk(k, depth + 1)
    walk(reg.pack.human_id, 0)
    return f"{reg.pack.human_id} (human)\n" + "\n".join(lines)
