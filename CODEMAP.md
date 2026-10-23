# Code map

`harness/` is organized around replaceable manager contracts. Concrete
components import other components only through `harness/contracts/*` (plus
shared structural modules such as `constants`, `storage`, `text`, and
`prompts`). `harness/app.py` is the composition root; `harness/ui/web.py`,
`harness/measure.py`, `harness/eval.py`, and `harness/__main__.py` sit at the
composition/edge layer.

## Entry points and composition

| Path | Responsibility |
| --- | --- |
| `harness/__main__.py` | CLI: `serve`, `health`, `turn`, `consolidate`, `measure`, `eval` |
| `harness/app.py` | `Harness` container and `build_harness()` wiring |
| `harness/constants.py` | Schema version, ID prefixes, enums, reserved `self` id |
| `harness/storage/atomic.py` | UTC timestamps, safe IDs/filenames, atomic JSON/text, JSONL append |
| `harness/text.py` | Tokenization, reasoning stripping, overlap, token estimate |
| `harness/prompts.py` | Base action instructions and task-plan instructions |

## Measurement scripts

| Path | Responsibility |
| --- | --- |
| `scripts/experiments.py` | Writes each experiment config, runs the fixed eval set, saves and tabulates results |
| `scripts/cold_start_probe.py` | Idle/warm/concurrent llama-server latency probe with model-status and router hints |

## Contracts

`harness/contracts/` contains dataclasses, protocols, and shared error types.
It has no concrete imports.

| File | Contract |
| --- | --- |
| `actions.py` | Five action types and `ExecutionResult` |
| `capabilities.py` | Manifest, operation spec, execute request, executor protocol |
| `consolidation.py` | Consolidation run and service protocol |
| `context.py` | Context package/section/inclusion and builder protocol |
| `domains.py` | Domain/project/workspace/draft/suggestion and registry protocol |
| `events.py` | Event and event-log protocol |
| `loop.py` | Turn result and interaction-loop protocol |
| `memory.py` | Memory record/candidate/submit/usage and manager protocol |
| `model.py` | Message/result, model adapter protocol, `ModelError`/`ModelUnavailable` |
| `profile.py` | Persona, draft, proposal, version, profile protocol |
| `session.py` | Session/interaction and session-manager protocol |
| `settings.py` | Setting definition/change, settings protocol, `SettingsError` |
| `sources.py` | Source resolver protocol |
| `tasks.py` | Task spec/task/outcome and task-manager protocol |
| `traces.py` | Trace and trace-store protocol |

## Components

| Path | Responsibility |
| --- | --- |
| `harness/settings/catalog.py` | Every operating setting and its bounds |
| `harness/settings/service.py` | YAML load, validation/coercion, env overrides, history |
| `harness/events/log.py` | Append-only event log with retention query |
| `harness/traces/store.py` | Per-call trace files with prompt/inclusion/validation data |
| `harness/session/manager.py` | Session resume, append-only interactions, subject hits |
| `harness/domains/registry.py` | Self domain, explicit structure creation, suggestions, enablement |
| `harness/tasks/manager.py` | Task files, valid transitions, dependency release, sequencing |
| `harness/profile/provider.py` | Persona versions and proposed edits |
| `harness/memory/core.py` | Validation, conflict/duplicate policy, lifecycle, retrieval, usage, rollback |
| `harness/memory/file_manager.py` | File-backed memory records and usage JSONL |
| `harness/memory/inmemory.py` | Non-persistent parity implementation for swaps/tests |
| `harness/memory/tokens.py` | Token/overlap aliases for retrieval |
| `harness/context/builder.py` | Budgeted context sections, scopes, capability disclosure |
| `harness/capabilities/mock.py` | Pure-Python mock operations |
| `harness/capabilities/harness_ops.py` | Memory and structure manifests |
| `harness/capabilities/executor.py` | Validation, enablement, permissions, dispatch, attempt records |
| `harness/capabilities/skills.py` | `SKILL.md` discovery |
| `harness/capabilities/mcp.py` | Optional stdio MCP discovery/call via the official SDK |
| `harness/orchestrator/parse.py` | Pydantic parsing and JSON schemas |
| `harness/orchestrator/machine.py` | Allowed-state and action-transition rules |
| `harness/orchestrator/loop.py` | Turn handling, structure commands, heuristic/LLM planning, task loop, retry cap, turn timeline and metrics |
| `harness/orchestrator/timeline.py` | Per-turn stage recorder (start offset, duration, detail) serialized into traces |
| `harness/consolidation/service.py` | Evidence snapshot, proposals, apply/results, rollback |
| `harness/model/scripted.py` | Deterministic test adapter |
| `harness/model/openai_http.py` | OpenAI-compatible HTTP adapter and health probe |
| `harness/measure.py` | Learning report from stored events/tasks/memories/usage |
| `harness/eval.py` | Isolated live eval, deterministic checks, fixed latency/failure scenarios, p50/p95 and token metrics |
| `harness/ui/web.py` | Authenticated server-rendered inspection/control UI, including turn timeline and execution envelope |

## Turn data flow

```text
user text
  -> SessionManager.record_interaction (append-only)
  -> structure command?  yes -> DomainRegistry.create_* / confirm / decline
                         no  -> short single request in heuristic mode?
                                  yes -> TaskManager.create(one code task)
                                  no  -> ModelAdapter.complete(task plan)
                                        -> TaskManager.create(sequence, deps)
  -> TaskManager.next_ready
  -> ContextBuilder.build(task, trace_id)
       persona + preferences + domain/project/workspace + task + history
       + enabled capability manifests; budget overflow records exclusions
  -> ModelAdapter.complete(proposal)
  -> parse_proposal -> orchestrator.machine.apply_action
       act -> CapabilityExecutor.execute -> attempt file + event
  -> repeat until response/ask/end or a limit
  -> TraceStore.put + model.call event
  -> SessionManager.append_assistant
  -> TurnResult(messages, tasks)
```

Every model trace records `duration_ms`, `time_to_first_token_ms`,
`prompt_tokens`, `completion_tokens`, `visible_output_tokens` (reasoning
stripped), `reasoning_tokens`, and `thinking_produced`. Each turn also writes a
`role="turn"`, `model_called=False` trace whose `timeline` lists stage names,
start offsets, durations, and details (prompt build, model calls,
parse/validation, execution envelope, response). Execution stage details carry
the full status/error_category/message envelope. `harness/eval.py` derives turn
and call p50/p95, TTFT, token means, thinking ratio, and tool success from those
traces.

Planning is selectable through `tasks.planning_mode`: `llm` always asks the
model to split, while `heuristic` (the default) creates one task in code for
short single-request messages and reserves the model split for marker-bearing
input. `loop.stop_after_nonretryable_failure` (default true) rejects further
capability calls after a non-retryable execution failure and forces an honest
failure response. `models.<role>.enable_thinking` (default true) and
`thinking_budget_tokens` control reasoning; `models.<role>.stream` (default
true) controls streaming/TTFT measurement. The experiment evidence and the
recommendation for these defaults are in `EXPERIMENTS.md`.

The trace id is generated before context construction so memory usage rows,
traces, and model-call events share an id. Each execution attempt is stored
before dispatch as `in_flight` and completed with `started_at`, `ended_at`,
`confirmation`, and a monotonic `sequence` used for deterministic ordering.

## Persistence layout

| Path under data dir | Contents |
| --- | --- |
| `settings.yaml` | Fully materialized setting tree |
| `settings_history.jsonl` | Append-only `SettingChange` rows |
| `events/events.jsonl` | Append-only `Event` rows |
| `traces/trc_*.json` | One trace per interactive model call |
| `sessions/session.json` | Session id/timestamps/subject hits |
| `sessions/session_meta.json` | Cooldown metadata |
| `sessions/interactions.jsonl` | User rows plus assistant-update rows |
| `registry/domains/*.json` | Domain records, including reserved `self` |
| `registry/projects/*.json` | Project records and workspace pointer |
| `registry/workspaces/*.json` | Workspace notes metadata |
| `registry/suggestions/*.json` | Suggested domains/projects and status/cooldown |
| `tasks/tsk_*.json` | Task state, constraints, outcome, flags, sequence |
| `persona/persona.json` | Current persona version |
| `persona/history.json` | Persona version history |
| `persona/proposals/per_*.json` | Proposed persona edits |
| `capabilities/attempts/op_*.json` | Durable execution attempt + evidence |
| `memory/records/mem_*.json` | Memory records and lifecycle |
| `memory/usage.jsonl` | Memory usage rows with task/trace ids |
| `consolidation/run_*.json` | Consolidation run, proposals, results |
| `consolidation/run_*.input.json` | Exact evidence snapshot for rollback/audit |
| `eval/<tag>/...` | Isolated per-scenario-per-repeat data dirs |
| `eval/results/<tag>.json` | Eval config, summary, and run details |
| `experiments/results/<tag>.json` | Experiment config, summary, per-run traces, fixed scenario set |
| `experiments/results/cold_start_idle.json` | Idle/warm/concurrent cold-start probe samples and model state |
| `experiments/results/cold_start_unload.json` | Forced-unload reload measurement |
| `experiments/results/summary.json` | Cross-experiment configs and summaries |

## Adding a component

1. Add types/protocol to `harness/contracts/` if another component needs it.
2. Implement it under its own package without importing another component's
   concrete module.
3. Wire the implementation in `harness/app.py`.
4. Add deterministic tests under `tests/` and a protocol check if the manager is
   replaceable.

## Adding a capability

- Built-in Python: add an `OperationSpec`/manifest and a dispatch branch in the
  executor, keeping validation and permission metadata in the manifest.
- Skill: add `<skills_dir>/<name>/SKILL.md` (instructions only, no operations).
- MCP: declare a stdio server in the YAML file; tools become operations and the
  configured permission map controls policy. The harness does not bypass
  enablement or global permission policy for MCP calls.
