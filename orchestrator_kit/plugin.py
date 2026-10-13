"""Generate a Claude Code plugin (plugin/) from a pack's definitions: one subagent per agent with its skills inside, the
orchestrator skill (written by the pack), the tools MCP server and the guard hook. zh copies go to plugin/zh/.

A pack supplies what is its own through `Generator`: the subagent naming, the orchestrator's lead and description, the
orchestrator skill, the README. Its CLI calls `main(argv, generator)`.
"""
import json
from pathlib import Path

import yaml

from .registry import Registry
from .validate import ROOT

OUT = ROOT / "plugin"  # default for drift(); main() uses <registry root>/plugin
dump = lambda o: yaml.safe_dump(o, sort_keys=False, allow_unicode=True)  # noqa: E731
q = json.dumps  # JSON strings are valid YAML double-quoted scalars (and parse in zh's loose frontmatter reader)


def md(front: list[str], body: str) -> str:
    return "---\n" + "\n".join(front) + "\n---\n\n" + body.rstrip() + "\n"


def skill_body(s: dict) -> str:
    sec = lambda title, items: f"## {title}\n" + "\n".join(items) + "\n"  # noqa: E731
    body = (f"# {s['id']} v{s['version']}\n\n## Purpose\n{' '.join(s['purpose'].split())}\n\n"
            + sec("Preconditions", [f"- {p}" for p in s["preconditions"]]) + "\n"
            + sec("Procedure", [f"{i}. {p}" for i, p in enumerate(s["procedure"], 1)]) + "\n"
            + sec("Validation", [f"- {p}" for p in s["validation"]]) + "\n"
            + sec("Failure modes", [f"- {f['mode']}: {f['handling']}" for f in s["failure_modes"]]) + "\n"
            + sec("Outputs", [f"- {o}" for o in s["outputs"]]) + "\n"
            + sec("Required capabilities", [f"- {c}" for c in s["required_capabilities"]]))
    return body


def agent_parts(reg: Registry, aid: str, gen: "Generator") -> dict:
    a = reg.agents[aid]
    ai = reg.ai_for(aid, None)
    cap_tools = reg.harnesses["claude-code"].get("capability_tools") or {}
    zh_tools = (reg.harnesses.get("zcode") or {}).get("capability_tools") or {}
    # Read-only baseline: an omitted `tools` key would inherit every tool (Bash, Write) in both harnesses.
    tools = lambda m: sorted({"Read", "Grep", "Glob"} | {t for c in a["tools"] for t in m.get(c, [])})  # noqa: E731
    cands = [c for c in reg.model_policies[a["model_policy"]]["candidates"] if c.get("harness") == "claude-code" and c.get("model")]
    claude_model = ai["model"] if ai.get("harness") == "claude-code" and ai.get("model") else (cands[0]["model"] if cands else None)
    zh = {}
    zc = next((c for c in reg.model_policies[a["model_policy"]]["candidates"]
               if c["harness"] == "zcode" and c.get("provider") and c["model"] != "configured"), None)
    if ai.get("harness") == "zcode" and ai.get("provider") and ai.get("model"):
        zh["model"] = f"{ai['provider']}/{ai['model']}"
    elif zc and ai.get("harness") in (None, "zcode"):  # zh's built-in model for this agent's policy (zh providers)
        zh["model"] = f"{zc['provider']}/{zc['model']}"
    if ai.get("effort") and ai.get("harness") in (None, "zcode"):
        zh["thoughtLevel"] = ai["effort"]
    skills = [(s["id"], reg.agent_skill(aid, s["id"])) for s in a["skills"]]
    pol = [f"- {p['id']}/{r['id']} ({r['enforcement']}): {r['statement']}" for ref in a["policies"] for p in [reg.policy(ref)] for r in p["rules"]]
    lead = [gen.orchestrator_lead] if a["type"] == "orchestrator" and gen.orchestrator_lead else []
    body = "\n\n".join(lead + [
        reg.preamble.strip(), (reg.root / a["prompt_file"]).read_text().strip(),
        "## Your definition\n```yaml\n" + dump({k: a[k] for k in reg.pack.def_keys if k in a}) + "```",
        "## Policies in force\n" + "\n".join(pol),
        "## Your skills\nFollow the procedure of the skill that matches your task.\n\n"
        + "\n\n".join("\n".join("##" + ln if ln.startswith("#") else ln for ln in skill_body(sk).splitlines()) for _, sk in skills)])
    desc = (gen.orchestrator_desc if a["type"] == "orchestrator" and gen.orchestrator_desc else " ".join(f"{a['role']} Mission: {a['mission']}".split()))
    return {"desc": desc, "body": body, "claude_tools": tools(cap_tools),
            "zh_tools": tools(zh_tools), "model": claude_model, "zh": zh, "skills": [i for i, _ in skills]}


def manifests(pack) -> tuple[dict, dict, dict]:
    """plugin.json, .mcp.json and hooks.json from the pack manifest (bump pack.yaml plugin.version whenever plugin/ changes)."""
    pj = {"name": pack.name, "version": pack.plugin["version"], "author": {"name": pack.plugin["author"]}, "description": pack.plugin["description"]}
    mcp = {pack.server: {"command": pack.mcp["command"], **({"args": pack.mcp["args"]} if pack.mcp.get("args") else {})}}
    hooks = {"description": pack.hook["description"],
             "hooks": {"PreToolUse": [{"matcher": f"{pack.tool_prefix}{pack.human_prefix}.*|Bash|Edit|Write|MultiEdit|NotebookEdit",
                                       "hooks": [{"type": "command", "command": pack.hook["command"]}]}]}}
    return pj, mcp, hooks


class Generator:
    """The pack's part of the plugin. Defaults: subagent = agent id, a generic orchestrator skill, a plain README."""
    orchestrator_lead = ""   # first paragraph of the orchestrator agent's subagent prompt
    orchestrator_desc = ""   # its subagent description (default: role + mission)
    readme = "# plugin/ (generated from the pack's definitions; never edit here)\n"

    def subagent_name(self, reg: Registry, aid: str) -> str:
        return aid

    def orchestrator_skill(self, reg: Registry, aid: str, subagents: dict) -> dict[str, str]:
        """Files (path -> text) of the skill the orchestrator session runs on; `subagents`: name -> {description, ...}."""
        from .mcp import core_tools
        a, p, h = reg.agents[aid], reg.pack, reg.pack.human_id
        tools = "\n".join(f"- `{t['name']}`: {t['description']}" for t in core_tools(p).values())
        pol = [f"- {x['id']}/{r['id']} ({r['enforcement']}): {r['statement']}" for ref in a["policies"] for x in [reg.policy(ref)] for r in x["rules"]]
        body = "\n\n".join([
            f"# {a['name']}\n\nThe human is the {h}. You are the {a['name']} for this conversation: you run the company's projects with "
            f"its agents (subagents) and the tools of the MCP server `{p.server}`. The tools refuse what policy forbids. Never work around "
            f"a refusal, never edit `{p.state_dir}/` files, never take a decision reserved to the {h}.",
            f"## Projects\nA project is a workspace folder with a `{p.project_file}` (name, goal, daily budget, roster). Call `projects` first; "
            f"if it is unclear which project the {h} means, ask once with your best guess. The company's agents are a catalog; each project "
            f"hires its team from it (roster) and tasks go only to agents on the roster. Propose who to hire or release and why; the {h} "
            f"decides (`{p.human_prefix}staff`). A role the company lacks is added to the company repository first "
            "(`orchestrator-kit hire <role>`, a reviewed change), never invented in a task.",
            f"## Doing work\nEach task: `task_add` -> `task_start` -> the subagent it names (scoped `{p.name}:<name>`) does it with the "
            "returned prompt -> `task_submit` with its JSON. Use `dispatch` when the task needs another harness or a model the subagent "
            f"cannot take. Approvals: `approvals`; the {h}'s own decisions go through `{p.human_prefix}decide` only after the {h} decided.",
            "## Tools\n" + tools,
            "## Your team (subagents)\n" + "\n".join(f"- {k}: {v['description']}" for k, v in sorted(subagents.items())),
            (reg.root / a["prompt_file"]).read_text().strip(),
            "## Policies in force\n" + "\n".join(pol),
            "## Your skills\n\n" + "\n\n".join("\n".join("##" + ln if ln.startswith("#") else ln
                                                         for ln in skill_body(reg.agent_skill(aid, s["id"])).splitlines()) for s in a["skills"])])
        desc = (f"Use when the human addresses the {a['name']} or asks about the projects of {p.plugin['author']}: status, results, costs, "
                "approvals, the team, or wants work done.")
        text = md([f"name: {aid}", "description: " + q(desc, ensure_ascii=False)], body)
        return {f"skills/{aid}/SKILL.md": text, f"zh/skills/{aid}/SKILL.md": text}


def generate(reg: Registry, gen: Generator | None = None) -> dict[str, str]:
    gen = gen or Generator()
    files: dict[str, str] = {}
    agents_json = {}
    orchestrators = sorted(a for a, d in reg.agents.items() if d["type"] == "orchestrator")
    for aid in sorted(reg.agents):
        if aid.startswith("example"):
            continue
        name = gen.subagent_name(reg, aid)
        p = agent_parts(reg, aid, gen)
        d = "description: " + q(p["desc"], ensure_ascii=False)
        # ponytail: no effort key for Claude subagents (not verified in subagent frontmatter); add when verified.
        cf = [f"name: {name}", d] + ([f"tools: {', '.join(p['claude_tools'])}"] if p["claude_tools"] else []) \
            + ([f"model: {p['model']}"] if p["model"] else [])
        zf = [f"name: {name}", d] + ([f"tools: {', '.join(p['zh_tools'])}"] if p["zh_tools"] else []) \
            + [f"{k}: {q(v)}" for k, v in p["zh"].items()]
        files[f"agents/{name}.md"] = md(cf, p["body"])
        files[f"zh/agents/{name}.md"] = md(zf, p["body"])
        agents_json[name] = {"description": p["desc"], "prompt": p["body"], **({"tools": p["claude_tools"]} if p["claude_tools"] else {}),
                            **({"model": p["model"]} if p["model"] else {})}
    for aid in orchestrators:
        files.update(gen.orchestrator_skill(reg, aid, agents_json))
    js = lambda o: json.dumps(o, indent=2, ensure_ascii=False) + "\n"  # noqa: E731
    files[".claude-plugin/plugin.json"], files[".mcp.json"], files["hooks/hooks.json"] = map(js, manifests(reg.pack))
    files["README.md"] = gen.readme
    return files


def existing(out: Path = OUT) -> set[str]:
    return {str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()} if out.exists() else set()


def drift(files: dict[str, str], out: Path = OUT) -> list[str]:
    cur = existing(out)
    return sorted([f"stale {f}" for f in cur - files.keys()] + [f"missing {f}" for f in files.keys() - cur]
                  + [f"differs {f}" for f in files.keys() & cur if (out / f).read_text() != files[f]])


def main(argv: list[str], gen: Generator | None = None, reg: Registry | None = None) -> int:
    """[--check]: exit 1 if plugin/ differs from a fresh generation."""
    reg = reg or Registry()
    out = reg.root / "plugin"
    files = generate(reg, gen)
    if "--check" in argv:
        bad = drift(files, out)
        print("\n".join(bad) or "plugin/ is up to date")
        return 1 if bad else 0
    for f in existing(out) - files.keys():
        (out / f).unlink()
    for f, text in files.items():
        (out / f).parent.mkdir(parents=True, exist_ok=True)
        (out / f).write_text(text)
    for d in sorted((p for p in out.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    print(f"wrote {len(files)} files to {out}")
    return 0

