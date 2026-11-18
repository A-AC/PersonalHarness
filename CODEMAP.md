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
| `harness/config.py` | Defaults, YAML load/validate, env overrides, save; unknown keys fail fast; read-only load (`create=False`) for diagnostics; `setup.complete` first-run marker; retired groups are accepted for one-time module migration |
| `harness/db.py` | SQLite schema (WAL), FTS5 external-content tables + triggers, transactions, schema v10 (domain routing evidence, summaries, `routing_feedback`/`domain_suggestions`, embedding cache, Phase 13 `user_id` prep), pre-migration backup, online backup |
| `harness/logging.py` | Structured stdout + rotating file logs |
| `harness/model.py` | Provider-aware `ModelClient` (OpenAI-compatible chat completions, native tool calls, Hermes fallback parsing, bounded retries, probe, `list_models`); `ScriptedModel` for tests |
| `harness/providers.py` | Provider records and resolution, synthesized built-in `default` provider, model allowlists, catalogue construction, and explicit selection verification without fallback |
| `harness/store.py` | `Messages` (unified history + FTS search), `Events` (observability log), `Domains` (records + routing evidence v2 with learned/task affinity), `RoutingFeedback` (durable correction labels and `k_routing`-format export), `Tasks` (statuses, resume-candidate scoring, parent/child tree relationships) |
| `harness/memory.py` | `MemoryManager`: explicit capture and correction, advisory possible conflicts (never implicit supersession), scopes/kinds, revisions + provenance, FTS retrieval with scope priority and cross-domain modes, General-as-global, always-on core profile, final-payload usage accounting with per-run dedupe, deterministic lifetime sweep, archive/read-only archived retrieval/explicit restore/pin, expiry, merge, bounded artifact reads with path containment, and artifact consistency checks |
| `harness/context.py` | `ContextBuilder`: budgeted system prompt (persona, operating rules including conflict handling, state, domain instructions/descriptions/hints, memories and cross-domain labels, core profile, recent + retrieved history with provenance, registry-driven skill disclosure, cross-domain tool discovery), sub-agent mode, tainted-note/history/memory labelling, disclosure markers, and final inclusion/exclusion sources |
| `harness/tools.py` | `ToolRegistry` (builtin tools, JSON-schema-lite validation, risk classes, state-mutation vs external-egress vs untrusted-output policy, per-tool summaries, persistent risk overrides, trust flags, banned-name refusal, execution records, budgeted per-run schema selection and frozen spec metadata, synchronized atomic module replacement, frozen-schema/policy resolution, and fail-closed removed tools), result-carried untrusted provenance, minimal MCP stdio client with scrubbed child environments |
| `harness/approvals.py` | `Approvals`: lifecycle (pending/approved/rejected/cancelled/executed/expired/revised/superseded), free-text revision, supersede chains (`supersedes_id`, argument diff), per-tool template summaries, expiry sweep, name-keyed skill dedupe, quick-confirm classification (keyword + tiny model call) |
| `harness/runtime.py` | `AgentRuntime`: the single bounded loop; frozen schema/policy snapshots; trust/taint propagation and state-change/external-egress escalation; routing/resume entry point and strategies; domain entry/correction; approvals including frozen-definition edit presentation/validation; per-run denial memory; untrusted result envelopes; final-prompt compaction/source-ledger reconciliation and memory disclosure accounting; transcript persistence; child budgets and role-aware denial; per-run model/provider snapshots |
| `harness/suggestions.py` | `DomainSuggestions`: deterministic usage-based clustering of repeated General-task vocabulary; bounded open suggestions, visible notes, create/dismiss state, and remembered dismissals; never creates a domain |
| `harness/subagents.py` | `SubAgentManager`: bounded sequential delegation, goal-hash dedupe/reuse, child task/run creation, blocked-outcome closing, durable result artifacts, clean/tainted child result provenance propagation and reuse, and sub-agent events |
| `harness/scheduler.py` | Durable schedules (once/interval/daily/weekly in system or explicit timezones), idempotent occurrence submission, missed-run policy, self-scheduling bounds (`create_for_task`/`cancel_for_task`), inbox-before-schedule heartbeat ordering, module health refresh, bounded skill builder and memory-lifetime sweep, scheduler thread |
| `harness/inbox.py` | `InboxManager`: idempotent external event intake with bounded bodies, trust marking, quiet hours, content dedupe, declarative automation CRUD/matching, cooldown + hourly loop breaker, task/notify/ignore actions, fire history, promoted-task recovery and tainted run triggers |
| `harness/consolidation.py` | Bounded model pass proposing create/update/merge/retract from trusted inputs only; tainted memories/messages are excluded and tainted row mutations refused; changes apply via `MemoryManager` with revisions and markdown logs |
| `harness/modules/types.py` | `Module`, `ModuleBinding`, `ModuleTool` value objects over the v3 tables |
| `harness/modules/registry.py` | Module persistence, domain eligibility, lazy skill manager, and the loader seam (`load_all`, `reload`, `set_enabled`, `health_tick`); skill imports and reloads route here |
| `harness/modules/mcp.py` | Generic registry-backed MCP loader: manifest/import, private env sidecars and scrubbed subprocess env, discovery cache, per-domain visibility, atomic rediscovery/removal, health, trust/read-only/external-egress flags, trusted annotations, per-tool risk, summary templates, tool-name prefixing, user setup operations, command allowlist, connection preview, and bundled templates |
| `harness/modules/skills.py` | `SkillManager` (front-matter import, scope disclosure, bounded validation, draft/active/disabled lifecycle, versioned bodies, rollback, revisions, agent-owned edit guard) and `SkillBuilder` (bounded heartbeat drafting, strict JSON, taint refusal, artifact logs) |
| `harness/modules/web/server.py` | Self-contained stdio MCP server over SearXNG JSON with its own `--config`; NFKC/control/zero-width/HTML normalisation, http(s)-only URL policy, heuristic injection flags, nonce-labelled data output, capped queries, bounded results, no `web_fetch` |
| `harness/modules/calendar/server.py` | Self-contained stdio MCP server over CalDAV with its own `--config`; ETag-guarded VEVENT read/write tools, `readOnlyHint` annotations on reads, approval summary templates |
| `harness/app.py` | `App` composition root, startup recovery, `ProcessLock` (single writer), backup/restore, reset (backup then wipe with in-data-dir guard), graceful shutdown; `build_app` test helper (configured) |
| `harness/routing/` | Pluggable domain routing: `RoutingService` strategy selection/fallback/shadow; unchanged `LexicalRouter` control; OpenAI-compatible embedding client with packed-float32 SQLite cache and stdlib cosine; constrained `LLMRouter` JSON label call; `HybridRouter` top-3 embedding margin plus LLM pick; bounded domain cards (name, description, examples, summary) |
| `harness/server.py` | Starlette JSON API + auth; thin delegation to `App`; first-run setup page and `/api/setup`, `/api/setup/probe`, `/api/setup` (unauthenticated only while unconfigured) and authenticated `/api/reset`; per-panel endpoints (`/api/summary`, `/api/messages`, `/api/tasks` with optional `include_subagents=1`, `/api/approvals`, `/api/schedules`, `/api/inbox`, `/api/automations`, `/api/memories`, `/api/domains`, `/api/skills`, `/api/consolidations`, `/api/modules`, `/api/tools`, `/api/events`, `/api/system`, `/api/config`, `/api/backups`, `/api/models`, `/api/model`), memory edit/retract and skill draft/delete and module-binding mutations, MCP setup/test/template/tool-risk endpoints with redacted module payloads, task detail with each run's full persisted model transcript/context sources and the child tree (children hidden from the default task list), approval payloads with summary/schema/editable/taint fields, `approve|reject|cancel|revise|edit` decisions with feedback/arguments/remember, `/api/tools/{tool}/risk`, `/api/system` with version/git/sha/schema, `/api/state` as a compatibility aggregate for the CLI; Phase 10 `/api/domains/route-preview`, `/api/domains/feedback`, and `/api/domain-suggestions` endpoints, environment-card counts/routing stats, core/pinned memory filters, task domain-correction payloads, and task routing "why"/domain-entered detail; summary/system/task-detail model and provider attribution; authenticated static UI assets |
| `harness/web/` | Zero-build UI shell: `setup.html` (standalone first-run wizard: token, endpoint probe/model pick, timezone, optional module forms), `index.html` (grouped Conversation/Work/Knowledge/System navigation, working-task strip, task drawer), `style.css` (served only through the authenticated handler) |
| `harness/web/app.js` | Per-panel loading and mutations; always polls approvals so chat shows inline cards; shared approval card with argument grid, taint warning, schema-driven edit form, revision feedback, approve-and-remember, approve-all-safe tray, keyboard shortcuts, and revocable risk overrides; inline memory/skill/domain editors; recursive sub-agent detail; domain environment cards, routing "why" popover, visible domain chip, route preview, core-profile filter/editor, and usage-based domain suggestions |
| `harness/__main__.py` | CLI: `serve`, `init` (interactive or `--non-interactive` from `HARNESS_*`), `doctor`, `reset`, `endpoint-check`, `msg`, `status`, `tasks`, `approvals`, `schedules`, `inbox`, `automations`, `memories`, `domains` (including `--export-feedback` and `--suggestions`), `consolidate`, `events`, `modules`, `skills`, `tools`, `eval`, `backup`, `restore`, `config`, `models` |
| `harness/evaluation/` | `harness eval` framework: YAML scenarios, routing and persisted-state assertions, measurement-only scenarios, optional deterministic quality rubrics plus `quality.required` gating assertions, live behavior/holdout suites, sandbox guards, multi-tool loops, run bounds, model latency/token metrics, JSON/Markdown reports, comparison, aggregation, and routing bake-offs |
| `harness/evaluation/bakeoff.py` | Routing bake-off runner/report: per-strategy and per-split accuracy, wrong-domain and fallback rates, confusion, latency p50/p95, unavailable-endpoint reporting, scripted fakes for deterministic plumbing |
| `harness/evaluation/routing.py` | Pure deterministic routing metrics: accuracy, General fallback, false-positive rate, case-insensitive confusion tables, and grid-point selection shared by the sweep script and tests |
| `harness/setup.py` | First-run setup values: bounds/validation, base-URL normalisation, endpoint model probe, config application, safe wizard state, `HARNESS_*` bootstrap values |
| `harness/doctor.py` | `harness doctor` preflight checks: Python, data writability/UID, endpoint reachability/model, port ownership, writer lock, module status, disk; warnings never fail |
| `scripts/docker-entrypoint.sh` | Container entrypoint: bootstrap a fresh volume with `harness init --non-interactive` when `HARNESS_TOKEN` is set, then `exec python -m harness serve` |
| `scripts/container_check.py` | Build + start the compose stack, verify health/auth/API/endpoint, tear down |
| `scripts/route_sweep.py` | Sweep deterministic domain routing over threshold/margin grids and write deterministic JSON/Markdown metrics reports |
| `scripts/memory_recall_report.py` | Compare `off`, `search`, and `boost` memory modes against the cross-domain recall corpus and write recall@5/budget reports |
| `examples/mcp/echo_server.py` | Dependency-free stdio MCP echo/add server (also the test fixture) |
| `examples/mcp/hostile_server.py` | Fixture stdio MCP search server returning injection text, hidden characters, an exfiltration image, and a fake tool call; used by the `i_live_hostile_web` eval scenario |
| `examples/mcp_servers.yaml` | MCP server manifest/import example (stdio, scope, tool filters, `trust_annotations`, `tool_risk`, `summary_templates`) |
| `examples/mcp.json` | Standard `mcpServers` registration example (auto-detected at `data/mcp.json`) |
| `examples/skills/weekly-review/SKILL.md` | Adaptive skill format example with preconditions |
| `examples/inbox_curl.sh` | Authenticated `POST /api/inbox` example and duplicate/idempotency check |
| `examples/config.routing.yaml` | Semantic routing and embeddings config sample, including container-reachable endpoint guidance |
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
  -> else: route domain (evidence v2; per-term breakdown recorded)
             -> create a new task
       side path: route preview? -> scores/breakdown; no state changes
       side path: task correction? -> move task + routing_feedback;
                                       suggest keywords only
  -> start_run: context build (sources recorded on the run)
                capability snapshot: eligible modules + frozen schemas/policy + schema budget
                  -> runs.meta {toolset: {names, modules, exclusions}, model, provider}
  -> loop:
       limits?        -> limit_reached; task paused; honest note
       domain changed? -> recompute snapshot once; record toolset.changed
       model.chat(transcript, snapshot tool schemas, model=run snapshot)
         malformed?  -> bounded retries; then visible failure
         tool_calls? -> resolve inside snapshot (outside -> denied record)
                        validate args (all problems at once)
                        tainted run + state change/external egress? -> confirm escalation
                        tainted run + skill write? -> refused; no draft
                        confirm-risk? -> approval + awaiting execution row;
                                          run waiting_approval; visible note
                                          (reason names untrusted origin;
                                           skill writes show bounded body)
                        else           -> execute; record outcome; append
                                          observation to transcript
                                          untrusted output? -> taint run
                        use_domain? -> entry briefing; domain.entered +
                                       visible status chip
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
             optional future guard seam
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
| `config.yaml` | Materialized configuration (created with defaults; `setup.complete` distinguishes a fresh install from an existing one and is managed by the wizard/`harness init`) |
| `harness.db` | Authoritative state: `messages` (+FTS5), `event_log`, `domains` (+ routing-evidence columns), `tasks` (+ parent/role tree columns), `runs` (+ `parent_run_id`), `memories` (+FTS5, lifetime columns), `memory_revisions`, `memory_access`, `schedules`, `schedule_occurrences`, `inbox_events`, `automations`, `automation_fires`, `approvals`, `tool_executions`, `modules`, `module_bindings`, `module_tools`, `routing_feedback`, `domain_suggestions`, `embedding_cache`, `schema_meta` |
| `artifacts/memories/<id>.md` | Long memory notes referenced from rows |
| `artifacts/consolidation/<run>.md` | Per-run change logs |
| `artifacts/skills/<run>.md` | Skill builder prompt/report logs (bounded) |
| `artifacts/subagents/<task_id>.md` | Completed child result, goal, parent-supplied brief, and run reference |
| `artifacts/domains/…` | Reserved for future domain notes |
| `modules/skills/<id>/v<n>.md` | Versioned skill bodies owned by `kind=skill` module rows; directory imports are one source, rows are authoritative |
| `modules/mcp/<id>.json` | Server-owned config files (e.g. the migrated web config); MCP rows remain authoritative |
| `modules/mcp/<id>.env.json` | Private sidecar for explicit `env` values from an `mcpServers` file (never stored in SQLite) |
| `backups/harness-<ts>.zip` | DB snapshot + config + artifacts + modules (including private env sidecars; protect credentials) |
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
- Approvals are immutable once decided; a revision closes the old row as
  `revised` or `superseded` and creates a new pending row, and execution
  binds back through the awaiting `tool_executions` row, so a stale
  approval can never execute.
- Occurrence ids are `(schedule_id, due_time)`; submission is INSERT-or-
  reuse, so restarts and duplicate ticks never double-run.
- `row_num` (INTEGER PRIMARY KEY) backs the FTS5 external-content index on
  memories; the TEXT `id` remains the logical identity everywhere else.

## Test suites

| File | Coverage |
| --- | --- |
| `tests/test_model.py` | Response parsing, native + Hermes tool calls, error classes, retries, thinking kwargs |
| `tests/test_model_selection.py` | Model/provider config validation, provider resolution, allowlist intersection, per-call model switching, catalogue and selection API contracts, run attribution/pinning, resume re-selection, and UI wiring |
| `tests/test_runtime.py` | Core loop: responses, tool lifecycle, sequential calls, failure recovery, unknown/invalid tools, budgets, waiting/resume, transcript persistence |
| `tests/test_unified_history.py` | Web+CLI shared runtime/state, scheduled prompts and heartbeat work in the same history, source metadata, maintenance not flooding chat, auth, schedules API |
| `tests/test_domains_tasks.py` | Routing to existing domains, no domain per topic, no cross-domain leakage, restart resumption, ambiguity clarification, follow-up preference, domain creation via approval |
| `tests/test_domains_foundations.py` | v8→v9 migration, domain routing-evidence columns/tables, Phase 13 owner-id prep, stemming table, bounded evidence/instructions, fixtures, and Phase 10 config defaults/validation |
| `tests/test_domain_routing_v2.py` | Evidence-v2 routing, learned memory/skill affinity, per-term breakdowns, General false positives, `create_domain` routing fields, and shipped corpus targets |
| `tests/test_domain_entry.py` | Domain instructions/descriptions, below-threshold hints, `use_domain` briefing, cross-domain tool discovery, visible entry status, taint safety, and sub-agent domain inheritance |
| `tests/test_memory_cross_domain.py` | Boosted sibling recall/labels/caps, no lexical-free filler, core profile, General-as-global behavior, explicit cross-domain search, and consolidation isolation |
| `tests/test_routing_feedback.py` | Task corrections, durable feedback labels, eval-format export, read-only route preview, API/core contracts, approval-gated `edit_domain`, taint refusal, and CLI export |
| `tests/test_semantic_routing.py` | Embedding endpoint/cache, domain cards, lexical fallback, strict-JSON LLM routing, shadow strategies, and user-only routing |
| `tests/test_routing_bakeoff.py` | Deterministic bake-off plumbing/reporting and CLI flags |
| `tests/test_memory_scope_policy.py` | Model-authored memory scope narrowing/widening, origin provenance, General/sub-agent/use_domain/approval paths |
| `tests/test_domain_classifier.py` | `off`/`shadow`/`on` model tie-break behavior, trusted-only input, invalid/unknown/timeout fallback, deterministic preview, bounded prompt, and unchanged permissions |
| `tests/test_domain_suggestions.py` | Usage-based clustering, thresholds and bounded scans, existing-domain coverage, remembered dismissals, API/CLI/create flows, heartbeat failure handling, and UI wiring |
| `tests/test_domain_ui.py` | Domain environment-card counts/routing stats, task routing evidence, "why" detail, domain-entry status chip, and working-task summary wiring |
| `tests/test_routing_eval.py` | Labelled routing/memory corpora, routing metrics/confusion, measurement-gating contract, General-only control, report determinism, and fixture updates |
| `tests/test_memory_context.py` | Explicit capture, old-memory recall, scopes, context sources, budgets, supersession/revisions, temp expiry, consolidation, artifact consistency |
| `tests/test_memory_lifetime.py` | v4 migration, per-run access dedupe, deterministic sweep bounds, pin/protected kinds, archive retrieval/restore, `search_knowledge` include-archived, consolidation exclusion, heartbeat sweep bound, API usage and filters |
| `tests/test_scheduler.py` | Due times + timezones, restart survival, missed-run policies, occurrence idempotency, approval pause/resume, heartbeat overlap/notify/retry |
| `tests/test_time.py` | System-time default, local rendering, naive `at_ts` semantics, explicit-zone override, timezone validation, UI local timestamps |
| `tests/test_approvals.py` | Exact action binding, quick confirm (keyword/model/ambiguous), rejection/cancellation/expiry/staleness, untrusted-output immunity, no shell tools, risk override validation |
| `tests/test_recovery.py` | Restart preservation, interrupted-run resume without repeats, unknown outcomes, graceful shutdown, backup/restore round trip, writer lock, concurrent web+CLI with one scheduler |
| `tests/test_modules_foundations.py` | UI shell/assets, registry loader, v3→v9 schema migration, backup/restore of `modules/`, module API/toggle/reload, heartbeat health, tool-schema budget and run snapshots, retired web-row migration |
| `tests/test_skills.py` | Front-matter import, global/domain/adaptive disclosure, proposal/edit approval lifecycle, in-approval refinement, agent ownership refusal, cross-run denial, bounded validation, dedupe, taint refusal, builder batching/limits/failure, skills API |
| `tests/test_tools_mcp.py` | Generic MCP discovery/risk/round trip/bounded results/unavailable servers, domain scope and refusal, reload, schema budget priority, self-contained web search (bounded/untrusted/malformed), taint escalation with source provenance, skill disclosure |
| `tests/test_calendar_mcp.py` | Calendar server config file precedence, read-only tool surface, iCalendar parse/serialize edge cases, CalDAV discovery/CRUD round trip, ETag conflicts, out-of-collection refusal |
| `tests/test_web_mcp.py` | Web server config file/env precedence (neutral and legacy names), JSON/TOML loading, clean config errors, bounded/untrusted tool surface |
| `tests/test_subagents.py` | Parent delegation/result return, child context isolation, reduced toolset/depth/confirm refusal, blocked limits, goal-hash reuse, child budgets/caps, crash-mid-child unknown recovery, v5 migration, task-detail child tree |
| `tests/test_inbox.py` | Inbox idempotency/API bounds, dedupe, automation task/cooldown, hourly breaker, tainted run approval, quiet hours, self-scheduling caps/depth, inbox-before-schedule ordering, v6 migration |
| `tests/test_ui_panels.py` | Phase 6 panel contracts: summary/messages/tasks (full model transcript + context sources)/approvals/schedules/inbox/automations/memory/domains/skills/consolidation/modules/tools/events/system/config/backups, memory edit/retract, skill draft/delete, module bindings, guard placeholder |
| `tests/test_deploy.py` | Pre-migration backup and abort, `/api/system` version/git/schema, `harness status` reporting, LF guards for shell scripts and the Dockerfile, staged compose/Dockerfile targets |
| `tests/test_install.py` | First-run marker and legacy-config migration, `harness init` env bootstrap/force, wizard state/probe/configure gating (409 after setup), invalid setup values, reset backup+wipe/`--keep-config`/outside-data-dir guard, doctor checks and exit codes, Docker entrypoint contract, fresh-install smoke (init -> serve -> message -> reset) |
| `tests/test_taint_memory.py` | Tainted memory/task-note writes escalate with origin; approved writes preserve provenance into later runs; untainted writes stay low-risk |
| `tests/test_memory_trust.py` | Archived duplicate behavior, no implicit supersession support, tainted initial context/search/history/artifact provenance, consolidation exclusion/refusal, and explicit tainted update provenance |
| `tests/test_mcp_tool_risk.py` | Per-tool MCP risk, `trust_annotations`-gated `readOnlyHint`, manifest `tool_risk` overrides, summary-template import, and read-taints-then-write-confirm behaviour |
| `tests/test_web_hardening.py` | Web normalisation, URL policy, injection flags, query cap, nonce/notice output, and the generic untrusted-output envelope |
| `tests/test_approvals_reject.py` | Per-run denial memory: no duplicate approval or note after rejection, different arguments may ask again, new user turn clears memory, bounded entries |
| `tests/test_approvals_rework.py` | Revision feedback, unrelated messages, supersede chains, structured edits and edited-argument visibility, template summaries, always-allow limits, risk-override revoke, approval/tool HTTP contracts, calendar/web summary templates, v7→v9 approval-row migration |
| `tests/test_approval_cards_ui.py` | Approval payload card/edit fields, tool override/template payload, UI asset wiring, edit/revise over HTTP |
| `tests/test_evaluation.py` | Scenario loading/sandbox, deterministic runs, tool sequences, run bounds, model metrics, optional and `quality.required` rubric behavior, reports/comparison/aggregation |
| `tests/test_scenarios.py` | The ten end-to-end acceptance scenarios |
| `tests/test_live_endpoint.py` | Real-endpoint integration (marked `live`): models list, plain completion, native tool-call protocol, multi-step round trip, one full live turn |
