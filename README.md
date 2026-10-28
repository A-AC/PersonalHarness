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
  schedules with explicit timezones. Occurrences are recorded before
  submission, so restarts and duplicate ticks never re-run a completed
  occurrence. Missed runs follow a documented policy (run_once coalesces,
  skip discards).
- **Heartbeat.** A deterministic periodic scan that submits bounded work
  through the normal runtime: due schedules, interrupted runs, retryable
  model failures, one-time approval reminders, expiry sweeps. Quiet checks
  write events, not chat messages.
- **Approvals with a quick confirmation path.** Confirm-risk tool calls
  (external MCP actions by default, domain creation) become approvals bound
  to the exact tool and arguments. Short replies like "yes" or "no" are
  classified affirmatively/negatively/ambiguously — by keyword or a tiny
  constrained model call — and resolve exactly one approval. Ambiguous
  replies never approve.
- **Honest recovery.** In-flight tool executions interrupted by a crash
  become unknown, never success. Unknown outcomes are surfaced and
  reconciled instead of silently retried. Runs paused by limits stay paused
  until the user or scheduler resumes them deliberately.
- **Web UI + terminal CLI.** A polling chat page with panels for tasks,
  approvals, domains, persona, settings, schedules, memory, tools, events,
  and configuration; a CLI that talks to the same HTTP API. No second
  runtime, no separate history.

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
| Context builder | `harness/context.py` |
| Messages, events, domains, tasks | `harness/store.py` |
| Memory + artifacts | `harness/memory.py` |
| Tools, permissions, MCP | `harness/tools.py` |
| Approvals + quick confirm | `harness/approvals.py` |
| Schedules + heartbeat | `harness/scheduler.py` |
| Consolidation | `harness/consolidation.py` |
| SQLite schema | `harness/db.py` |
| Web server / JSON API | `harness/server.py`, `harness/web_page.py` |
| CLI client | `harness/__main__.py` |
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
API plus `config.yaml` and the artifacts directory, zipped into
`<data>/backups/harness-<timestamp>.zip`.

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
| `memories [--search Q]` | List or search memories; reports artifact problems |
| `domains [--create NAME DESCRIPTION]` | List or explicitly create domains |
| `consolidate` | Run one memory consolidation pass |
| `events [-N]` | Recent event-log rows |
| `tools` | Registered tools with their risk classes; MCP status |
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

The `search_knowledge` tool lets the model retrieve beyond the initial
context — bounded results from memories and older messages, with
provenance. The `read_artifact` tool reads bounded artifacts by path, with
traversal blocked.

### Consolidation

Manual (`python -m harness consolidate`) or scheduled
(`consolidation.schedule_interval_s`). One bounded model pass over recent
messages and active memories proposes create/update/merge/retract changes
as strict JSON; the harness validates each change and applies it through
the memory manager so every change carries provenance
(`consolidation:<run_id>`), a revision, and a line in a markdown change
log under `artifacts/consolidation/`. A failed pass changes nothing.

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
`weekly` (time-of-day and weekday), each with an explicit timezone.
Occurrence rows keyed by (schedule id, due time) are recorded before
submission, making submission idempotent across restarts and duplicate
ticks. Missed slots while the process was down are coalesced (policy
`run_once`: the most recent missed slot runs once, older ones are recorded
skipped) or discarded (`skip`). Once-schedules disable themselves after
their run. Scheduled prompts are submitted as ordinary tasks to the
ordinary runtime with the same tools, permissions, approvals, and context.

The heartbeat (one scheduler thread in the server process, tick interval
`scheduler.tick_interval_s`) submits due schedules, resumes interrupted
runs (configurable), retries model-failure pauses with exponential backoff
within a retry budget, sweeps expired approvals and temp memories, reminds
once about pending approvals, and runs scheduled consolidation. Background
model work is bounded per tick (`scheduler.max_runs_per_tick`,
`scheduler.max_retries_per_tick`). Permanent protocol failures wait for a
human unless `scheduler.retry_permanent` is enabled, and no retry starts
behind an active user (`scheduler.user_activity_grace_s`). It never
auto-resumes limit-paused tasks. Overlapping ticks are prevented with a
non-blocking lock; quiet ticks record nothing in chat.

### Recovery

On startup the harness deterministically converts crash debris: in-flight
tool executions become `unknown` (the effect may have happened; never
reported as success), running runs with a pending approval become
waiting_approval, any other running run becomes `interrupted` with its task
paused. The heartbeat then resumes interrupted runs once. Resumption
replays the persisted transcript, so completed actions are not repeated.
Unknown external outcomes are surfaced to the model and the user for
reconciliation rather than blindly retried.

## Tools, permissions, MCP, and skills

Built-in tools (all low-risk unless noted): `remember`, `update_memory`,
`retract_memory`, `search_knowledge`, `read_artifact`,
`update_task_notes`, and `create_domain` (confirm-risk).

There is no shell, command, or code-execution tool, and the registry
refuses to register any tool whose name starts with bash/sh/exec/run/
python/eval and similar, no matter where the configuration comes from.

Risk classes are enforced by the harness, not the model: `low` runs
immediately; `confirm` requires an approval. MCP tools default to
confirm-risk unless the server entry sets `autoapprove: true`;
`tools.risk_overrides` can override individual tools. Tool outputs and
skills are untrusted data: they can never change risk classes or bypass
approvals (regression-tested).

**MCP servers.** Set `tools.mcp_servers_file` to a YAML file listing stdio
servers (see `examples/mcp_servers.yaml`):

```yaml
servers:
  - name: example_echo
    command: python
    args: [examples/mcp/echo_server.py]
    autoapprove: true
```

Servers are launched per call for isolation; a server that fails to start
is reported as unavailable and never blocks turns. The client speaks the
MCP stdio JSON-RPC protocol directly (initialize, tools/list, tools/call)
with no heavy SDK dependency.

**Skills.** Set `tools.skills_dir` to a folder of
`<name>/SKILL.md` files with optional front matter (`name`, `description`,
`preconditions`). A skill is instruction text only — it is disclosed in
the context when the task objective matches its preconditions, and every
action it describes still goes through the normal tools and permissions.

## Configuration

Settings live in `data/config.yaml` (created with defaults on first start)
and are validated at startup: unknown keys fail fast. Environment
overrides: `HARNESS_DATA_DIR`, `HARNESS_HOST`, `HARNESS_PORT`,
`HARNESS_TOKEN`, `HARNESS_MODEL_BASE_URL`, `HARNESS_MODEL_NAME`,
`HARNESS_MODEL_API_KEY`, `HARNESS_TIMEZONE`.

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
- `memory.*`: result limits, temp expiry
- `tools.*`: MCP servers file, skills dir, default MCP risk, risk overrides
- `scheduler.*`: enabled, tick interval, missed policy and grace,
  interrupted-run resume, per-tick run/retry caps, retry cooldown and
  exponential backoff, permanent-failure policy, user-activity grace,
  approval reminders
- `approvals.*`: expiry window, classifier token budget and thinking
- `consolidation.*`: schedule interval, bounds
- `persona.*`: name, speaker labels, purpose, principles, style, response
  preferences — the entire visible personality is here, not in code
- `timezone`, `logging.*`

Changing the persona or budgets takes effect on the next run; endpoint and
server changes need a restart. The web UI shows the redacted configuration
and can update whole editable groups.

## Observability

Everything important is inspectable without exposing chain-of-thought:

- `event_log` rows for routing, tasks, runs, model calls, tool outcomes,
  approvals, schedules, heartbeat actions, recovery, consolidation
- `tool_executions` rows: status (including `unknown`), arguments,
  bounded results, errors, approval links, durations
- `runs` rows: status, decision ("why it stopped"), iterations, tool-call
  count, context sources with reasons
- `memory_revisions` rows: before/after, changed-by, reason
- The CLI `events`, `tasks ID`, and `memories` commands and the web UI's
  panels read this state directly; the browser holds nothing of its own.

Structured logs go to stdout and `data/logs/harness.log`.

## Testing

```bash
python -m pytest tests -o addopts="" -q     # deterministic suite (no network)
python -m pytest tests -m live -o addopts="" # real-endpoint integration tests
```

The deterministic suite covers the required categories: core runtime,
unified history, domains/resumption, context/memory, scheduling/heartbeat,
approvals/permissions, recovery/deployment, plus the ten end-to-end
acceptance scenarios (preference recall, interrupted multi-step resume,
scheduled approval pause/resume, heartbeat-initiated work, old-history
grounding, web+CLI shared state, inspectable failures, traceable
consolidation, no duplicate occurrences after restart, no indirect
permission escape). Live tests verify the endpoint's models list, plain
completion, native tool-call protocol, and multi-step round trip; they are
marked `live` and were run against the reachable endpoint
(2026-10-27, passing).

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
- The quick-confirm classifier is conservative by design; anything
  non-obvious is ambiguous and becomes a normal conversation turn.
- Tool results are bounded by size, and MCP servers pay process startup
  latency per call.
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
- Skills and MCP results are data, never instructions; they cannot change
  permissions. Confirm-risk actions always need an explicit approval.
- Backups contain the full conversation and memory state; protect them.



