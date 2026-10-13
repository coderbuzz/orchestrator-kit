# orchestrator-kit

**A multi-agent orchestrator for Claude Code.** You define an AI organization in Git (agents, skills, policies,
approvals, budgets); the kit turns it into a Claude Code plugin. Your Claude Code session becomes the orchestrator: it
plans the work, hands tasks to subagents (or to worker CLIs), and the kit's code checks every result before a task can
close.

There is no separate scheduler or agent loop. Claude Code already is the loop. The kit adds what a session alone
cannot guarantee:

- **Task lifecycle** with contracts: every task carries the project goal, inputs, budget and expected artifacts.
- **Evidence checked by machine**: a task closes only when its artifacts pass the checks for their type (a git commit
  exists, a URL answers, sources are cited, or a check you register).
- **Approvals up the reporting chain**: levels 0-4 follow `reports_to`; level 4 and the human's own tools always ask
  you. No agent approves its own work.
- **Budgets**: per task and per project per day (notice at 80%, refusal at 100%), with a usage ledger per call.
- **Exactly-once actions and an append-only audit log**, stored atomically in the project folder.
- **Routing across harnesses**: tasks run as native Claude Code subagents, or are dispatched to another CLI
  (`claude -p`, zh, agy, dsh) with a chosen model and effort, fallbacks and a circuit breaker.

## What the plugin contains

`orchestrator-kit plugin` generates a standard Claude Code plugin from your company repo:

| Part | What it does |
|---|---|
| Orchestrator skill | tells the session how to run the company: plan, staff, delegate, verify, ask for approval |
| One subagent per agent | its prompt, skills, policies in force, a read-only tool baseline plus the tools its capabilities need, and its model |
| MCP server | the company's tools: `task_add`, `task_start`, `task_submit`, `task_close`, `dispatch`, approvals, project and staff tools |
| `PreToolUse` hook | keeps project state behind the MCP tools and makes tools reserved to the human always ask |

Install it like any plugin (`claude plugin marketplace add`, `claude plugin install`) and talk to the orchestrator in a
normal Claude Code session.

## Quick start

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and [Claude Code](https://claude.com/claude-code).

```sh
uv tool install git+https://github.com/coderbuzz/orchestrator-kit@v0.2.0

orchestrator-kit roles                                   # the role library: 15 roles in engineering, product, finance, marketing, sales, customer
orchestrator-kit new-company ~/acme --name acme --title "Acme Labs" --mission "..."
orchestrator-kit hire cto --pack ~/acme                  # copies the role and the skills, policies, capabilities and routing it needs; validates or rolls back
orchestrator-kit hire fullstack-developer --pack ~/acme  # reports to cto
orchestrator-kit org --pack ~/acme                       # org chart from reports_to / delegates_to
orchestrator-kit plugin --pack ~/acme                    # generate the Claude Code plugin
orchestrator-kit new-project ~/workspace/shop --pack ~/acme --budget 3 --goal "Launch the MVP" --roster cto,fullstack-developer

claude plugin marketplace add ~/acme && claude plugin install acme@acme
cd ~/workspace/shop && claude                            # this session is the orchestrator
```

## Concepts

| | Where | Notes |
|---|---|---|
| Company | a repo: `pack.yaml`, `agents/`, `skills/`, `policies/`, `tools/capabilities/`, `model-routing/`, `config/`, `protocols/artifacts/evidence-requirements.yaml` | one plugin per company; several companies = several repos |
| Hire into the company | `orchestrator-kit hire <role>`: a reviewed change to the company repo | roles are **copied**, never inherited: a newer kit does not change a company's agents |
| Project | `<workspace>/<project>/<project_file>`: name, goal, daily budget, **roster**, default AI | state in `<project>/<state_dir>/` (never committed) |
| Hire into a project | the human's `<human_prefix>staff` tool (asks for confirmation) | tasks go only to agents on the roster; the orchestrator is always on it |
| Goal ancestry | mission (company) > project goal > task | the goal is in every task contract |
| Routing | `ai: {harness, provider, model, effort}` per task > skill > approved mapping > agent > default | model-policy fallbacks; breaker |
| Profiling | `orchestrator-kit profile` (deterministic battery through the router, hard budget), then `match` | `match` only **proposes** agent-to-model mappings; a human approves each one |

## Harnesses

| Harness | Role |
|---|---|
| Claude Code | the orchestrator session and its native subagents; also a worker through `claude -p` |
| zh (zcode), agy, dsh | optional workers through `dispatch`, for other providers or cheaper models |

Adding a harness is a descriptor in `model-routing/providers/<name>.harness.yaml` plus a builder and parser in
`orchestrator_kit/harness.py`.

## Writing your own pack

Most companies need no code: definitions in YAML and Markdown, validated by `orchestrator-kit validate`. A pack with its
own behaviour subclasses `orchestrator_kit.runtime.Runtime` (hooks: `init_state`, `filter_extras`, `apply_result`,
`record_outcome`, `action_gate`, `visible_state`, `workspace_key`, `subagent_name`; tables: `builtin_adapters`,
`evidence_checks`), adds MCP tools through `orchestrator_kit.mcp.Server(tools=, handlers=)` and words its orchestrator
skill with `orchestrator_kit.plugin.Generator`.

[`examples/review-pipeline/`](examples/review-pipeline/) is a complete pack: a multi-agent code review where a review lead
plans, security, bug and simplicity reviewers report findings that must quote a changed line of the diff, a verifier
checks them, and a human maintainer approves the PR comment.

## Limits

- The hook is a speed bump for an honest session, not a sandbox.
- Native subagents are not metered by the kit; `dispatch` to a worker harness is.
- No loop or scheduler: the orchestrator is a session. Use your harness's scheduled triggers for recurring work.
- 0.x: a minor release may break (see [CHANGELOG.md](CHANGELOG.md)). Pin a tag.

## Development

```sh
uv sync && uv run python -m pytest
```

Tests use the role library as their company (`ORCHESTRATOR_KIT_PACK`) and `examples/review-pipeline` as a second pack.
Contributor rules are in [AGENTS.md](AGENTS.md).

## License

[Apache-2.0](LICENSE)
