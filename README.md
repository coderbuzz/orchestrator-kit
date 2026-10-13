# orchestrator-kit

Run an AI organization from definitions in Git. A **company** is a repo holding its catalog of agents, skills and
policies; a **project** is a workspace folder that hires its team from that catalog. The orchestrator is a harness
session (Claude Code or zh) driving the company's tools; the kit's code is the guardrail: task lifecycle, evidence
checked by machine, approvals up the reporting chain, budgets, exactly-once actions, an append-only audit log.

```
orchestrator-kit roles                                   # the role library (15 roles: engineering, product, finance, marketing, sales, customer)
orchestrator-kit new-company ~/acme --name acme --title "Acme Labs" --mission "..."
orchestrator-kit hire cto --pack ~/acme                  # copies the role + skills, policies, capabilities, routing it needs; validates or rolls back
orchestrator-kit hire fullstack-developer --pack ~/acme  # reports to cto (org chart from reports_to / delegates_to)
orchestrator-kit org --pack ~/acme
orchestrator-kit plugin --pack ~/acme                    # Claude Code plugin: orchestrator skill, one subagent per agent, MCP tools, guard hook
orchestrator-kit new-project ~/workspace/shop --pack ~/acme --budget 3 --goal "Launch the MVP" --roster cto,fullstack-developer
claude plugin marketplace add ~/acme && claude plugin install acme@acme
```

## Concepts
| | Where | Notes |
|---|---|---|
| Company | a repo: `pack.yaml`, `agents/`, `skills/`, `policies/`, `tools/capabilities/`, `model-routing/`, `config/`, `protocols/artifacts/evidence-requirements.yaml` | one plugin per company; several companies = several repos |
| Hire into the company | `orchestrator-kit hire <role>`: a reviewed change to the company repo | roles are **copied**, never inherited: a newer kit does not change a company's agents |
| Project | `<workspace>/<project>/<project_file>`: name, goal, daily budget, **roster**, default AI | state in `<project>/<state_dir>/` (never committed) |
| Hire into a project | the human's tool `<human_prefix>staff` (asks for confirmation) | tasks go only to agents on the roster; the orchestrator is always on it |
| Goal ancestry | mission (company) > project goal > task | the goal is in every task contract |
| Budgets | per task, per day per project (notice at 80%, refusal at 100%), `--max-budget-usd` in the harness | usage ledger per call |
| Approvals | levels 0-4 up `reports_to`; level 4 and `<human_prefix>*` tools are the human's | no self-approval |
| Evidence | per artifact type (`evidence-requirements.yaml`), `special` checks (git commit exists, URL answers, sources, your own) | an unregistered `special` fails |
| Routing | `ai: {harness, provider, model, effort}` per task > skill > approved mapping > agent > default; model-policy fallbacks; breaker | harnesses: claude-code, zcode (zh), agy, dsh |
| Profiling | `orchestrator-kit profile` (deterministic battery, hard budget) then `match` | `match` only **proposes** mappings; a human approves each one |

## A pack's code (optional)
Most companies need none. A pack with its own behaviour subclasses `orchestrator_kit.runtime.Runtime` (hooks:
`init_state`, `filter_extras`, `apply_result`, `record_outcome`, `action_gate`, `visible_state`, `workspace_key`,
`subagent_name`; tables: `builtin_adapters`, `evidence_checks`), adds MCP tools through `orchestrator_kit.mcp.Server(tools=,
handlers=)` and words its orchestrator skill with `orchestrator_kit.plugin.Generator`. `examples/review-pipeline/` is a
complete second pack (code review with a maintainer); company-os (QUMATIC) is the first.

## Limits
- The hook is a speed bump for an honest session, not a sandbox.
- Subagents run natively in the session are not metered; `dispatch` (worker harness) is.
- No loop or scheduler: the orchestrator is a session. Use your harness's scheduled triggers for recurring work.

Development: `uv sync && uv run python -m pytest`. Tests use the role library as their company (`ORCHESTRATOR_KIT_PACK`).
