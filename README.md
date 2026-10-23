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

State is file-backed and inspectable: settings, memory, domains and projects,
tasks, sessions, traces, events, persona versions, consolidation runs, and
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
  constraint, without stopping unrelated tasks.
- **Five-action protocol.** `respond`, `ask`, `act`, `continue`, and `end`.
  Malformed proposals consume a bounded retry budget and end in a visible
  failure, not a silent success.
- **Validated capabilities.** Registration, availability, operation existence,
  idempotency, domain enablement, permission policy, argument schema, dispatch,
  and evidence recording happen in a fixed order.
- **Memory with provenance.** Facts, preferences, decisions, procedures, and
  lessons in global, self, domain, or project scope, with explicit/inferred
  source rules, conflict handling, supersession, retraction, usage logging, and
  consolidation rollback.
- **Domains, projects, and workspaces.** Structure is created only by an
  explicit user command or confirmation; the model may only suggest.
- **Budgeted context and full traces.** Each model call records the context
  sections included or excluded, the prompt, the raw response, validation
  result, latency, and token counts.
- **Inspectable UI and files.** A small authenticated server-rendered UI exposes
  tasks, traces, memory, settings, and consolidation state without a separate
  database.
- **Deterministic tests and a live eval tier.** Behavior tests use a scripted
  adapter; the live eval runs real scenarios with deterministic checks and
  reports pass rates, latency, and proposal validity.

## Architecture at a glance

`harness/` is organized around replaceable manager protocols in
`harness/contracts/`. Concrete components import peers only through those
contracts; `harness/app.py` is the composition root that wires everything
together. See [CODEMAP.md](CODEMAP.md) for the full module and storage map.

```text
user text
  -> session interaction (append-only)
  -> structure command?  yes -> registry create/confirm/decline
                         no  -> model task plan -> persisted tasks
  -> next ready task
  -> budgeted context (persona, memory, domain/project, history, capabilities)
  -> model proposal (respond / ask / act / continue / end)
  -> validation + permission check
  -> capability dispatch -> attempt record + evidence + event
  -> repeat until response, question, completion, or a limit
  -> trace + assistant update + turn result
```

| Area | Main location |
| --- | --- |
| Composition root | `harness/app.py` |
| Contracts and protocols | `harness/contracts/` |
| Orchestration | `harness/orchestrator/` |
| File-backed managers | `harness/{settings,events,traces,session,domains,tasks,profile,memory,capabilities,consolidation}/` |
| Model adapters | `harness/model/` |
| Context construction | `harness/context/` |
| UI | `harness/ui/web.py` |
| Live eval | `harness/eval.py` |

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

The settings file is materialized on first start. Unknown keys fail startup;
remove typos or restore a backup rather than leaving an unknown key in place.

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

The live eval requires a reachable model. It is evidence only for the exact
configuration recorded in the result file. Do not report a pass rate from a run
that did not happen.

## CLI

Global options come before the command:

```text
python -m harness [--data-dir DIR] COMMAND
```

| Command | Purpose |
| --- | --- |
| `serve` | Run the Starlette UI/API |
| `health` | Print the resolved data directory |
| `turn TEXT` | Run one interaction and print messages |
| `consolidate` | Run one background consolidation pass |
| `measure` | Print the learning report as JSON |
| `eval` | Run the live deterministic-check eval suite |

Evaluation examples:

```bash
python -m harness --data-dir ./data eval --list
python -m harness --data-dir ./data eval --repeats 3 --tag baseline
python -m harness --data-dir ./data eval --only injection,follow_up --repeats 5
python -m harness --data-dir ./data eval --compare baseline another-tag
```

Each scenario repeat gets an isolated directory under
`data/eval/<tag>/<scenario>/repeat_N`. The summary is written to
`data/eval/results/<tag>.json` and includes pass rates by category and scenario,
proposal validity, capability-call counts, latency, and failure reasons. Checks
are deterministic: they inspect tasks, execution attempts, memory records, and
traces. No model judges another model.

## Tests

```bash
python -m pytest tests -q
```

The suite has two tiers:

- deterministic behavior tests using `ScriptedAdapter`
- contract tests for manager protocols, memory implementation parity, component
  import boundaries, settings, and storage

`tests/test_eval.py` exercises the eval runner with a scripted adapter; it does
not replace a live run. `pytest -m live` is reserved for live tests and is not
required by the default suite.

Container checks are separate from the pytest suite:

```bash
python scripts/container_check.py
```

## Configuration

Settings are stored in `<data-dir>/settings.yaml` and edited in the UI. Every
key has a catalog definition with type, bounds, default, group, and whether it
is live or requires restart. Unknown keys in the file fail startup. A history of
changes is appended to `settings_history.jsonl`.

Important groups:

- `models.interactive.*` and `models.background.*`: endpoint, model, limits,
  sampling, seed, thinking budget, timeout, retries, response format
- `context.budgets.*`: per-section and total token budgets
- `context.self_scope_keywords`: phrases that load the reserved `self` domain
- `memory.*`: retrieval weights, duplicate overlap, source policy, auto-commit
- `capabilities.*`: permission policy, default enablement, skill/MCP paths
- `domain.suggestion.*`: repeated-subject threshold and cooldown
- `loop.*`: iteration/time/retry limits
- `session.history_window`, `ui.*`, `events/traces.retention_days`

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
user, the next user message is appended as a constraint and the task resumes. A
blocked task does not stop unrelated ready tasks.

Important invariants:

- The model may only **suggest** a domain or project (`harness_structure`).
  Creation happens only after an explicit user command or confirmation.
- Capability execution is validated in this order: registration, availability,
  operation exists, idempotency key, domain enablement, permission policy,
  argument schema, dispatch, result recording.
- A call with an unknown outcome is recorded as `unknown`. Identical retries of
  unknown write/destructive calls are rejected; the model must reconcile or ask.
- Every execution attempt is written before dispatch as `in_flight` and
  completed with an evidence envelope.
- Memory candidates need resolved source refs. Execution outcomes can only use
  structured fields. Inferred conflicts with explicit memories queue for
  approval.
- Interaction history is append-only: user rows and assistant-update rows are
  merged on read.
- All timestamps are UTC ISO-8601 (`Z`). Files are UTF-8 with LF endings and
  writes use a temporary file plus `os.replace`.

## Memory

Memory types are `fact`, `preference`, `decision`, `procedure`, and `lesson`.
Scopes are `global`, `self`, `domain:<id>`, and `project:<id>`. Explicit user
statements may commit directly; inferred and consolidated records follow the
auto-commit settings, and conflicts with explicit memories always queue.

Records keep provenance and soft lifecycle state: `proposed`, `active`,
`superseded`, `retracted`, `rejected`. Corrections supersede rather than delete.
Usage is logged with task and trace ids. Consolidation edits memory, persona, and
structure suggestions in one run with an input snapshot and can roll the whole
run back.

## Capabilities

Built-in manifests:

- `mock`: pure-Python read operations (`ping`, `echo`, `status`, `add`), a
  structured `fail`, and write/destructive demos that are denied by default
- `harness_memory`: `remember`, `correct`, `retract`
- `harness_structure`: `suggest_domain` only

Enablement is per domain; tasks without a domain use
`capabilities.default_enabled`. Permission policy is separate:
`capabilities.policy.read|write|destructive`. For each enabled manifest the
context discloses the operation description, argument names and types, and
required fields, so the model can choose `act` and fill arguments instead of
guessing.

Optional extension points:

- Skills: set `capabilities.skills_dir` to a folder of `<name>/SKILL.md` files.
  See `examples/skills/homelab-notes/SKILL.md`.
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
  registry/domains/*.json, projects/*.json, workspaces/*.json, suggestions/*.json
  tasks/tsk_*.json
  persona/persona.json, history.json, proposals/per_*.json
  capabilities/attempts/op_*.json
  memory/records/mem_*.json, usage.jsonl
  consolidation/run_*.json, run_*.input.json
  eval/...
```

See [CODEMAP.md](CODEMAP.md) for module responsibilities and the data-flow map.

## Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Turn fails with `model_unavailable` | Endpoint unreachable, wrong URL, wrong model name, or timeout. Test `/v1/models` from the same host/container. |
| UI returns 401 | Token mismatch or stale cookie. Use `?token=<current token>` or the Bearer header. Environment overrides beat `settings.yaml`. |
| Startup fails on an unknown setting | A typo or removed key in `settings.yaml`. Fix it or restore a backup; do not leave unknown keys. |
| `permission denied` writing `/data` | Bind-mounted data is not writable by UID 10001. `chown -R 10001:10001` the host directory or use the named volume. |
| Container is unhealthy | Check `docker compose logs harness`; `/health` only proves the app opened its data directory, not that the model is reachable. |
| Execution is `unknown` | The call may have happened. Reconcile before retrying; identical unknown write/destructive retries are rejected. |
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
- Retrieval is keyword overlap, not embeddings.
- The UI is intentionally server-rendered and small; it is not a public
  multi-user service.
- The live eval needs a live model and network access to that endpoint. It is
  not part of the default test suite.
