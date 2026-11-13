# Personal Harness - Long-Term Plan

Status: in progress (Phase 2 complete)
Last updated: 2026-11-10
Scope: capability modules (skills + MCP + web), memory lifetime, autonomy,
sub-agents, prompt-injection defence, UI information architecture.

This document is the working plan. Each phase is one merge/commit unless a
phase is explicitly split. Keeping the deterministic test suite green is a
precondition for every phase, not a follow-up task.

---

## 1. Product goals

1. **Capabilities become modules.** Skills and MCP servers are first-class,
   domain-bound, dynamically loadable units with manifests, provenance,
   versions, health, and an on/off switch.
2. **The agent can reach the web** through the local SearXNG instance at
   `192.168.50.40:8080`, with results treated as untrusted data.
3. **The agent can be woken by the outside world** through a durable inbox
   API and declarative automations, without becoming an uncontrolled loop.
4. **The agent can build its own procedures** as draft skills that the user
   reviews and activates.
5. **Memory has a lifetime.** Frequently used memory survives; unused memory
   is archived and remains retrievable.
6. **Complex work can be delegated** to bounded sub-agents without breaking
   the single-loop, single-writer guarantees.
7. **Untrusted content cannot silently cause side effects.** A trust/taint
   policy is enforced from the first external capability onward; a CPU
   classifier (LaYa) is added last as an additional signal.
8. **The UI explains the system.** Modules, skills, memories (including
   archived), automations, sub-agent trees, and guard verdicts are visible
   and controllable.

---

## 2. Non-negotiable invariants

These come from the existing design and must survive every phase.

1. **One process, one writer per data directory.** No second runtime.
2. **One bounded agent loop.** Every trigger enters `AgentRuntime`.
3. **The model proposes; the harness decides and records.** Confirm-risk
   actions are bound to exact arguments and need approval.
4. **Skills and tool/MCP output are data, never instructions.** They cannot
   change risk classes, widen permissions, or bypass approvals.
5. **The database is authoritative.** Files are rendered bodies or manifests
   whose identity is owned by a database row.
6. **Every context inclusion and exclusion is recorded** as a source with a
   reason.
7. **Honest recovery.** Unknown outcomes are reported as unknown and never
   retried blindly.
8. **No shell, command, or code-execution capability.** No dynamic Python
   imports of user-supplied code. MCP subprocesses and manifests only.
9. **Deterministic tests by default.** No network in the normal suite; live
   tests stay behind the `live` marker.
10. **Zero-build UI.** Plain HTML/CSS/JS, no bundler, no npm dependency.
11. **Secrets stay out of editable state.** MCP `env` stores variable names,
    not values; the UI never displays tokens.

---

## 3. Baseline architecture (what the plan builds on)

| Area | Current shape | Consequence for this plan |
| --- | --- | --- |
| `harness/app.py` | Composition root; `mcp.load()` at construction | Replace with `modules.load_all()` |
| `harness/runtime.py` | One `RLock`-serialized loop; `self.tools.schemas()` per turn | Add per-run capability snapshot + schema budget |
| `harness/tools.py` | Flat `ToolRegistry`, builtins + MCP; `McpServer` launches per call | Add module metadata, side-effect/trust flags, visibility enforcement |
| `harness/context.py` | Fixed sections; skills read from a directory | Registry-driven skill disclosure (complete) |
| `harness/memory.py` | FTS5, scopes, revisions, `expires_at`, retraction | Add usage counters, `archived_at`, archive/restore |
| `harness/scheduler.py` | Deterministic heartbeat; idempotent occurrences | Add memory sweep, inbox triage, skill draft pass (skill pass complete) |
| `harness/db.py` | `SCHEMA_VERSION = 3`; additive `_migrate_to` | Add v4..v7 migrations, one per phase |
| `harness/server.py` | Monolithic `/api/state`; thin endpoints | Add module/inbox/guard endpoints; split state later |
| `harness/web/` | Static `index.html`, `app.js`, `style.css` behind auth | Add panels incrementally in Phase 6 |
| `tests/` | Scripted model; 10 acceptance scenarios | Extend with module/web/inbox/guard fakes |

Current data flow is already correct for this plan:

```
trigger -> runtime.submit/resume -> route -> context build -> model
        -> resolve/validate/permission -> execute/approve -> record -> repeat
```

New features attach to existing seams; they do not add a second loop.

---

## 4. Target architecture

### 4.1 Capability modules

One record shape for every capability. Skills, MCP servers, and the web
module are the same kind of thing to the runtime; they differ only in how
they contribute capability.

```sql
CREATE TABLE modules (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('skill','mcp','web')),
  name TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK(status IN ('draft','active','disabled','error')),
  scope TEXT NOT NULL DEFAULT 'global'
    CHECK(scope IN ('global','domain','adaptive')),
  manifest TEXT NOT NULL,          -- JSON: preconditions, transport, risk, limits
  content_path TEXT,               -- skill body / module docs (relative)
  origin TEXT NOT NULL,            -- user | agent | system | import
  provenance TEXT NOT NULL,        -- JSON: run/task/approval/url references
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_loaded_at TEXT,
  last_error TEXT,
  usage_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE module_bindings (
  module_id TEXT NOT NULL,
  domain_id TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  risk_override TEXT,
  PRIMARY KEY (module_id, domain_id)
);

CREATE TABLE module_tools (
  module_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  schema TEXT NOT NULL,
  risk TEXT NOT NULL,
  side_effects INTEGER NOT NULL DEFAULT 1,
  read_only INTEGER NOT NULL DEFAULT 0,
  last_seen TEXT NOT NULL,
  PRIMARY KEY (module_id, tool_name)
);
```

Rules:

- `scope=global`: eligible in every domain.
- `scope=domain`: eligible only where a `module_bindings` row says enabled.
- `scope=adaptive`: eligible wherever its preconditions match (skills).
- A module in `draft` or `disabled` is never disclosed and never callable.
- Skill content lives at `data/modules/skills/<id>/v<version>.md`.
- MCP manifests are rows (editable from the UI); `env` holds variable names.
- The legacy `tools.skills_dir` and `tools.mcp_servers_file` become one-time
  import sources, then optional read-only mirrors.

### 4.2 Per-run capability snapshot

At `start_run`:

1. Resolve modules eligible for `task.domain_id`.
2. Filter by preconditions (skills) and bindings/health (MCP).
3. Apply the tool-schema budget.
4. Persist the chosen tool names, module ids, and exclusions on the run
   (`runs.meta.toolset`) and as `context_sources`.

The loop uses the snapshot for the whole run. A module reload or domain
switch cannot silently change the available toolset mid-run. If `use_domain`
changes the domain, recompute at the next iteration and record a
`toolset.changed` event and context source.

Visibility is enforced twice:

- schemas are filtered per run, and
- `ToolRegistry.resolve()` refuses a tool not in the run snapshot.

Hiding a schema while still accepting a call by name is a permission bypass
and must be regression-tested.

### 4.3 Trust and taint

Added in Phase 1, before any external content is enabled, and completed by
the guard in Phase 7.

`ToolSpec` gains:

```python
side_effects: bool = False      # can change the world outside the harness
read_only: bool = False         # explicitly declared by a module manifest
untrusted_output: bool = False  # output text is not authored by the user
```

Run state gains `tainted_sources` in `runs.meta`, a bounded list of
`{tool, module, origin, at}` entries.

Policy:

1. A result with `untrusted_output=True` taints the run for subsequent steps.
2. In a tainted run, a `side_effects=True` tool is escalated to confirm-risk
   even if it was configured `low` or `autoapprove: true`.
3. The approval prompt names the untrusted origin (for example, the result
   URL or inbox source), so the user authorizes with context.
4. Read-only tools remain low-risk in a tainted run.
5. A tool with unknown provenance is `side_effects=True` by default. MCP
   modules must opt into `read_only` explicitly.
6. The guard (Phase 7) may add friction, never remove it.

This is the primary defence. A classifier is a signal, not the control.

### 4.4 Tool schema budget

Tool schemas are part of every prompt. `context.tool_schema_tokens` is
currently only subtracted from the budget; it must be enforced.

- Measure with the existing rough estimator (`util.token_len`).
- Priority when over budget: builtins > trusted low-risk modules > web >
  domain-bound modules > global modules > the rest.
- Drop whole modules, never half a schema, and record each drop on the run.
- Surface the budget in the Modules UI.

If module count later makes this painful, the escape hatch is progressive
disclosure: a `find_tools`/`use_module` meta-tool that loads a module's
schemas on demand. Not in this plan unless measured need appears.

### 4.5 Data layout

```
data/
  harness.db
  config.yaml
  modules/
    skills/<id>/v<n>.md
    mcp/<id>.yaml          # optional export/backup mirror
  artifacts/
    memories/...
    consolidation/...
    subagents/<task_id>.md
  backups/...
```

`App.backup()` and `App.restore()` must include `modules/`. This is easy to
miss and silently loses capabilities on a restore.

---

## 5. Phase overview

| Phase | Theme | Depends on | Commit |
| --- | --- | --- | --- |
| 0 | Foundations and seams | - | one |
| 1 | MCP modules, web module, trust flags | 0 | one |
| 2 | Skill modules and self-building | 1 | one |
| 3 | Memory lifetime and archive | 0 | one |
| 4 | Sequential sub-agents | 1, 3 | one |
| 5 | Inbox, automations, self-scheduling | 1, 3, 4 | one |
| 6 | UI information architecture | all above | one |
| 7 | Injection guard (LaYa) | 1, 5, 6 | one |

Parallelism: Phase 3 can run any time after Phase 0 and is a good early win.
Phase 6 panels are added incrementally as phases land; the phase-6 commit is
the navigation and information-architecture pass.

Ordering rationale:

- Modules before everything: every later feature consumes the registry.
- MCP before skills: it forces the hard parts (dynamic discovery, health,
  per-domain visibility, schema budget) and the web module.
- Trust flags in Phase 1: external content exists from then on.
- Memory lifetime is independent and low-risk.
- Sub-agents before inbox: stabilise decomposition before external systems
  can create work.
- Inbox after sub-agents and memory: automation-generated tasks benefit from
  both, and the trust policy is already in place.
- Guard last because LaYa is not online yet; the policy it feeds already
  exists and is testable without it.

---

## 6. Phase details

### Phase 0 - Foundations and seams

**Status:** complete (2026-10-28).

**Goal:** prepare the codebase without changing behaviour.

**Scope**

- Split `harness/web_page.py` into `harness/web/index.html`,
  `harness/web/app.js`, `harness/web/style.css`. Serve assets through an
  authenticated handler; do not mount raw `StaticFiles` outside auth.
- Add `harness/modules/` package with `types.py`, `registry.py`, and an empty
  `load_all()` used by `App`. Existing skills/MCP loading stays the source of
  truth in this phase.
- Add schema v3: `modules`, `module_bindings`, `module_tools`.
- Add `ToolRegistry.specs()` and stop reading `app.tools._tools` in
  `server.py`.
- Add `ToolRegistry.schemas_for(domain_id, *, budget_tokens)` returning all
  tools in this phase, with budget measurement and deterministic dropping.
- Write the capability snapshot to `runs.meta.toolset` (names + modules +
  exclusions). All tools are global for now.
- Include `data/modules/` in `App.backup()` and `App.restore()`.
- Add `modules.*` config keys, all inert in this phase.

**Config**

```yaml
modules:
  enabled: true
  dir: modules                 # relative to the data dir
  health_interval_s: 300.0
  tool_schema_budget_tokens: 2400
```

**Tests**

- UI still serves `/`, assets resolve, all existing `data-tab` values remain.
- Schema migration from v2 opens an existing database.
- Backup includes a file under `data/modules/`; restore puts it back.
- Schema budget drops whole tools and records the exclusion.
- Full deterministic suite passes unchanged.

**Exit criteria:** no user-visible behaviour change; one commit.

**Commit:** `Phase 0: split the UI shell and add module/capability foundations`

---

### Phase 1 - MCP modules, dynamic loading, web module, trust flags

**Status:** complete (2026-11-09).

**Goal:** MCP servers become domain-scoped modules that reload without a
process restart, and the agent gets web search through SearXNG.

**Scope**

- `harness/modules/mcp.py`: replace `McpTools` with a registry-backed
  loader. Keep the existing stdio JSON-RPC client and per-call process
  isolation.
- Manifest fields: `transport` (`stdio` now), `command`, `args`, `env_names`,
  `domains`, `autoapprove`, `risk_default`, `tools_allow`, `tools_deny`,
  `read_only`, `side_effects`, `startup` (`eager|lazy`), `timeout_s`.
- Import existing `examples/mcp_servers.yaml` / `tools.mcp_servers_file` into
  module rows on first load; keep the file supported as an import source.
- Domain scoping: `scope=global` or `scope=domain` + `module_bindings`.
- Reload without restart:
  - `POST /api/modules/reload`
  - `POST /api/modules/{id}/reload`
  - `POST /api/modules/{id}/enable|disable`
  - CLI `modules`, `modules --reload ID`, `modules --enable/--disable ID`
- Health: heartbeat checks enabled modules every `modules.health_interval_s`,
  records `module.unavailable`, never blocks a turn. Discovery is cached in
  `module_tools`.
- Add trust fields to `ToolSpec`; mark MCP output untrusted by default and
  builtin memory/history output trusted.
- Taint enforcement in `_handle_tool_call`: escalation rule and approval
  provenance line. Persist `runs.meta.tainted_sources`.
- Enforce run-snapshot visibility in `resolve()` and `execute()`.
- Ship the web module: `modules/web/` stdio MCP server.
  - `web_search(query, categories?, max_results?)` -> bounded
    `{title, url, snippet}`.
  - Uses SearXNG's JSON API. Verified 2026-10-28:
    `http://192.168.50.40:8080/search?q=test&format=json` returns 200 JSON.
  - Results marked `untrusted_output=True`, each with its URL as provenance.
  - `web_fetch` is deferred (SSRF + injection surface; see section 9).
- Do **not** add dynamic Python imports.

**Config**

```yaml
modules:
  default_mcp_risk: confirm
  lazy_start: true
web:
  enabled: true
  searxng_url: http://192.168.50.40:8080
  max_results: 5
  timeout_s: 10.0
  categories: general
  safe_search: 1
```

**Schema:** reuse v3 tables; `module_tools.side_effects/read_only` populated.

**Tests**

- Fake stdio MCP server with two tools; discovery and round trip.
- A module bound to `domain:A` is invisible in `domain:B`; calling it by
  name is denied, not executed.
- Reload changes the registered toolset without restarting the app.
- Unavailable module recorded and never blocks a turn.
- Schema budget excludes a low-priority module and records why.
- Web module against a local stub HTTP server; bounded output; untrusted
  flag; malformed JSON is a clean error.
- Taint: a low-risk side-effecting MCP tool becomes confirm after a web
  result, and the approval names the source.
- Port note in docs: harness defaults to 8080 too; do not run both on the
  same host without changing `HARNESS_PORT`.

**Exit criteria:** web search works end to end in chat; modules can be
disabled/reloaded from API and CLI; no side effect can follow untrusted
content without approval.

**Commit:** `Phase 1: module-scoped MCP, dynamic reload, and SearXNG web search`

---

### Phase 2 - Skill modules and self-building skills

**Status:** complete (2026-11-10).

**Goal:** skills are modules with domain/adaptive scoping, and the agent can
propose new skills as reviewable drafts.

**Scope**

- Import `tools.skills_dir` into module rows (`kind=skill`), keeping the
  `SKILL.md` + front-matter format as an import source.
- Replace `ContextBuilder._skills_block` with registry disclosure:
  - `scope=domain` only in bound domains;
  - `scope=global` always eligible;
  - `scope=adaptive` wherever preconditions match;
  - record `skill:<id>` sources and non-applicable reasons as today.
- Skills remain instruction text. No handlers, no schemas, no executable
  content. Executable capability is a tool or MCP module.
- `propose_skill` tool (confirm-risk): name, description, preconditions,
  domains, body. Approval shows the bounded full body. On approval the skill
  is written as `status=draft`, never `active`.
- `SkillBuilder` heartbeat pass:
  - After N successfully completed tool-using tasks, run one bounded model
    pass that proposes at most one draft skill as strict JSON.
  - Reuse the consolidation pattern: strict JSON -> validate -> apply ->
    markdown log under `artifacts/skills/`.
  - A failed pass changes nothing.
  - Never derive a draft from a run whose content was untrusted. If the
    provenance is untrusted, record a refusal event instead.
- Lifecycle: draft -> active -> disabled; versioned bodies; one-click
  rollback; provenance and revision on every change.
- Cap body size (`skills.max_body_chars`), preconditions, and name length.

**Config**

```yaml
skills:
  enabled: true
  auto_draft: true
  draft_after_successful_tasks: 5
  max_drafts_per_day: 2
  max_body_chars: 6000
```

**API/CLI:** `skills` list, `skills --activate ID`, `--disable ID`,
`--rollback ID`, plus module endpoints for drafts.

**Tests**

- Domain-bound skill disclosed only in its domain; adaptive skill disclosed
  on precondition match; draft never disclosed.
- `propose_skill` creates an approval and nothing else; approval creates a
  draft; a second approval is refused or deduped.
- Untrusted-origin skill proposal is refused with an event.
- Rollback restores the previous body and revision.
- Existing skill tests (`tests/test_tools_mcp.py`) updated, not deleted.

**Exit criteria:** the agent can learn a procedure it used successfully, but
only the user can make it active.

**Commit:** `Phase 2: domain-scoped skill modules and approval-gated self-built drafts`

---

### Phase 3 - Memory lifetime and archive

**Goal:** used memory survives; unused memory is archived; archived memory
is still retrievable and explainable.

**Scope**

- Schema v4: additive columns, no `CHECK` change.
  `access_count`, `last_accessed_at`, `archived_at`, `pinned`, `importance`.
  Do not add `status='archived'`; the existing `CHECK` cannot be altered and
  a table rebuild is unnecessary risk.
- `MemoryManager.record_access(ids, run_id)`: dedupe per run, one update per
  memory, batched. Called when memories are included in context and when
  `search_knowledge` returns them. Not on raw FTS candidate scans.
- `MemoryManager.sweep_lifetime(now)`: deterministic, no model call.
  `lifetime = clamp(base_days * (1 + access_count) ** growth, min_days, max_days)`
  measured from `last_accessed_at` (fallback `created_at`). Archive when the
  age exceeds it, unless pinned or a protected kind.
- `archive(id)` / `restore(id)` with a revision-style audit event.
- `search(..., include_archived=False)`: archived rows get a score penalty,
  an `archived` label, and a clear reason. Explicit retrieval un-archives and
  records why.
- `search_knowledge` gains `include_archived`.
- `expire_stale` stays for `temp`/`expires_at`; archived and expired are
  different states.
- Heartbeat runs the sweep on `memory.lifetime.sweep_interval_s`, bounded by
  `sweep_batch`.
- CLI/API/UI: archive, restore, filters, usage counts.

**Config**

```yaml
memory:
  lifetime:
    enabled: true
    base_days: 30
    growth: 0.5
    min_days: 14
    max_days: 1095
    protected_kinds: [constraint, decision, preference]
    sweep_interval_s: 3600
    sweep_batch: 200
```

**Tests**

- Access bumps counters once per run.
- Unused memory archives after its lifetime; used memory survives longer.
- Pinned and protected kinds never archive.
- Archived memory is absent from default context/search and present with
  `include_archived`, labelled and penalised.
- Explicit retrieval un-archives and logs it.
- `temp` expiry still behaves exactly as before.
- Consolidation ignores archived rows and never resurrects them.

**Exit criteria:** lifetime is deterministic and inspectable; no model call
is spent on retention.

**Commit:** `Phase 3: usage-based memory lifetime with archive and retrieval`

---

### Phase 4 - Sequential sub-agents

**Goal:** the agent can delegate a bounded sub-problem and get a result back,
without changing the single-loop, single-writer model.

**Decision:** v1 is **sequential and nested**, depth 1. Parallel sub-agents
are explicitly deferred (section 9). This is the only shape that fits the
current single `RLock`, exact approvals, and deterministic recovery.

**Scope**

- Schema v5: `tasks.parent_task_id`, `tasks.parent_run_id`,
  `tasks.role` (`user|subagent`), `runs.parent_run_id`. Keep depth and
  counters in `meta`.
- `delegate_task` tool:
  `goal` (required), `context` (optional), `expected_output` (optional).
  Low-risk, but only available to user-origin tasks (not to sub-agents).
- Child context: `ContextBuilder` in sub-agent mode.
  - Include the goal, the parent-supplied context, and domain memories.
  - Do **not** include the parent transcript.
  - Add a short sub-agent guidance block: return a concise result, do not
    ask the user, do not create domains.
- Child toolset: low-risk only. No `delegate_task`, no `create_domain`, no
  confirm-risk tools. If a child needs an approval-gated action it returns
  `blocked`; the parent proposes the action normally.
- Budgets: `max_depth=1`, `max_children_per_run=3`,
  `max_iterations=8`, `max_tool_calls=12`, `max_seconds=180`,
  `max_result_chars=4000`.
- Durability:
  - Child is a normal task/run, so existing recovery applies.
  - Write the child result to `artifacts/subagents/<task_id>.md` and into the
    parent's tool result.
  - Re-delegation of the same open child reuses it by
    `origin_ref = parent_task_id:goal_hash` instead of spawning duplicates.
  - A crash mid-child becomes an unknown execution for the parent; the parent
    sees the unknown and decides, exactly like any other tool.
- Events: `subagent.started`, `subagent.completed`, `subagent.blocked`,
  `subagent.reused`.
- UI: child tree inside task detail; parent shows delegated work as a tool
  result with a link.

**Config**

```yaml
subagents:
  enabled: true
  max_depth: 1
  max_children_per_run: 3
  max_iterations: 8
  max_tool_calls: 12
  max_seconds: 180.0
  max_result_chars: 4000
  allow_confirm_risk: false
```

**Tests**

- Parent delegates, child runs, result returns, parent continues.
- Child cannot see the parent transcript.
- Child cannot call `delegate_task` (depth) or confirm-risk tools.
- Child limit or question returns `blocked`; the parent does not hang.
- Crash mid-child -> unknown for the parent -> no duplicate side effect.
- Reuse by goal hash; no duplicate children.
- Budget caps enforced.

**Exit criteria:** delegation is useful for research/summarisation tasks and
cannot escape approvals or duplicate work.

**Commit:** `Phase 4: bounded sequential sub-agents with durable child runs`

---

### Phase 5 - Inbox, automations, and self-scheduling

**Goal:** external systems can durably wake the agent through one idempotent
API, with rules and loop protection. No specific email/messaging adapter is
built now; the API is the contract.

**Scope**

- Schema v6:
  - `inbox_events(id, source, external_id, kind, title, body, received_at,
    status, severity, meta, trust)` with `UNIQUE(source, external_id)`.
    Statuses: `new|triaged|promoted|ignored|deduped`.
  - `automations(id, name, enabled, source, kind, match_keywords,
    match_regex, action, prompt_template, domain_id, cooldown_s,
    max_per_hour, last_fired_at, disabled_reason)`.
- `POST /api/inbox` (token auth), idempotent insert, bounded body size.
  Returns `{accepted, id, duplicate}`.
- `harness/inbox.py`:
  - `receive(...)`, `triage(now)`.
  - Triage order: dedupe -> trust/taint marking -> guard (Phase 7) ->
    automation match -> rate limit -> create task with
    `origin='inbox:<source>'` and `meta.inbox_event_id` -> run within
    `scheduler.max_runs_per_tick` -> otherwise notify.
  - Inbox bodies are `untrusted`; runs derived from them start tainted, so
    side effects need approval (Phase 1 rule).
- Automations:
  - Actions: `task`, `notify`, `ignore`.
  - Per-automation cooldown and `max_per_hour`.
  - Loop breaker: hitting the cap disables the automation, records
    `automation.disabled`, and writes one visible system note.
  - Quiet hours configurable.
- Heartbeat: triage new inbox events before due schedules, so urgent
  external work is not delayed behind a clock tick.
- Self-scheduling tools: `schedule_task` / `cancel_schedule`, confirm-risk.
  Reuse `scheduler.create_schedule`, bind origin to the parent task, and
  enforce a per-task cap so a scheduled task cannot recursively schedule
  itself forever.
- No adapter in this phase. Document the contract and provide a `curl`
  example plus an `integrations/README.md` describing the future mailbox and
  messaging bridges as **separate pushers**, not code in the core process.
- API/CLI/UI: inbox list, ignore, automation CRUD.

**Config**

```yaml
inbox:
  enabled: true
  max_body_chars: 12000
  max_events_per_tick: 5
  dedupe_window_days: 30
  quiet_hours: ""
automations:
  default_action: notify
  max_per_hour: 6
```

**Tests**

- Duplicate `(source, external_id)` inserts once.
- Automation creates exactly one task; second event within cooldown is
  recorded, not duplicated.
- Rate-limit breaker disables the automation and notifies once.
- Inbox-origin run is tainted; a side-effecting tool needs approval.
- Quiet hours suppress runs but keep events.
- Self-scheduled task cannot exceed the per-task cap.
- Heartbeat ordering: inbox before schedules.
- A malformed/oversized event is rejected without a traceback.

**Exit criteria:** any external app can wake the agent with one HTTP call and
cannot cause an uncontrolled loop or an unapproved side effect.

**Commit:** `Phase 5: durable inbox, declarative automations, and self-scheduling`

---

### Phase 6 - UI information architecture

**Goal:** the UI explains and controls the system instead of accumulating
flat tabs.

**Scope**

- Navigation groups:
  - **Conversation**: chat, task detail drawer.
  - **Work**: tasks (+ sub-agent tree), approvals, schedules, inbox,
    automations.
  - **Knowledge**: memories (active/archived/superseded, usage), domains,
    skills, consolidation.
  - **System**: modules (MCP/web health, bindings, reload), tools, events,
    config, backup, guard later.
- Split `/api/state` into per-panel endpoints (`/api/tasks`, `/api/memories`,
  `/api/modules`, `/api/inbox`, ...) and keep a small `/api/summary` for
  header badges. Poll only the active panel; keep the chat poll.
- Modules page: health, last load, last error, tool count, domain bindings,
  enable/disable/reload, schema budget usage.
- Skills page: drafts, active, disabled, provenance, version, rollback.
- Memory page: usage count, last accessed, archived filter, restore.
- Tasks page: child tree, delegated results, context sources.
- Inbox page: events, trust label, automation rules, fire history.
- Keep the browser stateless; every action goes through the API.
- Update `test_web_page_exposes_every_panel` to the new contract, and add one
  API contract test per panel.
- No build step; no framework.

**Exit criteria:** a new user can find modules, memories, automations, and
sub-agent work without reading the source.

**Commit:** `Phase 6: UI information architecture for modules, memory, inbox, and sub-agents`

---

### Phase 7 - Injection guard with the LaYa CPU model

**Goal:** add a classifier layer on top of the trust policy when LaYa is
online. The system must be fully functional and safe before this phase.

**Preconditions**

- LaYa is running and reachable through an OpenAI-compatible endpoint (or a
  documented adapter), and its latency/throughput is measured.
- Phases 1 and 5 are stable; taint escalation is already enforced.

**Scope**

- Schema v7: `content_scans(id, hash, verdict, labels, model, ts, meta)` with
  a unique hash index for caching.
- `harness/guard.py`:
  - `screen(text, *, origin, kind) -> {allow, risk, labels, reason, cached}`.
  - Tier 1 heuristics: instruction override, role/tool spoofing, encoded
    payloads, exfiltration requests, urgency/social engineering, suspicious
    URLs, hidden markup.
  - Tier 2 model classification only for ambiguous chunks: one short prompt,
    strict label output, bounded chars, `timeout_s`, no thinking.
  - Cache by `sha256(text + model + prompt_version)`.
  - Never feed guard output into the transcript as instructions; only labels.
- Config (disabled by default until LaYa is verified):

```yaml
guard:
  enabled: false
  base_url: ""
  model: laya
  timeout_s: 3.0
  max_chars: 4000
  fail_mode: closed          # side effects need approval if the guard is down
  cache_ttl_days: 30
  block_high_risk: false     # default: escalate, do not hard-block
```

- Integration points:
  - Web results before they enter context.
  - Inbox bodies before task creation (Phase 5).
  - Skill drafts before they can be written (Phase 2 hook).
  - Artifact reads whose provenance is untrusted.
- Never scan the user's own messages by default.
- `fail_mode=closed`: read-only work continues; side-effecting work needs
  approval. `fail_mode=open` is opt-in and documented as weaker.
- This is a **classifier endpoint, not a fallback reasoning model.** The
  one-model invariant for reasoning is unchanged.
- UI: Guard panel with recent verdicts, cache hit rate, and manual re-scan.
- Tests: adversarial corpus with a fake classifier; down endpoint; cache
  behaviour; labels never become instructions; high-risk still needs
  approval; no scan of user messages.

**Exit criteria:** LaYa can be enabled with one config change; disabling it
restores exactly the Phase 6 behaviour.

**Commit:** `Phase 7: optional LaYa-backed injection classification on top of trust policy`

---

## 7. Cross-cutting concerns

### 7.1 Migrations

- One additive migration per phase, `SCHEMA_VERSION` bumped in `db.py`.
- Never edit an existing `CHECK` constraint in place. Use columns
  (`archived_at`) or a new table.
- A migration test opens a database created by the previous version.
- `App.startup()` recovery must account for any new state (modules in
  `error`, inbox events mid-triage, child runs).

### 7.2 Records and observability

Every phase adds event types; the UI reads them instead of inventing state.

```
module.loaded / module.reloaded / module.disabled / module.unavailable
module.tool_refused / toolset.budget_dropped / toolset.changed
trust.tainted / trust.escalated
skill.draft_proposed / skill.activated / skill.rolled_back / skill.refused
memory.accessed / memory.archived / memory.restored
subagent.started / subagent.completed / subagent.blocked / subagent.reused
inbox.received / inbox.deduped / inbox.triaged / inbox.promoted
automation.fired / automation.disabled
guard.scanned / guard.verdict / guard.unavailable
```

### 7.3 Tests and fakes

- Fake MCP stdio server in `tests/` for discovery, reload, failure.
- Local HTTP stub for SearXNG; the web module must not require the LAN.
- Fake guard classifier through `ScriptedModel` or a stub endpoint.
- Inbox tests use the HTTP API and the heartbeat, never wall-clock sleeps.
- Keep the ten acceptance scenarios passing; add one scenario per new
  guarantee (web result cannot trigger an unapproved send; archived memory
  recall; inbox dedupe; sub-agent isolation).

### 7.4 Documentation

Each phase updates:

- `README.md`: behaviour, config groups, limitations.
- `CODEMAP.md`: new modules, tables, data flow.
- `examples/`: module manifests, an inbox `curl`, a guard config sample.
- `LONGTERMPLAN.md`: tick the phase, record decisions that changed.

### 7.5 Commit discipline

One commit per phase, matching the message above. If a phase grows beyond
roughly 1,500 lines, split it into two commits by layer (backend/schema then
UI) and update this plan. Do not mix phases.

---

## 8. Risk register

| Risk | Impact | Mitigation | Phase |
| --- | --- | --- | --- |
| Module schemas blow the prompt budget | Degraded reasoning on every turn | Enforce `tool_schema_budget_tokens`; record drops; UI shows usage | 0-1 |
| Dynamic MCP changes tools mid-run | Non-reproducible approvals | Per-run capability snapshot; re-snapshot only on domain change | 0-1 |
| Hidden tool still callable by name | Permission bypass | Enforce snapshot in `resolve`/`execute`; regression test | 1 |
| Web content prompts a tool call | Unapproved side effect | Taint escalation + approval provenance; guard later | 1, 7 |
| Self-built skill persists injected text | Durable prompt injection | Drafts only; approval; refuse untrusted provenance; versioned rollback | 2 |
| Memory archive loses useful facts | Recall regression | Usage counters, protected kinds, pins, archive retrieval with penalty | 3 |
| Sub-agent escapes approvals | Privilege escalation | Low-risk-only child toolset; depth 1; blocked result on confirm need | 4 |
| Inbox loop (email storm) | Resource burn / spam | Idempotency, cooldown, `max_per_hour`, auto-disable breaker, quiet hours | 5 |
| Inbox content drives outbound send | Data exfiltration | Untrusted taint, side-effect confirm, no autoapprove for untrusted runs | 1, 5 |
| Guard model slow or down | Blocked work | Disabled by default; timeouts; cache; fail-closed only for side effects | 7 |
| Backup omits modules | Capabilities lost on restore | Include `data/modules/`; round-trip test | 0 |
| Port 8080 collision with SearXNG | Web module unreachable or harness unreachable | Document `HARNESS_PORT`; explicit `web.searxng_url` | 1 |
| UI rewrite churn | Lost time | Static split in Phase 0; panels incremental; no framework | 0, 6 |

---

## 9. Explicitly deferred / out of scope

- **Parallel sub-agents.** Would require per-run locks, a worker pool,
  concurrent approval semantics, and scheduler changes. Revisit only if
  sequential delegation is demonstrably too slow.
- **Web fetch / browsing automation.** SSRF and injection surface. If added:
  allowlist, confirm-risk, guard, and bounded extraction.
- **Email, IMAP, Telegram, Matrix adapters.** The inbox API is the contract.
  Adapters are separate pushers with their own credentials and lifecycle.
- **Persistent MCP connections.** Per-call process isolation is slow but
  crash-safe. Measure before changing.
- **Dynamic Python plugins or executable skills.** Reopens the code-execution
  hole the project intentionally closes.
- **Vector search / embeddings.** FTS5 remains the retrieval mechanism.
- **Multi-user, accounts, TLS.** Single user behind a token and a VPN/proxy.
- **Skill parameterisation/templating.** Adaptive scope plus domains covers
  the current need; templates add a language to maintain.
- **Automatic activation of self-built skills.** Never. Drafts require the
  user.
- **Consolidation-driven archive decisions.** Retention stays deterministic.

---

## 10. Decision log

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-10-28 | Guard/LaYa is the last phase and disabled by default | LaYa is not online yet; the trust policy must stand alone |
| 2026-10-28 | Sequential nested sub-agents for v1 | Fits the single `RLock`, exact approvals, and deterministic recovery |
| 2026-10-28 | Build the generic inbox API now; no adapter | No source chosen yet; the API is the durable contract |
| 2026-10-28 | One commit per phase | Keeps runtime invariants reviewable and bisectable |
| 2026-10-28 | Keep the UI zero-build | Single user, single process; a bundler adds cost without benefit |
| 2026-10-28 | Skills stay instruction-only | Preserves the "data, never instructions/execution" guarantee |
| 2026-10-28 | Modules before skills/MCP features | One registry serves every capability and the UI |
| 2026-10-28 | Memory lifetime uses `archived_at`, not a new status | SQLite `CHECK` constraints cannot be altered in place |
| 2026-10-28 | Phase 0 serves UI assets through the authenticated handler | Avoids a `StaticFiles` mount that would bypass the token |
| 2026-10-28 | Phase 0 snapshots the toolset with a budget but changes no loader | Tests the seam and records exclusions before modules become authoritative |
| 2026-11-09 | The registry is authoritative for MCP and web in Phase 1; skills stay directory-based until Phase 2 | Keeps the phase focused on dynamic loading and trust, and avoids a half-migrated skill path |
| 2026-11-09 | Drop lowest-priority schemas first: rest, global, domain-bound, web, trusted low-risk, builtins | The Phase 0 ordering dropped builtins first, which made the core loop fail before optional modules |
| 2026-11-09 | The web module starts lazily and reuses cached discovery | Avoids launching a subprocess for every app construction while still refreshing on demand |
| 2026-11-09 | Taint escalation applies to any side-effecting, non-read-only tool, not only MCP tools | Phase 1 already exposes external content; the rule must hold for every future side-effecting capability |
| 2026-11-09 | Approval reasons name the first recorded untrusted origin (URL, source, or tool) | The user needs the source in the prompt to authorize with context |
| 2026-11-10 | Keep schema version 3; skill drafts reuse module rows | Phase 2 needs no new tables, and one record shape keeps the UI/API uniform |
| 2026-11-10 | Imported skills map to `global`, `domain`, or `adaptive` from front matter | The scope is the disclosure rule; domains bind, preconditions match |
| 2026-11-10 | Re-proposing a skill name revises the same row and writes a new version | Prevents near-duplicate rows and makes rollback a property of one skill |
| 2026-11-10 | `version_history` stores rollback snapshots; `revisions` is a bounded audit list | Rollback needs full prior state, while the audit trail stays small |
| 2026-11-10 | Auto-draft consumes each completed run once via `schema_meta` | A failed or empty pass must not retry the same batch forever |
| 2026-11-10 | A tainted run is refused for skill proposals and marked seen | Skills must never encode text derived from untrusted content |

---

## 11. Definition of done

The plan is complete when:

1. A user can install a module (skill, MCP, web), bind it to a domain, and
   see its health and schema budget in the UI without editing code.
2. Web search works, and a web result cannot cause a side effect without an
   approval that names the source.
3. The agent can propose a skill from a successful task, and the user can
   review, activate, disable, and roll back it.
4. Memory usage determines retention; archived memory can be found again.
5. An external app can POST to `/api/inbox` and get exactly one durable task,
   with loop protection and no unapproved side effect.
6. A complex task can delegate bounded sub-work and recover honestly from a
   crash.
7. Launching the guard is a config change that adds friction without
   changing the trust policy.
8. The full deterministic suite passes, the ten acceptance scenarios still
   pass, and each phase has its own commit.
