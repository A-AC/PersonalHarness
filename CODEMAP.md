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
| `harness/__main__.py` | CLI: `serve`, `health`, `turn`, `tick`, `status`, `consolidate`, `review`, `skills`, `measure`, `eval`, `reset` |
| `harness/app.py` | `Harness` container and `build_harness()` wiring, including the structure hook (enter/leave/suggest with duplicate and cooldown checks), the learning stack (journal, skill library, pipeline, review), the persona safety guards, and the post-reset skill refresh |
| `harness/constants.py` | Schema version, ID prefixes, enums, reserved `self` id |
| `harness/storage/atomic.py` | UTC timestamps, safe IDs/filenames, atomic JSON/text, JSONL append |
| `harness/text.py` | Tokenization, reasoning stripping, overlap, normalized name matching (`normalize_name`, `name_overlap`, `match_by_name`), token estimate, keyword matching, success-claim/unknown/placeholder checks shared by guards and evals |
| `harness/timefmt.py` | UTC storage timestamps rendered as compact local markers, current-time block, human gap phrases, and summary window labels |
| `harness/prompts.py` | Full and trimmed action instructions, task-plan instructions, and background summary instructions |

## Measurement scripts

| Path | Responsibility |
| --- | --- |
| `scripts/experiments.py` | Writes each experiment config, runs the fixed eval set, saves and tabulates results |
| `scripts/cold_start_probe.py` | Idle/warm/concurrent llama-server latency probe with model-status and router hints |

## Test suites

| Path | Responsibility |
| --- | --- |
| `tests/conftest.py` | `make_harness`, scripted `plan`/`act`/`ask`/`respond` builders, settings writer, and the shared baseline fixture |
| `tests/test_end_to_end.py` | 18 integrated scenarios from user input through planning, execution, persistence, restart, and recovery; uses scripted model output only where a real model call is required |
| `tests/test_evaluation_audit.py` | 15 known-answer measurements for decision resolution, memory retrieval, project association, task recovery/state, permission enforcement, and learning/rollback |

The Phase 5 measured results, fixture sizes, and caveats are in
[README.md](README.md#phase-5-audit-measured-results). The audit tests assert
small labeled datasets; they are regression evidence, not a general
intelligence benchmark.

## Contracts

`harness/contracts/` contains dataclasses, protocols, and shared error types.
It has no concrete imports.

| File | Contract |
| --- | --- |
| `actions.py` | Five action types and `ExecutionResult` |
| `chat.py` | Turn event/listener, turn detail with retrieved memories and writes, and the single turn-service protocol |
| `capabilities.py` | Manifest, operation spec, execute request, argument validation, executor protocol |
| `consolidation.py` | Consolidation run and service protocol |
| `context.py` | Context package/section/inclusion and builder protocol |
| `control.py` | Control-plane directive, action gate, approved action, and coordinator protocol |
| `decisions.py` | Decision record, store/resolver protocols, and deterministic interpretation |
| `domains.py` | Domain/draft/suggestion, `DomainMatch`, the read-only `DomainInference` protocol, and the registry protocol |
| `events.py` | Event and event-log protocol |
| `history.py` | History note/segment/view and the replaceable history-service protocol |
| `learning.py` | Learning signal/outcome/journal entry, skill draft/record/version, review item/report, and the journal/library/pipeline/review protocols |
| `loop.py` | Turn result and interaction-loop protocol |
| `maintenance.py` | Removed-state/report and the reset-service protocol |
| `memory.py` | Memory record/candidate/submit/usage (with lifecycle metadata), `RankedMemory` with `score_parts`, the optional `SemanticScorer` contract, and the manager protocol |
| `model.py` | Message/result, model adapter protocol, `ModelError`/`ModelUnavailable` |
| `profile.py` | Persona, draft, proposal, version, profile protocol |
| `scheduler.py` | Tick item/report, work item/report, decision item, and the scheduler protocol |
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
| `harness/session/manager.py` | Session resume (including old files without an active domain), append-only interactions with user/assistant timestamps, subject hits, persisted active domain |
| `harness/session/turns.py` | `DefaultTurnService`: one serialized turn entry point for CLI and web, progress listener, trace/memory detail read model, plus `run_ready` so the scheduler executes eligible work under the same lock and epoch gate |
| `harness/domains/registry.py` | Self domain, explicit structure creation, suggestions, normalized suggestion cooldown, enablement, and a best-effort suggestion listener that mirrors lifecycle changes into the decision log |
| `harness/domains/inference.py` | Read-only project inference: additive name/memory/task/history evidence plus a small active-domain bonus, confidence threshold and ambiguity margin, short project summaries; never creates, moves, or renames anything |
| `harness/tasks/manager.py` | Task files, valid transitions (including `deferred` and reason-only blocked changes), dependency release, priority-aware sequencing, checkpoints (bounded), deferral windows, counted resumes, running-task domain reassignment; legacy rows with removed structure keys load by filtering to current fields |
| `harness/control/decisions.py` | File-backed decision log (`decisions/dec_*.json`), open/terminal queries, task/suggestion lookup, suggestion mirroring |
| `harness/control/resolver.py` | Deterministic reading of a reply in decision context: explicit/contextual selection, ambiguity, approval/rejection/modification/defer/cancel, and question vs unrelated request. Questions that refer to the decision set itself ("which one?", "what are my options?") are clarifications, never new planner work. |
| `harness/control/plane.py` | Turn coordination: structure commands, decision resolutions (including reconciliation decisions for interrupted/unknown-outcome attempts), plan/approved-action directives, confirmation gating, task deferral on a deferred decision, execution recording, task cancellation |
| `harness/profile/provider.py` | Persona versions and proposed edits, with an injectable approve-time safety guard |
| `harness/history/store.py` | One atomic JSON file per stored history segment/summary |
| `harness/history/service.py` | Budgeted history strategies (`full`, rolling `summary`, gap `segment`), compact time/gap markers, off-path summary generation and fallback |
| `harness/memory/core.py` | Candidate validation, classifier-driven semantic typing, orthogonal kind derivation/hint fallback (procedural hints normalize non-procedure subtypes to `lesson`), kind-aware duplicate/conflict policy (near-duplicate merges also require the same semantic subtype so wording overlap never conflates a fact with a preference), lifecycle metadata persistence and derived lifecycle view, hybrid explainable retrieval (exact identifiers, lexical, optional semantic, scope priority, recency/provenance/kind weights, conflict and validity suppression), usage, consolidation maintenance (semantic-only retype), rollback |
| `harness/memory/semantic.py` | Pluggable `SemanticScorer` implementations: dependency-free lexical-semantic scorer (configurable concept groups, stemming, character n-grams) and the off switch; a deployment can substitute an embedding-backed scorer at the composition root |
| `harness/memory/capture.py` | Deterministic execution-outcome capture: failure fingerprint dedupe, task-derived scope, resolving-success supersede, `memory.capture` events, no model call |
| `harness/memory/classifier.py` | Replaceable deterministic (keyword-rule) memory type classifier plus the deterministic type-to-kind mapping |
| `harness/memory/file_manager.py` | File-backed memory records and usage JSONL; legacy rows without `kind` derive it on load |
| `harness/memory/inmemory.py` | Non-persistent parity implementation for swaps/tests, including legacy kind derivation |
| `harness/memory/tokens.py` | Token/overlap aliases for retrieval |
| `harness/context/builder.py` | Query-driven budgeted context: cross-project memory retrieval with priority scopes (self stays on demand), per-item memory trimming that drops whole blocks, project summary and stats, pending decisions for the task or its project protected from trimming, bounded active-domain directory that drops whole entries, persona-trim fallback when the total budget cannot fit the safety instructions, prompt variant, local current-time block, age markers, memory tags with kind/lifecycle, bounded history, capability disclosure with enum values/defaults, precondition-gated skill disclosure (a skill whose required tokens are absent from the task goal is excluded with a recorded reason), and inclusion/exclusion/budget stats. Skill blocks are emitted before ordinary capability blocks so end-of-section clipping cannot silently drop a disclosed learned procedure; the skill's operations still pass the normal permission and confirmation checks. |
| `harness/capabilities/mock.py` | Pure-Python mock operations |
| `harness/capabilities/harness_ops.py` | Memory and structure manifests (`enter_domain`, `leave_domain`, `suggest_domain`); memory enums/defaults generated from `constants` |
| `harness/capabilities/executor.py` | All-field validation, hint coercion/pass-through, placeholder rejection, task-derived memory scope, enablement, permissions, dispatch, attempt records, one-scan attempts-by-task index, skill discovery from the configured or default active-skills dir with a `reload_skills` refresh, and a best-effort explicit-write observer hook for learning journaling |
| `harness/capabilities/skills.py` | `SKILL.md` discovery with front-matter name/description/preconditions parsing |
| `harness/capabilities/mcp.py` | Optional stdio MCP discovery/call via the official SDK |
| `harness/orchestrator/parse.py` | Pydantic parsing, loose/strict proposal JSON schemas, actionable parse errors |
| `harness/orchestrator/machine.py` | Allowed-state and action-transition rules |
| `harness/orchestrator/loop.py` | Turn handling through the control plane, approved-action dispatch, heuristic/LLM planning with the domain directory, active-domain hint plus project inference for tasks with no planner domain, multi-word suggestion gate, task loop, retry cap, confirmation gate, honesty/internal-schema guards, `run_ready` background execution path (same per-task loop, checkpoint per run, turn trace), turn timeline and metrics. Project inference is logged as `plan.domain_inference` with `resolved: true` for a confident match and `resolved: false` plus `status: ambiguous`/`uncertain` and candidates when it refuses to route; the session selection is never changed. |
| `harness/orchestrator/timeline.py` | Per-turn stage recorder (start offset, duration, detail) serialized into traces |
| `harness/consolidation/service.py` | Evidence snapshot, optional model extraction (persona/strategy edits pass an injectable safety guard before staging), deterministic retype/merge/supersede pass, run report, rollback (run listing excludes `.input.json` snapshots) |
| `harness/learning/journal.py` | File-backed learning journal: one JSON entry per learning decision (signal kind, category, decision, rationale, source evidence refs, created-artifact refs) with an event mirror |
| `harness/learning/strategy.py` | Deterministic safety invariants for learned strategy/skill text (honesty, permissions, confirmation gates, no settings/code self-modification, no auto-execution of unverified code, memory/tool text is data); validators for persona drafts and skill text |
| `harness/learning/skills.py` | `FileSkillLibrary`: staged skill lifecycle (propose/record_test/approve/reject/retire/rollback) with safety-scanned text, slug names, overlap guards against trivial variations, provenance and version history, candidate text kept out of the executor's discovery dir, an untested candidate can never replace an approved skill, and an on-change disclosure refresh callback |
| `harness/learning/pipeline.py` | `EvidenceLearningPipeline`: one deterministic entry point that classifies a signal into memory/procedure/strategy learning, gates on evidence strength (thresholds from settings; model explanations are never accepted as verified root causes), creates the candidate through the owning store, applies it at the right authorization level (memory can apply, procedures and strategy only stage), journals every decision with its source refs, and journals explicit corrections through the executor observer hook. Learned procedure preconditions are derived from content words shared by the observed task goals, excluding stopwords, generic task verbs, and capability/operation names, with the old capability token only as a fallback when no goal evidence exists. |
| `harness/learning/review.py` | Deterministic, no-model learning review over persisted evidence: succeeded workflows repeated across enough distinct tasks become staged skill candidates (a failed step disqualifies the sequence), carrying the observed task goals as provenance so relevance tokens can be derived; repeated failure fingerprints, pending candidates, and expired knowledge surface as findings; with no meaningful evidence it creates nothing |
| `harness/maintenance/reset.py` | Full/selective state wipe (learning journal cleared on all scopes; the skills tree only on full), timestamped backup archive, seed re-application, stale-reference scrub, reset audit event, and an injectable post-reset hook that refreshes executor skill disclosure and drops conversation-wiped skill candidates |
| `harness/scheduler/service.py` | `WorkScheduler`: deterministic heartbeat (startup reconcile + ticks) over persisted tasks/decisions/attempts — unknown-outcome reconciliation decisions, dependency re-checks, deferred-window release, bounded/cooled requeues, one-time due reminders, scheduled consolidation, the optional scheduled learning review (deterministic, never model-backed), and bounded autonomous execution through the turn service; honest-status read model (`report()`), daemon-thread start/stop, and an `exclusive` reset gate. A tick folds reconcile/requeue/reminder/maintenance findings into one `scheduler.tick` event, and a retry-limit exhaustion is reported and checkpointed only once rather than on every scan. |
| `harness/model/scripted.py` | Deterministic test adapter |
| `harness/model/openai_http.py` | OpenAI-compatible HTTP adapter and health probe |
| `harness/measure.py` | Learning report from stored events/tasks/memories/usage plus learning-journal and skill-library counters |
| `harness/eval.py` | Isolated live eval, deterministic checks, long-history real-use and time-awareness scenarios, scripted honesty cases, p50/p95, malformed-output, prompt-token, and token metrics |
| `harness/ui/web.py` | Authenticated server-rendered inspection/control UI (viewport-sized chat panel with a scrollable log, collapsible live strip, composer pinned to the panel bottom, and an active-domain selector outside the SSE path; completed turns are appended from server-rendered fragments over SSE without reloading, memory actions, on-demand/scheduled consolidation, stored conversation history, turn timeline, execution envelope, confirm-gated reset); chat streams stage/token events over SSE and links messages to traces and back; malformed control requests return 4xx with a message |

## Turn data flow

```text
user text (CLI `turn` or web chat)
  -> DefaultTurnService.submit (one lock; shared by both clients)
  -> SessionManager.record_interaction (append-only, UTC ts)
  -> ControlPlane.begin_turn
       sync suggestions into the decision log
       explicit structure command? -> handled by the registry
       open decisions? -> resolver says approve/reject/modify/defer/cancel/
                          clarify; unrelated input falls through to planning
       task awaiting the user? -> answer/resume that question first
  -> (plan) structure command?  yes -> create / confirm / decline, or
                                enter/leave/switch an existing domain
                         no  -> short single request in heuristic mode?
                                  yes -> TaskManager.create(one code task)
                                  no  -> ModelAdapter.complete(task plan with directory)
                                        -> TaskManager.create(sequence, deps)
                                 a task with no planner domain: infer the
                                 likely existing project (name/memory/task/
                                 history evidence, active domain as a small
                                 bonus), fall back to the active domain when
                                 nothing is confident, log plan.domain_inference
  -> TaskManager.next_ready
  -> ContextBuilder.build(task, session, trace_id)
       persona + preferences + bounded structure directory
       + cross-project memory retrieval (priority scopes, per-item budget
       trimming, lifecycle-tagged memory blocks, usage rows)
       + current project summary + task + pending decisions for the task
       or its project (protected from trimming)
       + local current time + bounded history (full/summary/segment, compact ts
       and gap markers, memory updated/age markers)
       + enabled capability manifests with enum values/defaults;
       budget overflow records exclusions; the total-budget drop order is
       history, structure, domain, preferences, capabilities, then a persona
       trim before ever touching the safety instructions;
       prompt variant selects full/trimmed rules
  -> ModelAdapter.complete(proposal, schema=strict|loose, on_delta=...)
  -> parse_proposal -> orchestrator.machine.apply_action
       internal-schema ask? -> reject and retry
       act -> ControlPlane.gate_action
              confirmation required? -> persist Decision, block task, deliver prompt
              approved/not gated -> CapabilityExecutor.execute
              validate every field, coerce/default optional hints, reject placeholders
              memory hint inputs pass through to the memory core for precise fallback notes
              -> attempt file + event; record the outcome against the decision
  -> response/end after a non-succeeded execution? -> honesty guard rejects success claims
  -> repeat until response/ask/end or a limit
  -> (approve) ApprovedAction -> same capability executor, no new authorization
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
input. Both paths treat `session.active_domain_id` as the fallback scope: a
task with no planner domain first goes through project inference
(`domain.inference.*`), which scores name, project-scoped memory, open-task,
and recent-history evidence plus a small active-domain bonus, associates the
new task only on a confident unambiguous match, and logs the decision as a
`plan.domain_inference` event. The session selection is never changed and
existing tasks are never reassigned. The planner prompt carries
the same domain directory as the context and records unresolved names as
`plan.structure_miss` events before falling back to the active domain. `loop.stop_after_nonretryable_failure` (default true) rejects further
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
`context.budgets.history` caps the history section. `context.budgets.structure`
(default 300) caps the active-domain directory; overflow drops whole
directory entries, and the total-budget drop order is history, structure,
domain, preferences, capabilities, followed by a persona trim so safety
instructions survive an otherwise impossible total budget.
`context.retrieval.candidate_multiplier`/`min_candidates` set how many memory
candidates are considered before per-item trimming, `context.project.*` bounds
the project summary, and `context.pending_decisions.*` bounds and scopes the
pending-decision block (task-linked, plus same-project decisions when
`include_project` is true). Retrieval weights live under
`memory.retrieval.*` (`lexical`, `semantic`, `exact`, `scope`, `recency`,
`provenance`, `kind`, plus thresholds and the optional concept vocabulary);
`implementations.retriever` selects `hybrid` (default, with the
dependency-free lexical-semantic scorer) or `keyword` (scorer off).
`domain.inference.*` tunes project inference, and
`domain.suggestion.min_words` (default 2) stops one-word topics from ever
producing a domain suggestion. `domain.match_overlap` (default 0.8) is the
name overlap at which a suggestion is treated as a duplicate. `context.history.strategy`
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
`memory.consolidation.retype`, `memory.capture.execution_outcomes`,
`consolidation.extract_enabled`, `consolidation.schedule_enabled`/
`schedule_interval_s` (the scheduler heartbeat runs the periodic pass), and
the source policy. The `learning.*` group tunes the evidence thresholds
(`min_workflow_repeats`, `min_workflow_steps`, `min_preference_repeats`,
`min_failure_repeats`, `max_review_candidates`, `skill_name_overlap`) and the
scheduled review (`review_schedule_enabled` default false,
`review_interval_s`); all defaults keep the review opt-in exactly like
scheduled consolidation. The `scheduler.*` group configures the heartbeat:
`enabled`, `interval_s`, `max_tasks_per_tick` (0 keeps recovery/reminders but
disables autonomous execution), `retry_cooldown_s`, `max_task_retries`, and
`default_defer_hours`.
The type-to-kind mapping is structural and needs no setting.
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

## Persistent work and the heartbeat

`WorkScheduler` (`harness/scheduler/service.py`) is the single background
mechanism; it owns no execution path of its own. `reconcile()` runs at
startup (and as the first phase of every tick) and is purely deterministic:
it blocks interrupted tasks whose last attempt is still `in_flight` with
`unknown_outcome` plus a durable `reconciliation` decision, re-checks
dependencies (release stale blocks, fail dependents of failed/cancelled
dependencies), and releases deferred windows. `tick()` adds the recovery and
progress steps: requeue of recoverable work after `scheduler.retry_cooldown_s`
bounded by `scheduler.max_task_retries` (exhaustion marks the task failed
with a `retry_limit` checkpoint and a `task.retry_exhausted` event, once),
one-time `task.due` reminders, scheduled consolidation (the web app's old
private loop is gone; the heartbeat owns it), and finally up to
`scheduler.max_tasks_per_tick` ready tasks executed through
`DefaultTurnService.run_ready` — the same lock, model, action machine,
confirmation gate, and executor as a user turn. Open decisions always hold
their task: nothing waiting on the user, an approval, or a reconciliation
answer is auto-requeued or auto-run, and a deferred decision (not the clock)
resumes deferred work.

A tick applies reconcile, requeue, reminder, and maintenance steps to one
report and emits one `scheduler.tick` event for that pass; a quiet tick emits
none. `reconcile()` called on its own still emits its own event, because it is
a standalone public operation. Retry-budget exhaustion is checkpointed,
announced, and reported once; later ticks leave the failed task and its event
log unchanged.

```text
startup / scheduler.interval_s (serve thread) / harness tick (CLI)
  -> reconcile: unknown-outcome attempts -> reconciliation decision
  -> dependency re-check, deferred-window release
  -> recoverable requeue (cooldown + retry budget) or retry-limit failure
  -> due reminder (once per deadline)
  -> scheduled consolidation when due
  -> scheduled learning review when due (deterministic; no model calls;
     creates staged skill candidates at most, findings otherwise)
  -> run ready tasks via TurnService.run_ready (checkpoint + turn trace)
  -> scheduler.tick event (only when something happened)
```

`scheduler.enabled` gates the tick (startup recovery still runs, so
interrupted work is never mistaken for runnable); `max_tasks_per_tick: 0`
disables autonomous execution while keeping recovery and reminders. The
honest-status read model (`report()`, printed by `harness status` and shown
on the UI tasks page) answers what is working, waiting on the user, blocked
and why, deferred, due, failed, completed recently, and what needs approval,
with per-task attempt evidence summarized from one attempt-directory scan.
The reset service quiesces the scheduler first (`gates=[scheduler.exclusive,
chat.exclusive, ...]`) so a wipe never interleaves with a tick; the tick
thread holds the same lock for its whole duration and its runner takes the
turn lock, so background work is fully serialized with user turns.

## Memory lifecycle

`harness_memory.remember` exposes only `content` as required. `type` and `scope`
are optional hint inputs: enum validation never fails the call for them. The
memory core assigns `type` with the configured classifier
(`memory.classifier.rules`, `memory.classifier.default_type`) and computes the
default scope from domain/self context before honoring a valid hint.
Rejected hints are written to `hint_notes` on the record and returned as
`argument_notes`; explicit `self` is ignored outside an assistant-focused task.

`kind` is an orthogonal axis: `semantic`, `episodic`, `procedural`, or
`prospective`. `type` remains the semantic subtype. New writes persist the kind
derived from the assigned type (`procedure`/`lesson` -> `procedural`;
`fact`/`preference`/`decision` -> `semantic`), or a valid `kind_hint`; an invalid
hint falls back to the derived kind with a `hint_notes` entry. Legacy records
without `kind` derive it at load in both stores, so no migration write is
needed. Dedupe, conflict supersession, explicit `supersedes`, and consolidation
merge all require the same kind as well as the same scope/type. A procedural
hint that arrives with a non-`procedure`/`lesson` classifier subtype is
normalized to `lesson` in the core, so the kind/type axes stay consistent;
semantic writes and explicit `remember` calls (which pass no kind hint) are
unaffected.

Retrieval (`MemoryCore.retrieve`) is a single additive, explainable score:
exact identifier matches (hostname/IP/path/id via a shared regex), lexical
overlap, optional semantic similarity from the injected `SemanticScorer`
(off entirely when `implementations.retriever` is `keyword`), a scope-priority
bonus (other scopes stay eligible with a tiny penalty), recency decay,
provenance/evidence/confidence, and kind usefulness for the query. Every
component lands in `RankedMemory.score_parts` and the reason string, so usage
rows and traces explain why a memory was served. Superseded/retracted rows,
records outside their `valid_from`/`valid_until` window, and an older row whose
newer replacement is active are removed before ranking; `_suppress_conflicts`
drops an active row that a newer active row on the same subject beats, so an
old statement can never win on wording alone. The baseline semantic scorer
(`harness/memory/semantic.py`) is dependency-free: configurable concept groups,
light stemming, and character trigrams, with the generic vocabulary overridable
through `memory.retrieval.semantic_concepts`. An embedding-backed scorer can be
injected through the same `SemanticScorer` protocol without touching the core.

Records persist lifecycle metadata next to the status:
`evidence_status` (`stated`/`observed`/`inferred`/`corroborated`),
`confidence` (0..1), optional `valid_from`/`valid_until`, and `tags`.
`MemoryCore.lifecycle()` derives the explainable category — verified fact,
preference, decision, lesson, temporary event, unresolved hypothesis,
proposed commitment, superseded, retracted — from the stored fields, and the
context builder renders it on each memory tag. Inferred or low-confidence
rows rank below explicit corroborated ones and are never promoted to verified
facts; an explicit user correction supersedes the outdated record through the
normal conflict path.

Procedural capture (`harness/memory/capture.py`) is deterministic and needs no
model call. It records only `failed`, `denied`, `unavailable`, and `unknown`
executions, plus a `succeeded` execution that follows a non-succeeded attempt
of the same `capability.operation` within the same task; routine successes stay
silent. `memory.capture.execution_outcomes` (default true) and
`memory.source_policy.execution_outcome` (`enabled_structured_only` or
`disabled`) can both turn it off, and disabled means no write and no events.
`harness_memory` and `harness_structure` executions are excluded. The candidate
is `kind=procedural`, `origin=inferred`, with
`source_kind=execution_outcome`, `source_refs=[operation_id]`,
`evidence_status=observed`, and only the structured
`{status, error_category, message, capability, operation}` fields. Its content
is one deterministic sentence naming the operation and outcome (no user text),
and the default scope follows the task: a non-self domain, then self, then
global. Before writing a failure, active procedural records in that
scope are scanned and an existing record with the same
`(capability, operation, status, error_category)` fingerprint suppresses the
duplicate. A resolving success writes a resolution lesson and supersedes the
failure by `memory_id`; nothing is auto-retracted. Each created record emits
one `memory.capture` event with `target=memory_id`, `result=status`, and the
fingerprint data; memory text is never inlined into the execution envelope.

An explicit write activates immediately. Provenance is harness-filled
(`source_kind=user_statement`, `source_refs=[interaction_id]`) and still passes
the resolved-source and policy checks. Exact duplicate content in the same scope
and kind returns the existing active record. A newer explicit statement that
passes both `memory.conflict_containment` and `memory.conflict_overlap`
supersedes the older one; the execution message reports the replaced content.
Statuses are only `active`, `superseded`, and `retracted`.

Consolidation (`harness/consolidation/service.py`) runs off the turn path. It
optionally asks the background model for inferred memories (verified source
refs, `origin=inferred`, `run_id` attached), then runs the deterministic pass:
re-type, merge exact/near-duplicates, and supersede older conflicting records by
recency. Re-typing is limited to semantic-kind records; procedural, episodic,
and prospective rows keep their type and kind, and a classifier result that
would move a record to another kind is ignored. Merges require the same scope,
kind, and semantic subtype, so wording overlap alone never merges a fact with a
preference; conflicts additionally require the same type as before. Newest
active records win; older rows remain as history with `superseded_by`. Each run
writes an evidence snapshot and a report of added, merged, and superseded rows.
Rollback retracts records created by the run, restores merged/superseded losers,
and restores previous types; inferred rows can also be retracted individually.
The UI exposes manual run/rollback and can start a periodic run when
`consolidation.schedule_enabled` is true.

Removed from V1: the proposed/approve/reject lifecycle, the memory approval
queue and approve endpoint, inferred-vs-explicit approval gating, and the
`auto_commit` settings. Malformed memory/consolidation control requests now
return 400/404 with a message instead of raising through the ASGI handler.

## Learning lifecycle

Phase 4 adds an evidence-based learning pipeline on top of the mechanisms
above (it does not replace them). The core principle: learning must change
future behavior in a traceable way, and the system must be able to evaluate
whether the change helped — and revert it.

**Three separated categories.** A signal is classified into exactly one of:

- **Memory** (`user_correction`, `repeated_preference`, `rejected_behavior`,
  `recurring_commitment`, `verified_failure`, `transient_observation`): facts,
  preferences, decisions, lessons with provenance, scope, and temporal
  validity. Handled by the existing memory lifecycle.
- **Procedure** (`repeated_workflow`, `task_success`): a reusable workflow
  becomes a staged skill candidate, never a direct prompt change.
- **Strategy** (`strategy_proposal`): a persona/planning change is validated
  against safety invariants and staged as a persona proposal.

**Authorization levels.**

| Change | Level | Reversibility |
| --- | --- | --- |
| Explicit user memory statement/correction | automatic (user-authorized) | supersession/retraction; journaled |
| Harness-observed memory (repeated preference, verified failure past threshold) | automatic at low rank (`inferred` origin, `corroborated`/`observed` evidence) | retract; journaled |
| New skill candidate | staged (pipeline/review only creates it) | reject removes it; journaled |
| Skill activation (new or improvement) | approval-gated (`FileSkillLibrary.approve`) | rollback restores the previous version |
| Skill replacing an approved skill | approval-gated **plus** recorded passing test evidence | rollback |
| Persona/strategy change | approval-gated persona proposal; safety-guarded at proposal and approval | persona history; reject |

**The pipeline** (`harness/learning/pipeline.py`) is the single entry point:
classify → evidence gate → candidate creation through the owning store → apply
at the right level → journal. The gate never accepts a bare success claim
(`task_success` alone is insufficient) and never treats a model-generated
explanation as a verified root cause (`verified_failure` requires structured
execution evidence with the attempt operation ids). Explicit corrections that
arrive through the `harness_memory.remember`/`correct` operations are
journaled by the executor's best-effort observer hook.

**The skill library** (`harness/learning/skills.py`) keeps candidates under
`data/skills/candidates/<name>/SKILL.md`, outside the executor's discovery
directory; only `approve` writes to `data/skills/active/<name>/SKILL.md` (the
default discovery dir when `capabilities.skills_dir` is unset) and triggers a
disclosure refresh. Skill text is safety-scanned at proposal; names are
slug-shaped and overlapping variations are rejected. Every approval that
replaces an approved skill requires harness-verified test evidence on the
candidate, keeps the previous version on the retired record, and can be
rolled back.

**Precondition-gated disclosure.** A skill's front matter may declare
`preconditions` (required tokens). The context builder discloses a skill only
when the task goal contains them and records the omission as an exclusion
with the reason — a learned procedure is never reused for a task it does not
apply to. When the review learns a workflow, it derives those tokens from
content words shared by the observed task goals, excluding stopwords, generic
task verbs, and the workflow's own capability/operation names; the old
capability-name fallback is used only when no goal evidence exists. Skills are
instruction text with no operations, so they are disclosed without capability
enablement; every operation a skill names still goes through the normal
permission checks. Skill blocks are rendered before ordinary capability blocks
so the section's end-clipping cannot silently remove an already-disclosed
skill.

**Strategy safety.** `harness/learning/strategy.py` holds deterministic
invariant patterns (honesty/success claims, permission and confirmation
bypasses, guard disabling, settings/code self-modification, auto-execution of
unverified code, memory/tool-text-as-instruction). The pipeline rejects a
violating proposal outright, the consolidation service refuses to queue one,
and the profile provider blocks approval of any staged proposal that
violates them. The pipeline itself never writes settings, permissions, or
code — strategy learning can only produce persona text proposals.

**Background review** (`harness/learning/review.py`) is deterministic and
model-free. It scans attempts by task: a fully succeeded sequence in
completed tasks, repeated across `learning.min_workflow_repeats` distinct
tasks, becomes a skill candidate through the pipeline (bounded by
`learning.max_review_candidates`; a failed step disqualifies the sequence so
a failed procedure is never learned as a successful one). The candidate keeps
the observed task goals as provenance alongside the task/operation references.
Repeated failure
fingerprints, pending candidates, and expired knowledge are findings for the
user. A review with no meaningful evidence creates no candidates, writes no
journal entries, and emits no events. The scheduler runs it from the
heartbeat when `learning.review_schedule_enabled` is true (interval
`learning.review_interval_s`, tracked by the last `learning.review` event);
the CLI exposes the same pass as `harness review` and lists records as
`harness skills`.

**Traceability.** Every decision — applied, staged, rejected, insufficient,
or duplicate — writes one journal entry (`data/learning/journal/lrn_*.json`)
with the signal kind, category, rationale, source evidence refs
(interaction/attempt/task ids), and the ids of anything it created, mirrored
as a `learning.record` event. Skill records keep the reason and evidence in
`provenance`; memory records keep source refs as before. The journal is
cleared by every reset scope (its evidence rows are scope-dependent); the
skill tree is only cleared by a full reset, while a conversations reset drops
pending candidates whose evidence was wiped and keeps approved skills.


## Reset and state ownership

All durable assistant state is rooted at the launch data directory
(`--data-dir`, `HARNESS_DATA_DIR`, or `./data`): settings and change history,
events, sessions, history summaries, traces, domain registry, tasks, persona,
capability attempts, memories, consolidation runs, the learning journal, and
the skill library. The only non-durable state is the web UI's in-memory
turn-job buffer. `eval/` and `experiments/` trees are measurement artifacts,
not learned assistant state. `experiments/data` and `tmp_smoke` in the
repository are gitignored test outputs outside the runtime tree.

`FileResetService` (`harness/maintenance/reset.py`) is the single maintenance
boundary. It depends only on manager contracts and quiesce gates supplied by
the composition root; it imports no concrete component.

| Scope | Removes | Re-seeds / keeps |
| --- | --- | --- |
| `full` | settings file + history, sessions, traces, history, events, registry, tasks, decisions, persona, capability attempts, memories, consolidation, learning journal, skills | default settings from `settings/catalog.py`, code-defined `DEFAULT_PERSONA`, reserved `self` domain, a new session with no active domain |
| `memories` | `memory/`, `consolidation/`, learning journal, memory ids in retained traces/events/attempts | conversations, tasks, domains, persona, settings, skills, and the active domain stay |
| `conversations` | sessions, traces, history, events, capability attempts, tasks, decisions, consolidation, learning journal, pending skill candidates; memory usage and conversation source refs | memories, domains, persona, settings, approved skills stay; a new session is created with no active domain |

Before a wipe (unless `maintenance.reset.backup` is false or the CLI passes
`--no-backup`) it writes `backups/reset_<scope>_<UTC timestamp>.zip` containing
the runtime tree plus a `_reset_manifest.json`. Archives, `eval/`, and
`experiments/` are excluded from subsequent archives. `ResetReport` lists every
removed top-level path with file/byte counts, the backup path, seeded items,
cleared stale references, and duration; the CLI prints it, the UI renders it,
and a `reset.run` event records the same data.

Safety: reset enters the scheduler's exclusive gate first (quiescing the
heartbeat before it can queue work), then the turn service's exclusive epoch
gate, so an in-flight turn finishes before the wipe and turns queued behind
it are rejected; it also waits for history-summary and consolidation workers. A conversation
reset drops conversation-derived tasks instead of leaving them pointing at
missing interactions. A memory reset scrubs memory ids from retained traces,
events, and attempts and drops memory-operation attempts. A conversation reset
clears memory usage and strips removed interaction/attempt/event ids from memory
provenance into `provenance.reset_cleared_refs`. After the wipe, an
injected composition-root hook refreshes executor skill disclosure and drops
pending skill candidates on a conversations reset, so in-memory state can
never show skills the wipe removed. The UI requires the configured
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
| `sessions/session.json` | Session id/timestamps/subject hits/active domain id |
| `sessions/session_meta.json` | Cooldown metadata |
| `sessions/interactions.jsonl` | User rows plus assistant-update rows, each with UTC timestamps |
| `history/segments/seg_*.json` | Stored history segment, covered interaction ids, rolling summary, status/error, and summary token counts |
| `registry/domains/*.json` | Domain records, including reserved `self` |
| `registry/suggestions/*.json` | Suggested domains and status/cooldown |
| `tasks/tsk_*.json` | Task state, constraints, outcome, flags, sequence, priority, due/defer windows, progress checkpoints, resume count |
| `decisions/dec_*.json` | Durable decision/proposal, status, scope, risk, resolution, execution history |
| `persona/persona.json` | Current persona version |
| `persona/history.json` | Persona version history |
| `persona/proposals/per_*.json` | Proposed persona edits |
| `capabilities/attempts/op_*.json` | Durable execution attempt + evidence |
| `memory/records/mem_*.json` | Memory records and lifecycle |
| `memory/usage.jsonl` | Memory usage rows with task/trace ids |
| `consolidation/run_*.json` | Consolidation run, proposals, results |
| `consolidation/run_*.input.json` | Exact evidence snapshot for rollback/audit |
| `learning/journal/lrn_*.json` | Learning journal: one traceable entry per learning decision |
| `skills/records/skl_*.json` | Skill metadata: status, provenance, test evidence, version history |
| `skills/candidates/<name>/SKILL.md` | Staged candidate text (never disclosed) |
| `skills/active/<name>/SKILL.md` | Approved skill text; the executor's default discovery dir |
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
  A learned skill follows the staged lifecycle in "Learning lifecycle" instead
  of being hand-dropped into the active directory: propose through the skill
  library, review the candidate, approve (with test evidence when it replaces
  an approved skill), and keep the version history for rollback.
- MCP: declare a stdio server in the YAML file; tools become operations and the
  configured permission map controls policy. The harness does not bypass
  enablement or global permission policy for MCP calls.
