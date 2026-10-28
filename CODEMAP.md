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
| `harness/db.py` | SQLite schema (WAL), FTS5 external-content tables + triggers, transactions, schema version, online backup |
| `harness/logging.py` | Structured stdout + rotating file logs |
| `harness/model.py` | `ModelClient` (OpenAI-compatible chat completions, native tool calls, Hermes fallback parsing, bounded retries, probe); `ScriptedModel` for tests |
| `harness/store.py` | `Messages` (unified history + FTS search), `Events` (observability log), `Domains` (records + deterministic routing), `Tasks` (statuses, resume-candidate scoring) |
| `harness/memory.py` | `MemoryManager`: capture with conflict supersession, scopes/kinds, revisions + provenance, FTS retrieval with scope priority and recency, expiry, merge, bounded artifact reads, artifact consistency checks |
| `harness/context.py` | `ContextBuilder`: budgeted system prompt (persona, operating rules, state, memories, recent + retrieved history, precondition-gated skills), inclusion/exclusion sources |
| `harness/tools.py` | `ToolRegistry` (builtin tools, JSON-schema-lite validation, risk classes, banned-name refusal, execution records), minimal MCP stdio JSON-RPC client and loader |
| `harness/approvals.py` | `Approvals`: lifecycle (pending/approved/rejected/cancelled/executed/expired), expiry sweep, quick-confirm classification (keyword + tiny model call) |
| `harness/runtime.py` | `AgentRuntime`: the single bounded loop; routing/resume entry point for user messages; approval/question/interrupt/limit pause and resume; transcript persistence |
| `harness/scheduler.py` | Durable schedules (once/interval/daily/weekly with timezones), idempotent occurrence submission, missed-run policy, heartbeat checks, scheduler thread |
| `harness/consolidation.py` | Bounded model pass proposing create/update/merge/retract; applied via `MemoryManager` with revisions; markdown change log per run |
| `harness/app.py` | `App` composition root, startup recovery, `ProcessLock` (single writer), backup/restore, graceful shutdown; `build_app` test helper |
| `harness/server.py` | Starlette JSON API + auth; thin delegation to `App` |
| `harness/web_page.py` | The single polling web page served at `/` |
| `harness/__main__.py` | CLI: `serve`, `endpoint-check`, `msg`, `status`, `tasks`, `approvals`, `schedules`, `memories`, `domains`, `consolidate`, `events`, `tools`, `backup`, `restore`, `config` |
| `scripts/container_check.py` | Build + start the compose stack, verify health/auth/API/endpoint, tear down |
| `examples/mcp/echo_server.py` | Dependency-free stdio MCP echo/add server (also the test fixture) |
| `examples/mcp_servers.yaml` | MCP server configuration example |
| `examples/skills/weekly-review/SKILL.md` | Skill format example with preconditions |

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
  -> loop:
       limits?        -> limit_reached; task paused; honest note
       model.chat(transcript, tool schemas)
         malformed?  -> bounded retries; then visible failure
         tool_calls? -> resolve (unknown -> denied record + observation)
                        validate args (all problems at once)
                        confirm-risk? -> approval + awaiting execution row;
                                          run waiting_approval; visible note
                        else           -> execute; record outcome; append
                                          observation to transcript
         final reply? -> ask_user marker? task waiting_user
                        else task completed; assistant message recorded
  -> transcript + counters persisted at every step
```

## Persistence layout

| Path under data dir | Contents |
| --- | --- |
| `config.yaml` | Materialized configuration (created with defaults) |
| `harness.db` | Authoritative state: `messages` (+FTS5), `event_log`, `domains`, `tasks`, `runs`, `memories` (+FTS5), `memory_revisions`, `schedules`, `schedule_occurrences`, `approvals`, `tool_executions`, `schema_meta` |
| `artifacts/memories/<id>.md` | Long memory notes referenced from rows |
| `artifacts/consolidation/<run>.md` | Per-run change logs |
| `artifacts/domains/…` | Reserved for future domain notes |
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
| `tests/test_scheduler.py` | Due times + timezones, restart survival, missed-run policies, occurrence idempotency, approval pause/resume, heartbeat overlap/notify/retry |
| `tests/test_approvals.py` | Exact action binding, quick confirm (keyword/model/ambiguous), rejection/cancellation/expiry/staleness, untrusted-output immunity, no shell tools, risk override validation |
| `tests/test_recovery.py` | Restart preservation, interrupted-run resume without repeats, unknown outcomes, graceful shutdown, backup/restore round trip, writer lock, concurrent web+CLI with one scheduler |
| `tests/test_tools_mcp.py` | MCP discovery/autoapprove/round trip/bounded results/unavailable servers, skill disclosure by preconditions |
| `tests/test_scenarios.py` | The ten end-to-end acceptance scenarios |
| `tests/test_live_endpoint.py` | Real-endpoint integration (marked `live`): models list, plain completion, native tool-call protocol, multi-step round trip, one full live turn |
