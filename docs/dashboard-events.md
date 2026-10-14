# Design: event format for a visual "office" dashboard

Status: proposal (no code), revised after review: task-level events come from the kit itself, model-written notes are optional, stream-JSON is optional. Goal: one picture of every team and agent at work: who sits where, what each is doing right now, what waits in the queue, what stopped and why.

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

**Principle: three layers, each with a different trust level.**

| Layer | Written by | Trust | Used for |
|---|---|---|---|
| A. Lifecycle | the kit runtime, at the points where it already changes task state | deterministic | what is being worked on, result, next task, project done (the dashboard's main need) |
| B. Liveness | the supervisor (CPU, workdir, zcode tool-call log) | independent of the model | slow/stuck/loop, live activity |
| C. Notes | the worker/orchestrator itself (optional, see `note`) | best-effort, never a status or evidence | in-task narrative ("reading the schema...") |

Start, end, result and next task are already known to the kit (`task_start`/`dispatch` set RUNNING, settle/submit record the result, `dependencies`/`PLANNED` give the queue, `objective` is the human text), so layer A needs no prompt change and no forced task splitting. Self-reported progress cannot feed the supervisor: a stuck or looping worker is exactly the one that stops reporting.

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
| `task.start` | task goes RUNNING (`task_start`/`dispatch`) | objective (≤200 chars, the human line), agent, harness/model if known |
| `task.end` | task settles (COMPLETED/FAILED/BLOCKED/CANCELLED) | status, result summary (≤300 chars, from the submitted result), cost, tokens, `next`: the tasks this unblocks or the next in the agent's queue (id + objective) |
| `project.done` | no task left open (all terminal) | counts per status, charged and equivalent cost |
| `note` | worker or orchestrator wrote a note (optional, layer C) | text (≤200 chars, `clean()`ed), source `worker`\|`orchestrator`; never changes `state` |
| `task.status` | any other task status transition | from, to, why (from audit) |

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

## 5. What is missing, and is stream-JSON needed?

| # | Gap | Fix | Needs stream-JSON |
|---|---|---|---|
| G1 | No run/attempt/agent/harness in log lines; time without date; text only | structured `<task>.events.jsonl` written by `Supervisor.note` beside the human log; run id carries the attempt | no |
| G2 | Lifecycle (start, wait, status changes, settle) is spread over audit, usage and run files | layer A: emit `task.start`/`task.end`/`project.done` from `Runtime` at the points that already write them; this covers "what is being worked on, result, next" with no prompt change | no |
| G3 (optional) | Tool calls visible for zcode only | agy, dsh, claude-code parse their stream into the same `tool.call` events | **yes** |
| G4 (optional) | Tokens/cost only at settle (zcode: running totals from rollout) | stream usage events give running totals for agy/dsh/claude-code | **yes** |
| G5 (optional) | Loop detection only for zcode | same `Detector` fed by stream events (changelog already states this limit) | **yes** |
| G6 | Native Claude subagents: no live data | hooks: start/stop events from the plugin hooks; live tool calls by tailing the subagent transcript **(verify)** | no |
| G7 | Supervisor state is in memory | `run.tick` persists last_alive/last_novel/calls | no |
| G8 | Waiting reason unstructured | `wait_on` from task `dependencies` and `approval_id` | no |
| G9 | No in-task narrative | layer C: optional `note`. Worker one-shot processes (zcode, agy, dsh) have no "turn" the orchestrator can ask to report, so the path is a tiny CLI (`orchestrator-kit note "<text>"` via the worker's shell) or one line appended to a file named in the prompt, not MCP (no new surface for workers). One sentence in the system prompt; best-effort | no |

Verdict on stream-JSON: **not needed for the dashboard's main need, kept as an optional later reader.** Task-level events (layer A) and the supervisor's own signals (layer B) already answer what is worked on, the result, the queue and stuck/loop. Stream-JSON would add seconds-level tool activity, running tokens and loop detection for agy, dsh and claude-code (G3-G5). Add it per harness only if a real case shows the gap, most likely agy: it is the first route for coding and today is supervised on workdir and CPU only (stuck works, loop does not). The flags exist (`agy --output-format stream-json`, `dsh --stream-json`, `claude --output-format stream-json --verbose`, the last already used by `quota.py`) but their event shapes have **not been sampled for this design (verify)**; if the need arises, record one small run first.

Why not rely on model-written events alone: (1) a stuck or looping worker stops reporting, so the supervisor needs signals independent of the model; (2) compliance is uneven (profiling: DeepSeek weak on the agent-result contract without an example, GLM sometimes returns no final answer, 2.7%), so a self-reported "done" is never status, the verifier decides; (3) forcing many tiny tasks costs contract, evidence and verification per task, and `dispatch` workers are fresh processes that re-read files, so only native Claude sessions get the cache benefit. Split work where it is naturally separate, not to produce events.

## 6. Order of implementation

1. **G2 lifecycle events** (`task.start`, `task.end` with result and `next`, `project.done`) plus `seq`/run ids, from the runtime. Additive; no prompt, no harness change. Tests on a fixture project.
2. **G1+G7:** structured supervisor events (`tool.call`, `run.tick`, `run.stop`) beside the human `.activity.log` (that and `run_log` stay as they are).
3. **`office.py` + CLI `office --json`**, tested by replaying fixture runs into a snapshot. Already enough for a terminal view.
4. **Dashboard v0:** one static HTML page + the localhost endpoint, polling 2 s, desks and trays only.
5. **Optional, when wanted:** `note` (G9), native hooks (G6), polish (SSE, replay timeline, phone access).
6. **Only if a real gap shows:** stream readers per harness (G3-G5), starting with agy; sample one run first.

Steps 1-4 deliver a working office (task level for every harness, live activity for zcode) without touching how any harness is invoked.

## 7. Open points

- Retention: `events.jsonl` grows; rotate per day or cap per run (suggest: keep per-run files, drop `tool.call` older than 7 days, keep lifecycle events).
- Who may view: the dashboard shows work content only as short targets, but file names can be sensitive; localhost by default, auth is the deployer's job.
- `seq` assignment when a worker outlives the server (detached workers write events themselves): use a per-run `n` plus a project-level ordering by `ts`, or give detached workers an append-only per-run file that the reader merges. Needs a decision at step 2.
