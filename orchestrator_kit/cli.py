"""`orchestrator-kit`: generate companies and projects, hire roles, validate, build the plugin, serve the MCP tools, guard tool calls.

Stdlib only at import time: `orchestrator-kit hook` runs on every tool call of every session.
"""
import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["hook"]:  # fast path: no yaml/jsonschema import
        from .hook import main as hook_main
        return hook_main(argv[1:])
    if argv[:1] == ["tail"]:  # watch a worker's activity log; stdlib only
        from .tail import main as tail_main
        return tail_main(argv[1:])
    if argv[:1] == ["usage-hook"]:  # SubagentStop only (rare): imports the runtime lazily
        from .usage import hook_main as usage_main
        return usage_main(argv[1])
    p = argparse.ArgumentParser(prog="orchestrator-kit")
    sub = p.add_subparsers(dest="cmd", required=True)
    pack = lambda s: s.add_argument("--pack", type=Path, default=Path("."), help="company repo (default: current directory)")  # noqa: E731
    sub.add_parser("roles", help="list the role library")
    s = sub.add_parser("new-company", help="create a company repo with its orchestrator and plugin")
    s.add_argument("dir", type=Path)
    s.add_argument("--name", required=True, help="kebab-case id (plugin name)")
    s.add_argument("--title", required=True, help="display name, e.g. 'Acme Labs'")
    s.add_argument("--human", default="board", help="id of the human at the top (default: board)")
    s.add_argument("--mission")
    s = sub.add_parser("hire", help="copy a library role and what it needs into the company")
    s.add_argument("role")
    s.add_argument("--as", dest="as_id")
    s.add_argument("--reports-to")
    pack(s)
    s = sub.add_parser("new-project", help="create a project file (the team hired from the company catalog)")
    s.add_argument("dir", type=Path)
    s.add_argument("--title", required=True)
    s.add_argument("--budget", type=float, required=True, help="daily budget, USD")
    s.add_argument("--goal")
    s.add_argument("--roster", help="comma-separated agent ids (default: every enabled agent)")
    pack(s)
    for name, h in (("validate", "validate the company's definitions"), ("org", "print the org chart")):
        pack(sub.add_parser(name, help=h))
    s = sub.add_parser("plugin", help="regenerate plugin/ (default generator)")
    s.add_argument("--check", action="store_true")
    pack(s)
    s = sub.add_parser("mcp", help="serve the company's tools over MCP stdio")
    s.add_argument("root", type=Path)
    s = sub.add_parser("profile", help="run the profiling battery on one harness/model/effort")
    s.add_argument("--harness", required=True)
    s.add_argument("--model", required=True)
    s.add_argument("--provider")
    s.add_argument("--effort")
    s.add_argument("--tasks", default="all", help="comma-separated task ids or 'all'")
    s.add_argument("--budget", type=float, required=True, help="stop once this much USD is spent")
    pack(s)
    s = sub.add_parser("match", help="propose agent -> model mappings from the profiles (never applied)")
    s.add_argument("--out", type=Path, help="write the proposal here (default: print)")
    pack(s)
    a = p.parse_args(argv)

    from .runtime import Refused
    try:
        return _run(a)
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1


def _run(a) -> int:
    from . import scaffold
    if a.cmd == "roles":
        for r in scaffold.roles():
            print(f"{r['id']:22} {r['department']:12} {r['type']:12} {r['role']}")
    elif a.cmd == "new-company":
        files = scaffold.new_company(a.dir, a.name, a.title, a.human, a.mission)
        print(f"created {a.dir} ({len(files)} files). Next: orchestrator-kit hire <role> --pack {a.dir}")
    elif a.cmd == "hire":
        changed = scaffold.hire(a.pack, a.role, a.as_id, a.reports_to)
        print("\n".join(changed) + "\nvalid. Review the change, then regenerate the plugin.")
    elif a.cmd == "new-project":
        cfg = scaffold.new_project(a.dir, a.pack, a.title, a.budget, a.goal, a.roster.split(",") if a.roster else None)
        print(f"{a.dir}: {cfg}")
    elif a.cmd == "validate":
        from .validate import main
        return main([str(a.pack)])
    elif a.cmd == "org":
        print(scaffold.org(a.pack))
    elif a.cmd == "plugin":
        bad = scaffold.write_plugin(a.pack, check=a.check)
        print("\n".join(bad) or ("plugin/ is up to date" if a.check else "plugin/ written"))
        return 1 if bad else 0
    elif a.cmd == "mcp":
        from .mcp import Server, core_tools, serve
        from .pack import Pack
        root = a.root.resolve()
        return serve(Server(root=root, tools=list(core_tools(Pack.load(root)).values())))
    elif a.cmd == "profile":
        from .profiling import profile
        summary = profile(a.pack, a.harness, a.model, a.provider, a.effort, None if a.tasks == "all" else a.tasks.split(","), a.budget)
        print(summary)
    elif a.cmd == "match":
        from .profiling import match
        text = match(a.pack)
        a.out.write_text(text) if a.out else print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
