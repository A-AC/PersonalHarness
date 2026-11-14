# Personal Assistant Harness

A single-user personal assistant built around one rule: **the model
proposes, the harness decides and records what actually happened.**

The harness is one Python process in one Docker container. It owns
execution, persistence, validation, context assembly, scheduling,
permissions, recovery, and observability. One OpenAI-compatible model
endpoint does the reasoning. Everything else — the conversation, tasks,
memories, schedules, approvals, and tool outcomes — lives in one SQLite
database, durable across restarts, inspectable from a web UI and a
terminal CLI that are two views of the same runtime.

- One model endpoint (OpenAI-compatible chat completions, native tool calls)
- One agent runtime: every trigger — chat message, scheduled prompt,
  heartbeat work, approval decision, restart recovery — enters the same
  bounded loop
- One durable interaction history: web, CLI, schedule, and heartbeat inputs
  are rows in one messages table, tagged by source
- One authoritative state store: SQLite (WAL) plus readable markdown
  artifacts beside it
- No shell tools, no arbitrary command execution, no bundled user-specific
  integrations; external capabilities come from configured MCP servers and
  skills
- Approvals bound to exact actions; a generic "yes" authorizes nothing
  without a matching pending approval

## What it does

- **Conversation with durable tasks.** Every user objective becomes a task
  with an explicit status (active, waiting_user, waiting_approval, paused,
  completed, failed, cancelled). Tasks persist their run transcript, so an
  interrupted, paused, or waiting task resumes exactly where it stopped —
  completed actions are never repeated.
- **Domains with a high creation threshold.** A domain is a durable area of
  work with its own memory scope. New messages are routed automatically to
  the domain the evidence points to; unmatched input lands in the default
  General domain. Domains are created only by explicit user command or by a
  model proposal that the user approves.
- **Memory with provenance.** The model saves durable facts, preferences,
  decisions, procedures, constraints, and questions through memory tools.
  Retrieval is FTS5 keyword search with scope priority and recency,
  explainable per result. Corrections supersede, updates create revisions,
  consolidation merges duplicates and expires stale rows with a readable
  change log.
- **Context construction as a subsystem.** Each run starts from a budgeted
  system prompt built from the persona, operating rules, current task and
  domain state, retrieved memories, recent history, and older history
  retrieved by relevance. Every inclusion and exclusion is recorded on the
  run as a source with its reason.
- **Durable scheduled prompts.** One-time, interval, daily, and weekly
  schedules in the host's system timezone (or an explicit IANA zone).
  Occurrences are recorded before
  submission, so restarts and duplicate ticks never re-run a completed
  occurrence. Missed runs follow a documented policy (run_once coalesces,
  skip discards).
- **Heartbeat.** A deterministic periodic scan that submits bounded work
  through the normal runtime: inbox events before due schedules, interrupted
  runs, retryable model failures, one-time approval reminders, expiry sweeps.
  Quiet checks write events, not chat messages.
- **Durable inbox and declarative automations.** External systems wake the
  agent with one idempotent `POST /api/inbox`. Events are stored first and
  triaged by the heartbeat; rules can create a task, notify, or ignore.
  Content dedupe, per-rule cooldowns, an hourly cap with an auto-disable
  breaker, and quiet hours bound the loop. Inbox content is always untrusted,
  so a derived run starts tainted and side effects still need approval.
- **Self-scheduling with durable bounds.** A task can create or cancel its
  own schedules through confirm-risk `schedule_task` / `cancel_schedule`
  tools. A per-task schedule cap and a schedule-depth limit stop a scheduled
  task from recursively scheduling itself forever.
- **Approvals with a quick confirmation path.** Confirm-risk tool calls
  (external MCP actions by default, domain creation) become approvals bound
  to the exact tool and arguments. Short replies like "yes" or "no" are
  classified affirmatively/negatively/ambiguously — by keyword or a tiny
  constrained model call — and resolve exactly one approval. Ambiguous
  replies never approve.
- **Sequential, bounded sub-agents.** A parent task can delegate one
  self-contained sub-problem through `delegate_task`. The child is a normal
  durable task and run with a reduced context (goal, parent-supplied brief,
  domain memories), a low-risk-only toolset, and tighter budgets. It cannot
  ask the user, create domains, or perform approval-gated actions; those
  return `blocked` to the parent. Re-delegating the same goal reuses the
  existing child instead of duplicating work.
- **Honest recovery.** In-flight tool executions interrupted by a crash
  become unknown, never success. Unknown outcomes are surfaced and
  reconciled instead of silently retried. Runs paused by limits stay paused
  until the user or scheduler resumes them deliberately.
- **Web UI + terminal CLI.** A polling chat page with grouped navigation:
  *Conversation* (chat and the live working-task strip), *Work* (tasks with a
  detail drawer showing the full persisted model transcript, context sources,
  and sub-agent tree, plus approvals, schedules, inbox, automations),
  *Knowledge* (memory, domains, skills, consolidation), and *System* (status,
  modules, tools, events, persona, config, backup, guard). It polls
  `/api/summary` for badges plus only the active panel's endpoint, while the
  chat poll stays on `/api/messages`; `/api/state` remains a compatibility
  aggregate for the CLI. A CLI talks to the same HTTP API. No second runtime,
  no separate history, no build step.

## Architecture at a glance

See [CODEMAP.md](CODEMAP.md) for the module map and data flow.

```text
web UI / CLI / schedule / heartbeat / approval decision
  -> runtime.submit (one lock; one loop)
  -> route: quick-confirm -> resume candidate -> domain routing -> new task
  -> context build (persona, rules, state, memories, history, skills)
  -> model chat completions with native tool schemas
  -> resolve tool -> validate args -> permission policy
       confirm risk -> approval, run waits, resumes after decision
       low risk     -> execute, record outcome, observe result
  -> repeat until final reply, question, approval wait, or a limit
  -> persist transcript + outcome at every step
```

| Area | Location |
| --- | --- |
| Composition root, recovery, backup | `harness/app.py` |
| Model client + scripted test model | `harness/model.py` |
| Agent loop | `harness/runtime.py` |
| Sequential sub-agents | `harness/subagents.py` |
| Context builder | `harness/context.py` |
| Messages, events, domains, tasks | `harness/store.py` |
| Memory + artifacts | `harness/memory.py` |
| Tools, permissions, MCP | `harness/tools.py` |
| Approvals + quick confirm | `harness/approvals.py` |
| Schedules + heartbeat | `harness/scheduler.py` |
| Consolidation | `harness/consolidation.py` |
| SQLite schema | `harness/db.py` |
| Web server / JSON API | `harness/server.py`, `harness/web/` |
| CLI client | `harness/__main__.py` |
| Scenario evaluation (`harness eval`) | `harness/evaluation/`, `eval/scenarios/` |
| Configuration | `harness/config.py`, `data/config.yaml` |

## Requirements

- Python 3.12+ (native path), or Docker 24+ with Compose v2
- An OpenAI-compatible chat-completions endpoint that supports native tool
  calls (this deployment uses a llama.cpp server; see below)

## Model endpoint

The configured endpoint is `http://192.168.50.40:8181/` serving
`qwen3.5-9b`. Verified behavior (2026-10-27, live probes):

- `GET /v1/models` lists models; `qwen3.5-9b` is served.
- `POST /v1/chat/completions` with `tools` and `tool_choice=auto` returns
  native OpenAI-style tool calls: `finish_reason="tool_calls"`,
  `message.tool_calls[].function.{name, arguments}` (arguments as a JSON
  string).
- Round trips work: echo the assistant tool-call message back with a
  `role="tool"` result (matching `tool_call_id`) and the model answers.
- Thinking output arrives in a separate `reasoning_content` field; the
  client keeps it out of visible content. Thinking can consume small
  token budgets, so visible replies can be empty on tight budgets — the
  harness treats empty final replies as malformed and retries within a
  bounded budget, then fails honestly.
- Unknown model names return 400 with a JSON error body.
- No authentication is sent unless `model.api_key` is configured.

Configure via `data/config.yaml` (`model.base_url`, `model.name`,
`model.api_key`, `model.chat_path`) or the environment variables
`HARNESS_MODEL_BASE_URL`, `HARNESS_MODEL_NAME`, `HARNESS_MODEL_API_KEY`.
The harness never falls back to another model or provider. Verify at any
time with:

```bash
python -m harness endpoint-check --data-dir ./data
```

which probes the models list, a plain completion, and a tool-call request
and prints the raw findings.

## Quick start (local development)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

python -m harness --data-dir ./data serve
```

Open `http://127.0.0.1:8080/?token=dev-token` (the default token; change it
before exposing the UI beyond localhost). `/health` is unauthenticated.

In a second terminal, talk to the same runtime:

```bash
python -m harness msg "Say the single word: ready"
python -m harness status
```

The CLI is an HTTP client of the running server — it never opens the
database or runs its own model loop.

## Deployment (Docker Compose, recommended)

One container runs the web UI, API, agent runtime, scheduler, heartbeat,
memory, and context management. SQLite and artifacts live in one mounted
volume; the model endpoint is external.

```bash
cp .env.example .env       # set HARNESS_TOKEN to a long random value
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8080/health      # {"status":"ok"}
```

- The host port comes from `HARNESS_PORT` (container listens on 8080).
- The image runs as unprivileged UID 10001; state lives in the
  `harness-data` volume at `/data`.
- `HARNESS_MODEL_BASE_URL` must be reachable **from inside the container**.
  Use `http://192.168.50.40:8181` for a LAN model server, or
  `http://host.docker.internal:8181` if the model runs on the Docker host
  (the compose file maps that name on Linux).
- Verify the model path from inside the container:

```bash
docker compose exec harness python -m harness endpoint-check --data-dir /data
```

- A complete automated smoke check (build, start, health, auth, API shape,
  endpoint probe) is available:

```bash
HARNESS_TOKEN=check-token HARNESS_PORT=18080 python scripts/container_check.py
```

### Native alternatives

Systemd or a Windows service work the same way: run
`python -m harness serve` with `HARNESS_DATA_DIR` pointing at a durable
directory and `Restart=on-failure`. Keep the UI bound to localhost behind a
TLS reverse proxy or a VPN; the token is the only access control.

### Concurrency rules

Exactly one process may write a data directory. `serve` holds a
`process.lock` (pid + heartbeat timestamp) and refuses to start beside a
live writer; a stale lock (dead pid) is taken over. The CLI is a client and
never writes directly. Docker's `restart: unless-stopped` brings the
container back after a restore-induced restart.

### Backup and restore

Backups are consistent snapshots: the database via SQLite's online backup
API plus `config.yaml`, the artifacts directory, and `data/modules/`, zipped
into `<data>/backups/harness-<timestamp>.zip`.

```bash
python -m harness backup                      # via the running server
python -m harness restore <zip> --yes         # restarts the service
```

Restore replaces the live state; the server responds, then exits so the
container or service manager brings it back cleanly. Back up before
upgrading. There is no in-place migration between v1 (the previous
file-based prototype) and v2: v2 does not read v1's files, and they remain
untouched on disk if present.

## CLI reference

Global options (`--data-dir`, `--url`, `--token`) may appear before or
after the command; the server URL defaults to `HARNESS_URL` or
`http://127.0.0.1:8080`, and the token defaults to `HARNESS_TOKEN` or the
value in `data/config.yaml`.

| Command | Purpose |
| --- | --- |
| `serve` | Run the application (web UI, API, scheduler, heartbeat) |
| `msg TEXT` | Send a message and print the response (`turn`/`say` aliases) |
| `status` | Open work, pending approvals, schedules |
| `tasks [ID]` | List tasks, or show one task's runs, executions, context sources |
| `approvals [approve/reject/cancel] [ID]` | List or decide pending approvals |
| `schedules [--add JSON] [--action ID enable/disable/delete]` | Manage schedules |
| `inbox [--add JSON] [--status STATUS] [--ignore ID] [--triage]` | List, push, ignore, or triage durable inbox events |
| `automations [--add JSON] [--update ID JSON] [--action ID enable/disable/delete]` | Manage declarative inbox rules |
| `memories [--search Q] [--archived|--all] [--include-archived] [--archive ID] [--restore ID] [--pin ID] [--unpin ID]` | List, search, filter, or manage memory lifetime; reports usage counts and artifact problems |
| `domains [--create NAME DESCRIPTION]` | List or explicitly create domains |
| `consolidate` | Run one memory consolidation pass |
| `events [-N]` | Recent event-log rows |
| `tools` | Registered tools with their risk classes; MCP status |
| `eval` | Run scenario evaluations in-process (deterministic or live); `--list`, `--list-models`, `--summary`, `--compare` |
| `backup` / `restore ZIP --yes` | Snapshot / restore state |
| `config` | Effective configuration (secrets redacted) |
| `endpoint-check` | Probe the configured model endpoint directly |

Example schedule creation (single quotes on POSIX shells):

```bash
python -m harness schedules --add '{"name":"morning-brief","prompt":"Summarize open work and anything due today.","kind":"daily","time_of_day":"08:30"}'
```

## Behavior model

### The runtime loop

One bounded loop serves every trigger. Each iteration: check limits, call
the model with the persisted transcript and the registered tool schemas,
then either execute proposed tool calls (validating arguments, applying
permissions, possibly waiting for approval) or deliver the final reply.
The transcript is persisted after every step, so any interruption resumes
from exactly the recorded state.

The model signals "I need user input" by ending a reply with a special
`ask_user` marker (described to it in the operating rules); the runtime
strips the marker, records the question, and marks the task waiting_user.
The next user message is bound to that task and appended to the same run.
Without the marker, the reply is the final answer and the task completes.

Limits (all configurable): 16 model turns, 24 tool calls, 600 seconds per
run, 3 retries for malformed output, 2 endpoint retries. Reaching a limit
pauses the run with an honest, visible note; paused work resumes when the
user continues it or the heartbeat retries a model failure.

### Domains and routing

Messages are routed deterministically: keyword evidence from the domain
name and description, open task objectives, and recent mentions. The best
scoring domain wins when it clears a threshold and beats the runner-up by
a margin; otherwise the message goes to General. Routing decisions are
recorded as `domain.routed` events with their evidence. A new topic never
creates a domain by itself.

Resumption is preferred over new work: a message that plausibly continues
an open task (keyword overlap, recency, and a bonus for tasks waiting on
the user) resumes that task's run. When two candidates tie materially, the
harness creates a task whose context names the candidates so the assistant
asks which to continue instead of guessing.

Domains are created only by the explicit API/CLI command or by the model's
`create_domain` tool, which is confirm-risk: the exact name and description
become an approval, and nothing exists until the user approves it.

### Memory

Kinds: fact, preference, decision, procedure, constraint, open_question,
context, temp. Scopes: global and per-domain. Explicit user statements are
captured immediately with provenance (run/task references); nothing is
saved per message automatically. Retrieval is FTS5 keyword matching with
scope priority and recency decay; superseded, retracted, and expired rows
never return. A newer statement on the same subject (same scope and kind,
strong keyword overlap) supersedes the older row, which keeps its full
history in `memory_revisions`. Long notes live as markdown artifacts under
`data/artifacts/memories/`, referenced from the row; the database is
authoritative and missing artifacts are reported, not silently read.

Memory lifetime is deterministic and model-free. Every memory tracks
`access_count` and `last_accessed_at`; a memory is counted once per run when
it is actually included in context or returned by `search_knowledge` (raw FTS
candidate scans never count), with a durable per-run dedupe row in
`memory_access`. The heartbeat sweeps on `memory.lifetime.sweep_interval_s`
and archives rows whose age since `last_accessed_at` (falling back to
`created_at`) exceeds `clamp(base_days * (1 + access_count) ** growth,
min_days, max_days)`. Pinned rows and `protected_kinds` are never archived,
and `temp`/`expires_at` expiry remains a separate state.

Archiving sets `archived_at` rather than changing `status`, so the existing
`CHECK` constraint and history stay intact. Archived rows are excluded from
context and search by default. `search(..., include_archived=True)` (and the
tool's `include_archived` argument) returns them with a score penalty, an
`archived` label, and a reason; an explicit retrieval also restores the row
and records the revision. The CLI (`--archived`, `--all`, `--archive`,
`--restore`, `--pin`, `--unpin`), the API, and the web memory panel expose the
same state and usage counts.

The `search_knowledge` tool lets the model retrieve beyond the initial
context — bounded results from memories and older messages, with
provenance; `include_archived` opts into archived rows, which are labelled
and restored by that explicit retrieval. The `read_artifact` tool reads
bounded artifacts by path, with traversal blocked.

### Consolidation

Manual (`python -m harness consolidate`) or scheduled
(`consolidation.schedule_interval_s`). One bounded model pass over recent
messages and active memories proposes create/update/merge/retract changes
as strict JSON; the harness validates each change and applies it through
the memory manager so every change carries provenance
(`consolidation:<run_id>`), a revision, and a line in a markdown change
log under `artifacts/consolidation/`. A failed pass changes nothing.
A merge folds a duplicate into one statement (the fuller text, or the
model's single-sentence replacement); it never concatenates both texts,
and changes that glue facts together with `Also:` are rejected and logged.

### Approvals and quick confirmation

Confirm-risk actions (MCP tools by default, `create_domain`) create an
approval bound to the exact tool and arguments, record an
awaiting-approval execution row, and pause the run. The user decides via
the web panel, the CLI, or a short chat reply. Short replies are resolved
by deterministic keywords ("yes", "approve", "go ahead", "no", "reject",
"cancel", ...) or, for short non-obvious wording, a tiny constrained model
call that classifies affirmative/negative/ambiguous and nothing else.
Ambiguous replies never approve. With more than one pending approval, a
reply resolves one only when it also names that action (for example
`approve Logs`); otherwise the assistant asks which action is meant.
Approvals expire after `approvals.expires_after_s` (default one week; set
`0` to disable). Approving executes exactly the bound action through the
normal executor and records the real outcome; a rejected or cancelled
approval can never be re-approved, and an approval whose run moved on is
refused as stale.

### Scheduling and heartbeat

Schedules: `once` (at a time), `interval` (every N seconds), `daily`, and
`weekly` (time-of-day and weekday). Times use the host's **system timezone**
by default; each schedule stores either `system` or an explicit IANA zone.
An `at_ts` without an offset is interpreted in that schedule's zone, and all
stored timestamps remain UTC. Occurrence rows keyed by (schedule id, due
time) are recorded before submission, making submission idempotent across
restarts and duplicate ticks. Missed slots while the process was down are coalesced (policy
`run_once`: the most recent missed slot runs once, older ones are recorded
skipped) or discarded (`skip`). Once-schedules disable themselves after
their run. Scheduled prompts are submitted as ordinary tasks to the
ordinary runtime with the same tools, permissions, approvals, and context.

The heartbeat (one scheduler thread in the server process, tick interval
`scheduler.tick_interval_s`) submits due schedules, resumes interrupted
runs (configurable), retries model-failure pauses with exponential backoff
within a retry budget, sweeps expired approvals, temp memories, and the
deterministic memory-lifetime archive, reminds once about pending approvals,
and runs scheduled consolidation. Background
model work is bounded per tick (`scheduler.max_runs_per_tick`,
`scheduler.max_retries_per_tick`). Permanent protocol failures wait for a
human unless `scheduler.retry_permanent` is enabled, and no retry starts
behind an active user (`scheduler.user_activity_grace_s`). It never
auto-resumes limit-paused tasks. Overlapping ticks are prevented with a
non-blocking lock; quiet ticks record nothing in chat.

### Inbox and automations

The inbox is the durable wake-up contract; there is no mailbox or messaging
adapter in the core process. Any external pusher can call:

```http
POST /api/inbox
Authorization: Bearer <server.token>
Content-Type: application/json

{"source":"mail","external_id":"42","title":"Invoice due",
 "body":"...","kind":"message","severity":"info","meta":{}}
```

`(source, external_id)` is the idempotency key: a repeated insert returns the
original id with `duplicate: true`. Bodies are bounded by
`inbox.max_body_chars`; malformed or oversized events are rejected as `400`
or `413`, never truncated. Stored events always have `trust: untrusted`.

Triage runs inside the heartbeat, before due schedules, and is deterministic:

1. a future Phase 7 guard hook may reject the event;
2. an enabled automation matches by source, kind, keywords, and/or regex;
3. events no rule owns are deduped by identical source/kind/title/body
   within `inbox.dedupe_window_days`;
4. quiet hours leave events `new` for a later tick;
5. the rule's cooldown and hourly cap are enforced;
6. the action runs: `task` creates an `inbox:<source>` task, `notify` writes
   one visible system note, `ignore` records the decision.

Hitting an automation's hourly cap disables that rule, records
`automation.disabled`, and writes exactly one visible note. Inbox-derived
runs are seeded with `tainted_sources`, so after external content enters the
run any side-effecting tool is escalated to confirm-risk by the same Phase 1
taint rule; read-only work continues.

Rules are managed with `POST /api/automations`, `PATCH
/api/automations/{id}`, and `POST /api/automations/{id}/enable|disable|delete`
(or `python -m harness automations --add JSON`). Inbox events are listed and
ignored with `GET /api/inbox`, `POST /api/inbox/{id}/ignore`, and
`POST /api/inbox/triage` (or `python -m harness inbox`). See
`examples/inbox_curl.sh` and `integrations/README.md` for the pusher
contract.

Self-scheduling uses the same scheduler but adds durable bounds:
`scheduler.max_schedules_per_task` (default 5) and
`scheduler.max_schedule_depth` (default 2). The limits are enforced in
`Scheduler.create_for_task`, not only in the tool handler, so an API caller
cannot bypass them. A schedule created by a task records `parent_task_id` in
its meta; the task it later submits carries `schedule_depth`, which is how a
chain is stopped. Sub-agents cannot create schedules.

### Recovery

On startup the harness deterministically converts crash debris: in-flight
tool executions become `unknown` (the effect may have happened; never
reported as success), running runs with a pending approval become
waiting_approval, any other running run becomes `interrupted` with its task
paused. The heartbeat then resumes interrupted runs once. Resumption
replays the persisted transcript, so completed actions are not repeated.
Unknown external outcomes are surfaced to the model and the user for
reconciliation rather than blindly retried.

## Tools, permissions, modules, and skills

Built-in tools (all low-risk unless noted): `remember`, `update_memory`,
`retract_memory`, `search_knowledge`, `read_artifact`,
`update_task_notes`, `use_domain`, `delegate_task`, `create_domain`
(confirm-risk), `schedule_task` / `cancel_schedule` (confirm-risk; bounded
to schedules the task owns), `propose_skill` (confirm-risk; saves a draft
only after approval), and `edit_skill` (confirm-risk; refines only skills
the agent created).

There is no shell, command, or code-execution tool, and the registry
refuses to register any tool whose name starts with bash/sh/exec/run/
python/eval and similar, no matter where the configuration comes from.

Risk classes are enforced by the harness, not the model: `low` runs
immediately; `confirm` requires an approval. MCP tools default to
confirm-risk unless the manifest sets `autoapprove: true`;
`tools.risk_overrides` can override individual tools. Tool outputs and
skills are untrusted data: they can never change risk classes or bypass
approvals (regression-tested).

**Capability modules.** MCP servers and the shipped web module are database
rows (`modules`, `module_bindings`, `module_tools`) owned by
`harness/modules/`. Each row has a kind, status, scope, manifest,
provenance, version, health, and cached discovery. `scope=global` is
always eligible; `scope=domain` requires an enabled binding for the task's
domain. A draft, disabled, or error module is never disclosed and never
callable. Reloading a module changes the registered toolset without a
process restart. Each run keeps a capability snapshot, so a reload cannot
silently change the tools mid-run; an explicit `use_domain` change
recomputes once and records `toolset.changed`.

**MCP servers.** Set `tools.mcp_servers_file` to a YAML file listing stdio
servers (see `examples/mcp_servers.yaml`). The file is an import source:
entries become module rows on first load and are never overwritten by later
edits. Manifest fields include `transport` (stdio), `command`, `args`,
`env_names`, `domains`, `autoapprove`, `risk_default`, `tools_allow`,
`tools_deny`, `read_only`, `side_effects`, `startup` (`eager`/`lazy`), and
`timeout_s`:

```yaml
servers:
  - name: example_echo
    command: python
    args: [examples/mcp/echo_server.py]
    autoapprove: true
    read_only: true             # echo cannot change external state
    # domains: [dom_example]    # domain-bound instead of global
    # tools_allow: [echo, add]
    # startup: lazy             # default; use cached discovery
```

Servers are launched per call for isolation; a server that fails to start
is reported as unavailable and never blocks turns. Discovery is cached in
`module_tools`; the heartbeat refreshes enabled modules every
`modules.health_interval_s`. The client speaks the MCP stdio JSON-RPC
protocol directly (initialize, tools/list, tools/call) with no heavy SDK
dependency.

Manage modules from the API or CLI:

```bash
python -m harness modules                  # status, health, tool counts
python -m harness modules --reload         # reload every module
python -m harness modules --reload mcp:example_echo
python -m harness modules --disable web
python -m harness modules --enable web
```

The same actions are available as `GET /api/modules`,
`POST /api/modules/reload`, and
`POST /api/modules/{id}/reload|enable|disable`.

**Web search.** The shipped `web` module is a small stdio MCP server that
queries SearXNG's JSON API and returns bounded `{title, url, snippet}`
results. Configure it with `web.searxng_url` (verified against
`http://192.168.50.40:8080`). Results are marked `untrusted_output`, so
they taint the run: after web content enters a run, any side-effecting
tool is escalated to confirm-risk and the approval names the source URL.
Read-only tools stay low-risk. `web_fetch` is deliberately not provided
(SSRF and injection surface).

> Port note: the harness also defaults to port 8080. Do not run it and
> SearXNG on the same host unless you change `HARNESS_PORT` or
> `server.port` for the harness.

**Trust and taint.** `ToolSpec` carries `side_effects`, `read_only`, and
`untrusted_output`. MCP output is untrusted by default; builtin memory and
history output is trusted. An untrusted result appends a bounded entry to
`runs.meta.tainted_sources` (`tool`, `module`, `origin`, `at`). In a
tainted run, a tool with `side_effects=True` becomes confirm-risk even if
it was configured low or auto-approved. A module must opt into
`read_only` explicitly; unknown provenance is treated as side-effecting.
The Phase 7 guard can add friction on top of this policy, never remove it.

**Sub-agents.** `delegate_task(goal, context?, expected_output?)` creates a
child task with `tasks.role='subagent'`, `tasks.parent_task_id`, and
`tasks.parent_run_id`; the child run records `runs.parent_run_id`. The child
gets its own system prompt built from the goal, the parent-supplied context,
and domain memories — never the parent transcript. Child messages are stored
but hidden from the ordinary chat view, and the child tree appears in the
parent's task detail.

The child toolset is low-risk only: `delegate_task`, `create_domain`, and
every confirm-risk tool are excluded from its schema snapshot and refused by
name. If the child needs an approval-gated action, asks a question, or hits
a budget, its result is `blocked`, the child is closed, and the parent
continues normally. The child result is written to
`artifacts/subagents/<task_id>.md` and returned in the parent's tool result.
Re-delegating the same normalized goal reuses the existing child by
`origin_ref = parent_task_id:goal_hash` instead of spawning duplicates.
Crash recovery is the normal one: an in-flight delegation becomes `unknown`
for the parent and is never retried blindly.

Depth is one; parallel children are deliberately deferred. Budgets live
under `subagents.*` and are recorded on the child task's meta.

**Skills.** Set `tools.skills_dir` to a folder of `<name>/SKILL.md` files
with optional front matter (`name`, `description`, `preconditions`,
`domains`). Imported skills are `kind=skill` module rows; the directory is
an import source, and an existing row is authoritative.

A skill is instruction text only — it is disclosed in the context when it
applies, and every action it describes still goes through the normal tools
and permissions. Skills never register tools, schemas, or code.

- `scope=global` is always disclosed.
- `scope=domain` needs an enabled binding for the task's domain; matching
  preconditions gate it further.
- `scope=adaptive` is disclosed when its preconditions match the objective.
- `status=draft` and `status=disabled` are never disclosed. Every exclusion
  is recorded as a `skill:<id>` context source with a reason.

Lifecycle is `draft -> active -> disabled`. Bodies are versioned under
`modules/skills/<id>/v<n>.md`; every change appends a revision, and
rollback restores the previous versioned body and lifecycle state.

The model can call `propose_skill` (confirm-risk) with a name, description,
preconditions, domains, and body. The approval shows the full body bounded
by `skills.max_body_chars`; approval writes `status=draft` and never
activates it. A run that consumed untrusted content cannot propose a skill
(`skill.refused`). The heartbeat may also propose at most one draft after
`skills.draft_after_successful_tasks` completed tool-using tasks, subject
to `skills.max_drafts_per_day`; tainted runs are refused, and a failed pass
changes no skill. Builder logs are written under `artifacts/skills/`.

The agent may refine skills it created with `edit_skill` (confirm-risk):
it names the existing skill and supplies any of description, preconditions,
domains, or a replacement body; omitted fields stay unchanged. Approval
writes a new draft version, so an active skill becomes a draft until the
user activates it again. Agent ownership is established only when the model
or the builder creates the row (`origin=agent`, `created_by=model|builder`).
Imported skills and skills created by the user through the UI/API remain
read-only to the agent; `propose_skill` cannot overwrite them by reusing
their name. Users can still revise any skill through the UI.

While a skill create/edit request is pending, the same run may send another
`propose_skill` or `edit_skill` for that name to refine the pending request
in place. The approval keeps its original tool and id, and its arguments and
the awaiting execution record are updated to the latest version, so the user
makes one decision about what is actually shown. No second approval is
created and nothing is written before that decision; a same-name skill write
from a different run is still denied until the existing decision is made.

```bash
python -m harness skills                    # status, scope, versions
python -m harness skills --activate ID      # draft/disabled -> active
python -m harness skills --disable ID
python -m harness skills --rollback ID      # restore previous version
```

The same actions are available as `GET /api/skills` and
`POST /api/modules/{id}/activate|disable|rollback|reload`.

## Configuration

Settings live in `data/config.yaml` (created with defaults on first start)
and are validated at startup: unknown keys fail fast. Environment
overrides: `HARNESS_DATA_DIR`, `HARNESS_HOST`, `HARNESS_PORT`,
`HARNESS_TOKEN`, `HARNESS_MODEL_BASE_URL`, `HARNESS_MODEL_NAME`,
`HARNESS_MODEL_API_KEY`, `HARNESS_TIMEZONE`, `HARNESS_WEB_ENABLED`,
`HARNESS_WEB_SEARXNG_URL`.

Timestamps are stored in UTC and shown in the host's local time. The
`timezone` setting defaults to `system`; set an IANA name only to override
it. In Docker, set `TZ` (or `HARNESS_TIMEZONE`) so the container's system
clock matches your zone. Configs still carrying the old hard-coded default
(`America/Mexico_City`) are migrated to `system` on the next start.

Main groups:

- `model.*`: endpoint, model name, api key, paths, timeout, retries,
  max_tokens, temperature, `enable_thinking`, `context_window`,
  `empty_retry_max_tokens`
- `server.*`: host, port, access token
- `limits.*`: iteration/tool/time budgets per run, tool result size
- `context.*`: token budgets, reserved output and tool-schema headroom,
  recent-message window, memory and history retrieval counts, per-section
  budgets
- `domains.*`: routing threshold, ambiguity margins
- `memory.*`: result limits, temp expiry, and `memory.lifetime.*` (enabled,
  base/growth/min/max days, protected kinds, sweep interval and batch)
- `tools.*`: MCP servers import file, skills dir, default MCP risk, risk
  overrides
- `modules.*`: enabled, module directory, health interval, tool-schema
  budget, default MCP risk, lazy startup
- `skills.*`: enabled, auto-draft, successful-task threshold, daily draft
  cap, maximum body characters
- `subagents.*`: enabled, max depth, children per run, child iteration/
  tool-call/seconds budgets, result size, and the reserved
  `allow_confirm_risk` flag (always false in v1: children stay low-risk)
- `web.*`: enabled, `searxng_url`, `max_results`, `timeout_s`,
  `categories`, `safe_search`
- `scheduler.*`: enabled, tick interval, missed policy and grace,
  interrupted-run resume, per-tick run/retry caps, retry cooldown and
  exponential backoff, permanent-failure policy, user-activity grace,
  approval reminders, `max_schedules_per_task`, `max_schedule_depth`
- `inbox.*`: enabled, body limit, events examined per tick, dedupe window,
  quiet hours (`"22:00-07:00"`, local time; empty disables)
- `automations.*`: default action when no rule matches, hourly cap
- `approvals.*`: expiry window, classifier token budget and thinking
- `consolidation.*`: schedule interval, bounds
- `persona.*`: name, speaker labels, purpose, principles, style, response
  preferences — the entire visible personality is here, not in code
- `timezone`: `system` (default; use the host clock) or an IANA name to
  override; `logging.*`

Changing the persona or budgets takes effect on the next run; endpoint and
server changes need a restart. The web UI shows the redacted configuration
and can update whole editable groups.

## Observability

Everything important is inspectable without exposing chain-of-thought:

- `event_log` rows for routing, tasks, runs, model calls, tool outcomes,
  approvals, schedules, heartbeat actions, module load/reload/health,
  trust taint/escalation, sub-agent started/completed/blocked/reused,
  recovery, consolidation
- `tool_executions` rows: status (including `unknown`), arguments,
  bounded results, errors, approval links, durations
- `runs` rows: status, decision ("why it stopped"), iterations, tool-call
  count, context sources with reasons
- `memory_revisions` rows: before/after, changed-by, reason
- `memory_access` rows: which memory was disclosed to which run, and when
- The CLI `events`, `tasks ID`, and `memories` commands and the web UI's
  grouped panels read this state directly; every view is rendered from an API
  response and the browser holds nothing of its own. Panel mutations (memory
  edit/retract, skill draft/delete, module bindings, domain edits, schedule and
  automation changes) all go through the same authenticated endpoints as the
  CLI. The task drawer exposes each run's persisted model transcript — the
  fully built system prompt, the user turn, assistant/tool turns, tool
  results — plus the context sources that fed it. Sub-agent tasks can be shown
  in the task list and expanded recursively with their own runs and
  transcripts.

Structured logs go to stdout and `data/logs/harness.log`.

## Testing

```bash
python -m pytest tests -o addopts="" -q     # deterministic suite (no network)
python -m pytest tests -m live -o addopts="" # real-endpoint integration tests
```

### Scenario evaluation

`harness eval` runs scenario files from `eval/scenarios/` in-process against a
scratch data directory. It never touches the deployed `data/` directory or the
repository (the sandbox refuses both). See `eval/README.md` for the scenario
and assertion reference.

```bash
# See what would run (live-only scenarios appear only in live mode).
python -m harness eval --list
python -m harness eval --mode live --list

# Deterministic: ScriptedModel exercises the harness itself, no network.
python -m harness eval --mode deterministic
python -m harness eval --mode deterministic --scenario "b_memory_*" --tag memory

# Live: the real runtime against a named model. Reports land in
# data/eval/results/<tag>.json and .md.
python -m harness eval --mode live --model MODEL_NAME \
    --base-url http://HOST:PORT --api-key KEY --tag live_MODEL
```

Live runs probe `/v1/models` first and refuse to start unless the requested
model is advertised. Use `--allow-unlisted-model` for a server that serves the
model without listing it. `--list-models` prints what the endpoint offers:

```bash
python -m harness eval --list-models --base-url http://HOST:PORT
```

Model settings resolve in this order (lowest to highest): built-in defaults,
`data/config.yaml` (or `--config-from DIR`), `HARNESS_MODEL_NAME` /
`HARNESS_MODEL_BASE_URL` / `HARNESS_MODEL_API_KEY`, then explicit
`--model` / `--base-url` / `--api-key`. So this works too:

```bash
$env:HARNESS_MODEL_NAME = "MODEL_NAME"
python -m harness eval --mode live --base-url http://HOST:PORT --tag live_MODEL
```

Useful follow-ups:

```bash
python -m harness eval --summary live_MODEL
python -m harness eval --compare deterministic final_deterministic
```

Deterministic scenarios assert persisted state (tasks, runs, memories,
approvals, schedules, inbox events, modules, tool executions, and the actual
run system prompt). Live scenarios (`eval/scenarios/g_live.yaml`) are smoke and
capability checks: a live result measures that model, not harness regressions.
Reports include per-assertion expected/observed evidence and real denominators.

The deterministic test suite covers the required categories: core runtime,
unified history, domains/resumption, context/memory, scheduling/heartbeat,
approvals/permissions, recovery/deployment, memory lifetime/archive,
sequential sub-agents, durable inbox/automations/self-scheduling, plus the
ten end-to-end acceptance scenarios (preference recall, interrupted multi-step resume,
scheduled approval pause/resume, heartbeat-initiated work, old-history
grounding, web+CLI shared state, inspectable failures, traceable
consolidation, no duplicate occurrences after restart, no indirect
permission escape). Live tests verify the endpoint's models list, plain
completion, native tool-call protocol, and multi-step round trip; they are
marked `live`. The live scenario suite (`eval/scenarios/g_live.yaml`) checks a
named model through the real runtime (plain reply, memory write, memory recall,
and `search_knowledge`); it was verified against `qwen3.5-4b` with all four
scenarios passing.

## Known limitations

- Single user, single process per data directory; no accounts, no TLS.
  Put the token-protected UI behind a VPN or a TLS reverse proxy.
- Domain routing and task resumption are deterministic keyword scoring,
  not semantic understanding; paraphrases without shared words may
  misroute to General (recoverable — the user just says which domain).
- Retrieval is FTS5 keyword search; no embeddings or vector database.
  Deep paraphrase recall can miss.
- Scheduling is process-bound: due work is noticed on a tick while
  `serve` (or a CLI-driven heartbeat) runs. Nothing is lost when it stops.
- The inbox has no bundled mailbox, IMAP, Telegram, or Matrix adapter. Those
  are separate pushers outside the core process; the durable contract is the
  authenticated API and its bounded retry/idempotency rules.
- The quick-confirm classifier is conservative by design; anything
  non-obvious is ambiguous and becomes a normal conversation turn.
- Tool results are bounded by size, and MCP servers pay process startup
  latency per call. Discovery is cached; calls still launch one process.
- Web search covers SearXNG results only. `web_fetch`/browsing is deferred
  because it adds SSRF and injection surface.
- Sub-agents are sequential and depth-one. Parallel children would require
  per-run locks, a worker pool, and concurrent approval semantics, so they
  are deferred until sequential delegation is measured to be too slow.
- The harness and SearXNG both default to port 8080; use a different
  `HARNESS_PORT` (or `server.port`) when they share a host.
- The waiting-for-user marker is an instructed convention; if a model
  forgets it, a question may complete its task and the next message opens
  new work (recoverable, not a safety issue).
- No automatic migration from the v1 file-based prototype; v2 starts a
  fresh database beside the old files.

## Security notes

- Treat the UI as a private service: strong token, localhost or trusted
  LAN/VPN only.
- The harness sends no credentials to the model endpoint unless you
  configure `model.api_key`.
- Skills and tool/MCP results are data, never instructions; they cannot
  change permissions. Confirm-risk actions always need an explicit approval.
- MCP output is untrusted by default. After web or MCP content enters a run,
  any side-effecting tool is escalated to confirm-risk and the approval
  names the untrusted source. Mark a module `read_only: true` only when it
  genuinely cannot change external state.
- Inbox events are untrusted by definition. A run derived from one starts
  tainted, so no inbox text can trigger an outbound side effect without the
  same explicit approval; automations can schedule work but cannot widen
  permissions.
- Backups contain the full conversation and memory state; protect them.



