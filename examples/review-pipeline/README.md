# review-pipeline: the second pack

A multi-agent code review on `orchestrator_kit` alone: `review-lead` (the session) plans; `security-reviewer`,
`bug-reviewer`, `simplicity-reviewer` and `verifier` are subagents or dispatched workers; the human is the `maintainer`.
It is the extraction gate of the framework review (step 5): a second organization runs on the kit with no kit code
written for it. Tests: `tests/examples/test_review_pipeline.py`.

| File | What the pack writes |
|---|---|
| `pack.yaml` | names (`review-pipeline`, server `review`), project file `review.yaml`, state `.review`, human `maintainer` / `maintainer_`, vocabulary |
| `agents/`, `skills/`, `policies/` | 5 agents, 5 skills, 4 policies (`approval-levels`, `spending`, `autonomy-limits` are the ids the kit reads parameters from) |
| `tools/capabilities/` | `read_repository`, `run_tests` (harness), `create_task`, `post_pr_comment` (external, level 1, adapter in code) |
| `protocols/artifacts/evidence-requirements.yaml` | `review_scope`, `review_finding` (with `special: file_line_in_diff`), `verification_report`, `review_summary`, `pr_comment` |
| `model-routing/`, `config/` | Claude Code only: cheap reviewers on haiku, security on opus |
| `review_pipeline.py` | the evidence check, the `gh pr comment` adapter, `ReviewRuntime`, the review-lead skill, the MCP server |

Not installed: `review-pipeline-mcp` in `pack.yaml` names the entry point a real pack would ship (`review_pipeline:main`).

## What a reviewer must prove
A finding names a file and a line that the diff `diff_base..HEAD` adds or changes, and quotes that line. A finding on an
unchanged or missing line, with an invented quote, or outside the workspace fails verification; the task goes back with
the reason, then fails after the attempt limit. "Nothing found" is valid: every reviewer returns a `review_scope`.

## Kit changes this pack needed (all additive; company-os unchanged)
1. `verify.CHECKS` + `Runtime.evidence_checks`: a pack registers `special` checks (gap G10). A `special` nobody registered now
   fails; before, it passed unchecked.
2. `pack.yaml` `vocabulary` (departments, policy domains), checked by validate; the kit schemas only require kebab-case (G3).
   company-os moved its lists there, so its validation is as strict as before.
3. `validate`: a pack without `company/state.schema.json` (no shared state) validates (G5).
4. `orchestrator_kit/hook.py`: a generic stdlib-only guard configured by arguments.

## Gaps of the review (§5) after steps 1-5
G1 human id, G2 project file and state dir, G7 orchestrator skill, G9 adapters: manifest and pack code (steps 2-3).
G3, G5, G10: kit changes above. G4 artifact types: open pattern + evidence rules (step 2). G6 status report: kit default
report. G8 decision subagent: optional (`Generator.subagent_name`). G11 deploy gate: in company-os (step 3). Open: none.
