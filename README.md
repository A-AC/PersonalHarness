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
- **Domains with a high creation threshold.** A domain is a durable work
  environment with routing evidence, instructions, memories, skills, and
  optional bound tools. New messages are routed automatically to the domain
  the evidence points to; unmatched input lands in General. General is
  unscoped rather than a memory silo. Domains are created only by explicit
  user command or by a model proposal that the user approves.
- **Memory with provenance.** The model saves durable facts, preferences,
  decisions, procedures, constraints, and questions through memory tools.
  Retrieval is FTS5 keyword search with scope priority, optional cross-domain
  lexical boost, and recency, explainable per result. Corrections supersede,
  updates create revisions, consolidation merges duplicates and expires stale
  rows with a readable change log.
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
- **Untrusted output stays data.** MCP reads (web search, calendar) are
  low-risk but their output is untrusted: it enters the transcript inside a
  nonce-delimited envelope, taints the run, and forces later side-effecting
  actions to confirm-risk. A tainted memory write needs an approval that
  names the untrusted origin. The bundled web server normalises text, drops
  non-http(s) URLs, flags injection-shaped snippets, labels results as data,
  and caps the query length; inbox content follows the same rule.
- **Approvals that behave like a conversation.** Confirm-risk tool calls
  (external MCP actions by default, domain creation, memory writes in a
  tainted run) become approvals bound to the exact tool and arguments. The
  chat shows an inline card and the Approvals panel is a tray with
  approve-all-safe. A pending approval can be approved, rejected, changed
  with free-text feedback (the run resumes with `revision_requested` and the
  model re-proposes), or edited field-by-field and executed with the edited
  arguments. Short chat replies resolve exactly one approval: "yes"/"no" by
  keyword or a tiny constrained model call, anything else as revision
  feedback when it clearly refers to the pending action. Ambiguous replies
  never approve. A rejection is remembered for the run, so an identical
  re-proposal is denied without a second approval.
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
- **Install, reset, and doctor.** A first-run web wizard writes the token,
  model endpoint (with a live probe and model pick), timezone, and optional
  modules; `harness init --non-interactive` does the same from `HARNESS_*`
  environment variables so the container entrypoint can bootstrap itself.
  `harness doctor` checks Python, the data volume, the endpoint, the port,
  the writer lock, module health, and disk. `harness reset` backs up first,
  then wipes the runtime state.
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
| Usage-based domain suggestions | `harness/suggestions.py` |
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

- Python 3.12+ (native path), or Docker 24+ (Compose v2 is optional)
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

Each provider has one OpenAI-compatible endpoint. `providers` is a list of
`{id, base_url, api_key_env, models}`; `model.provider` selects one. The
built-in `default` provider is synthesized from `model.base_url`/`model.api_key`,
so existing single-endpoint configs keep working. `model.allowed` narrows the
advertised names. A switch is explicit (by the user or a future router), is
recorded as a `model.changed` event, and never falls back. Every run records
`model` and `provider` in `runs.meta`; a run in flight keeps its snapshot model,
while resuming after an explicit switch uses the new one and records the
change. `GET /api/models` lists the catalogue with provider reachability and the
configured context window; `POST /api/model {name, provider?}` validates and
applies the selection without a restart. The header picker and Settings card
are the UI. An unreachable provider is reported rather than silently used, and
a name is only accepted while unverified when a configured allowlist names it.

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

Open `http://127.0.0.1:8080/` and complete the first-run wizard. To skip
the wizard, write the same values headlessly **before** starting `serve`:

```bash
export HARNESS_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export HARNESS_MODEL_BASE_URL=http://192.168.50.40:8181
export HARNESS_MODEL_NAME=qwen3.5-9b
python -m harness init --non-interactive --data-dir ./data
```

The wizard is only served while setup is incomplete; after it finishes, `/`
is the normal token-protected UI. A config created by an older release is
treated as configured and keeps its existing token. `/health` is always
unauthenticated.

In a second terminal, talk to the same runtime:

```bash
python -m harness msg "Say the single word: ready"
python -m harness status
```

The CLI is an HTTP client of the running server — it never opens the
database or runs its own model loop.

## Deployment (Docker)

One container runs the web UI, API, agent runtime, scheduler, heartbeat,
memory, and context management. SQLite and artifacts live in one mounted
volume; the model endpoint is external. Setup from zero needs no hand-edited
files and no Compose file.

### Compose-free `docker run`

```bash
docker build --build-arg GIT_SHA=$(git rev-parse --short HEAD) \
  -t personal-harness:2.0.0 .

docker run -d --name harness \
  -p 8080:8080 \
  -v harness-data:/data \
  -e HARNESS_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  -e HARNESS_MODEL_BASE_URL=http://192.168.50.40:8181 \
  -e HARNESS_MODEL_NAME=qwen3.5-9b \
  -e HARNESS_TIMEZONE=system \
  --restart unless-stopped \
  personal-harness:2.0.0

curl -fsS http://127.0.0.1:8080/health      # {"status":"ok"}
```

The image entrypoint runs `python -m harness init --non-interactive` on a
fresh volume when `HARNESS_TOKEN` is present (with
`HARNESS_MODEL_BASE_URL`, as in the example), so those values become
`data/config.yaml` without a manual edit. Without `HARNESS_TOKEN` the
container starts unconfigured and `http://127.0.0.1:8080/` serves the
first-run setup wizard: set the token, probe and pick the model endpoint,
choose the timezone, and optionally install the bundled web/calendar
modules. The setup endpoints are unauthenticated only until the first
successful setup and return `409` afterwards — open them on a trusted
network. `harness init --non-interactive --force` rewrites an existing
config deliberately.

### Compose (optional)

Compose is a convenient wrapper around the same image and volume:

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

### Staging and upgrades

Keep one writer per data directory. `docker-compose.stage.yml` documents a
separate Compose project, volume, port, and token for trying a build beside
production. On startup, a schema upgrade first writes
`backups/pre-migrate-v<old>-to-v<new>-<ts>.zip` and aborts if the backup
cannot be written. The authenticated `/api/system` endpoint and
`python -m harness status` report `{version, git_sha, schema_version}`;
`/health` stays unauthenticated and minimal. The full build, backup, stage,
promote, and rollback runbook is [docs/DEPLOY.md](docs/DEPLOY.md).

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

### Install, reset, and doctor

`harness init` writes the first `config.yaml`. Interactive mode prompts for
the token, endpoint, model, and timezone and probes the endpoint; the
non-interactive path reads the same values from the environment and does
not contact the endpoint unless `--probe` is given:

```bash
export HARNESS_TOKEN=...
export HARNESS_MODEL_BASE_URL=http://HOST:PORT
export HARNESS_MODEL_NAME=MODEL
export HARNESS_TIMEZONE=system       # optional; system is the default
python -m harness init --non-interactive
```

`harness doctor` runs preflight checks without a server and exits non-zero
on a hard failure:

```bash
python -m harness doctor          # or: python -m harness doctor --json
```

It checks the Python version; that the data directory is writable (it should
be writable by the container user, UID 10001); that the model endpoint is
reachable from this process/container and advertises the configured model;
whether the server port is free or already owned by a harness (a foreign
listener, such as SearXNG on 8080, is a warning); the `process.lock` writer
lock; stored module status; and free disk space. Warnings do not fail.

`harness reset` is destructive and requires `--yes`. It takes a backup first,
then removes the database, artifacts, and module runtime state, and (without
`--keep-config`) `config.yaml`, so the next start is a fresh install.
Backups, logs, and a data-root `mcp.json` import file are kept. It is an
authenticated call to the running server, which exits afterwards; the
container's `restart` policy brings it back:

```bash
python -m harness reset --yes                     # via the running server
python -m harness reset --yes --keep-config       # keep token/model/timezone
docker compose exec harness python -m harness reset --yes
```

On staging, deleting the `harness-stage-data` volume is the equivalent reset.

## CLI reference

Global options (`--data-dir`, `--url`, `--token`) may appear before or
after the command; the server URL defaults to `HARNESS_URL` or
`http://127.0.0.1:8080`, and the token defaults to `HARNESS_TOKEN` or the
value in `data/config.yaml`.

| Command | Purpose |
| --- | --- |
| `serve` | Run the application (web UI, API, scheduler, heartbeat) |
| `msg TEXT` | Send a message and print the response (`turn`/`say` aliases) |
| `status` | Open work, pending approvals, schedules, version/git/schema |
| `tasks [ID]` | List tasks, or show one task's runs, executions, context sources |
| `approvals [approve/reject/cancel] [ID]` | List or decide pending approvals |
| `schedules [--add JSON] [--action ID enable/disable/delete]` | Manage schedules |
| `inbox [--add JSON] [--status STATUS] [--ignore ID] [--triage]` | List, push, ignore, or triage durable inbox events |
| `automations [--add JSON] [--update ID JSON] [--action ID enable/disable/delete]` | Manage declarative inbox rules |
| `memories [--search Q] [--archived|--all] [--include-archived] [--archive ID] [--restore ID] [--pin ID] [--unpin ID]` | List, search, filter, or manage memory lifetime; reports usage counts and artifact problems |
| `domains [--create NAME DESCRIPTION] [--export-feedback [PATH]] [--suggestions]` | List or explicitly create domains, export routing feedback as `k_routing`-format scenarios, or list usage-based domain suggestions |
| `consolidate` | Run one memory consolidation pass |
| `events [-N]` | Recent event-log rows |
| `tools` | Registered tools with their risk classes; MCP status |
| `eval` | Run scenario evaluations in-process (deterministic or live); `--list`, `--list-models`, `--summary`, `--compare`, `--aggregate`, `--routing-bakeoff` |
| `backup` / `restore ZIP --yes` | Snapshot / restore state |
| `config` | Effective configuration (secrets redacted) |
| `models [--set NAME] [--provider ID]` | List provider/model choices or switch the active model |
| `endpoint-check` | Probe the configured model endpoint directly |
| `init [--non-interactive] [--force] [--probe]` | Write the first configuration from prompts or `HARNESS_*` env |
| `reset --yes [--keep-config]` | Back up, then wipe runtime state (server must be running) |
| `doctor [--json]` | Preflight checks without a server |

Example schedule creation (single quotes on POSIX shells):

```bash
python -m harness schedules --add '{"name":"morning-brief","prompt":"Summarize open work and anything due today.","kind":"daily","time_of_day":"08:30"}'
```

`domains --export-feedback [PATH]` writes the durable `routing_feedback` rows
as `k_routing`-format evaluation scenarios. The default output is
`eval/scenarios/k_routing_feedback.yaml`; `domains --suggestions` lists open
usage-based domain suggestions.

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

Domains are work environments. Routing evidence v2 scores a domain's name,
user-authored keywords, example phrases, and description, plus learned
vocabulary from its active memories, bound skills, and past task objectives.
Learned terms are deliberately weaker (0.5 each): explicit evidence wins, and
a single learned term can never select a domain by itself; chat/status
messages are not learned evidence.
Message and domain terms use the small deterministic stemmer when
`domains.stemming` is enabled. Each candidate keeps a per-term breakdown, and
`domain.routed` events preserve that evidence. The shipped
`domains.routing_threshold` and `domains.ambiguity_margin` are both `1.0`:
a clear specific-domain winner is selected, while weak or ambiguous evidence
falls back to General. Routing never creates a domain.

When routing falls back to General but a candidate scores at least
`domains.hint_min_score`, the context includes a bounded hint naming up to
two possible domains and their matched terms. The `use_domain` result carries
the entered domain's entry briefing: its description, user-authored
instructions, top memories for the task, applicable skills, and newly
available tools. The active domain's instructions are also included in
normal context. When domain eligibility hides tools from another domain, a
bounded discovery line names those tools without exposing their schemas.
A domain change records `domain.entered` and displays an "Entering
<domain>" chip in the chat; General does not display one.

The task drawer shows the selected domain, candidates, scores, matched terms,
hints, and any model tie-break. `POST /api/tasks/{id}/domain` corrects the
task's domain and records a `routing_feedback` label. It may return up to
three suggested keywords, but nothing is added to the domain automatically.
`POST /api/domains/route-preview` returns deterministic scores and breakdown
without creating a task, message, memory, or event.

`domains.model_routing` selects the optional tie-break behavior: `off`
(the default), `shadow`, or `on`. It runs only for weak evidence from trusted
input, using one bounded, tool-free label call. Shadow mode records the
choice without changing the route; invalid output, an unknown id, or a
timeout falls back to General. Inbox, automation, schedule, and other
untrusted text never reaches the classifier. A deterministic heartbeat also
looks for repeated vocabulary in General work and proposes domain suggestions.
Suggestions are never created automatically, and dismissed clusters are
remembered.

Resumption is preferred over new work: a message that plausibly continues
an open task (keyword overlap, recency, and a bonus for tasks waiting on
the user) resumes that task's run. When two candidates tie materially, the
harness creates a task whose context names the candidates so the assistant
asks which to continue instead of guessing.

Domains are created only by the explicit API/CLI command or by the model's
`create_domain` tool, which is confirm-risk: the exact name, description,
keywords, and examples become an approval, and nothing exists until the user
approves it.

### Semantic router (opt-in)

`routing.strategy` selects a pluggable router. The default `lexical` is the
unchanged control. The opt-in strategies are `embedding`, `llm`, and `hybrid`.
The older `domains.model_routing` tie-break still applies only to the lexical
control; semantic strategies return their own label and confidence.
Only user-typed messages are routed; inbox, automation, and schedule text keep
their configured domain. A router returns a label only: it never widens
permissions and is never injected into the transcript as instructions.

Every domain card contains its name, description, `examples`, and editable
`summary` (`summary_updated_at` records the last edit). The embedding strategy
sends the current message plus the last two user turns to an OpenAI-compatible
`/v1/embeddings` endpoint (`embeddings:` config, disabled by default), caches
packed float32 vectors in SQLite keyed by `sha256(model + text)`, and compares
domain-card vectors with stdlib cosine. `embeddings.card_examples: true` uses
the mean of the card text and its example vectors. The LLM strategy makes one
constrained, no-thinking JSON call (`routing.model`, default main model); low
confidence, invalid JSON, or an unknown id becomes General. Hybrid takes the
embedding top three and asks the LLM only when the margin is below
`routing.hybrid_margin`.

Any timeout, error, or invalid strategy output falls back to the lexical
decision and records `router.fallback`; a turn never fails because routing
failed. `routing.shadow_strategies` runs extra strategies alongside and records
their choices without acting. `domain.routed` and task metadata carry the
strategy, chosen domain, top three scores, confidence, reason, latency, inputs,
fallback, and shadow results.

```bash
# Plumbing-only deterministic bake-off (scripted fakes).
python -m harness eval --mode deterministic --routing-bakeoff     --scenario "k_routing*" --tag routing_bakeoff_deterministic

# Live LLM/lexical bake-off against the configured model endpoint.
python -m harness eval --mode live --routing-bakeoff     --scenario "k_routing*" --strategies lexical,llm --tag routing_bakeoff_live
```

To measure embedding/hybrid live, start an OpenAI-compatible embeddings
server and point `embeddings.base_url` at it from inside the harness container.
For example, a vLLM server exposes `/v1/embeddings`:

```bash
docker run --rm --gpus all -p 8081:8000 vllm/vllm-openai:latest   --model BAAI/bge-small-en-v1.5 --task embed
```

On Docker Desktop use `http://host.docker.internal:8081`; on Linux use the
host IP or a compose service name. `examples/config.routing.yaml` is a complete
routing/embeddings sample.

Current measurement state: the deterministic bake-off is a plumbing check
only (`eval/results/routing_bakeoff_deterministic.*`). A live five-case sample
(`routing_bakeoff_live.*`) measured lexical at 100% on its base case and 0% on
the held-out case, while the constrained LLM strategy scored 100% on both at
roughly 1.0?1.2 s per routed turn. The embedding endpoint was not running, so
`embedding` and `hybrid` are reported unavailable. The full 65-case live sweep
was not completed in this environment; the >=85% held-out hypothesis remains
unverified until it is run against a reachable embeddings endpoint.

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

`memory.cross_domain` controls memory sharing: `off` keeps the current
domain/global partition, `boost` (the default) also lets lexically matched
sibling-domain memories into normal context, and `search` leaves context
partitioned while making `search_knowledge` search every domain. Sibling
memories are admitted only on a lexical hit, never as no-hit filler, are
capped by `memory.cross_domain_max`, and are labelled `from: <domain>` with
their origin. Archived, retracted, expired, and inactive-domain rows never
cross over.

General has no memory silo. Legacy `domain:general` rows read as global, and
new memories saved from General are stored as global. Pinned global memories
form the always-on core profile, bounded by `memory.core_profile_chars`; the
Memory panel has a Core profile filter and editor for this set. Domain facts
remain domain-scoped, while durable facts about the user should be saved as
global.

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

Model-authored `remember` writes default to the task's domain, except
`preference` and `constraint` kinds, which default to `global` as facts about
the user. The model may explicitly widen to `global` or name the current
domain; a request for a different domain is narrowed back to the task domain. Provenance records `task_domain_id`, `task_domain`,
`scope_requested`, `scope_effective`, and `scope_choice`, so a global memory
still says which domain it came from. Consolidation and fixture writes are
manager-level operations without a task origin and keep their proposed scope;
General remains the unscoped interface and saves global memory.

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

Confirm-risk actions (MCP tools by default, `create_domain`, `edit_domain`)
create an approval bound to the exact tool and arguments, record an
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

A pending approval can also be changed instead of accepted or refused.
Free-text feedback (from the card, or from a chat reply that clearly refers
to the pending action) closes it as `revised`, hands the model
`{status: "revision_requested", feedback: ...}`, and lets it re-propose. A
structured edit re-validates the arguments, marks the old approval
`superseded`, records an `approval.edited` diff, and executes exactly the
edited arguments; the model sees what ran. Revised and superseded approvals
can never execute, and the chain is visible in the UI. An approval
authorizes one exact action only.

"Approve and always allow" is offered only for an editable, untainted,
non-side-effecting tool. It writes a per-tool `risk_override` to
`config.yaml`, is listed and revocable under Tools, and never weakens the
taint escalation.

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
(confirm-risk), `edit_domain` (confirm-risk; changes user-authored domain
instructions only after approval), `schedule_task` / `cancel_schedule`
(confirm-risk; bounded
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

**Capability modules.** Skills and MCP servers are database rows (`modules`,
`module_bindings`, `module_tools`) owned by `harness/modules/`. Each row has a
kind, status, scope, manifest, provenance, version, health, and cached
discovery. `scope=global` is always eligible; `scope=domain` requires an
enabled binding for the task's domain. A draft, disabled, or error module is
never disclosed and never callable. Reloading a module changes the registered
toolset without a process restart. Each run keeps a capability snapshot, so a
reload cannot silently change the tools mid-run; an explicit `use_domain`
change recomputes once and records `toolset.changed`.

**MCP servers.** The simplest install is to drop a standard `mcpServers`
file at `data/mcp.json` (YAML is accepted too):

```json
{
  "mcpServers": {
    "example_echo": {
      "type": "stdio",
      "command": "python",
      "args": ["examples/mcp/echo_server.py"],
      "autoapprove": true,
      "read_only": true,
      "tools_allow": ["echo", "add"]
    }
  }
}
```

`<data_dir>/mcp.json`, `mcp_servers.yaml`, and `mcp_servers.yml` are picked
up automatically when `tools.mcp_servers_file` is empty. To keep several
files or store one elsewhere, set `tools.mcp_servers_file` to its path. Both
the standard `mcpServers` mapping and the harness `servers:` list are
understood; see `examples/mcp.json` and `examples/mcp_servers.yaml`.

Entries become module rows (`id: mcp:<name>`) on first load. An explicit
reload re-reads the file and updates existing rows, so editing an entry needs
only:

```bash
python -m harness modules --reload
```

Manifest fields include `transport` (stdio), `command`, `args`, `env`,
`domains`, `autoapprove`, `risk_default`, `tools_allow`, `tools_deny`,
`tool_prefix`, `read_only`, `side_effects`, `startup` (`eager`/`lazy`), and
`timeout_s`. `${data_dir}` inside `command`/`args` expands to the absolute
data directory, so one entry works both locally and in Docker. Environment
variables named in `env` are forwarded from the harness process; explicit
values are written to a private sidecar (`modules/mcp/<name>.env.json`) and
never to SQLite.

Registered names default to `mcp_<server>_<tool>`. Set `tool_prefix: ""` when
a server should keep its own tool names. Server-specific settings — endpoints,
credentials, categories — belong in that server's own config file passed
through `args`; the harness never reads them. Two self-contained examples
ship in the repo: `harness/modules/web/server.py` (SearXNG search) and
`harness/modules/calendar/server.py` (CalDAV). Their config templates are
`web.example.json` and `calendar.example.json`; copy them to
`data/modules/mcp/` and point the entry at that path.

Servers are launched per call for isolation; a server that fails to start
is reported as unavailable and never blocks turns. Startup failures include
the server's stderr tail, so a bad config path or malformed JSON is visible in
`python -m harness modules`. Discovery is cached in `module_tools`; the
heartbeat refreshes enabled modules every `modules.health_interval_s`. The
client speaks the MCP stdio JSON-RPC protocol directly (initialize, tools/list,
tools/call) with no heavy SDK dependency.

Manage modules from the API or CLI:

```bash
python -m harness modules                  # status, health, tool counts
python -m harness modules --reload         # reload every module
python -m harness modules --reload mcp:example_echo
python -m harness modules --disable mcp:example_echo
python -m harness modules --enable mcp:example_echo
```

The same actions are available as `GET /api/modules`,
`POST /api/modules/reload`, and
`POST /api/modules/{id}/reload|enable|disable`.

The Modules panel can also add, edit, and remove stdio MCP servers, preview a
connection with **Test connection** before saving, and set a per-tool risk
override. Creating or installing a server requires explicit confirmation, and
new commands are restricted to `modules.command_allowlist` (`python`,
`python3`, `node`, `npx`, and `uvx` by default); a request may explicitly set
`allow_any_command` to override that restriction. The API exposes the same
operations at `POST /api/modules/mcp`, `PATCH|DELETE /api/modules/{id}`,
`POST /api/modules/test`, and `POST /api/modules/{id}/tool-risk`.

Bundled web and calendar templates are discovered from each server's
`config.schema.json`. Installing one writes the validated settings to
`data/modules/mcp/<id>.json`; explicit environment secrets are written only to
the private `data/modules/mcp/<id>.env.json` sidecar. Module API responses
return environment names and whether a value is set, never the value itself.

**Web search.** `harness/modules/web/server.py` is a self-contained stdio
MCP server that queries SearXNG's JSON API and returns bounded
`{title, url, snippet}` results. Copy `web.example.json` to
`data/modules/mcp/web.json`, set `searxng_url`, and add the entry from
`examples/mcp.json`; `WEB_*` environment variables override the file when set.
The server validates the URL at startup, so a missing or malformed endpoint
shows up as an unavailable module instead of a surprise on the first call. See
`harness/modules/web/README.md`. Results are untrusted data, so they taint the
run: after web content enters a run, any side-effecting tool is escalated to
confirm-risk and the approval names the source URL. Read-only tools stay
low-risk. `web_fetch` is deliberately not provided (SSRF and injection
surface).

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
`HARNESS_MODEL_API_KEY`, `HARNESS_TIMEZONE`.

Timestamps are stored in UTC and shown in the host's local time. The
`timezone` setting defaults to `system`; set an IANA name only to override
it. In Docker, set `TZ` (or `HARNESS_TIMEZONE`) so the container's system
clock matches your zone. Configs still carrying the old hard-coded default
(`America/Mexico_City`) are migrated to `system` on the next start.

Main groups:

- `model.*`: endpoint, model name, selected `provider`, allowed model names,
  api key, paths, timeout, retries, max_tokens, temperature, `enable_thinking`,
  `context_window`, `empty_retry_max_tokens`
- `providers`: list of `{id, base_url, api_key_env, models}` provider records
- `server.*`: host, port, access token
- `limits.*`: iteration/tool/time budgets per run, tool result size
- `context.*`: token budgets, reserved output and tool-schema headroom,
  recent-message window, memory and history retrieval counts, per-section
  budgets
- `routing.*`: strategy (`lexical|embedding|llm|hybrid`, default `lexical`),
  shadow strategies, routing model, timeout, hybrid margin, recent-turn depth,
  and the domain-summary character bound. `embeddings.*`: enabled, base_url,
  model, path, timeout, minimum similarity, and the card-example variant
- `domains.*`: routing threshold and ambiguity margin (both `1.0`),
  task-resume margin, evidence weights (name `1.5`, keyword `1.0`, example
  `0.75`, description `0.5`, learned term `0.5`, learned-vocabulary cap
  `2.0`; learned-only routing requires at least two terms), stemming (`true`),
  `hint_min_score` (`0.5`), `briefing_chars` (`1800`),
  `instructions_max_chars` (`1200`), `model_routing`
  (`off|shadow|on`), `model_routing_timeout_s` (`3.0`), `status_line`
  (`true`), `suggestions.enabled` (`true`), `suggestions.min_tasks` (`6`),
  and `suggestions.window_days` (`30`). Domain
  instructions are user-authored; the agent can write them only through an
  approved `edit_domain`, and a tainted run is refused.
- `memory.*`: result limits, temp expiry, `cross_domain`
  (`off|boost|search`, default `boost`), `cross_domain_max` (`3`),
  `core_profile_chars` (`1200`), and `memory.lifetime.*` (enabled,
  base/growth/min/max days, protected kinds, sweep interval and batch)
- `tools.*`: MCP servers import file, skills dir, default MCP risk, risk
  overrides
- `modules.*`: enabled, module directory, health interval, tool-schema
  budget, default MCP risk, lazy startup, and the MCP command allowlist
- `skills.*`: enabled, auto-draft, successful-task threshold, daily draft
  cap, maximum body characters
- `subagents.*`: enabled, max depth, children per run, child iteration/
  tool-call/seconds budgets, result size, and the reserved
  `allow_confirm_risk` flag (always false in v1: children stay low-risk)
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
- `setup.*`: `complete`/`completed_at`, the first-run marker. Managed by
  the wizard and `harness init`, not editable through the config UI.

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
  count, context sources with reasons, and the model/provider attribution used
  by that run
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

Phase 10 adds `eval/scenarios/k_routing.yaml` with 65 labelled routing cases
and `eval/scenarios/k_memory_recall.yaml` for cross-domain recall. The
`routing` assertion reads the recorded `domain.routed` decision and can check
the domain, candidates, reason, and score. `scripts/route_sweep.py` measures
routing across threshold/margin grids, while
`scripts/memory_recall_report.py` compares the memory modes. Committed
reports under `eval/results/` include the Phase 10A baseline, the Phase 10B
one-turn sweep (85.25% accuracy on 61 cases and 0% false positives;
General-only control 27.87%), the semantic bake-off (lexical base 92.73% /
heldout 20.00%; LLM live sample 100% on both), and the Phase 10D memory result
(`boost` and explicit `search`: 100% recall@5). The memory result is from the deterministic corpus, not a
live-model or real-use measurement. Routing scenarios are marked
`measurement: true`: misses are reported but do not gate the eval command.

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

# Behavioral suite: five trials, then aggregate per-model distributions.
python -m harness eval --mode live --model MODEL --base-url http://HOST:PORT \
    --scenario "i_live_*" --repeats 5 --tag behavior_MODEL

# Held-out judgment suite: three trials.
python -m harness eval --mode live --model MODEL --base-url http://HOST:PORT \
    --scenario "j_live_*" --repeats 3 --tag holdout_MODEL

# Per-model distributions plus a per-scenario table for routing decisions.
python -m harness eval --aggregate behavior_MODEL_A behavior_MODEL_B \
    holdout_MODEL_A holdout_MODEL_B
python -m harness eval --compare behavior_MODEL_A behavior_MODEL_B
```

`--aggregate` groups reports by the model recorded inside each report and writes
`aggregate_<model>_vs_<model>.{json,md}`. It reports pass rates over every
repeat, rubric score distributions, per-criterion failure counts, model-only
latency (mean, median, spread), completion tokens per second, iterations, tool
calls, wall time, and a per-scenario comparison. Run live repeats sequentially;
concurrent runs share the endpoint and distort latency.

Deterministic scenarios assert persisted state (tasks, runs, memories,
approvals, schedules, inbox events, modules, tool executions, and the actual
run system prompt). The `h_agent_loops.yaml` suite adds sequential multi-tool
loops: result chaining, denied/invalid-tool recovery, an approval boundary,
budget exhaustion, and a deferred second action. It also verifies execution
order with `tools.sequence` and reads deferred calls from the run transcript.
Deterministic mode measures the harness, not the model.

The deterministic pytest suite covers core runtime, unified history,
domains/resumption, context/memory, scheduling/heartbeat,
approvals/permissions, recovery/deployment, install/setup/reset/doctor,
memory lifetime/archive, sequential sub-agents,
durable inbox/automations/self-scheduling, and the ten end-to-end acceptance
scenarios.

Live scenarios are split into `g_live.yaml` (single-turn smoke and capability
checks), `i_live_behavior.yaml` (behavioral coverage), and
`j_live_holdout.yaml` (held-out judgment and instruction following). The
behavior suite exercises multi-turn continuity, memory correction, active-skill
following, delegation, hostile-inbox safety, approval-gated scheduling, and a
live `remember` → `search_knowledge` loop. Every behavior step carries
`iterations_max`/`tool_calls_max` runaway bounds so a loop becomes a named
failure instead of a slow pass. The holdout suite adds cases the tuned prompts
were not written against: admitting an unknown fact, surfacing a contradictory
request, constructing exact weekly schedule arguments, grounded two-sentence
summarisation, secret refusal, and a strict three-bullet format.

Action claims are checked against persisted state. Open-ended reply quality is
scored by a local deterministic rubric and reported separately from assertion
pass/fail; a low rubric score does not change scenario status. Pass rates and
rubric scores are different axes, and neither is a general model-quality
benchmark.

Reports include per-assertion expected/observed evidence, real denominators,
rubric scores, criterion pass rates, model latency, token throughput,
iterations, tool calls, and wall time. Model latency comes from the recorded
`model.call` round trip, so harness overhead and tool execution are not counted
as model speed. Per-step metrics are deltas, so a resumed approval step does not
re-count the call that created the approval. Compare models with multiple
repeats on the same suite and read the per-scenario rows rather than one total.

### Model choice: quality path and fast path

The measured recommendation for this harness, from 5 repeats of the behavior
suite plus 10 repeats of the holdout per model (95 scenario runs each):

| | `qwen3.5-9b` | `minicpm5-2B` |
| --- | ---: | ---: |
| Scenario pass | 95/95 (100%) | 93/95 (97.9%) |
| Assertion pass | 770/770 (100%) | 766/770 (99.5%) |
| Critical failures | 0 | 4 |
| Rubric mean / criteria | 91.3% / 91.4% | 91.4% / 91.4% |
| Model time per run | 10.4 s | 9.5 s |
| Completion tokens/s | 26.6 | 43.2 |
| Iterations / tool calls | 1.9 / 0.7 | 2.2 / 1.0 |

Use `qwen3.5-9b` as the default and quality path. It passed every scenario and
assertion, had no critical failures, and was faster on the cases where judgment
matters: hostile inbox 12.7 s vs 35.4 s, conflicting instruction 10.6 s vs
14.9 s, secret refusal 7.4 s vs 6.5 s with 10/10 vs 8/10 completions, and the
live multi-tool loop 9.2 s vs 12.2 s. Those are the paths where a wrong action
is expensive.

`minicpm5-2B` is a credible fast path for bounded, single-shot, low-risk work
with trusted input. It was materially faster on grounded summarisation (5.7 s vs
11.0 s), strict three-bullet formatting (2.2 s vs 8.0 s), weekly schedule
arguments (6.3 s vs 12.5 s), approval-gated scheduling (6.1 s vs 11.4 s),
delegation (8.9 s vs 16.9 s), and memory correction (10.8 s vs 17.1 s), at 43
tokens/s versus 27. Its behavior-suite rubric was 100% on those scenarios.

Do not route `minicpm5-2B` to untrusted or adversarial input. It missed the
`flags_untrusted_input` criterion in 4 of 5 hostile-inbox runs, took 35.4 s and
extra tool calls there, and in 2 of 10 secret-refusal runs went tool-hunting and
ended `waiting_user` instead of answering (the same pattern appeared in earlier
3- and 5-repeat runs, so treat it as roughly a one-in-five event, not noise). It
also triggered a runaway bound once while handling the contradictory request.
A router should send inbox triage, prompt/credential requests, and ambiguous
multi-action instructions to the quality path.

Both models share one real weakness: neither reliably states that a request is
contradictory (`flags_conflict` and `asks_clarification` failed 10/10 for both).
The runtime contains the action behind an approval, so this is a contained
quality gap and a good next prompt or policy improvement — not a reason to pick
one model over the other.

These are measurements of this suite, not a universal ranking. Re-run
`--aggregate` after changing prompts, tools, or the endpoint before moving a
scenario between paths.

## Known limitations

- Semantic routing is opt-in and measured separately. When no OpenAI-compatible
  embeddings endpoint is reachable, the `embedding`/`hybrid` bake-off rows are
  reported as unavailable rather than approximated. The deterministic bake-off
  uses scripted fakes and measures plumbing only. Domain summaries are editable
  but the optional automatic summary-refresh pass is deferred.

- Single user, single process per data directory; no accounts, no TLS.
  Put the token-protected UI behind a VPN or a TLS reverse proxy.
- The default domain router remains the deterministic lexical control;
  embedding, LLM, and hybrid strategies are opt-in and not enabled until their
  live bake-off is reviewed. Paraphrases without shared explicit or learned
  evidence may still fall back to General under the default (recoverable
  through task correction).
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
- An unconfigured instance serves the first-run wizard at `/` without
  authentication, including the ability to set the token. Start it on
  localhost or a trusted network, or bootstrap it with `harness init` and
  `HARNESS_TOKEN`. The setup endpoints return `409` once configured.
- Backups contain the full conversation and memory state; protect them.



