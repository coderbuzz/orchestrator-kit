"""The review-pipeline pack's code: everything that is not a definition file, written against the kit's extension points only.

- evidence check `file_line_in_diff` (Runtime.evidence_checks): a finding must name a changed line that says what it quotes
- adapter `post_pr_comment` (Runtime.builtin_adapters): `gh pr comment`, run only after the maintainer approved it
- the review-lead skill (plugin Generator) and the MCP server (kit core tools, no extra ones)
"""
import re
import subprocess
import sys
from pathlib import Path

from orchestrator_kit.mcp import Server, core_tools, serve
from orchestrator_kit.pack import Pack
from orchestrator_kit.plugin import Generator, md, q, skill_body
from orchestrator_kit.registry import Registry
from orchestrator_kit.runtime import Runtime

ROOT = Path(__file__).resolve().parent
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def changed_lines(repo: Path, base: str, file: str) -> set[int] | None:
    """Line numbers at HEAD that the diff base..HEAD adds or changes in `file`; None if git cannot answer."""
    r = subprocess.run(["git", "-C", str(repo), "diff", "-U0", f"{base}..HEAD", "--", file], capture_output=True, text=True)
    if r.returncode:
        return None
    return {n for start, count in HUNK.findall(r.stdout) for n in range(int(start), int(start) + int(count or 1))}


def file_line_in_diff(key: str, value, evidence: dict, workspace: Path | None, known_ids: set) -> str | None:
    if workspace is None:
        return "finding: no workspace to check the diff in"
    path = (workspace / str(evidence["file"])).resolve()
    if not path.is_relative_to(workspace.resolve()) or not path.is_file():
        return f"finding: {evidence['file']} is not a file in the workspace"
    lines = changed_lines(workspace, evidence["diff_base"], evidence["file"])
    if lines is None:
        return f"finding: diff base {evidence['diff_base']!r} not found"
    if evidence["line"] not in lines:
        return f"finding: {evidence['file']}:{evidence['line']} is not a changed line of the diff"
    text = path.read_text().splitlines()[evidence["line"] - 1]
    if str(value).strip() not in text:
        return f"finding: {evidence['file']}:{evidence['line']} does not contain the quoted {key}"
    return None


def post_pr_comment(orch, task, params):
    """`gh pr comment <pr> --body-file -` in the task workspace; the comment URL is the evidence."""
    r = subprocess.run(["gh", "pr", "comment", str(params["pr"]), "--body-file", "-"], input=params["body"], capture_output=True,
                       text=True, cwd=orch._workspace(task), timeout=60)
    if r.returncode:
        return {"ok": False, "detail": r.stderr.strip()[:300]}
    url = r.stdout.strip().splitlines()[-1]
    return {"ok": True, "detail": url, "artifact": {"type": "pr_comment", "title": f"PR {params['pr']} comment", "evidence": {"url": url}}}


class ReviewRuntime(Runtime):
    actor = "review-lead"
    builtin_adapters = {"post_pr_comment": post_pr_comment}
    evidence_checks = {"file_line_in_diff": file_line_in_diff}


class ReviewGenerator(Generator):
    readme = "# plugin/ (generated from examples/review-pipeline; never edit here)\n"

    def orchestrator_skill(self, reg: Registry, aid: str, subagents: dict) -> dict[str, str]:
        a = reg.agents[aid]
        body = "\n\n".join([
            "# Review lead\n\nThe human is the maintainer. You plan the review and run it through the review tools; reviewers are "
            "subagents. Anything that leaves the repository (a PR comment) waits for the maintainer's approval.",
            "## Review tools\n" + "\n".join(f"- `{t['name']}`: {t['description']}" for t in TOOLS),
            "## Reviewers (subagents)\n" + "\n".join(f"- {k}: {v['description']}" for k, v in sorted(subagents.items())),
            (reg.root / a["prompt_file"]).read_text().strip(),
            "## Your skills\n\n" + "\n\n".join(skill_body(reg.agent_skill(aid, s["id"])) for s in a["skills"])])
        desc = "Use when the human asks for a code review of a pull request or a change in a review project."
        return {f"skills/{aid}/SKILL.md": md([f"name: {aid}", "description: " + q(desc)], body)}


TOOLS = list(core_tools(Pack.load(ROOT)).values())


def server(ws: Path | None = None, **runtime_kw) -> Server:
    return Server(ws, runtime=ReviewRuntime, tools=TOOLS, root=ROOT, **runtime_kw)


def main() -> int:
    return serve(server())


if __name__ == "__main__":
    sys.exit(main())
