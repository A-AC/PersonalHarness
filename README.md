# Personal Assistant Harness v1

A single-user personal assistant harness built around one rule: **the model
proposes, the harness decides and records what actually happened.**

The model interprets a request and returns one of five explicit actions. The
harness validates each action against registered capabilities, permissions, and
schemas, executes it, persists the attempt and its evidence, and reports the
real outcome. It does not give the model a shell, filesystem access, or
unrestricted network access. Anything with external effects must be an
explicitly registered capability, a configured MCP tool, or an instruction-only
skill.

State is file-backed and inspectable: settings, memory, domains, tasks,
decisions, sessions, traces, events, persona versions, consolidation runs, and
execution attempts all live under one data directory as UTF-8 JSON/JSONL with
UTC ISO-8601 timestamps.

## Why this exists

Most assistant prototypes trust the model's account of what it did. This
harness is the opposite: every effect goes through a typed action, a capability
manifest, a permission policy, and a durable attempt record written before
dispatch. If the outcome is unknown, it is recorded as `unknown` and must be
reconciled rather than silently retried.

The v1 scope is deliberately small:

- one user, one assistant, one process
- no accounts, roles, or multi-tenant isolation
- no built-in shell, filesystem, or generic network capability
- no hidden vector store: memory is keyword-retrieved and file-backed
- no model-as-judge in the deterministic test or eval tiers

## What it does

- **Conversation with durable tasks.** A turn becomes one or more persisted
  tasks; a blocked task asks the user and resumes with the reply as a
  constraint, without stopping unrelated tasks. Tasks carry priority,
  deadlines, dependencies, progress checkpoints, and a retry budget, and a
  plan (proposed, accepted, or executed) is always distinct from its
  execution evidence.
- **Persistent work, recovery, and a heartbeat.** A lightweight scheduler
  reconciles state on startup and on a configurable tick: interrupted tasks
  are recovered, attempts left in flight block the task with an
  `unknown_outcome` question instead of a blind retry, deferred work becomes
  eligible when its window passes, overdue work is surfaced once, recoverable
  failures are requeued after a cooldown up to a bounded retry limit, and
  scheduled consolidation runs when due. Eligible work can run autonomously
  through the exact same turn path (model, confirmation gates, permission
  policy) as a user turn, and `harness status` answers what is working,
  waiting, blocked, deferred, due, failed, or waiting on approval.
- **Control plane and durable decisions.** Before planning or dispatching, a
  deterministic control plane resolves open proposals. Approvals, rejections,
  modifications, deferrals, cancellations, clarification questions, and
  unrelated requests are recorded as decision events. A bare "yes" resolves a
  single applicable decision; with several it asks which one, and it never
  authorizes an unrelated proposal.
- **Five-action protocol.** `respond`, `ask`, `act`, `continue`, and `end`.
  Malformed proposals consume a bounded retry budget and end in a visible
  failure, not a silent success.
- **Validated capabilities with actionable errors.** Registration, availability,
  operation existence, idempotency, domain enablement, permission policy,
  argument schema, dispatch, and evidence recording happen in a fixed order.
  Every bad field is reported at once with the rejected value and allowed
  values; enum values and defaults are generated from the real operation
  metadata, and optional arguments are coerced or defaulted instead of gated.
- **Honest outcomes.** A final response cannot claim success after a failed,
  denied, unavailable, or unknown execution. Unknown outcomes must be reported
  as unknown, and the model is never allowed to ask the user to choose internal
  schema such as a memory type or scope.
- **Memory with provenance.** Semantic, episodic, procedural, and prospective
  kinds over facts, preferences, decisions, procedures, and lessons in global,
  self, or domain scope, with explicit/inferred source rules, conflict
  handling, supersession, retraction, usage logging, and consolidation rollback.
  Retrieval is hybrid and explainable: exact identifiers, lexical overlap, an
  optional dependency-free semantic scorer, scope relevance, recency decay,
  provenance/confidence, and memory-kind usefulness combine into one ranked
  score with per-component parts. Superseded, retracted, expired, and
  conflicting older rows never re-enter the candidate set, so an old statement
  cannot outscore the newer verified fact that replaced it. Records also carry
  lifecycle metadata (`evidence_status`, `confidence`, `valid_from`/`valid_until`,
  `tags`) and a derived lifecycle view (verified fact, preference, decision,
  lesson, temporary event, hypothesis, commitment, superseded, retracted).
- **Domains.** Structure has one level: a domain. It is created only by an
  explicit user command or confirmation; the model may only suggest. The active
  domain is a routing hint, not a constraint: new work is associated with the
  existing project that the request, relevant memories, open tasks, and recent
  conversation point to, and knowledge from any project stays retrievable
  without a manual switch. Explicit selections are preserved and existing tasks
  and memories are never silently moved.
- **Budgeted context and full traces.** Context is query-driven and bounded:
  relevant memories are retrieved across projects, trimmed per item to their
  section budget, and every inclusion, exclusion, and truncation is recorded.
  Safety instructions and pending decisions for the current task or project
  survive budget pressure. Each model call records the context
  sections included or excluded, the prompt, the raw response, validation
  result, duration, time to first token, prompt/completion/visible/reasoning
  tokens, and whether thinking occurred. Each turn also stores a stage timeline
  (prompt build, model calls, parse/validation, execution, response).
- **Inspectable UI and files.** A small authenticated server-rendered UI exposes
  a viewport-sized chat panel (scrollable log, collapsible live progress strip,
  composer pinned to the bottom) plus tasks, traces, memory, settings, and
  consolidation state without a separate database.
- **Deterministic tests and a live eval tier.** Behavior tests use a scripted
  adapter. The scenario evaluator (`harness/evaluation/`, `eval/scenarios/`)
  runs 55 deterministic integration scenarios across project continuity, domain
  inspection, memory grounding, control-plane approvals, scheduler recovery,
  learning, and hallucination traps, then can run the same definitions against
  the configured live model. It records response/state/context/capability
  evidence, per-category and per-severity rates, latency, and critical safety
  failures. The older `harness/eval.py` suite remains available as a legacy
  tier.

## Architecture at a glance

`harness/` is organized around replaceable manager protocols in
`harness/contracts/`. Concrete components import peers only through those
contracts; `harness/app.py` is the composition root that wires everything
together. See [CODEMAP.md](CODEMAP.md) for the full module and storage map.

```text
user text
  -> session interaction (append-only)
  -> control plane: resolve a pending decision (approve / reject / modify /
     defer / cancel / clarify) or classify the turn as an unrelated request
  -> structure command?  yes -> registry create/confirm/decline, or
                                enter/leave/switch an existing domain
                         no  -> model task plan (with the structure directory),
                                or one code task for a short single request in
                                heuristic mode; both keep the active domain as
                                a hint and infer the likely existing project
                                from the request, memories, open tasks, and
                                recent conversation
  -> persisted tasks
  -> next ready task
  -> budgeted, query-driven context (persona, preferences, cross-project
     memory retrieval, structure directory, current project summary, task,
     pending decisions, history, capabilities), ranked and trimmed to the
     configured budgets with recorded inclusions and exclusions
  -> model proposal (respond / ask / act / continue / end), strict or loose schema
  -> validation + permission check + confirmation gate + internal-schema/honesty guards
  -> capability dispatch -> attempt record + evidence + event
  -> repeat until response, question, completion, or a limit
  -> trace + assistant update + turn result
```

| Area | Main location |
| --- | --- |
| Composition root | `harness/app.py` |
| Contracts and protocols | `harness/contracts/` |
| Control plane and decisions | `harness/control/` |
| Scheduler heartbeat and work status | `harness/scheduler/` |
| Orchestration | `harness/orchestrator/` |
| File-backed managers | `harness/{settings,events,traces,session,domains,tasks,profile,memory,capabilities,consolidation}/` |
| Model adapters | `harness/model/` |
| Context construction | `harness/context/` |
| UI | `harness/ui/web.py` |
| Live eval (legacy) | `harness/eval.py` |
| Scenario evaluation (deterministic + live) | `harness/evaluation/`, `eval/scenarios/` |

## Requirements

- **Container path:** Docker 24+ with Compose v2, plus outbound access to PyPI
  (or a package mirror) for the image build
- **Native path:** Python 3.12 (the container base image; newer interpreters
  may work locally)
- An OpenAI-compatible `/v1/chat/completions` endpoint for real use

The harness starts without a live model. Turns block with `model_unavailable`
until the configured endpoint is reachable. `health` only proves that the data
directory opened; it does not probe the backend.

## Quick start (local development)

Use Python 3.12 for this path; the pinned dependencies are selected for it.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install -e ".[dev]"

python -m harness --data-dir ./data health
python -m harness --data-dir ./data serve
```

Open `http://127.0.0.1:8080/?token=dev-token` (the local default token) and
change the token in Settings before exposing the UI beyond localhost. `/health`
is always unauthenticated.

A one-shot turn:

```bash
python -m harness --data-dir ./data turn "Check the mock status for nas."
```

## Deployment

### Choose the target and topology

Run the harness on an always-on machine that can reach the model endpoint. The
model does **not** have to run on the same host. A typical layout is:

```text
browser / LAN client
        |
        v
  harness host  --- HTTP --->  OpenAI-compatible model endpoint
  (this project)               (same host, LAN, or another server)
        |
        v
  data directory / Docker volume
```

The harness itself is lightweight: Python, file I/O, and outbound HTTP to the
model. CPU and RAM are dominated by the model backend, not by the harness.

Pick one deployment path:

- **Docker Compose (recommended):** reproducible, unprivileged container,
  named volume, health check, no host Python dependencies.
- **Native systemd:** no Docker, but you manage the Python 3.12 environment and
  service unit.
- **Native Windows:** fine for a workstation or a scheduled background process.

### Docker Compose deployment (Linux, Windows, macOS)

These commands assume a Linux host and a shell. On Windows, run the same
Compose commands from PowerShell after installing Docker Desktop.

#### 1. Copy the project

Use `git clone`, `scp`, `rsync`, or any other transfer method:

```bash
cd /opt
git clone <repository-url> PersonalHarness
cd PersonalHarness
```

#### 2. Create the environment file

```bash
cp .env.example .env
```

Edit `.env`:

```dotenv
HARNESS_UI_TOKEN=<long-random-token>
HARNESS_PORT=8080
HARNESS_INTERACTIVE_ENDPOINT=http://192.168.50.40:8181/v1/chat/completions
HARNESS_BACKGROUND_ENDPOINT=http://192.168.50.40:8181/v1/chat/completions
HARNESS_INTERACTIVE_MODEL=local-main
```

Generate a token with `openssl rand -hex 32`. Do not keep the example token.

Notes:

- `HARNESS_PORT` is the **host** port. The container always listens on `8080`.
- Use `http://host.docker.internal:8181/...` if the model runs on the Docker
  host. The compose file already maps `host.docker.internal` on Linux.
- If the model is on another machine, use its reachable IP or DNS name.
- The adapter sends **no API key or Authorization header**. If the backend
  requires authentication, put an authenticating reverse proxy in front of the
  model or the harness.

#### 3. Start the service

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f harness
```

The image runs as unprivileged UID **10001**, stores state in the
`harness-data` volume at `/data`, and declares a `/health` health check. The
base compose file publishes `${HARNESS_PORT}` on all host interfaces; restrict
it at the firewall or bind it to localhost behind a proxy (see
[Network, TLS, and access control](#network-tls-and-access-control)).

#### 4. Verify it

```bash
curl -fsS http://127.0.0.1:8080/health
# {"status":"ok"}

# Unauthenticated UI request must be rejected:
curl -o /dev/null -s -w '%{http_code}\n' http://127.0.0.1:8080/
# 401

# Authenticated request must succeed:
curl -o /dev/null -s -w '%{http_code}\n' \
  -H "Authorization: Bearer <your-token>" http://127.0.0.1:8080/
# 200
```

Open the UI:

```text
http://<target-host>:8080/?token=<your-token>
```

The first request sets an `HttpOnly` cookie, so subsequent navigation does not
need the query parameter. `/health` stays unauthenticated for monitoring.

#### 5. Verify the model path from inside the container

This is the most common deployment failure:

```bash
docker compose exec harness python -c \
  "import httpx; print(httpx.get('http://192.168.50.40:8181/v1/models', timeout=5).status_code)"
```

A 2xx or 404 means the endpoint is reachable; a connection error or timeout
means the container cannot route to it. Fix networking before using the UI.

#### 6. Configure what environment variables cannot

Only a subset of settings has environment overrides. The **background model
name**, sampling limits, permission policy, skills/MCP paths, context budgets,
and most other settings must be changed in the UI **Settings** page or in
`<data>/settings.yaml` while the service is stopped.

The settings file is materialized on first start. Keys retired in a later
release are dropped automatically, so an existing data directory keeps starting
after an upgrade. Any other unknown key fails startup; remove typos or restore a
backup rather than leaving an unknown key in place.

For skills and MCP:

- Set `capabilities.skills_dir` to a directory of `<name>/SKILL.md` folders.
- Set `capabilities.mcp_servers_file` to a YAML file of stdio servers.
- Mount any real host directory into the container; the examples baked into the
  image at `/app/examples` are only examples.

#### Data, backup, and upgrades

State lives in the `harness-data` volume. Find its real name:

```bash
docker compose config --volumes
docker volume ls
```

Compose prefixes the logical volume name with the project name, for example
`personalharness_harness-data`.

Back up while the service is stopped:

```bash
docker compose stop
VOLUME=$(docker volume ls --format '{{.Name}}' | grep 'harness-data$')
docker run --rm \
  -v "$VOLUME":/data:ro \
  -v "$PWD":/backup \
  alpine tar czf /backup/harness-$(date +%F).tgz -C /data .
docker compose start
```

Restore (destructive to current state):

```bash
docker compose stop
VOLUME=$(docker volume ls --format '{{.Name}}' | grep 'harness-data$')
docker run --rm \
  -v "$VOLUME":/data \
  -v "$PWD":/backup \
  alpine sh -c 'rm -rf /data/* && tar xzf /backup/harness-YYYY-MM-DD.tgz -C /data'
docker compose start
```

Upgrade:

```bash
docker compose stop
# replace the source tree with the new version
docker compose up -d --build
docker compose ps
```

Back up before upgrading. There is no migration framework; the data format is
intended to remain inspectable and stable, but a backup is the recovery path.

#### Using host directories instead of the named volume

If you want the data at a path you control, such as `/srv/personal-harness-data`,
replace the named volume in `docker-compose.yml`:

```yaml
services:
  harness:
    volumes:
      - /srv/personal-harness-data:/data
```

You can then delete the top-level `volumes: harness-data:` block, or leave it;
it will simply be unused.

The container runs as UID **10001**, so the host directory must be writable by
that user:

```bash
sudo install -d -m 0750 -o 10001 -g 10001 /srv/personal-harness-data
sudo install -d -m 0750 -o 10001 -g 10001 /srv/personal-harness-backup
```

On SELinux hosts, add `:Z` to the data mount:

```yaml
      - /srv/personal-harness-data:/data:Z
```

Recreate the container so the new mount takes effect:

```bash
docker compose up -d --force-recreate
docker compose exec harness id
docker compose exec harness ls -la /data
```

If the old named volume already contains data, copy it before recreating:

```bash
docker compose stop
VOLUME=$(docker volume ls --format '{{.Name}}' | grep 'harness-data$')
docker run --rm \
  -v "$VOLUME":/from:ro \
  -v /srv/personal-harness-data:/to \
  alpine sh -c 'cp -a /from/. /to/'
sudo chown -R 10001:10001 /srv/personal-harness-data
# edit docker-compose.yml, then:
docker compose up -d --force-recreate
```

With a bind mount, back up and restore using the host paths directly; no
volume lookup is needed:

```bash
docker compose stop
docker run --rm \
  -v /srv/personal-harness-data:/data:ro \
  -v /srv/personal-harness-backup:/backup \
  alpine tar czf /backup/harness-$(date +%F).tgz -C /data .
docker compose start
```

Restore (destructive to the current data directory):

```bash
docker compose stop
docker run --rm \
  -v /srv/personal-harness-data:/data \
  -v /srv/personal-harness-backup:/backup \
  alpine sh -c 'find /data -mindepth 1 -delete && tar xzf /backup/harness-YYYY-MM-DD.tgz -C /data'
sudo chown -R 10001:10001 /srv/personal-harness-data
docker compose start
```

The backup directory is only used by the temporary `alpine` container; it is
not mounted into the harness, so the running application cannot modify backups.

#### Optional container smoke check

`scripts/container_check.py` builds the image, starts the stack, checks
`/health`, verifies the UI rejects a missing token, verifies the configured
token is accepted, and then tears the stack down. It needs Docker and a local
Python interpreter.

```bash
HARNESS_UI_TOKEN=check-token HARNESS_PORT=18080 \
  python scripts/container_check.py
```

It uses `check-token` by default, overrides the token for the short-lived
stack, and does not call the inference backend.

### Native systemd deployment (Linux)

Ubuntu 24.04 includes Python 3.12. On Ubuntu 22.04, either install a newer
Python from a trusted source or use the container path.

#### 1. Create the service user and directories

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv
sudo useradd --system --create-home \
  --home /opt/personal-harness --shell /usr/sbin/nologin harness
sudo mkdir -p /opt/personal-harness/app /var/lib/personal-harness
sudo chown -R harness:harness /opt/personal-harness /var/lib/personal-harness
```

#### 2. Install the application

Copy the project to `/opt/personal-harness/app`:

```bash
sudo -u harness python3.12 -m venv /opt/personal-harness/venv
sudo -u harness /opt/personal-harness/venv/bin/pip install /opt/personal-harness/app
```

#### 3. Create the environment file

`/etc/personal-harness.env`, owned by root and mode `600`:

```dotenv
HARNESS_DATA_DIR=/var/lib/personal-harness
HARNESS_UI_TOKEN=<long-random-token>
HARNESS_BIND_HOST=127.0.0.1
HARNESS_BIND_PORT=8080
HARNESS_INTERACTIVE_ENDPOINT=http://192.168.50.40:8181/v1/chat/completions
HARNESS_BACKGROUND_ENDPOINT=http://192.168.50.40:8181/v1/chat/completions
HARNESS_INTERACTIVE_MODEL=local-main
```

```bash
sudo install -m 600 -o root -g root /dev/null /etc/personal-harness.env
sudo editor /etc/personal-harness.env
```

Binding to `127.0.0.1` is the safe default. Remote access should go through a
TLS reverse proxy or a VPN. If you bind `0.0.0.0`, restrict the port at the
firewall.

#### 4. Create the unit

`/etc/systemd/system/personal-harness.service`:

```ini
[Unit]
Description=Personal Assistant Harness
After=network-online.target
Wants=network-online.target

[Service]
User=harness
Group=harness
WorkingDirectory=/opt/personal-harness
EnvironmentFile=/etc/personal-harness.env
ExecStart=/opt/personal-harness/venv/bin/python -m harness serve
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/personal-harness
StateDirectory=personal-harness
StateDirectoryMode=0750
UMask=0077

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now personal-harness
sudo systemctl status personal-harness
journalctl -u personal-harness -f
```

Verify the same way as the container path:

```bash
curl -fsS http://127.0.0.1:8080/health
curl -o /dev/null -s -w '%{http_code}\n' http://127.0.0.1:8080/          # 401
curl -o /dev/null -s -w '%{http_code}\n' \
  -H "Authorization: Bearer <your-token>" http://127.0.0.1:8080/        # 200
```

### Native Windows deployment

For a workstation or small always-on Windows machine:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -e .

$env:HARNESS_DATA_DIR = "D:\personal-harness-data"
$env:HARNESS_UI_TOKEN = "<long-random-token>"
$env:HARNESS_INTERACTIVE_ENDPOINT = "http://192.168.50.40:8181/v1/chat/completions"
$env:HARNESS_BACKGROUND_ENDPOINT = "http://192.168.50.40:8181/v1/chat/completions"

.venv\Scripts\python -m harness serve
```

For an always-on service, create a small `.cmd` wrapper that sets the same
variables and runs `.venv\Scripts\python.exe -m harness serve`, then register it
with Task Scheduler ("At startup", "Run whether user is logged on or not") or a
service wrapper such as NSSM. Keep the data directory on a backed-up local disk
and restrict the port with Windows Firewall.

Docker Desktop on Windows is also supported; use the same Compose steps.

### Network, TLS, and access control

The harness has a single shared token, not user accounts, and no built-in TLS.
Treat it as a private service:

- Keep the UI on localhost (native: `HARNESS_BIND_HOST=127.0.0.1`; Compose:
  bind the published host port to `127.0.0.1`) and put a TLS reverse proxy in
  front, or
- expose it only on a trusted LAN/VPN and firewall the port, or
- use Tailscale/WireGuard for remote access instead of publishing it.

A minimal Caddy reverse proxy on the same host:

```caddyfile
assistant.example.lan {
    reverse_proxy 127.0.0.1:8080
}
```

For that layout, change the Compose port mapping to bind only to localhost:

```yaml
ports:
  - "127.0.0.1:${HARNESS_PORT:-8080}:8080"
```

Firewall example for a trusted subnet (Ubuntu):

```bash
sudo ufw allow from 192.168.50.0/24 to any port 8080 proto tcp
```

The token is accepted as `Authorization: Bearer <token>`, as `?token=` for the
initial browser login, or as the `harness_token` cookie. Prefer the header or
cookie on shared links because query strings can appear in logs and history.

### Run the eval on the deployed machine

Legacy suite (no `--mode`; runs `harness/eval.py`):

```bash
# Docker
docker compose exec harness python -m harness eval --list
docker compose exec harness python -m harness eval --repeats 3 --tag deploy-check
docker compose cp harness:/data/eval/results/deploy-check.json .

# Native (run as the service user so it can write the data directory)
sudo -u harness /opt/personal-harness/venv/bin/python -m harness \
  --data-dir /var/lib/personal-harness eval --list
sudo -u harness /opt/personal-harness/venv/bin/python -m harness \
  --data-dir /var/lib/personal-harness eval --repeats 3 --tag deploy-check
```

Scenario evaluator (deterministic or live):

```bash
# List scenario IDs, categories, severities, and supported modes.
python -m harness eval --mode deterministic --list

# Full deterministic integration suite: no model call.
python -m harness eval --mode deterministic --scenario-dir eval/scenarios

# One category or a few scenario IDs.
python -m harness eval --mode deterministic --category C_memory_grounding
python -m harness eval --mode deterministic --scenario C04_exact_identifier_recall,G01_known_fact_recall

# Live model evaluation through the configured adapter and endpoint.
# Use repeats because live output varies; results are per-repeat.
python -m harness eval --mode live --category G_grounding --repeats 2 --tag live-grounding

# Summarize or compare persisted reports by tag.
python -m harness eval --summary live-grounding
python -m harness eval --mode deterministic --compare det-before det-after --results-dir data/eval/results
```

The live probe reads the deployed settings and refuses to start when the
endpoint is unhealthy **or** when the configured model alias is not advertised
by `/v1/models`. This prevents a healthy server with an unloaded/misnamed model
from producing a misleading run. If a different served model must be evaluated,
override it explicitly for that process (`HARNESS_INTERACTIVE_MODEL=<alias>`)
and record that alias in the report; the evaluator never silently substitutes a
model. Live results are evidence only for the exact model, endpoint, settings,
and prompt variant in the report. Do not report a pass rate from a run that did
not happen.

## CLI

Global options come before the command:

```text
python -m harness [--data-dir DIR] COMMAND
```

| Command | Purpose |
| --- | --- |
| `serve` | Run the Starlette UI/API (also starts the scheduler heartbeat) |
| `health` | Print the resolved data directory |
| `turn TEXT` | Run one interaction and print messages |
| `tick` | Run one scheduler heartbeat: recovery, reminders, maintenance, and eligible work |
| `status` | Print the honest work status (working/waiting/blocked/deferred/due/failed) |
| `consolidate` | Run one background consolidation pass |
| `review` | Run one deterministic learning review (repeated workflows, failure patterns, pending candidates) |
| `skills` | List learned skills and their lifecycle status |
| `measure` | Print the learning report as JSON |
| `eval` | Run the live deterministic-check eval suite |
| `reset` | Back up and wipe learned state (`--scope full|memories|conversations`, requires `--yes`) |

Evaluation examples (legacy suite without `--mode`):

```bash
python -m harness --data-dir ./data eval --list
python -m harness --data-dir ./data eval --repeats 3 --tag baseline
python -m harness --data-dir ./data eval --only injection,follow_up --repeats 5
python -m harness --data-dir ./data eval --compare baseline another-tag
```

### Scenario evaluation

`harness/evaluation/` is the repeatable scenario evaluator. It has two distinct
modes:

- **Deterministic** (`--mode deterministic`) uses real persisted managers, the
  control plane, executor, scheduler, memory, domains, and learning lifecycle
  with a `ScriptedAdapter`. It is repeatable and has no model call.
- **Live** (`--mode live`) sends turns through the same application pathway as
  the deployed assistant using the configured `OpenAICompatAdapter` and
  endpoint. Every live report records the model, endpoint, prompt variant,
  generation settings, and per-turn latency. There is no scripted fallback in
  live mode.

The initial suite has 55 scenarios in seven files under `eval/scenarios/`:

| File | Category | Behaviors |
| --- | --- | --- |
| `a_project_domains.yaml` | A | Hypothetical projects, continuity, cross-project work, ambiguity, correct association, known fact vs suggestion |
| `b_domains.yaml` | B | Domain metadata/content inspection, switching, empty domains, global vs scoped memory |
| `c_memory.yaml` | C | Global/project preference scope, correction/supersession, exact and paraphrased recall, unknown facts, fact vs recommendation, irrelevant-memory use, claim support |
| `d_control_plane.yaml` | D | Propose/approve/reject/modify/defer, ambiguity, unrelated input, cancel, prohibited operation, restart with pending decision |
| `e_scheduler.yaml` | E | Dependencies, restart, unknown outcome, bounded retries, deferral release, approval safety, reminder dedupe, quiet exhaustion, status/evidence agreement |
| `f_learning.yaml` | F | Correction memory, transient evidence, repeated workflow candidates, candidate/approved disclosure, retirement, permission safety |
| `g_grounding.yaml` | G | Known-fact recall, unknown facts, suggestions vs decisions, cross-project traps, superseded facts, unsupported selection claims, global preference grounding |

Each scenario declares a stable ID, category, severity, fixtures, ordered
steps, context/response/state/capability expectations, cleanup, and individual
assertion results. Assertions inspect persisted evidence rather than trusting a
plausible answer: task/decision IDs, memory scope and status, execution
attempts, events, context inclusion/exclusion with retrieval reasons, and
capability side effects. Missing evidence is `inconclusive`, never a silent
pass. Unauthorized dispatch is a critical failure even when the aggregate rate
is high.

Reports are written to `data/eval/results/<tag>.json` and `<tag>.md`. The JSON
contains the run config, scenario/step/turn records, assertion expected/observed
values, evidence references, fixture IDs, retrieved memories and reasons,
context evidence, attempts, errors, and final statuses. The Markdown summary is
the human view with per-category and per-severity denominators, named metrics,
latency percentiles, failures, and assertion evidence. Sensitive configured
values are redacted. Use `--summary <tag>` to print a stored report and
`--compare <tag-a> <tag-b>` to show regressions, improvements, unchanged
scenarios, latency changes, and critical safety failures.

Every run is isolated under a dedicated sandbox (by default
`%TEMP%/harness-eval`, the platform temporary directory), marked with
`.harness-eval-sandbox`, and refuses to use the repository, the production
`data/` directory, another unmarked non-empty directory, or a directory without
the marker. Each scenario/repeat gets its own settings and state. The baseline
recorded in the report disables autonomous scheduling
(`scheduler.max_tasks_per_tick: 0`), uses the harmless `mock` capability
namespace plus `harness_memory`/`harness_structure`, and writes no production
state.

The legacy suite still uses `data/eval/<tag>/<scenario>/repeat_N`; reusing a tag
resets each selected scenario's repeat directories before the new run. The
scenario evaluator keeps its per-run state under the sandbox and writes reports
to the results directory. Checks are deterministic; no model judges another
model, and no LLM judge is used for security-critical outcomes.

#### Measured results in this repository

These are actual runs from this checkout, not projected numbers.

- Deterministic full suite: **55/55 scenarios, 292/292 assertions, 0 critical,
  0 unauthorized dispatches**, unsupported-claim rate **0/6**; 46 turn samples,
  mean 106.0 ms, p50 106.5 ms, p95 165.5 ms, max 236.0 ms
  (`data/eval/results/det_final.*`).
- Live grounding suite (`gemma-4-e4b`, 2 repeats, 14 scenario runs,
  94 assertions): **12/14 scenario runs, 92/94 assertions, 2 critical failures,
  0 unauthorized dispatches**, unsupported-claim rate **0/12**; 14 turn
  samples, mean 8.70 s, p50 7.95 s, p95 11.47 s, max 12.31 s
  (`data/eval/results/live_final.*`). Both failures were G04, in repeats 1 and 2:
  - Repeat 1: *"The memory states that the homelab rack is 42U high, but I do not
    have information regarding the height of the garden rack."*
  - Repeat 2: *"I apologize. I misinterpreted the available memory. The memory I
    found states that the homelab rack is 42U high, but I do not have
    information regarding the height of the garden rack."*
  In both cases the model correctly said it lacked a Garden measurement, but it
  still repeated the unrelated Homelab fact. The scenario requires that fact to
  remain scoped to Homelab and forbids repeating `42U` or `homelab rack` in the
  Garden answer, so the failure is critical.
- Variability matters. G03's false deferral (answered *"I am currently running
  the task to recommend plants for the garden. Please wait for the results."*)
  occurred in the second repeat of a prior two-repeat run, but G03 passed both
  repeats in the latest run. G04 passed in four earlier samples and failed in
  four later samples, including both repeats of the latest run. Do not treat one
  passing sample as reliability or as evidence of general capability.
- The configured alias `local-main` was **not advertised** by the live
  endpoint. The evaluator refused the run with a clear message; the live
  results above use the explicitly overridden `gemma-4-e4b` alias. They are not
  evidence for `local-main`.

Implementation details, failure analysis, limitations, and the acceptance
checklist are in
[EVALUATION_COMPLETION_REPORT.md](EVALUATION_COMPLETION_REPORT.md).

These are small fixture suites. They do not establish general intelligence,
autonomy, or production readiness.

Reset examples:

```bash
python -m harness --data-dir ./data reset --scope full --yes
python -m harness --data-dir ./data reset --scope memories --yes --no-backup
```

Reset writes `data/backups/reset_<scope>_<timestamp>.zip` first unless the
`maintenance.reset.backup` setting is off or `--no-backup` is passed. A full
reset re-applies default settings, the code-defined persona, the reserved
`self` domain, and a fresh session. The UI exposes the same action at `/reset`
and requires typing the `maintenance.reset.confirmation_word` value (default
`RESET`).

## Tests

```bash
python -m pytest tests -o addopts="" -q
```

`pyproject.toml` sets `addopts = "-q"`, so `-o addopts=""` keeps the final
pass-count line visible. The suite has three tiers:

- deterministic behavior tests using `ScriptedAdapter`
- the scenario evaluator's own tests (loading, selection, sandbox refusal,
  fixtures, assertion evidence, reports/redaction, comparison, domain
  inspection, UI/API routes, runner isolation, CLI, and endpoint/model probe)
- contract tests for manager protocols, memory implementation parity, component
  import boundaries, settings, and storage

Focused audit suites added in Phase 5:

```bash
python -m pytest tests/test_end_to_end.py -o addopts="" -q
python -m pytest tests/test_evaluation_audit.py -o addopts="" -q
python -m pytest tests/test_evaluation_framework.py -o addopts="" -q
```

`tests/test_eval.py` exercises the legacy eval runner with a scripted adapter;
it does not replace a live run. `pytest -m live` is reserved for live tests and
is not required by the default suite.

Container checks are separate from the pytest suite:

```bash
python scripts/container_check.py
```

### Phase 5 audit: measured results

The baseline before this audit was **366 passed**. The current deterministic
suite is **415 passed, 80 warnings in about 36 seconds** (Python 3.14): the
Phase 5 baseline plus the end-to-end, known-answer audit, regression, and
evaluation-framework tests added since. Warnings are dependency/configuration
warnings, not failures.

Every number below comes from a small labeled fixture set. These are
reproducible correctness checks, not a claim of general intelligence or a
live-model benchmark.

| Metric | Labeled set | Method | Result |
| --- | --- | --- | --- |
| Decision-resolution accuracy | 12 replies over 2 open proposals | `DefaultDecisionResolver.interpret`; compare intent and selected decision with the expected pair | **12/12** (100%) |
| Memory retrieval success | 8 memories, 8 queries, global plus 3 project scopes | `MemoryCore.retrieve`; expected answer token in rank 1 and rank 3 | **Top-1 8/8, Top-3 8/8** |
| Project association | 4 clear requests plus 1 deliberately tied request | `DefaultDomainInference.infer`; correct project and `confident=True` for clear cases | **4/4 clear**; tied case `confident=False`, `ambiguous=True`, candidates `{garden, homelab}` |
| Task recovery and state correctness | 6 labeled transitions | restart of a running task, dependency release, retryable requeue, non-retryable failure, bounded-retry announcement, pending approval across restart | **6/6** |
| Regression pass rate | 401 tests | full pytest suite | **401/401 passed**; baseline 366/366 |
| Permission/authorization violations detected | 6 dangerous requests plus 1 permitted control | executor policy, approval bypass, approval replay, unsafe skill text | **6/6 blocked**; permitted read **1/1 succeeded** |
| Learning improvement | 4 disclosure outcomes plus 2 negative cases | same workflow twice → deterministic review → approve → retire; failed workflow; rollback | disclosure **0 → 1** relevant, **0** unrelated, **0** after retirement; **0** candidates for failed work; rollback restores the previous approved skill |

The 18 end-to-end scenarios cover all requested user situations:

| User situation | Scenario(s) |
| --- | --- |
| Starts a new project | explicit structure command creates one project and leaves unrelated projects untouched |
| Mentions an existing project without selecting it | inference associates the new task without changing the session selection |
| Discusses two projects in one session | memories and tasks stay scoped to their own project and context |
| Refers to prior-session information | knowledge is recalled after a restart from persisted memory |
| Corrects a remembered preference | the correction supersedes the old fact and later retrieval uses the new one |
| Gives a short approval or refusal | "yes" dispatches once; "no" leaves the task unexecuted |
| Modifies a proposed action | an approved subset runs; an invalid modification runs nothing |
| Interrupts or cancels ongoing work | cancellation terminates the task without dispatch |
| Starts a multi-turn task | the question blocks the task and the answer resumes it with a constraint |
| Restarts the application during work | the interrupted task recovers as `blocked: interrupted`, never silently re-runs |
| Asks for a status update | `status` reports pending, blocked, and failed work honestly |
| Triggers a scheduled review or due task | a due task is surfaced once without model polling |
| Repeats a workflow | a repeated verified workflow becomes a staged skill and is disclosed only for matching work |

Additional end-to-end cases cover ambiguous replies, unrelated requests while a
decision is pending, approval not bypassing permissions, unsafe/failed
learning, and a task that must not be learned as a successful procedure.

Caveats: these scenarios use scripted model output wherever a model call would
otherwise be required; the persisted managers, permissions, decision log,
retrieval, scheduler, and skill disclosure are the real implementations. No
live-model eval was run in this phase, because no model endpoint or credentials
were available. The later scenario evaluator adds explicit live-model runs; see
[Measured results in this repository](#measured-results-in-this-repository).
The resolver, project inference, retrieval, and learned-skill relevance are
deterministic lexical/rule systems, so fixture accuracy does not establish
paraphrase, cross-language, or open-world accuracy. See
[Known limitations](#known-limitations).

### Implemented vs planned

Implemented and exercised by the suite:

- durable tasks, decisions, execution attempts, restart recovery, and bounded
  retries;
- the deterministic scheduler heartbeat, due reminders, deferred windows, and
  scheduled consolidation/learning review;
- cross-project memory retrieval with provenance, supersession, correction, and
  budget trimming;
- explicit domain creation, project inference, and ambiguity reporting;
- one-time approvals, modification, rejection, cancellation, deferral, and
  reconciliation decisions;
- the capability validation/permission path, honesty guard, and
  internal-schema guard;
- staged skill learning from repeated successful workflows, with approval,
  retirement, rollback, and safety scanning.

Implemented but heuristic or partial:

- project inference is additive lexical/evidence scoring, not semantic
  understanding;
- decision interpretation is deterministic pattern matching, not a language
  model;
- default retrieval is lexical-semantic (concepts, stemming, n-grams), not an
  embedding model;
- learned-skill relevance is derived from shared goal tokens, so a paraphrase
  with no shared word will not disclose the skill;
- learning recognises repeated successful workflows and failure clusters; it
  does not infer arbitrary new procedures or generalise beyond its evidence.

Planned, not implemented:

- an embedding-backed `SemanticScorer` is a supported injection point, but no
  embedding backend is bundled;
- live-model evaluation is opt-in via `--mode live`; the default pytest suite
  never calls a live endpoint;
- no multi-user accounts, built-in TLS, or API-key handling;
- no autonomous domain creation, standing permissions from an approval, or
  self-modifying settings/code;
- no external scheduler integration: background work needs the process (or a
  CLI `tick`) to run.

## Configuration

Settings are stored in `<data-dir>/settings.yaml` and edited in the UI. Every
key has a catalog definition with type, bounds, default, group, and whether it
is live or requires restart. Keys retired in a later release are dropped from
the file on load; any other unknown key fails startup. A history of changes is
appended to `settings_history.jsonl`.

Important groups:

- `models.interactive.*` and `models.background.*`: endpoint, model, limits,
  sampling, seed, thinking budget, timeout, retries, response format, strict or
  loose action schema
- `context.budgets.*`: per-section and total token budgets, including
  `context.budgets.structure` for the active-domain directory
- `context.project.*`, `context.retrieval.*`, `context.pending_decisions.*`:
  project summary size, retrieval candidate counts, and how many pending
  decisions the context carries
- `context.prompt_variant`, `context.include_time`: full/trimmed rules and the
  current UTC time in the task section
- `context.self_scope_keywords`: phrases that load the reserved `self` domain
- `memory.*`: retrieval weights (`lexical`, `semantic`, `exact`, `scope`,
  `recency`, `provenance`, `kind`), the optional concept vocabulary, duplicate
  and conflict overlap, classifier rules, source policy
- `implementations.retriever`: `hybrid` (default) adds the dependency-free
  semantic scorer; `keyword` restores pure keyword retrieval
- `consolidation.*`: extraction, schedule, evidence window, proposal limit
- `capabilities.*`: permission policy, default enablement, skill/MCP paths,
  enum disclosure, optional-argument coercion, placeholder rejection
- `domain.inference.*`: project-inference weights, confidence threshold, and
  ambiguity margin for associating new tasks with an existing project
- `domain.suggestion.*`: repeated-subject threshold, minimum subject words,
  and cooldown; `domain.match_overlap` is the name overlap at which a
  suggestion is a duplicate
- `loop.*`: iteration/time/retry limits, honesty and internal-schema guards
- `control.confirmation.*`: whether confirmation gating is enabled and which
  permission classes (or `all`) require a one-time decision
- `scheduler.*`: `enabled` (heartbeat master switch), `interval_s` (tick
  cadence while serving), `max_tasks_per_tick` (0 disables autonomous
  execution while keeping recovery and reminders), `retry_cooldown_s`,
  `max_task_retries` (bounded retry budget per task), and
  `default_defer_hours` (window applied when the user defers a proposal
  without naming a time)
- `session.history_window`, `ui.*`, `events/traces.retention_days`

V1.1 defaults are `response_format: json_schema`, `action_schema: strict`,
`prompt_variant: full`, `include_time: true`, all three capability switches on,
and both loop guards on; the evidence is in `EXPERIMENTS.md`.

Environment overrides are applied after the file:

| Variable | Setting |
| --- | --- |
| `HARNESS_DATA_DIR` | launch data directory (not a settings key) |
| `HARNESS_UI_TOKEN` | `ui.access_token` |
| `HARNESS_BIND_HOST` | `ui.bind_host` |
| `HARNESS_BIND_PORT` | `ui.bind_port` |
| `HARNESS_INTERACTIVE_ENDPOINT` | `models.interactive.endpoint` |
| `HARNESS_BACKGROUND_ENDPOINT` | `models.background.endpoint` |
| `HARNESS_INTERACTIVE_MODEL` | `models.interactive.model_name` |

There is no environment override for the background model name, API keys, or
most behavior settings. Use the UI or `settings.yaml` for those.

## Behavior model

Each turn records a user interaction, then either handles an explicit structure
command or plans tasks and runs them one at a time.

The model can propose exactly five actions:

| Action | Effect |
| --- | --- |
| `respond` | Deliver a user-facing answer and complete the task |
| `ask` | Block the task with `awaiting_user` and deliver a question |
| `act` | Request one registered capability operation |
| `continue` | Stay running and take another step |
| `end` | Complete, cancel, or abandon the task |

`end(completed)` is rejected unless the task has a delivered response or a
succeeded execution. Invalid proposals consume a bounded retry budget; a task
fails visibly when the budget is exhausted. When a task is blocked awaiting a
user, the next user message becomes the current user turn and is appended as a
constraint, then the task resumes. A blocked task does not stop unrelated ready
tasks.

Important invariants:

- The model may only **suggest** a domain (`harness_structure`). Creation
  happens only after an explicit user command or confirmation. It may enter or
  leave an existing domain, but never create one for a switch request.
- A bare "yes" or "no" resolves exactly one applicable pending decision. If a
  task is awaiting the user, the reply answers that question instead. When more
  than one decision could apply, the harness asks which one and never guesses.
- Domain suggestions are mirrored into the durable decision log, so confirming
  or declining one is recorded like any other decision; the registry remains
  the domain source of truth.
- A confirmation authorizes exactly one dispatch. It never either grants or
  bypasses a capability permission, domain enablement, argument check, or
  execution guard: the ordinary executor still runs when the approved action is
  dispatched.
- Capability execution is validated in this order: registration, availability,
  operation exists, idempotency key, domain enablement, permission policy,
  argument schema, dispatch, result recording. Argument validation reports every
  bad field at once; optional values are hints that may be defaulted/coerced.
- A final response may not claim success after a failed, denied, unavailable, or
  unknown execution. An unknown outcome must be stated as unknown. An `ask` may
  only request information from the user's world, never internal schema choices.
- A call with an unknown outcome is recorded as `unknown`. Identical retries of
  unknown write/destructive calls are rejected; the model must reconcile or ask.
- Every execution attempt is written before dispatch as `in_flight` and
  completed with an evidence envelope.
- Memory candidates need resolved source refs. Execution outcomes can only use
  structured fields. Explicit writes activate immediately; inferred memories
  are created by consolidation, never by an approval queue.
- Interaction history is append-only: user rows and assistant-update rows are
  merged on read.
- All timestamps are UTC ISO-8601 (`Z`). Files are UTF-8 with LF endings and
  writes use a temporary file plus `os.replace`.

## Persistent work, recovery, and autonomy

Work is durable and evidence-based. A task row records its goal, state,
priority, optional deadline, dependencies, progress checkpoints (bounded,
most recent kept), deferral window, and how many times the scheduler has
requeued it. Execution evidence stays in the attempt store: a task with no
attempts is planned work, a task with attempts has been tried, and completion
requires a succeeded attempt and/or a delivered response — a plan alone is
never proof of progress.

States distinguish the situations that matter: `ready` (eligible now),
`running`, `blocked` (with an explicit reason: waiting on the user, waiting
on approval, a dependency, an iteration/time limit, a temporarily missing
model or capability, an interrupted run, or an unknown outcome),
`deferred` (postponed until its window passes, or held by an open decision),
`completed`, `failed`, and `cancelled`.

### The heartbeat

`WorkScheduler` (see `harness/scheduler/service.py`) is a deterministic scan
over persisted tasks, decisions, and attempt evidence. On startup
(`harness.on_start`) and on every tick it:

1. marks a task whose last attempt was left `in_flight` by a crash as
   `blocked: unknown_outcome`, creates a durable `reconciliation` decision
   asking the user whether it completed, and never re-executes on its own;
2. re-checks dependencies (releases stale dependency blocks; fails dependents
   whose dependency failed or was cancelled);
3. releases deferred work whose window has passed, unless an open decision
   still holds it (a deferred "later" answer stays paused until the user
   replies, not until the clock);
4. requeues recoverable work (interrupted, `model_unavailable`,
   `capability_unavailable`, or a failed task whose last attempt has a
   retryable error category) after `scheduler.retry_cooldown_s`, counting
   each resume against `scheduler.max_task_retries`; when the budget is
   exhausted the task is marked failed with a `retry_limit` checkpoint and
   surfaced, never retried again;
5. surfaces an overdue task once (`task.due` event plus checkpoint);
6. runs scheduled consolidation when `consolidation.schedule_enabled` and the
   interval has elapsed (the web app's old private loop is gone; the
   heartbeat owns it now);
7. runs the deterministic learning review when
   `learning.review_schedule_enabled` and its interval have elapsed — it reads
   persisted evidence only and can stage skill candidates, never approve them;
8. optionally runs up to `scheduler.max_tasks_per_tick` ready tasks through
   `TurnService.run_ready` — the same serialized, gated turn path as a user
   message.

The scan never calls the model. The model is invoked only when real work runs
(step 8) or consolidation extraction is enabled, so a quiet heartbeat costs
file reads, not tokens. While `serve` is running, a daemon thread ticks
every `scheduler.interval_s`; on the CLI, `harness tick` runs one heartbeat
and `harness status` prints the report. A tick that changes nothing emits no
`scheduler.tick` event; when it does change something, reconcile, requeue,
reminder, and maintenance findings are folded into one event for that pass.

### Autonomy boundaries

The scheduler grants itself nothing. Autonomous execution reuses the normal
loop, so capability permission policy, domain enablement, argument
validation, the confirmation gate (a destructive operation still creates a
pending decision and blocks), the honesty guard, and identical-retry
rejection all apply unchanged. Work waiting on the user, an approval, a
reconciliation answer, or an open decision is never auto-requeued or
auto-run. Setting `scheduler.max_tasks_per_tick` to `0` keeps recovery,
reminders, and maintenance while disabling autonomous execution entirely;
`scheduler.enabled: false` disables the heartbeat itself (startup recovery
still runs so interrupted work is never mistaken for runnable).

### What requires the process to remain running

- Ticks, autonomous execution, due reminders, and scheduled consolidation
  happen only while `serve` (or a CLI `tick`) is running; nothing is lost
  when it stops — the same deterministic recovery runs on the next start.
- Deadlines and deferral windows are evaluated against persisted
  timestamps, so they survive restarts; there is no wall-clock wake-up
  (the process must be alive at a tick to notice).
- Only one harness process may write a data directory; a reset quiesces the
  heartbeat first via the same gate mechanism as turns.

## Decisions and confirmations

A decision is a durable proposal, not an authorization. Each one has a stable
`dec_*` id, kind (`action`, `plan`, `domain_creation`, or `reconciliation`),
status (`pending`, `deferred`, `approved`, `rejected`, `cancelled`,
`completed`, `failed`, `superseded`, `expired`), prompt, summary, scope, risk,
linked task, and the proposal it describes. Files live under
`<data>/decisions/dec_*.json`.

The control plane runs before planning or execution on every turn. It syncs
domain suggestions, resolves an explicit structure command, then asks the
deterministic resolver what the latest message means for the open decisions. A
reply can approve, reject, modify, defer, cancel, or ask a clarifying question,
or it can be an unrelated request that leaves the decisions untouched. When the
choice is genuinely ambiguous it asks; it never treats a keyword or an
unrelated question as authorization. Questions that refer to the open
proposals themselves — "which one?", "which proposal?", "what are my
options?" — are clarifications, not new work, and create no task.

An approved action is returned to the existing interaction loop as an
`ApprovedAction` and dispatched through the ordinary capability executor. The
execution is recorded against the decision, and success is claimed only when the
executor reports `succeeded`; denied, failed, unavailable, and unknown outcomes
are reported honestly. Approval is one-time and does not change settings,
permission policy, or domain enablement. A later request creates a new decision.

By default `control.confirmation.enabled` is true and
`control.confirmation.permissions` is `destructive`, so a destructive operation
is gated even when policy would deny it; the user can approve, and the executor
then records the real denial. Set the permission list to `write`, `read`, or
`all` to widen the gate. An operation can also declare
`requires_confirmation=True`, and the model may set `requires_confirmation` on
an `act` proposal; neither grants permission.

"yes, but don't restart" modifies a plan by removing matching steps before
approval. If the exclusion removes every step or leaves arguments invalid, the
decision stays pending and nothing executes. Deferred decisions remain open and
can be approved later; the linked task is moved to `deferred` with a window
from `scheduler.default_defer_hours`, and the open decision (not the clock)
holds it until answered. Rejected or cancelled proposals are terminal and never
dispatch. Conversation and full resets remove the decision log with the tasks
and interactions it references.

A `reconciliation` decision is created by the harness, not the model: when an
attempt was left `in_flight` by an interruption, the linked task blocks with
`unknown_outcome` and the user is asked whether the call completed. "Yes"
records the completion as the outcome without re-executing; "no" requeues the
work (counted against the retry budget) so the next attempt still passes the
normal confirmation checks; cancelling it cancels the task.

## Domains

Structure is a single level: domains. A domain holds capability enablement and
its own memory scope. The reserved `self` domain holds memories about the
assistant and loads on demand.

The session stores an optional active domain. It is a hint, not a routing
constraint. A task planned with an explicit domain name keeps it. A task whose
planner names no domain (or names one that does not exist) keeps the active
domain as the fallback, but first the harness infers the likely existing
project from the request text, project-scoped memories, already-open tasks, and
recent conversation; a confident match associates the *new* task with that
project without switching the session. Inference never creates a domain,
renames one, or moves an existing task or memory, and every routing decision
is logged as a `plan.domain_inference` event with its evidence. When no signal
is confident the task simply keeps the active domain; a plausible but
sub-threshold match is logged as `uncertain`, and a tied match is logged as
`ambiguous` with its candidates instead of being guessed. The structure
directory in the prompt lists the active domain, non-archived domains,
and pending suggestions; the planner receives the same directory. The section
has its own `context.budgets.structure` budget and drops whole entries rather
than cutting a line, and the total-budget drop order is history, structure,
domain, preferences, capabilities.

The user can switch with an explicit "work on", "switch to", or "open <domain>"
command, or with the selector on the chat page. Both resolve an existing
non-archived domain by exact then normalized name. A missing or ambiguous name
returns the available/candidate names and never creates a domain. The model can
use `harness_structure.enter_domain` and `leave_domain` for the same purpose; a
succeeded call also reassigns the running task's domain so the next loop
iteration rebuilds context in the new scope. `leave_domain` clears the active
domain.

Suggestions are deduplicated before they are recorded: an existing domain, a
pending suggestion with the same normalized or overlapping name, or a declined
suggestion still in cooldown returns duplicate/cooldown data instead of creating
a second pending row. A suggestion also requires a meaningful multi-word
subject (`domain.suggestion.min_words`, default 2), so a brief one-word topic
such as "weather" never produces a permanent project. A bare confirmation is
only interpreted as a suggestion
answer when exactly one suggestion is pending and no task is awaiting the user.
Conversations-only and full resets clear the active domain; a memory reset keeps
it.

## Memory

Memory records have a `type` (`fact`, `preference`, `decision`, `procedure`,
`lesson`) and an orthogonal `kind`: `semantic`, `episodic`, `procedural`, or
`prospective`. For semantic-kind records, `type` is the semantic subtype. The
harness derives the kind from the assigned type (`procedure`/`lesson` ->
`procedural`; `fact`/`preference`/`decision` -> `semantic`) unless a valid
`kind_hint` is supplied. `procedure` and `lesson` remain valid types for legacy
rows, and records written before `kind` existed derive it on load, so old data
needs no migration.
Scopes are `global`, `self`, and `domain:<id>`. `remember` only
requires `content`. The harness derives `type` with a configurable keyword
classifier and `scope` from the task's domain (or `global`). The model's
`type`/`scope` values are hints: invalid values fall back to the derived value
and the rejected hint is recorded on the record, so a bad hint never fails the
call. An explicit `self` hint is kept only for a task about the assistant, so a
user fact cannot be hidden from normal retrieval.

Records keep harness-filled provenance and a three-state lifecycle: `active`,
`superseded`, `retracted`. A newer explicit statement that conflicts with an
older one of the same kind supersedes it and the response names the replaced
memory. Corrections supersede rather than delete. Duplicate detection, conflict
supersession, explicit supersedes, and consolidation merges all require the same
kind, so a procedural lesson cannot silently replace a semantic fact. Usage is
logged with task and trace ids.

Retrieval is hybrid and explainable. One additive score combines exact
identifier matches (hostnames, IPs, paths, ids), lexical overlap, optional
semantic similarity, scope relevance, recency decay, provenance and confidence,
and memory-kind usefulness for the query; each component is kept on the ranked
result as `score_parts` and the human-readable reason is logged with usage.
Superseded and retracted rows, temporally invalid records, and an older row
whose newer replacement is active are excluded before ranking, so a superseded
fact cannot beat its replacement on wording alone. The default semantic
component (`implementations.retriever: hybrid`) is a dependency-free
lexical-semantic scorer built from configurable concept groups, light stemming,
and character n-grams; no vector database or model call is involved, and
`implementations.retriever: keyword` restores pure keyword matching. A
deployment with an embedding service can supply any other
`SemanticScorer` implementation at the composition root without touching the
memory manager. Retrieval also accepts a scope-priority list: records from
other projects remain retrievable, with a small relevance bonus for the
active or inferred project instead of a hard filter.

Records carry lifecycle metadata alongside the status: `evidence_status`
(`stated`, `observed`, `inferred`, `corroborated`), a `confidence` between 0
and 1, optional `valid_from`/`valid_until` validity windows, and `tags`. The
harness derives a lifecycle view from these fields (verified fact, preference,
decision, lesson, temporary event, unresolved hypothesis, proposed commitment,
superseded, retracted), which the context builder surfaces on each memory tag.
Low-confidence or inferred statements rank below explicit, corroborated ones
but are never silently promoted to verified facts, and an explicit user
correction supersedes the outdated record through the normal conflict path.

Consolidation runs off the hot path (UI button or optional schedule). It first
extracts inferred memories from recent conversation with verified source refs,
then re-types only semantic-kind records, merges duplicates/near-duplicates, and
resolves conflicts by recency. Procedural, episodic, and prospective records
keep their type and kind. Merges also require the same semantic subtype, so
wording overlap alone never merges a fact with a preference or a procedure
with a lesson. A run stores an input snapshot and a report of what was
added, merged, and superseded; it can be rolled back as a whole (and inferred
rows individually).

## Learning

Learning is evidence-based and separated into three categories with different
risks, so they are never conflated:

- **Memory learning** (facts, preferences, decisions, lessons): explicit user
  statements apply immediately with provenance and are reversible through
  supersession/retraction; harness-observed learning (repeated preferences,
  verified failures) is retained only past its evidence threshold, at lower
  rank (`origin=inferred`, `corroborated`/`observed` evidence).
- **Procedural learning** (skills): a workflow whose steps all succeeded in
  enough distinct tasks becomes a *staged skill candidate*. The candidate is
  never disclosed to the model until an explicit approval promotes it, an
  improvement of an approved skill additionally requires recorded passing
  test evidence, and every approval can be rolled back to the previous
  version.
- **Strategy learning** (persona/planning changes): a proposal is validated
  against deterministic safety invariants first (honesty, permissions,
  confirmation gates, no self-modification of settings/code) and is only
  ever staged as a persona proposal; approval is a separate user action.

Every learning decision — applied, staged, rejected, or below-threshold — is
journaled under `data/learning/journal/` with its source evidence
(interaction, attempt, and task ids), rationale, and the ids of anything it
created, so each change is traceable back to the original observation.

The deterministic learning review (`harness review`, or scheduled with
`learning.review_schedule_enabled`) scans persisted evidence without any
model call: repeated successful workflows become skill candidates, repeated
failure fingerprints and pending candidates are surfaced as findings, and a
review with no meaningful evidence changes nothing. The scheduler runs it on
its heartbeat when enabled; it never approves anything by itself.

Skills live as `SKILL.md` folders. Externally provided skills are discovered
from `capabilities.skills_dir`; learned skills are managed under
`data/skills/` (`records/` for metadata, `candidates/` for staged text,
`active/` for approved text, which is also the default discovery directory).
A skill may declare `preconditions` (required tokens); the context builder
discloses a skill only when the task goal matches them, and records the
omission as an exclusion with the reason. For a learned workflow, those tokens
are derived from content words shared by the goals it was observed in
(stopwords, generic task verbs, and capability/operation names excluded), so a
skill is not disclosed merely because a later task happens to use the same
tool. This is lexical relevance; a paraphrase with no shared word will miss.

## Capabilities

Built-in manifests:

- `mock`: pure-Python read operations (`ping`, `echo`, `status`, `add`), a
  structured `fail`, and write/destructive demos that are denied by default
- `harness_memory`: `remember`, `correct`, `retract`
- `harness_structure`: `enter_domain`, `leave_domain`, and `suggest_domain`

Enablement is per domain; tasks without a domain use
`capabilities.default_enabled`. Permission policy is separate:
`capabilities.policy.read|write|destructive`. For each enabled manifest the
context discloses the operation description, argument names and types, and
required fields, so the model can choose `act` and fill arguments instead of
guessing.

Optional extension points:

- Skills: set `capabilities.skills_dir` to a folder of `<name>/SKILL.md` files
  (see `examples/skills/homelab-notes/SKILL.md`). When unset, learned skills
  install into `data/skills/active`, which the executor discovers instead. A
  skill's front matter may declare `preconditions`; it is disclosed only for
  matching tasks. See the Learning section for the staged approval lifecycle.
- MCP: set `capabilities.mcp_servers_file` to a YAML file of stdio servers. See
  `examples/mcp_servers.yaml` and `examples/mcp/echo_server.py`. A server that
  fails to start is listed as unavailable, not fatal.

## Storage layout

```text
data/
  settings.yaml
  settings_history.jsonl
  events/events.jsonl
  traces/trc_*.json
  sessions/session.json, session_meta.json, interactions.jsonl
  registry/domains/*.json, suggestions/*.json
  tasks/tsk_*.json
  decisions/dec_*.json
  persona/persona.json, history.json, proposals/per_*.json
  capabilities/attempts/op_*.json
  memory/records/mem_*.json, usage.jsonl
  consolidation/run_*.json, run_*.input.json
  learning/journal/lrn_*.json
  skills/records/skl_*.json, candidates/<name>/SKILL.md, active/<name>/SKILL.md
  eval/...
```

See [CODEMAP.md](CODEMAP.md) for module responsibilities and the data-flow map.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Turn fails with `model_unavailable` | Endpoint unreachable, wrong URL, wrong model name, or timeout. Test `/v1/models` from the same host/container. |
| UI returns 401 | Token mismatch or stale cookie. Use `?token=<current token>` or the Bearer header. Environment overrides beat `settings.yaml`. |
| Startup fails on an unknown setting | A typo in `settings.yaml`. Fix it or restore a backup; retired keys are dropped automatically, but other unknown keys are not. |
| `permission denied` writing `/data` | Bind-mounted data is not writable by UID 10001. `chown -R 10001:10001` the host directory or use the named volume. |
| Container is unhealthy | Check `docker compose logs harness`; `/health` only proves the app opened its data directory, not that the model is reachable. |
| Execution is `unknown` | The call may have happened. Reconcile before retrying; identical unknown write/destructive retries are rejected. A task blocked with `unknown_outcome` is waiting for your answer to its reconciliation question. |
| Task stuck in `deferred` | Either its window has not passed, or an open decision still holds it; answer the pending decision (or approve it) to resume. |
| Nothing runs automatically | Check `scheduler.enabled` and `scheduler.max_tasks_per_tick`; recovery and reminders work even when autonomous execution is 0/off. |
| Two processes write the same data | Unsupported. Run exactly one harness process per data directory. |
| Port already in use | Change `HARNESS_PORT` (host side). The container port stays `8080`. |

## Known limitations

- Single user, single assistant, one process, and file-backed locking. Do not
  run two writers against the same data directory.
- No API-key support in the model adapter. Authenticate at a proxy if the
  backend requires it.
- No built-in TLS or user accounts. Use a VPN or TLS reverse proxy beyond a
  trusted network.
- No automatic model download or backend management.
- MCP stdio servers are launched per discovery/call; latency includes startup.
- The default semantic retrieval is lexical-semantic (concept groups, stemming,
  character n-grams), not embeddings. It catches common paraphrases but not
  deep paraphrase or cross-language matches. An embedding-backed
  `SemanticScorer` can be injected without changing the memory manager; without
  one, purely conceptual matches can be missed.
- Project inference is deterministic scoring, not understanding: it associates
  new work with an existing project only when name, memory, task, or history
  evidence clears the confidence threshold, and reports ambiguity instead of
  guessing. Requests that name a brand-new persistent topic still need the
  user to create the domain explicitly.
- Context trimming is whole-item for memories and structure entries (never a
  half-rendered block), but the persona can be character-truncated as a last
  resort when the total budget cannot fit the safety instructions otherwise;
  per-section budgets for base instructions still raise rather than truncate.
- The UI is intentionally server-rendered and small; it is not a public
  multi-user service.
- The live eval needs a live model and network access to that endpoint. It is
  not part of the default test suite. Phase 5 measured only the deterministic
  scripted suite; no live-model accuracy claim is made.
- Decision interpretation is conservative pattern matching. A bare selection
  such as "the first one" identifies the target but still asks for an explicit
  approval; "no, the first one" is likewise clarified rather than treated as
  authorization. This is deliberate, but it costs a turn.
- Learned-skill relevance uses shared content words from the goals where the
  workflow was observed. A later request that shares no word with that
  evidence will not disclose the skill, and removing too many common words can
  make a skill narrower than a human would expect.
- The audit datasets are fixture-scale (8-12 cases per metric). They detect
  regressions and false authorization, not broad language understanding.
- Scheduling is process-bound: there is no OS-level timer or external job
  runner, so a due task or consolidation pass is noticed only on the next tick
  while the process (or a CLI invocation) is running.
