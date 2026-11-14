# Scenario evaluation

`harness eval` runs YAML scenarios against a real `App` in a throwaway data
directory. Tasks, runs, memory, approvals, schedules, inbox, modules, skills,
and sub-agents are the production implementations; only the model is scripted
in deterministic mode.

```bash
python -m harness eval --list                 # deterministic scenarios
python -m harness eval --mode live --list     # live-only scenarios
python -m harness eval --mode deterministic   # full local suite (no network)
```

Reports are written to `data/eval/results/<tag>.json` and `.md`. The JSON keeps
per-step evidence and per-assertion expected/observed values; the Markdown file
is the human summary.

## Modes

| Mode | Model | Purpose |
| --- | --- | --- |
| `deterministic` | `ScriptedModel` | Harness behavior: permissions, persistence, recovery, scheduling, retrieval. No network. |
| `live` | configured endpoint | Smoke and capability checks for a named model through the real runtime. |

A scenario declares `modes: [deterministic]`, `[live]`, or both. The shipped
suite partitions them: `a_`–`f_` are deterministic; `g_live.yaml` is live-only.

## Running a different model

Model settings resolve from lowest to highest precedence:

1. built-in defaults
2. `data/config.yaml` (or `--config-from DIR`)
3. `HARNESS_MODEL_NAME`, `HARNESS_MODEL_BASE_URL`, `HARNESS_MODEL_API_KEY`
4. `--model`, `--base-url`, `--api-key`

```bash
python -m harness eval --list-models --base-url http://HOST:PORT

python -m harness eval --mode live \
    --model MODEL_NAME --base-url http://HOST:PORT --api-key KEY \
    --tag live_MODEL
```

A live run probes `/v1/models` first and refuses to start unless the requested
model is advertised. Use `--allow-unlisted-model` for a server that serves a
model without listing it. The report records the endpoint probe, so a report
cannot silently describe a different model.

Follow up with:

```bash
python -m harness eval --summary live_MODEL
python -m harness eval --compare deterministic final_deterministic
```

## Safety

- The sandbox root defaults to `<temp>/harness-eval` and carries a marker. An
  unmarked non-empty directory is refused, not cleared.
- The repository and the deployed `data/` directory are always refused as
  sandbox paths.
- Each scenario/repeat gets its own data directory and SQLite store.
- Default cleanup removes the scratch directory (`--keep-sandbox` retains it).

## Scenario shape

```yaml
scenarios:
  - id: b_memory_scope
    name: Domain memory stays in its domain
    category: memory
    severity: high
    modes: [deterministic]
    tags: [memory, context]
    settings:          # optional config overrides for this scenario
      limits: {max_tool_calls: 4}
    fixtures:          # optional state created through public managers
      domains: [{name: Homelab, description: servers and network}]
      memories: [{content: "The NAS backup runs at 02:00.", domain: Homelab}]
    steps:
      - kind: turn
        name: ask about the NAS
        input: "check the homelab nas backup"
        script:                       # deterministic mode only
          - content: "It runs at 02:00."
        expect:
          status: completed
          response: {contains: "02:00"}
          context: {system_contains: "NAS backup"}
```

Fixtures support `domains`, `memories`, `tasks`, `skills`, `schedules`,
`automations`, and `mcp_servers`.

## Step kinds

`turn`, `inspect`, `tick`, `inbox`, `approve`, `reject`, `cancel`, `restart`,
`schedule_due`, `sweep_memory`, `sleep`, `set_config`, `create_task`,
`create_schedule`, `create_memory`, `archive_memory`, `restore_memory`,
`module_status`.

Step options are the YAML keys outside `kind`, `name`, `input`, `script`, and
`expect`. For example:

```yaml
- kind: inbox
  event: {source: mail, external_id: m-1, title: Invoice, body: "review"}
  triage: true
  max_runs: 1
- kind: module_status
  id: "skill:weekly-review"
  action: activate
- kind: create_schedule
  schedule_name: due-once
  prompt: "run the check"
  schedule_kind: once
  at_in_seconds: -5
```

## Assertions

Every assertion reads persisted state. An unobservable expectation fails with
`observed: null`; it never passes silently.

| Check | Examples |
| --- | --- |
| `status` | `completed`, `waiting_approval`, `limit_reached` |
| `response` | `contains`, `not_contains`, `regex`, `empty` |
| `task` | `status`, `domain`, `role`, `parent`, `reason_contains` |
| `tasks` | `count`, `exists: [{origin, status, tainted}]`, `none` |
| `domains` | `exists`, `none`, `count` |
| `run` | `status`, `iterations_min`, `tool_calls_min`, `context_not_empty` |
| `memories` | `exists`, `none`, `count_min`, `superseded_contains`, `revision_min` |
| `tools` | `calls`, `none`, `succeeded`, `forbidden`, `count_min` |
| `approvals` | `pending`, `tool`, `statuses`, `arguments_contains` |
| `events` | `include`, `exclude`, `data` |
| `context` | `sections`, `refs`, `system_contains`, `exclude_refs`, `source_counts` |
| `schedules` | `count`, `enabled`, `occurrences`, `occurrences_statuses` |
| `inbox` | `accepted`, `duplicate`, `triage_status`, `statuses`, `fires_min` |
| `modules` | `statuses`, `absent`, `tools_include`, `tools_exclude` |
| `subagents` | `count`, `statuses`, `result_contains`, `child_system_contains` |
| `misc` | `no_error`, `detail_contains`, `no_hidden_chat` |

Assertions inherit the scenario `severity` (default `medium`); critical
failures are counted separately in every report.
