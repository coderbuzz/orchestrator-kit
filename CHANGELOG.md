# Changelog

## 0.5.0 (2026-10-13)
Additive:
- `task_submit` takes an optional `usage` ({model, total_tokens, tool_uses, duration_ms}, as the Agent tool reports it): the
  native subagent's consumption is recorded in `usage.jsonl` as harness `native`, never charged (it runs on the session's own
  account), list-price equivalent None (only a total is known). The default orchestrator skill asks the session to pass it.
- `usage` adds `today_tokens` and `total_tokens`.

## 0.4.0 (2026-10-13)
Charged cost vs list-price equivalent:
- A call records `cost_usd` (charged; what budgets, the daily cap and the 80% notice count) and `equivalent_cost_usd`
  (list price, kept as data for cost projections; None = unpriced). A `{flat: true}` pricing entry charges 0 for any
  `cost_reporting`, claude-code included (its CLI-reported cost becomes the equivalent); a flat entry may carry list prices
  (`in`/`cache_hit`/`out`) to price its tokens. Pay-as-you-go routes are charged as before.
- `usage` returns `today_usd`, `today_equivalent_usd`, `total_usd`, `total_equivalent_usd`, `unpriced_calls` (was `today_usd` only).
- API change: `harness.parse` returns the list-price cost (a flat entry without list prices gives None, not 0.0);
  `harness.billing(price, listed)` gives (charged, equivalent).

## 0.3.0 (2026-10-13)
Behavior change:
- `task_submit` with a result that breaks the agent-result contract no longer raises and leaves the task RUNNING: it uses the
  attempt (PLANNED with the validation errors as `previous_attempt_feedback`, or FAILED when attempts are exhausted).
Fixes:
- `task_start` gives native subagents the same answer contract as worker harnesses: the self-contained result schema and
  `CONTRACT_GUIDE` (`harness.contract_section`). The guide now lists the `decision` fields and enums, `state_updates` as a list,
  artifact `type` (not `kind`), and a valid decision example.
- `task_add` for an agent the company has but the project did not hire says `not on this project's roster (use <prefix>staff)`.
- `status` starts with the project's goal, roster and daily budget.

## 0.2.0 (2026-10-13)
Breaking (no stored state existed anywhere, so no migrator):
- `company-os/task/2`: removed columns of the old workflow loop that nothing read: `workflow_run_id`, `step_id`, `kind`,
  `awaiting_task`, `iteration`, `optional`, `tried_agents`, `verification_mode`, `verifier_agent`, `waived`, `deadline`,
  `budget.max_tokens`. A record carrying them no longer validates.
- `company-os/harness/2`: `kind` is `cli` only (`api` had no builder).

## 0.1.0 (2026-10-13)
First release, extracted from qumatic/company-os (history of the kit files kept).
- Packs: `pack.yaml` manifest (names, project file, state dir, human id and tool prefix, vocabulary, plugin/MCP/hook).
- Company catalog + project roster (`company-os/project/2`: roster, goal), `<human_prefix>staff`, 80% daily budget notice.
- Generators: role library (15 roles), `new-company`, `hire`, `new-project`, `org`; default orchestrator skill.
- Profiling battery (12 deterministic tasks through the Router) and `match` (proposals only).
- Registrable evidence checks (unregistered `special` fails), generic hook, core schemas as package data.
- Fix: the claude-code builder sends a self-contained result schema (`../ai/1` was unresolvable by `claude --json-schema`).
