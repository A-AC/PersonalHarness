# Code map

One Python package (`harness/`), one process, one SQLite store. The
composition root is `harness/app.py`; the HTTP edge is
`harness/server.py`; the CLI is an HTTP client in `harness/__main__.py`.
Tests live in `tests/` (deterministic, scripted model) with live-endpoint
tests under the `live` marker.

## Modules

| Path | Responsibility |
| --- | --- |
| `harness/util.py` | UTC timestamps, ids, JSON helpers, keyword extraction, clipping |
| `harness/config.py` | Defaults, YAML load/validate, env overrides, save; unknown keys fail fast |
| `harness/db.py` | SQLite schema (WAL), FTS5 external-content tables + triggers, transactions, schema version (v6 inbox/automations), online backup |
| `harness/logging.py` | Structured stdout + rotating file logs |
| `harness/model.py` | `ModelClient` (OpenAI-compatible chat completions, native tool calls, Hermes fallback parsing, bounded retries, probe); `ScriptedModel` for tests |
| `harness/store.py` | `Messages` (unified history + FTS search), `Events` (observability log), `Domains` (records + deterministic routing), `Tasks` (statuses, resume-candidate scoring, parent/child tree relationships) |
| `harness/memory.py` | `MemoryManager`: capture with conflict supersession, scopes/kinds, revisions + provenance, FTS retrieval with scope priority and recency, usage accounting with per-run dedupe, deterministic lifetime sweep, archive/restore/pin, expiry, merge, bounded artifact reads, artifact consistency checks |
| `harness/context.py` | `ContextBuilder`: budgeted system prompt (persona, operating rules, state, memories, recent + retrieved history, registry-driven skill disclosure), sub-agent mode (goal + parent brief + domain memories, no parent history), inclusion/exclusion sources |
| `harness/tools.py` | `ToolRegistry` (builtin tools including `delegate_task`, `propose_skill`, and `edit_skill`, JSON-schema-lite validation, risk classes, trust flags, banned-name refusal, execution records, budgeted per-run schema selection scoped by eligible modules and role), minimal MCP stdio JSON-RPC client |
| `harness/approvals.py` | `Approvals`: lifecycle (pending/approved/rejected/cancelled/executed/expired), expiry sweep, name-keyed skill dedupe, in-place pending refinement, quick-confirm classification (keyword + tiny model call) |
| `harness/runtime.py` | `AgentRuntime`: the single bounded loop; capability snapshots; trust/taint escalation; routing/resume entry point; approval/question/interrupt/limit pause and resume; in-approval skill refinement; transcript persistence; child budgets, role-aware tool denial, and hidden child messages |
| `harness/subagents.py` | `SubAgentManager`: bounded sequential delegation, goal-hash dedupe/reuse, child task/run creation, blocked-outcome closing, durable result artifacts, and sub-agent events |
| `harness/scheduler.py` | Durable schedules (once/interval/daily/weekly in system or explicit timezones), idempotent occurrence submission, missed-run policy, self-scheduling bounds (`create_for_task`/`cancel_for_task`), inbox-before-schedule heartbeat ordering, module health refresh, bounded skill builder and memory-lifetime sweep, scheduler thread |
| `harness/inbox.py` | `InboxManager`: idempotent external event intake with bounded bodies, trust marking, quiet hours, content dedupe, declarative automation CRUD/matching, cooldown + hourly loop breaker, task/notify/ignore actions, fire history, promoted-task recovery and tainted run triggers |
| `harness/consolidation.py` | Bounded model pass proposing create/update/merge/retract; applied via `MemoryManager` with revisions; markdown change log per run |
| `harness/modules/types.py` | `Module`, `ModuleBinding`, `ModuleTool` value objects over the v3 tables |
| `harness/modules/registry.py` | Module persistence, domain eligibility, lazy skill manager, and the loader seam (`load_all`, `reload`, `set_enabled`, `health_tick`); skill imports and reloads route here |
| `harness/modules/mcp.py` | Registry-backed MCP/web loader: legacy import, manifests, discovery cache, per-domain visibility, reload, health, trusted/untrusted flags |
| `harness/modules/skills.py` | `SkillManager` (front-matter import, scope disclosure, bounded validation, draft/active/disabled lifecycle, versioned bodies, rollback, revisions, agent-owned edit guard) and `SkillBuilder` (bounded heartbeat drafting, strict JSON, taint refusal, artifact logs) |
| `harness/modules/web/server.py` | Shipped web-search stdio MCP server over SearXNG JSON; bounded results, no `web_fetch` |
| `harness/app.py` | `App` composition root, startup recovery, `ProcessLock` (single writer), backup/restore, graceful shutdown; `build_app` test helper |
| `harness/server.py` | Starlette JSON API + auth; thin delegation to `App`; per-panel endpoints (`/api/summary`, `/api/messages`, `/api/tasks` with optional `include_subagents=1`, `/api/approvals`, `/api/schedules`, `/api/inbox`, `/api/automations`, `/api/memories`, `/api/domains`, `/api/skills`, `/api/consolidations`, `/api/modules`, `/api/tools`, `/api/events`, `/api/system`, `/api/config`, `/api/backups`), memory edit/retract and skill draft/delete and module-binding mutations, task detail with each run's full persisted model transcript/context sources and the child tree (children hidden from the default task list), `/api/state` as a compatibility aggregate for the CLI; authenticated static UI assets |
| `harness/web/` | Zero-build UI: `index.html` (grouped Conversation/Work/Knowledge/System navigation, working-task strip, task drawer), `app.js` (per-panel loading; polls `/api/summary` plus only the active panel and the chat feed; inline memory/skill/domain editors; recursive sub-agent detail), `style.css` (served only through the authenticated handler) |
| `harness/__main__.py` | CLI: `serve`, `endpoint-check`, `msg`, `status`, `tasks`, `approvals`, `schedules`, `inbox`, `automations`, `memories`, `domains`, `consolidate`, `events`, `modules`, `skills`, `tools`, `eval`, `backup`, `restore`, `config` |
| `harness/evaluation/` | `harness eval` framework: YAML scenarios (`eval/scenarios/`) for deterministic multi-tool loops and live behavior, `ScriptedModel` or live model execution, sandbox/protected-path guards, persisted-state assertions (including tool result chaining and ordered sequences), separate rubric scoring for open-ended replies, JSON/Markdown reports, and report comparison |
| `scripts/container_check.py` | Build + start the compose stack, verify health/auth/API/endpoint, tear down |
| `examples/mcp/echo_server.py` | Dependency-free stdio MCP echo/add server (also the test fixture) |
| `examples/mcp_servers.yaml` | MCP server manifest/import example (stdio, scope, tool filters) |
| `examples/skills/weekly-review/SKILL.md` | Adaptive skill format example with preconditions |
| `examples/inbox_curl.sh` | Authenticated `POST /api/inbox` example and duplicate/idempotency check |
| `integrations/README.md` | Pusher contract for future mailbox/messaging bridges; no adapter ships in the core process |

## Turn data flow

```text
web POST /api/message | CLI msg | schedule due | inbox triage | heartbeat | approval decision
  -> AgentRuntime.submit_user_message / start_run / resume_*
  -> quick confirmation? classify (keyword | tiny model call); decide exactly
     one pending approval; resume that run
  -> else: record user message (source, visible)
  -> resume candidate? (waiting_user bonus + keyword overlap + recency)
       tie -> new task whose context lists the candidates (model asks)
       hit -> resume the same run (answer appended / run continued)
  -> else: route domain (recorded evidence) and create a new task
  -> start_run: context build (sources recorded on the run)
                capability snapshot: eligible modules + schema budget
                  -> runs.meta.toolset {names, modules, exclusions}
  -> loop:
       limits?        -> limit_reached; task paused; honest note
       domain changed? -> recompute snapshot once; record toolset.changed
       model.chat(transcript, snapshot tool schemas)
         malformed?  -> bounded retries; then visible failure
         tool_calls? -> resolve inside snapshot (outside -> denied record)
                        validate args (all problems at once)
                        tainted run + side effects? -> confirm escalation
                        tainted run + skill write? -> refused; no draft
                        confirm-risk? -> approval + awaiting execution row;
                                          run waiting_approval; visible note
                                          (reason names untrusted origin;
                                           skill writes show bounded body)
                        else           -> execute; record outcome; append
                                          observation to transcript
                                          untrusted output? -> taint run
                        delegate_task? -> child task/run (goal hash, depth 1)
                                          reduced context + low-risk snapshot
                                          completed -> artifact + parent result
                                          blocked   -> child closed + reason
         final reply? -> ask_user marker? task waiting_user
                        else task completed; assistant message recorded
  -> transcript + counters persisted at every step
```

Inbox flow (one deterministic heartbeat pass, before due schedules):

```text
POST /api/inbox -> receive: validate + bound body; insert with
                   UNIQUE(source, external_id), trust=untrusted
heartbeat -> inbox.triage(new events, oldest first)
             guard seam (Phase 7)
             automation match (source, kind, keywords, regex)
             content dedupe only when no rule owns the event
             quiet hours? leave status=new; return
             cooldown / effective hourly cap
               cap hit -> disable rule + automation.disabled + one note
             task -> inbox_events.status=promoted + task origin=inbox:<source>
                     + meta.inbox_event_id + meta.tainted_sources
                     -> start_run (shared scheduler.max_runs_per_tick budget)
             notify -> one visible system note; status=triaged
             ignore -> status=ignored
          -> then submit_due schedules with the remaining run budget
```

## Persistence layout

| Path under data dir | Contents |
| --- | --- |
| `config.yaml` | Materialized configuration (created with defaults) |
| `harness.db` | Authoritative state: `messages` (+FTS5), `event_log`, `domains`, `tasks` (+ parent/role tree columns), `runs` (+ `parent_run_id`), `memories` (+FTS5, lifetime columns), `memory_revisions`, `memory_access`, `schedules`, `schedule_occurrences`, `inbox_events`, `automations`, `automation_fires`, `approvals`, `tool_executions`, `modules`, `module_bindings`, `module_tools`, `schema_meta` |
| `artifacts/memories/<id>.md` | Long memory notes referenced from rows |
| `artifacts/consolidation/<run>.md` | Per-run change logs |
| `artifacts/skills/<run>.md` | Skill builder prompt/report logs (bounded) |
| `artifacts/subagents/<task_id>.md` | Completed child result, goal, parent-supplied brief, and run reference |
| `artifacts/domains/…` | Reserved for future domain notes |
| `modules/skills/<id>/v<n>.md` | Versioned skill bodies owned by `kind=skill` module rows; directory imports are one source, rows are authoritative |
| `modules/mcp/<id>.yaml` | Reserved export/backup mirror for MCP manifests (rows remain authoritative; not written yet) |
| `backups/harness-<ts>.zip` | DB snapshot + config + artifacts |
| `logs/harness.log` | Rotating structured log |
| `process.lock` | Writer lock held by `serve` (pid + heartbeat timestamp) |

### Key invariants

- The database is authoritative; artifacts are rendered bodies whose
  identity/revision is owned by the referencing row.
- `messages` is the single interaction history; `source` distinguishes
  web/cli/schedule/heartbeat/system, `visible` keeps maintenance and
  sub-agent messages out of chat without hiding them from inspection.
- A delegated child is a normal task/run with `role='subagent'` and parent
  ids. It never inherits the parent transcript or approvals; its result is
  the only thing that crosses back, through the parent's tool result.
- Approvals are immutable once created; execution binds back through the
  awaiting `tool_executions` row, so stale approvals can never execute.
- Occurrence ids are `(schedule_id, due_time)`; submission is INSERT-or-
  reuse, so restarts and duplicate ticks never double-run.
- `row_num` (INTEGER PRIMARY KEY) backs the FTS5 external-content index on
  memories; the TEXT `id` remains the logical identity everywhere else.

## Test suites

| File | Coverage |
| --- | --- |
| `tests/test_model.py` | Response parsing, native + Hermes tool calls, error classes, retries, thinking kwargs |
| `tests/test_runtime.py` | Core loop: responses, tool lifecycle, sequential calls, failure recovery, unknown/invalid tools, budgets, waiting/resume, transcript persistence |
| `tests/test_unified_history.py` | Web+CLI shared runtime/state, scheduled prompts and heartbeat work in the same history, source metadata, maintenance not flooding chat, auth, schedules API |
| `tests/test_domains_tasks.py` | Routing to existing domains, no domain per topic, no cross-domain leakage, restart resumption, ambiguity clarification, follow-up preference, domain creation via approval |
| `tests/test_memory_context.py` | Explicit capture, old-memory recall, scopes, context sources, budgets, supersession/revisions, temp expiry, consolidation, artifact consistency |
| `tests/test_memory_lifetime.py` | v4 migration, per-run access dedupe, deterministic sweep bounds, pin/protected kinds, archive retrieval/restore, `search_knowledge` include-archived, consolidation exclusion, heartbeat sweep bound, API usage and filters |
| `tests/test_scheduler.py` | Due times + timezones, restart survival, missed-run policies, occurrence idempotency, approval pause/resume, heartbeat overlap/notify/retry |
| `tests/test_time.py` | System-time default, local rendering, naive `at_ts` semantics, explicit-zone override, timezone validation, UI local timestamps |
| `tests/test_approvals.py` | Exact action binding, quick confirm (keyword/model/ambiguous), rejection/cancellation/expiry/staleness, untrusted-output immunity, no shell tools, risk override validation |
| `tests/test_recovery.py` | Restart preservation, interrupted-run resume without repeats, unknown outcomes, graceful shutdown, backup/restore round trip, writer lock, concurrent web+CLI with one scheduler |
| `tests/test_modules_foundations.py` | UI shell/assets, registry loader, v3 schema migration, backup/restore of `modules/`, module API/toggle/reload, heartbeat health, tool-schema budget and run snapshots |
| `tests/test_skills.py` | Front-matter import, global/domain/adaptive disclosure, proposal/edit approval lifecycle, in-approval refinement, agent ownership refusal, cross-run denial, bounded validation, dedupe, taint refusal, builder batching/limits/failure, skills API |
| `tests/test_tools_mcp.py` | Registry-backed MCP discovery/risk/round trip/bounded results/unavailable servers, domain scope and refusal, reload, schema budget priority, web search stub (bounded/untrusted/malformed), taint escalation with source provenance, skill disclosure |
| `tests/test_subagents.py` | Parent delegation/result return, child context isolation, reduced toolset/depth/confirm refusal, blocked limits, goal-hash reuse, child budgets/caps, crash-mid-child unknown recovery, v5 migration, task-detail child tree |
| `tests/test_inbox.py` | Inbox idempotency/API bounds, dedupe, automation task/cooldown, hourly breaker, tainted run approval, quiet hours, self-scheduling caps/depth, inbox-before-schedule ordering, v6 migration |
| `tests/test_ui_panels.py` | Phase 6 panel contracts: summary/messages/tasks (full model transcript + context sources)/approvals/schedules/inbox/automations/memory/domains/skills/consolidation/modules/tools/events/system/config/backups, memory edit/retract, skill draft/delete, module bindings, guard placeholder |
| `tests/test_evaluation.py` | Scenario loader/selection and validation, sandbox refusal/reset, deterministic end-to-end run, tool result/sequence/deferral assertions, rubric scoring kept separate from assertion pass/fail, report JSON/Markdown/summary, config/model override precedence, live endpoint probe guards, and report comparison |
| `tests/test_scenarios.py` | The ten end-to-end acceptance scenarios |
| `tests/test_live_endpoint.py` | Real-endpoint integration (marked `live`): models list, plain completion, native tool-call protocol, multi-step round trip, one full live turn |
