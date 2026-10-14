# Changelog

## 0.9.0 (2026-10-14)
Progress supervisor instead of a hard time limit (sirkel case: GLM-5.3-Flash was really working, 79 model calls in 29 min, files written, but slow, and was killed at 1800 s with its tokens unrecorded; 0.8.x would have cut a route with a fallback at 600 s).
- A worker runs until it is done while it makes progress. `orchestrator_kit/supervisor.py` judges it every 10 s from signals that need no model: new tool calls in the worker's own log (zcode rollout `~/.zcode/cli/rollout`, found by a `[run-id:<task>]` marker in the prompt; name + arguments), the workdir (git HEAD + dirty files), CPU of the process tree, stdout growth. It stops the whole process tree only for: **stuck** (no sign of life for `stuck_minutes` = 10 AND no CPU), **loop** (same call, or an A-B / A-B-C cycle, `loop_repeats` = 6 times with the workdir unchanged; same tool error `error_repeats` = 5 times; calls keep arriving but none is new and the workdir is unchanged for `no_progress_minutes` = 15), or a stop request. A slow model call (25-230 s) is neither. Thresholds: `config/models` `supervisor:` (documented in the schema). Replayed on three real 30-minute GLM runs (72/94/65 tool calls): no false verdict. Loop detection needs a readable tool-call log: zcode today; claude-code, agy and dsh are supervised on workdir, CPU and output only (stuck works, loop does not).
- Policy: stuck/loop/manual stop **stops and reports**. The task becomes BLOCKED with the full reason (what tripped, last tool call, last sign of life, calls seen, workdir summary, log path); no retry and no other route on its own. A route is left only after a real failure (exit non-zero, provider error, quota) or by the orchestrator's decision. A stopped run does not count against the harness circuit breaker.
- `max_wall_seconds` is a loose safety ceiling only: default 10800 (`supervisor.max_wall_seconds`, per task `budget.max_wall_seconds`; the old stored 1800 counts as unset). It is the total across routes; the per-route cut `fallback.route_seconds` (600) from 0.8.0 and `harnesses[].timeout_seconds` are gone (the latter is ignored).
- Tokens of a killed worker are no longer lost: read from the worker's log, priced like a finished call, recorded in `usage.jsonl` (zcode used to record `tokens: {}` on timeout).
- Live activity: `<state>/runs/<task>.activity.log`, one line per tool call (`HH:MM:SS  Tool  short target  (model Ns)`), tool errors, workdir changes, start/exit/stop markers; arguments are reduced to a short target and secrets are masked, file contents never appear. The MCP progress notification (every 30 s) now carries the last 5 lines. New MCP tools `run_log` (last N lines + running + time since last line; never blocks) and `run_stop` (reason mandatory, audited; the worker is killed within ~10 s, task BLOCKED). `orchestrator-kit tail <project dir> [task] [-n N] [-f]` follows the log from a terminal (stdlib only).
- Workers adopted after a server restart are supervised too (the run record carries cwd and thresholds).
- Quota guard (`quota.py`): before a route runs, a spent subscription is skipped without a call and the next route runs (never a wait for a reset): agy reads `agy -p /usage` (skip below 15% of the 5-hour or 20% of the weekly window), claude-code reads `rate_limit_event` utilization from one cheap haiku probe (skip at >= 85% 5-hour or >= 80% 7-day, or when the probe fails); readings cached 10 min. Limit/quota/429 errors from any route are labelled `route exhausted (quota/rate limit)`. A skipped route is a failed attempt, not a task failure, and does not trip the circuit breaker.
- Not done: lowering the priority of routes that often fail per task type/effort.

## 0.8.1 (2026-10-14)
- `serve` answers in-flight tool calls (up to 10 s) when stdin closes; 0.8.0 dropped the reply of a call sent right before EOF.

## 0.8.0 (2026-10-14)
- Fix "dispatch stuck" (sirkel task-c8d1c0befc ran ~50 min past `max_wall_seconds` 1800, tripping Claude Code's MCP idle timeout): the worker now runs in its own session and the wall limit kills the whole process tree. `subprocess.run(timeout=)` killed only the direct child, then hung while a grandchild (wrangler/pnpm) held the stderr pipe. stderr goes to a temp file.
- `dispatch` over MCP sends `notifications/progress` every 30 s (when the client passes a `progressToken`): elapsed/limit, output bytes, process count, and `STALLED` after 5 min of zero CPU in the worker's tree. The stall flag is a report only; a worker is stopped only by the wall limit.
- Workers outlive the MCP server: each runs detached with `<state>/runs/<task>.json|.out|.err|.rc` (pid, owner, harness, model, limit). When a server starts a project, `Runtime.reconcile()` settles every RUNNING task whose owner server is dead: a live worker is awaited (killed at its wall limit), a finished one is collected, one that left no exit code retries/fails with "worker lost". Before, a restart killed the worker and the task stayed RUNNING forever.
- The MCP server runs each `tools/call` on its own thread: a long `dispatch` no longer blocks `status`/`tasks` (stdout writes are locked).
- `url` health-check evidence sends a normal User-Agent (Cloudflare 403'd `Python-urllib`: four correct staging tasks stayed PLANNED), counts a 403/429 as reachable, and names the status code in the note otherwise.
- Per-route time: `max_wall_seconds` is now the total across fallback routes, and a route that has a fallback behind it gets at most `config.models.fallback.route_seconds` (default 600). Before, every route got the full limit (zcode GLM stuck 1800 s on 5 of 5 fullstack tasks, then agy ran).
- `Request.on_progress`, `Runtime.on_progress`, `harness.run_worker`.

## 0.7.6 (2026-10-14)
- A selection without a provider resolves to the harness's only provider (claude-code: `anthropic`): the route, `price_for` and the usage record carry it. Before, the flat-plan price (`anthropic/*`) was never found, so a claude-code call with a full model id was charged in full against the project budget.
- claude-code worker calls record tokens (`inputTokens`, `cacheWriteTokens`, `cacheReadTokens`, `outputTokens`) and the equivalent cost is priced from the descriptor tariff; the CLI's `total_cost_usd` is only the fallback for a model without a tariff.
- The contract guide says an artifact type without its own evidence rule (`other`) needs `evidence.summary` (a worker answer failed `missing evidence 'summary'`; the rule is unchanged).

## 0.7.5 (2026-10-14)
- `task_start` runs a claude-code task whose model is a full model id (e.g. `claude-haiku-5-5`) on the worker path (`claude -p --model <id>`): the session's Agent tool accepts only the aliases opus/sonnet/haiku, and an alias follows whatever the Claude Code version maps it to (haiku resolved to Haiku 4.5).

## 0.7.4 (2026-10-14)
- `Server.call` refuses a call missing a required argument of the tool schema, naming it (was a bare `KeyError`).

## 0.7.3 (2026-10-13)
- `config.models.harnesses[].inactive_providers`: provider ids the Board switched off. `Router.route` skips routes naming them
  (no call, no failed attempt), so the next route of the mapping is tried first.

## 0.7.2 (2026-10-13)
- `task_start` on a task whose resolved AI names a harness other than claude-code now runs it through `dispatch` (fallback
  order of the approved mapping, budget cap, cost charged) and returns `{dispatched, status, notes}`; no subagent prompt.
  0.7.1 refused such tasks instead.

## 0.7.1 (2026-10-13)
Fix:
- `task_start` refuses a task whose resolved AI names a harness other than claude-code (it used to hand out a native
  `subagent_prompt` anyway): the error points to `dispatch`; the task stays untouched.
- `dispatch` takes a task already RUNNING (started earlier) without a second attempt; it used to fail with
  "RUNNING -> RUNNING is not an allowed transition".
- Tool descriptions, the generated plugin text and the orchestration skill state which path a task takes.

## 0.7.0 (2026-10-13)
Additive:
- Agent mapping `native` (Board-approved like entries): the Claude model of the session's native subagents per agent and per
  skill, plus `light_tasks`. `Registry.native_model(agent, skill, task_ai)`: task ai on claude-code > native skill > native agent >
  agent's claude-code ai > first claude-code candidate of its policy. The generator writes it as the subagent's `model`;
  `task_start` returns it as `model` for the Agent tool. `validate` checks the models against the claude-code descriptor.

## 0.6.0 (2026-10-13)
Additive:
- `orchestrator_kit.usage` + `orchestrator-kit usage-hook <pack root>`: a SubagentStop hook reads the native subagent's
  transcript (model, input / cache write / cache read / output tokens, each API message once) and records it on the task named
  in its prompt, once per subagent id; never charged, equivalent priced from the claude-code descriptor. A subagent of another
  plugin or without a task is ignored; the hook never fails the session.
- `pack.yaml` `hook.usage_command` (optional) adds the SubagentStop hook to the generated plugin; `new-company` sets it.
- Pricing entries take `cache_write` (default: `in`); `token_cost` prices `cacheWriteTokens`.
- `task_submit.usage` stays for sessions without that hook; the default orchestrator skill no longer asks for it (no double count).

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
