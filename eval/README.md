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
| `deterministic` | `ScriptedModel` | Harness behavior: permissions, persistence, recovery, scheduling, retrieval, and multi-tool loops. No network. |
| `live` | configured endpoint | Smoke and behavioral checks for a named model through the real runtime. |

A scenario declares `modes: [deterministic]`, `[live]`, or both. The shipped
suite partitions them:

| File | Mode | Purpose |
| --- | --- | --- |
| `a_runtime.yaml`–`f_subagents.yaml` | deterministic | Core harness coverage |
| `g_live.yaml` | live | Single-turn smoke/capability checks |
| `h_agent_loops.yaml` | deterministic | Sequential multi-tool loops, recovery, approval boundaries, budgets, deferred batches |
| `i_live_behavior.yaml` | live | Multi-turn continuity, correction, skill following, delegation, hostile inbox, scheduling, live tool chaining |

## What this suite measures

- **Deterministic scenarios** verify harness behavior with a scripted model:
  permissions, persistence, recovery, scheduling, retrieval, state
  transitions, and multi-tool loop control. They do not measure model quality.
- **`g_live.yaml`** is smoke/capability coverage: explicit single-turn prompts
  confirm that a model can answer, call a named tool, write memory, and recall
  it.
- **`i_live_behavior.yaml`** is a behavioral suite. It asks for multi-turn
  continuity, correction, skill following, delegation, safe handling of
  untrusted mail, approval-gated scheduling, and a two-tool loop. Action claims
  are checked against persisted state; open-ended replies are scored by a local
  rubric.
- **Neither suite is a general model-quality benchmark.** A live pass does not
  establish parity with another model. Compare multiple repeats: a single pass
  can hide a loop or a refused instruction, and a keyword failure can be a
  wording difference rather than worse behavior.

Treat latency and quality as separate axes. Measure latency from the report
durations (including per-scenario durations), and measure quality with the
rubric mean and criterion pass rate over multiple trials. Do not infer quality
from scenario pass rate alone.

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

# Behavioral suite, three trials per model.
python -m harness eval --mode live --model MODEL_A --base-url http://HOST:PORT \
    --scenario "i_live_*" --repeats 3 --tag behavior_MODEL_A
python -m harness eval --mode live --model MODEL_B --base-url http://HOST:PORT \
    --scenario "i_live_*" --repeats 3 --tag behavior_MODEL_B
python -m harness eval --compare behavior_MODEL_A behavior_MODEL_B
```

The comparison shows scenario/assertion pass rates and the rubric mean side by
side. Use the per-scenario durations in the JSON/Markdown report for latency;
do not compare total wall time from a different task mix.

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

## Quality rubrics

A step may include `quality:` next to its state assertions. Rubric criteria are
local and deterministic (substring, regex, and word counts). They produce a
weighted score and criterion results, reported separately from assertion
pass/fail. A low score does **not** change the scenario status; that keeps
harness correctness and reply quality on separate axes.

```yaml
- kind: turn
  input: "What time does the trailhead start?"
  expect:
    status: completed
    response: {contains: "07:00"}
    quality:
      min_score: 0.75
      rubric:
        - id: states_time
          weight: 2
          any: ["07:00", "7:00"]
        - id: concise
          weight: 1
          max_words: 80
        - id: not_an_approval_prompt
          weight: 3
          none: ["reply approve/reject", "approvals panel"]
```

Criterion keys: `any`, `all`, `none`, `regex`, `min_words`, `max_words`.
`weight` defaults to 1. `min_score` records whether the weighted score crossed
the threshold; the report shows scores, criterion pass rate, and any steps below
their minimum.

This is a rubric, not an LLM judge. Use a judge when subjective judgment is the
thing being measured; the shipped suite stays offline and repeatable.

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
| `tools` | `calls`, `none`, `succeeded`, `forbidden`, `count_min`, `count_max`, `result_contains`, `error_contains`, `sequence`, `sequence_contiguous`, `deferred_min`, `deferred_max` |
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

`tools.calls` entries accept `result_contains` and `error_contains`, so a loop
can prove that a later call consumed the previous call's output. `tools.sequence`
checks an ordered subsequence (add `sequence_contiguous: true` for adjacent
rows). `deferred_min` reads the run transcript for calls the runtime deferred
because it executes one action per model response.
