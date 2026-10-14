# Design: event format for a visual "office" dashboard

Status: proposal (no code). Goal: one picture of every team and agent at work: who sits where, what each is doing right now, what waits in the queue, what stopped and why.

Facts below come from kit 0.9.0 (`supervisor.py`, `runtime.py`, `harness.py`). Things not yet verified are marked **(verify)**.

## 1. What exists today

| Source | Where | Shape | Gives | Does not give |
|---|---|---|---|---|
| Activity log | `<state>/runs/<task>.activity.log` | text, `HH:MM:SS  tool target (model Ns)`, `ERROR`, `workdir` lines; redacted | live tool calls | date, run/attempt/agent/harness (only in the file name), structure, tokens |
| Run record | `<state>/runs/<task>.json` | JSON: meta, `pid`, `started`, `limits`, `cwd`, `done`, `stopped`, `stop_reason` | alive/done/stopped | route in use, progress |
| Usage | `<state>/usage.jsonl` | one line per attempt, written at **settle** | tokens, cost, equivalent cost, harness, model, provider, effort, ok, error | anything while running |
| Tasks, approvals | `<state>/tasks/*.json`, `approvals/*.json` | status enum `PENDING PLANNED RUNNING BLOCKED WAITING_APPROVAL FAILED COMPLETED CANCELLED`; approval `pending/granted/rejected/expired` | queue, who is assigned, why blocked | |
| Audit | `<state>/audit.jsonl` | actor, action, why | status transitions | |
| Supervisor | in memory (`Detector`) | last_alive, last_novel, calls, verdict | stuck/loop/manual | not persisted until the stop report |
| Roster | project roster | agents per project, department | desks that exist with no task | |

Only zcode exposes tool calls (rollout log). claude-code, agy and dsh show workdir changes and CPU only. Native Claude subagents (Agent tool) have no live log; the `SubagentStop` hook reads their transcript at the end.

## 2. Event schema (`kit-event/1`)

One JSON object per line, append-only, UTC, secrets never present. Two shapes share one vocabulary: **events** (what happened) and a **snapshot** (the state of every desk, derived from the events plus task/approval state).

```json
{"v":1,"seq":1042,"ts":"2026-10-14T09:12:03Z","project":"p1",
 "run":"task-ab12#2","task":"task-ab12","attempt":2,
 "agent":"fullstack-developer","dept":"engineering",
 "harness":"zcode","model":"glm-5.3-flash","provider":"coding-plan","effort":"high",
 "kind":"tool.call","state":"working",
 "tool":"Edit","target":"src/app.ts","model_s":41.2,
 "tokens":{"in":0,"out":0,"cache_read":0,"cache_write":0,"reasoning":0},
 "cost_usd":0.0,"equivalent_cost_usd":0.0031,
 "stop":null,"source":"rollout"}
```

Required: `v seq ts project run task attempt agent kind state`. Everything else optional and absent when unknown (never `0` for unknown: an absent `tokens` means "not measured", which differs from zero).

**`kind`**

| kind | emitted when | extra fields |
|---|---|---|
| `run.start` | worker launched (each route/attempt) | harness, model, provider, effort |
| `tool.call` | worker made a tool call | tool, target (≤100 chars, `clean()`ed), model_s |
| `tool.error` | tool returned an error | tool, msg (≤160 chars) |
| `workdir.change` | git fingerprint changed | summary (files changed/new commit) |
| `run.tick` | supervisor poll, throttled to ~30 s | elapsed_s, cpu_idle_s, calls, last_alive_age_s, tokens (running total if known) |
| `run.wait` | task enters `WAITING_APPROVAL`, or waits on dependencies | wait_on (`approval:<id>` or `task:<id>`) |
| `run.stop` | supervisor/manual stop | stop.kind (`stuck`/`loop`/`manual`/`ceiling`), stop.reason (≤1500), last_call |
| `run.end` | attempt settled (ok or failed) | ok, error (≤300), tokens, cost_usd, equivalent_cost_usd, duration_s |
| `task.status` | task status transition | from, to, why (from audit) |

**`state`** (the desk lamp; derived, one value per run at emit time):

| state | meaning | source rule |
|---|---|---|
| `working` | making progress | task `RUNNING`, last sign of life younger than half of `stuck_minutes` |
| `slow` | alive but quiet | task `RUNNING`, no sign of life for ≥ half of `stuck_minutes` (warning before the verdict; 5 min with defaults) |
| `waiting` | waits on a human or a dependency | `WAITING_APPROVAL`, or `PLANNED` with unmet dependencies |
| `stuck` | supervisor verdict stuck or loop | `run.stop` with kind stuck/loop |
| `stopped` | stopped on request or ceiling; or `BLOCKED` for another reason | `run.stop` manual/ceiling, `BLOCKED` |
| `done` | `COMPLETED` | |
| `failed` | `FAILED`, all routes failed | |
| `idle` | roster agent with no active task | snapshot only |

Why `slow` is separate from `stuck`: a GLM-5.3-Flash call legitimately takes 25-230 s. The dashboard may show a quiet agent in amber; only the supervisor may call it stuck (0.9.0 rule: stuck is never decided by the UI).

**Ordering and ids.** `seq` is a per-project monotonic counter (assigned under the store lock that already serialises writes), so a client resumes with `after=<seq>`. `run` = `<task>#<attempt>`; today the log and run file are keyed by task only and a fallback route reuses them, so the attempt index must be added (gap G1).

**Money.** Keep the two fields the kit already separates: `cost_usd` (charged; flat plans = 0) and `equivalent_cost_usd` (list price, projection data). The UI shows charged by default and equivalent as secondary; never sum them.

**Not in events, ever:** prompts, task inputs/results, file contents, tool output, env values, absolute paths outside the workdir (targets are shown relative to `cwd`). Redaction stays where it is (`clean()` + `redact()`); the event writer calls them again as a second belt.

## 3. How the dashboard reads it

Options:

| Option | Blocking | Secrets | Verdict |
|---|---|---|---|
| Read files directly in the browser/server | no | whatever is on disk | only OK behind the reader below |
| Small read-only HTTP endpoint over the files | no (stat + tail by offset) | allowlist of fields | **recommended** |
| MCP tools (`run_log`, `status`) | goes through a model process; MCP calls are serialised per server | same | wrong tool: MCP is for the human-in-the-loop relay, not for a 2 s poll |

Recommendation: files remain the source of truth; add one pure module `orchestrator_kit/office.py` with two functions, `snapshot(state_dir)` and `events(state_dir, after_seq, limit)`, used by both a CLI (`orchestrator-kit office <project dir> --json [--after N]`, stdlib only, like `tail`) and a tiny stdlib `http.server` bound to `127.0.0.1` with `GET /snapshot` and `GET /events?after=N` (SSE later if polling is not enough).

- **Never blocks the runtime:** the reader opens files read-only, never takes the store lock, reads the log from a byte offset, ignores a trailing partial line (the supervisor already uses this pattern), and caches by `(mtime, size)`. Torn JSON = skip, retry next poll.
- **No secrets:** field allowlist in the reader (not in the browser); events are already redacted at write; no endpoint serves raw run files, `.out`, or the rollout log. Bind to localhost; exposing it to a phone is a deployment decision (reverse proxy with auth), not part of the kit.
- **Several projects:** the server takes a list of project dirs and tags each event with `project`; no cross-project joins in the kit.
- **Stale detection:** the snapshot carries `generated_at` and, per run, `last_event_age_s`; the UI greys a desk whose runner (`pid`/`owner`) is gone, which is what `Runtime.reconcile()` settles on the next server start.

## 4. Map to the office

| Office element | Data |
|---|---|
| Building = company, floor/room = project, zone = department | roster `dept` |
| Desk = roster agent (exists even when idle) | roster; `state=idle` |
| Desk lamp colour | `state` (working green, slow amber, waiting blue, stuck red, stopped grey-red, done check, failed cross) |
| Monitor text / speech bubble | last `tool.call`: `Edit src/app.ts`; `tool.error` shows briefly in red |
| Name plate chips | harness, model, effort |
| Progress strip under the desk | elapsed, calls count, tokens (if measured), charged cost; equivalent cost on hover |
| Inbox tray on the desk | tasks assigned to the agent in `PENDING`/`PLANNED`, ordered by priority, with blockers shown as locks |
| Approval door / board table | `WAITING_APPROVAL` tasks and `pending` approvals; who must decide |
| Stop note pinned on the desk | `run.stop` reason + last call (the full stop report) |
| Done tray | last N `COMPLETED` tasks with cost |
| Click on desk | drawer = `run_log`-style last 20 events, run history (attempts, routes tried, why each ended) |
| Header | project budget used today (charged), quota guard state (routes skipped for low quota) |

Concurrent work by one agent: one desk, stacked run cards. A fallback route is a new `run.start` on the same desk (chip changes), not a new person.

## 5. What is missing, and does it need stream-JSON?

| # | Gap | Fix | Needs stream-JSON |
|---|---|---|---|
| G1 | No run/attempt/agent/harness in log lines; time without date; text only | structured `<task>.events.jsonl` written by `Supervisor.note` beside the human log; run id carries the attempt | no |
| G2 | Lifecycle (start, wait, status changes, settle) is spread over audit, usage and run files | emit lifecycle events from `Runtime` at the points that already write them | no |
| G3 | Tool calls visible for zcode only | agy, dsh, claude-code parse their stream into the same `tool.call` events | **yes** |
| G4 | Tokens/cost only at settle (zcode: running totals from rollout) | stream usage events give running totals for agy/dsh/claude-code | **yes** |
| G5 | Loop detection only for zcode | same `Detector` fed by stream events (changelog already states this limit) | **yes** |
| G6 | Native Claude subagents: no live data | hooks: start/stop events from the plugin hooks; live tool calls by tailing the subagent transcript **(verify)** | no |
| G7 | Supervisor state is in memory | `run.tick` persists last_alive/last_novel/calls | no |
| G8 | Waiting reason unstructured | `wait_on` from task `dependencies` and `approval_id` | no |

Verdict on stream-JSON: **yes, but only after G1+G2 exist and only as readers**, one per harness, behind the schema above. It fills G3-G5 for three of four harnesses; zcode already has the rollout reader; native has hooks. The existing flags are `agy --output-format stream-json`, `dsh --stream-json`, `claude --output-format stream-json --verbose` (the last is already used by `quota.py`). **Their event shapes have not been sampled for this design (verify):** step 0 is to record one small run per harness and write the field mapping from real lines, not from the docs.

Shape of the refactor, to keep the change small: the adapters keep returning the same `CallResult` (the stream's final result line equals today's JSON output); a per-harness `StreamReader` (like `ZcodeLog`) turns lines into events as the worker runs. If the stream cannot be parsed the run still finishes from the final line or from the exit code; supervision degrades to workdir/CPU, as today for these harnesses. Test with recorded fixtures, replayed the way 0.9.0 replayed the GLM runs.

## 6. Order of implementation

1. **Sample and freeze.** One small run each of agy, dsh, claude stream-JSON (flat plans, cents at most); keep as test fixtures. Confirm this schema against them. No production code.
2. **G1+G2+G7:** structured events from the supervisor and the runtime (zcode + lifecycle). Additive; the human `.activity.log` and `run_log` stay as they are.
3. **`office.py` + CLI `office --json`** with tests that replay fixture runs into a snapshot. This is already enough for a terminal view.
4. **Dashboard v0:** one static HTML page + the localhost endpoint, polling 2 s, desks and trays only (no animation).
5. **Stream readers** for agy, dsh, claude-code (G3-G5), one PR per harness, each replayed on its fixture.
6. **Native hooks (G6)** and polish (SSE, replay timeline, phone access).

Steps 2-4 deliver a working office for zcode and all state/queue data without touching how any harness is invoked, so the stream-JSON refactor stays deferred until step 5 with its schema already fixed.

## 7. Open points

- Retention: `events.jsonl` grows; rotate per day or cap per run (suggest: keep per-run files, drop `tool.call` older than 7 days, keep lifecycle events).
- Who may view: the dashboard shows work content only as short targets, but file names can be sensitive; localhost by default, auth is the deployer's job.
- `seq` assignment when a worker outlives the server (detached workers write events themselves): use a per-run `n` plus a project-level ordering by `ts`, or give detached workers an append-only per-run file that the reader merges. Needs a decision at step 2.
