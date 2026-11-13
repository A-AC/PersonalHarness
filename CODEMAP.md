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
| `harness/db.py` | SQLite schema (WAL), FTS5 external-content tables + triggers, transactions, schema version (v4 memory lifetime), online backup |
| `harness/logging.py` | Structured stdout + rotating file logs |
| `harness/model.py` | `ModelClient` (OpenAI-compatible chat completions, native tool calls, Hermes fallback parsing, bounded retries, probe); `ScriptedModel` for tests |
| `harness/store.py` | `Messages` (unified history + FTS search), `Events` (observability log), `Domains` (records + deterministic routing), `Tasks` (statuses, resume-candidate scoring) |
| `harness/memory.py` | `MemoryManager`: capture with conflict supersession, scopes/kinds, revisions + provenance, FTS retrieval with scope priority and recency, usage accounting with per-run dedupe, deterministic lifetime sweep, archive/restore/pin, expiry, merge, bounded artifact reads, artifact consistency checks |
| `harness/context.py` | `ContextBuilder`: budgeted system prompt (persona, operating rules, state, memories, recent + retrieved history, registry-driven skill disclosure), inclusion/exclusion sources |
| `harness/tools.py` | `ToolRegistry` (builtin tools including `propose_skill`, JSON-schema-lite validation, risk classes, trust flags, banned-name refusal, execution records, budgeted per-run schema selection scoped by eligible modules), minimal MCP stdio JSON-RPC client |
| `harness/approvals.py` | `Approvals`: lifecycle (pending/approved/rejected/cancelled/executed/expired), expiry sweep, name-keyed proposal dedupe, quick-confirm classification (keyword + tiny model call) |
| `harness/runtime.py` | `AgentRuntime`: the single bounded loop; capability snapshots; trust/taint escalation; routing/resume entry point; approval/question/interrupt/limit pause and resume; transcript persistence |
| `harness/scheduler.py` | Durable schedules (once/interval/daily/weekly in system or explicit timezones), idempotent occurrence submission, missed-run policy, module health refresh, heartbeat checks including the bounded skill builder and memory-lifetime sweep, scheduler thread |
| `harness/consolidation.py` | Bounded model pass proposing create/update/merge/retract; applied via `MemoryManager` with revisions; markdown change log per run |
| `harness/modules/types.py` | `Module`, `ModuleBinding`, `ModuleTool` value objects over the v3 tables |
| `harness/modules/registry.py` | Module persistence, domain eligibility, lazy skill manager, and the loader seam (`load_all`, `reload`, `set_enabled`, `health_tick`); skill imports and reloads route here |
| `harness/modules/mcp.py` | Registry-backed MCP/web loader: legacy import, manifests, discovery cache, per-domain visibility, reload, health, trusted/untrusted flags |
| `harness/modules/skills.py` | `SkillManager` (front-matter import, scope disclosure, bounded validation, draft/active/disabled lifecycle, versioned bodies, rollback, revisions) and `SkillBuilder` (bounded heartbeat drafting, strict JSON, taint refusal, artifact logs) |
| `harness/modules/web/server.py` | Shipped web-search stdio MCP server over SearXNG JSON; bounded results, no `web_fetch` |
| `harness/app.py` | `App` composition root, startup recovery, `ProcessLock` (single writer), backup/restore, graceful shutdown; `build_app` test helper |
| `harness/server.py` | Starlette JSON API + auth; thin delegation to `App`; module list/reload/enable/disable endpoints, skill list/draft lifecycle endpoints; authenticated static UI assets |
| `harness/web/` | Zero-build UI: `index.html`, `app.js`, `style.css` (served only through the authenticated handler) |
| `harness/__main__.py` | CLI: `serve`, `endpoint-check`, `msg`, `status`, `tasks`, `approvals`, `schedules`, `memories`, `domains`, `consolidate`, `events`, `modules`, `skills`, `tools`, `backup`, `restore`, `config` |
| `scripts/container_check.py` | Build + start the compose stack, verify health/auth/API/endpoint, tear down |
| `examples/mcp/echo_server.py` | Dependency-free stdio MCP echo/add server (also the test fixture) |
| `examples/mcp_servers.yaml` | MCP server manifest/import example (stdio, scope, tool filters) |
| `examples/skills/weekly-review/SKILL.md` | Adaptive skill format example with preconditions |

## Turn data flow

```text
web POST /api/message | CLI msg | schedule due | heartbeat | approval decision
  -> AgentRuntime.submit_user_message / resume_*
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
                        tainted run + propose_skill? -> refused; no draft
                        confirm-risk? -> approval + awaiting execution row;
                                          run waiting_approval; visible note
                                          (reason names untrusted origin;
                                           propose_skill shows bounded body)
                        else           -> execute; record outcome; append
                                          observation to transcript
                                          untrusted output? -> taint run
         final reply? -> ask_user marker? task waiting_user
                        else task completed; assistant message recorded
  -> transcript + counters persisted at every step
```

## Persistence layout

| Path under data dir | Contents |
| --- | --- |
| `config.yaml` | Materialized configuration (created with defaults) |
| `harness.db` | Authoritative state: `messages` (+FTS5), `event_log`, `domains`, `tasks`, `runs`, `memories` (+FTS5, lifetime columns), `memory_revisions`, `memory_access`, `schedules`, `schedule_occurrences`, `approvals`, `tool_executions`, `modules`, `module_bindings`, `module_tools`, `schema_meta` |
| `artifacts/memories/<id>.md` | Long memory notes referenced from rows |
| `artifacts/consolidation/<run>.md` | Per-run change logs |
| `artifacts/skills/<run>.md` | Skill builder prompt/report logs (bounded) |
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
  web/cli/schedule/heartbeat/system, `visible` keeps maintenance out of
  chat without hiding it from inspection.
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
| `tests/test_skills.py` | Front-matter import, global/domain/adaptive disclosure, proposal approval lifecycle, bounded validation, dedupe, taint refusal, builder batching/limits/failure, skills API |
| `tests/test_tools_mcp.py` | Registry-backed MCP discovery/risk/round trip/bounded results/unavailable servers, domain scope and refusal, reload, schema budget priority, web search stub (bounded/untrusted/malformed), taint escalation with source provenance, skill disclosure |
| `tests/test_scenarios.py` | The ten end-to-end acceptance scenarios |
| `tests/test_live_endpoint.py` | Real-endpoint integration (marked `live`): models list, plain completion, native tool-call protocol, multi-step round trip, one full live turn |
