# Personal Assistant Harness v1

A single-user personal assistant harness. One model interprets requests and
proposes actions; the harness validates, executes, persists, and reports what
actually happened. Persistent memory, domains/projects, tasks, traces, events,
persona versions, settings history, and consolidation runs are inspectable files
under one data directory.

The v1 scope deliberately has one assistant, no multi-user accounts, and no
shell, filesystem, or network capability exposure. Anything with external
effects must be an explicitly registered capability (or an optional MCP server).

## Requirements

- Python 3.12 (the container base image; newer interpreters may work locally)
- Docker 24+ and Docker Compose v2 for the container path
- An OpenAI-compatible chat-completions endpoint for real use

The harness starts without a live model. Turns will block with
`model_unavailable` until the configured endpoint is reachable.

## Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install -e ".[dev]"

python -m harness --data-dir ./data health
python -m harness --data-dir ./data serve
```

Open `http://127.0.0.1:8080/?token=dev-token` (the default UI token) and change
the token in Settings before exposing the UI beyond localhost. `/health` is
always unauthenticated.

A one-shot turn:

```bash
python -m harness --data-dir ./data turn "Check the mock status for nas."
```

`health` only reports that the harness data directory opened; it does not probe
the inference backend.

## Container

```bash
cp .env.example .env            # optional; edit the token first
docker compose up --build -d
# UI: http://127.0.0.1:8080/?token=change-this-token
python scripts/container_check.py
```

The image runs as an unprivileged user, stores state in the `harness-data`
volume at `/data`, and includes a `/health` health check. `HARNESS_PORT`,
`HARNESS_UI_TOKEN`, and the model endpoint variables can be set in the shell or
in `.env`.

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

The live eval requires a reachable endpoint. A recorded result file is evidence
only for the exact configuration printed in it; this repository does not claim
a pass rate without a run.

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
`capabilities.policy.read|write|destructive`.

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

See `CODEMAP.md` for module responsibilities and the data-flow map.

## Known limitations

- Single user, single assistant, one process, and file-backed locking. Do not
  run two writers against the same data directory.
- No automatic model download or backend management.
- MCP stdio servers are launched per discovery/call; latency includes startup.
- Retrieval is keyword overlap, not embeddings.
- The UI is intentionally server-rendered and small; it is not a public
  multi-user service.
- The live eval needs a live model and network access to that endpoint. It is
  not part of the default test suite.
