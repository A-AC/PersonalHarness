# Code map

`harness/` is organized around replaceable manager contracts. Concrete
components import other components only through `harness/contracts/*` (plus
shared structural modules such as `constants`, `storage`, `text`, `timefmt`,
and `prompts`). `harness/app.py` is the composition root; `harness/ui/web.py`,
`harness/measure.py`, `harness/eval.py`, and `harness/__main__.py` sit at the
composition/edge layer.

## Entry points and composition

| Path | Responsibility |
| --- | --- |
| `harness/__main__.py` | CLI: `serve`, `health`, `turn`, `consolidate`, `measure`, `eval`, `reset` |
| `harness/app.py` | `Harness` container and `build_harness()` wiring |
| `harness/constants.py` | Schema version, ID prefixes, enums, reserved `self` id |
| `harness/storage/atomic.py` | UTC timestamps, safe IDs/filenames, atomic JSON/text, JSONL append |
| `harness/text.py` | Tokenization, reasoning stripping, overlap, token estimate, keyword matching, success-claim/unknown/placeholder checks shared by guards and evals |
| `harness/timefmt.py` | UTC storage timestamps rendered as compact local markers, current-time block, human gap phrases, and summary window labels |
| `harness/prompts.py` | Full and trimmed action instructions, task-plan instructions, and background summary instructions |

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
| `chat.py` | Turn event/listener, turn detail with retrieved memories and writes, and the single turn-service protocol |
| `capabilities.py` | Manifest, operation spec, execute request, executor protocol |
| `consolidation.py` | Consolidation run and service protocol |
| `context.py` | Context package/section/inclusion and builder protocol |
| `domains.py` | Domain/project/workspace/draft/suggestion and registry protocol |
| `events.py` | Event and event-log protocol |
| `history.py` | History note/segment/view and the replaceable history-service protocol |
| `loop.py` | Turn result and interaction-loop protocol |
| `maintenance.py` | Removed-state/report and the reset-service protocol |
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
| `harness/session/manager.py` | Session resume, append-only interactions with user/assistant timestamps, subject hits |
| `harness/session/turns.py` | `DefaultTurnService`: one serialized turn entry point for CLI and web, progress listener, trace/memory detail read model |
| `harness/domains/registry.py` | Self domain, explicit structure creation, suggestions, enablement |
| `harness/tasks/manager.py` | Task files, valid transitions, dependency release, sequencing |
| `harness/profile/provider.py` | Persona versions and proposed edits |
| `harness/history/store.py` | One atomic JSON file per stored history segment/summary |
| `harness/history/service.py` | Budgeted history strategies (`full`, rolling `summary`, gap `segment`), compact time/gap markers, off-path summary generation and fallback |
| `harness/memory/core.py` | Candidate validation, classifier-driven typing, hint/scope fallback, duplicate/conflict policy, lifecycle, keyword retrieval with recency tie-break, usage, consolidation maintenance, rollback |
| `harness/memory/classifier.py` | Replaceable deterministic (keyword-rule) memory type classifier |
| `harness/memory/file_manager.py` | File-backed memory records and usage JSONL |
| `harness/memory/inmemory.py` | Non-persistent parity implementation for swaps/tests |
| `harness/memory/tokens.py` | Token/overlap aliases for retrieval |
| `harness/context/builder.py` | Budgeted context sections, scopes, prompt variant, local current-time block, task created/memory updated age markers, bounded history, capability disclosure with enum values/defaults |
| `harness/capabilities/mock.py` | Pure-Python mock operations |
| `harness/capabilities/harness_ops.py` | Memory and structure manifests; memory enums/defaults generated from `constants` |
| `harness/capabilities/executor.py` | All-field validation, hint coercion/pass-through, placeholder rejection, task-derived memory scope, enablement, permissions, dispatch, attempt records |
| `harness/capabilities/skills.py` | `SKILL.md` discovery |
| `harness/capabilities/mcp.py` | Optional stdio MCP discovery/call via the official SDK |
| `harness/orchestrator/parse.py` | Pydantic parsing, loose/strict proposal JSON schemas, actionable parse errors |
| `harness/orchestrator/machine.py` | Allowed-state and action-transition rules |
| `harness/orchestrator/loop.py` | Turn handling, structure commands, heuristic/LLM planning, task loop, retry cap, honesty/internal-schema guards, turn timeline and metrics |
| `harness/orchestrator/timeline.py` | Per-turn stage recorder (start offset, duration, detail) serialized into traces |
| `harness/consolidation/service.py` | Evidence snapshot, optional model extraction, deterministic retype/merge/supersede pass, run report, rollback |
| `harness/maintenance/reset.py` | Full/selective state wipe, timestamped backup archive, seed re-application, stale-reference scrub, reset audit event |
| `harness/model/scripted.py` | Deterministic test adapter |
| `harness/model/openai_http.py` | OpenAI-compatible HTTP adapter and health probe |
| `harness/measure.py` | Learning report from stored events/tasks/memories/usage |
| `harness/eval.py` | Isolated live eval, deterministic checks, long-history real-use and time-awareness scenarios, scripted honesty cases, p50/p95, malformed-output, prompt-token, and token metrics |
| `harness/ui/web.py` | Authenticated server-rendered inspection/control UI (viewport-sized chat panel with a scrollable log, collapsible live strip, and composer pinned to the panel bottom; completed turns are appended from server-rendered fragments over SSE without reloading, memory actions, on-demand/scheduled consolidation, stored conversation history, turn timeline, execution envelope, confirm-gated reset); chat streams stage/token events over SSE and links messages to traces and back; malformed control requests return 4xx with a message |

## Turn data flow

```text
user text (CLI `turn` or web chat)
  -> DefaultTurnService.submit (one lock; shared by both clients)
  -> SessionManager.record_interaction (append-only, UTC ts)
  -> structure command?  yes -> DomainRegistry.create_* / confirm / decline
                         no  -> short single request in heuristic mode?
                                  yes -> TaskManager.create(one code task)
                                  no  -> ModelAdapter.complete(task plan)
                                        -> TaskManager.create(sequence, deps)
  -> TaskManager.next_ready
  -> ContextBuilder.build(task, trace_id)
       persona + preferences + domain/project/workspace + task
       + local current time + bounded history (full/summary/segment, compact ts
       and gap markers, memory updated/age markers)
       + enabled capability manifests with enum values/defaults;
       budget overflow records exclusions; prompt variant selects full/trimmed rules
  -> ModelAdapter.complete(proposal, schema=strict|loose, on_delta=...)
  -> parse_proposal -> orchestrator.machine.apply_action
       internal-schema ask? -> reject and retry
       act -> CapabilityExecutor.execute
              validate every field, coerce/default optional hints, reject placeholders
              memory hint inputs pass through to the memory core for precise fallback notes
              -> attempt file + event
  -> response/end after a non-succeeded execution? -> honesty guard rejects success claims
  -> repeat until response/ask/end or a limit
  -> TraceStore.put + model.call event
  -> SessionManager.append_assistant (UTC assistant ts)
  -> HistoryService.after_turn (off-path rolling summary, best effort)
  -> TurnResult(messages, tasks)
```

While a turn runs, `DefaultTurnService` forwards `turn.started`, `stage`
completion/start markers, raw `token` deltas, and a final
`turn.completed`/`turn.failed` payload to the optional listener. The web chat
uses one SSE channel per turn job backed by this listener; the terminal calls
the same `submit` without a listener. Completed payloads carry trace ids,
execution envelopes, retrieved memory ids, and memory writes (with undo ids).
The chat page appends the completed turn by fetching its server-rendered
fragment (`/chat/interactions/<id>`) instead of reloading the document. The
chat page resolves all visible details with one recent trace scan
(`detail_many`) and scans per message only for older focused links.

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
failure response. `loop.honesty_guard` (default true) rejects a final response
that claims success after the last execution failed, was denied/unavailable, or
has an unknown outcome; unknown outcomes must be stated as unknown.
`loop.internal_schema_ask_guard` (default true) rejects an `ask` that makes the
user choose an internal memory type, scope, capability, operation, or argument.

`models.<role>.enable_thinking` (default true) and `thinking_budget_tokens`
control reasoning; `models.<role>.stream` (default true) controls streaming/TTFT
measurement. `models.interactive.response_format` selects schema-constrained
JSON, JSON-object mode, or unconstrained text for the decoding experiment;
`models.interactive.action_schema` selects the strict per-kind proposal schema
or the loose V1 schema. `context.prompt_variant` selects the full or trimmed
instruction block, and `context.include_time` adds the current local time to the
task section using `context.timezone` (default `America/Mexico_City`, falling
back to UTC if the zone database is missing or invalid); `context.time.gap_hours`
controls when history lines get a relative "N hours later" marker.
`context.budgets.history` caps the history section. `context.history.strategy`
selects `full`, rolling `summary` (default), or gap-based `segment`;
`verbatim_turns`, `segment_gap_hours`, `summary_tokens`,
`summary_batch_turns`, `summaries`, `background_summary`,
`max_message_chars`, and `summary_input_tokens` tune how much recent text is
kept and how older turns are compressed. Summaries are stored per segment and
generated in a daemon thread after the response, or explicitly between eval
turns; a missing or failed summary falls back to verbatim truncation and never
blocks a turn. `capabilities.disclose_enum_values` renders
allowed values inline from the operation metadata;
`capabilities.coerce_optional_arguments` turns invalid optional values into
defaults or harness-derived omissions; `capabilities.reject_placeholders`
rejects required values that still contain `<...>`. The selected defaults are
`response_format=json_schema`, `action_schema=strict`, `prompt_variant=full`,
`include_time=true`, all three capability switches on, and both loop guards on.
Memory policy is settings-driven: `memory.duplicate_overlap`,
`memory.conflict_containment`/`conflict_overlap`, classifier rules,
`memory.consolidation.retype`, `consolidation.extract_enabled`,
`consolidation.schedule_enabled`/`schedule_interval_s`, and the source policy.
`ui.chat_stream_tokens` controls raw token display in the chat panel,
`ui.sse_heartbeat_s` controls SSE keepalive comments,
`ui.chat_trace_scan` sets how many recent traces the chat page loads once for
all visible messages (0 restores the per-message scan), and `ui.page_limit`
bounds chat rows and retained finished turn jobs.
`maintenance.reset.backup` (default true) controls the timestamped archive
written before a wipe, and `maintenance.reset.confirmation_word` (default
`RESET`) is the word the UI requires. The experiment evidence is in
`EXPERIMENTS.md`.

The trace id is generated before context construction so memory usage rows,
traces, and model-call events share an id. Each execution attempt is stored
before dispatch as `in_flight` and completed with `started_at`, `ended_at`,
`confirmation`, and a monotonic `sequence` used for deterministic ordering.

## Memory lifecycle

`harness_memory.remember` exposes only `content` as required. `type` and `scope`
are optional hint inputs: enum validation never fails the call for them. The
memory core assigns `type` with the configured classifier
(`memory.classifier.rules`, `memory.classifier.default_type`) and computes the
default scope from project/domain/self context before honoring a valid hint.
Rejected hints are written to `hint_notes` on the record and returned as
`argument_notes`; explicit `self` is ignored outside an assistant-focused task.

An explicit write activates immediately. Provenance is harness-filled
(`source_kind=user_statement`, `source_refs=[interaction_id]`) and still passes
the resolved-source and policy checks. Exact duplicate content in the same scope
returns the existing active record. A newer explicit statement that passes both
`memory.conflict_containment` and `memory.conflict_overlap` supersedes the older
one; the execution message reports the replaced content. Statuses are only
`active`, `superseded`, and `retracted`.

Consolidation (`harness/consolidation/service.py`) runs off the turn path. It
optionally asks the background model for inferred memories (verified source
refs, `origin=inferred`, `run_id` attached), then runs the deterministic pass:
re-type, merge exact/near-duplicates, and supersede older conflicting records by
recency. Newest active records win; older rows remain as history with
`superseded_by`. Each run writes an evidence snapshot and a report of added,
merged, and superseded rows. Rollback retracts records created by the run,
restores merged/superseded losers, and restores previous types; inferred rows
can also be retracted individually. The UI exposes manual run/rollback and can
start a periodic run when `consolidation.schedule_enabled` is true.

Removed from V1: the proposed/approve/reject lifecycle, the memory approval
queue and approve endpoint, inferred-vs-explicit approval gating, and the
`auto_commit` settings. Malformed memory/consolidation control requests now
return 400/404 with a message instead of raising through the ASGI handler.

## Reset and state ownership

All durable assistant state is rooted at the launch data directory
(`--data-dir`, `HARNESS_DATA_DIR`, or `./data`): settings and change history,
events, sessions, history summaries, traces, domain registry, tasks, persona,
capability attempts, memories, and consolidation runs. The only non-durable
state is the web UI's in-memory turn-job buffer. `eval/` and `experiments/`
trees are measurement artifacts, not learned assistant state. `experiments/data`
and `tmp_smoke` in the repository are gitignored test outputs outside the
runtime tree.

`FileResetService` (`harness/maintenance/reset.py`) is the single maintenance
boundary. It depends only on manager contracts and quiesce gates supplied by the
composition root; it imports no concrete component.

| Scope | Removes | Re-seeds / keeps |
| --- | --- | --- |
| `full` | settings file + history, sessions, traces, history, events, registry, tasks, persona, capability attempts, memories, consolidation | default settings from `settings/catalog.py`, code-defined `DEFAULT_PERSONA`, reserved `self` domain, a new session |
| `memories` | `memory/`, `consolidation/`, memory ids in retained traces/events/attempts | conversations, tasks, domains, persona, settings stay |
| `conversations` | sessions, traces, history, events, capability attempts, tasks, consolidation; memory usage and conversation source refs | memories, domains, persona, settings stay; a new session is created |

Before a wipe (unless `maintenance.reset.backup` is false or the CLI passes
`--no-backup`) it writes `backups/reset_<scope>_<UTC timestamp>.zip` containing
the runtime tree plus a `_reset_manifest.json`. Archives, `eval/`, and
`experiments/` are excluded from subsequent archives. `ResetReport` lists every
removed top-level path with file/byte counts, the backup path, seeded items,
cleared stale references, and duration; the CLI prints it, the UI renders it,
and a `reset.run` event records the same data.

Safety: reset enters the turn service's exclusive epoch gate first, so an
in-flight turn finishes before the wipe and turns queued behind it are rejected;
it also waits for history-summary and consolidation workers. A conversation
reset drops conversation-derived tasks instead of leaving them pointing at
missing interactions. A memory reset scrubs memory ids from retained traces,
events, and attempts and drops memory-operation attempts. A conversation reset
clears memory usage and strips removed interaction/attempt/event ids from memory
provenance into `provenance.reset_cleared_refs`. The UI requires the configured
confirmation word and returns 409 for mutating requests while a reset runs.
The CLI is `harness reset [--scope full|memories|conversations] [--yes]
[--no-backup]`.

## Persistence layout

| Path under data dir | Contents |
| --- | --- |
| `settings.yaml` | Fully materialized setting tree; keys listed in `RETIRED_SETTINGS` are dropped on load, other unknown keys fail startup |
| `settings_history.jsonl` | Append-only `SettingChange` rows |
| `events/events.jsonl` | Append-only `Event` rows |
| `traces/trc_*.json` | One trace per interactive model call |
| `sessions/session.json` | Session id/timestamps/subject hits |
| `sessions/session_meta.json` | Cooldown metadata |
| `sessions/interactions.jsonl` | User rows plus assistant-update rows, each with UTC timestamps |
| `history/segments/seg_*.json` | Stored history segment, covered interaction ids, rolling summary, status/error, and summary token counts |
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
| `backups/reset_*.zip` | Timestamped pre-reset archive and `_reset_manifest.json` |
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
  Declare `enums`/`defaults` on the spec so prompt disclosure and runtime
  validation use the same source of truth.
- Skill: add `<skills_dir>/<name>/SKILL.md` (instructions only, no operations).
- MCP: declare a stdio server in the YAML file; tools become operations and the
  configured permission map controls policy. The harness does not bypass
  enablement or global permission policy for MCP calls.
