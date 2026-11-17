# Personal Harness - Long-Term Plan

Status: in progress (Phases 0-6 and 8 complete; 9A implemented; Phase 7 waits on LaYa; 9B and Phases 10-13 proposed)
Last updated: 2026-11-16
Scope: capability modules (skills + MCP servers), memory lifetime, autonomy,
sub-agents, prompt-injection defence, UI information architecture, approvals,
model selection/routing, domains as work environments, install/setup, multi-user.

Revision 2026-11-15: every add-on capability is a generic MCP module. The
separate `web` module kind, the `web.*` config group, and the harness-side
web endpoint knowledge are retired; the web and calendar servers are
self-contained MCP servers registered through the MCP manifest. Phase 1 below
records the original design and is superseded where it names a web module or
`web.*` configuration.

Revision 2026-11-15 (b): added Phases 8-13 after reviewing the harness code:
approval rework and MCP/taint fixes (8), model selection and router (9),
domains as work environments (10), UI overhaul (11), install/reset/MCP setup UI
(12), and multi-user (13). The injection guard stays Phase 7 and still waits on
LaYa. Phase 6 is complete; the Phase 5 status line above was stale.

This document is the working plan. Section 7.7 describes the session and
deployment workflow (Claude Code on Windows, one session per phase, deployed to
Ares in Docker). Each phase is one merge/commit unless a
phase is explicitly split. Keeping the deterministic test suite green is a
precondition for every phase, not a follow-up task.

---

## 1. Product goals

1. **Capabilities become modules.** Skills and MCP servers are first-class,
   domain-bound, dynamically loadable units with manifests, provenance,
   versions, health, and an on/off switch.
2. **The agent can reach the web** through an MCP server the user registers
   (a SearXNG-based example ships in `harness/modules/web/`), with results
   treated as untrusted data. Endpoints and credentials live in that server's
   own config file, never in the harness config.
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
9. **Domains are work environments.** A domain bundles memories, skills,
   instructions, and exclusive tools; entering one is reliable, visible, and
   correctable, and never causes the agent to forget the user.
10. **Approvals feel conversational.** The user can approve, reject, give
    feedback, or edit a proposed action without restarting the request.

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

One record shape for every capability. Skills and MCP servers are the same
kind of thing to the runtime; they differ only in how they contribute
capability. A server's endpoints, credentials, and options belong to that
server, not to the harness.

```sql
CREATE TABLE modules (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('skill','mcp')),
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
- The legacy `tools.skills_dir` and `tools.mcp_servers_file` are import
  sources; the MCP file is re-read on startup and on explicit reload.

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
| 1 | MCP modules, first MCP server, trust flags | 0 | one |
| 2 | Skill modules and self-building | 1 | one |
| 3 | Memory lifetime and archive | 0 | one |
| 4 | Sequential sub-agents | 1, 3 | one |
| 5 | Inbox, automations, self-scheduling | 1, 3, 4 | one |
| 6 | UI information architecture | all above | one |
| 7 | Injection guard (LaYa) | 1, 5, 6 | one |
| 8 | Deployment safety, approval rework, MCP read-risk, taint gaps, web hardening | 1, 5 | four sessions (8.0, 8A, 8B-1, 8B-2) |
| 9 | Model selection and router | 0 | two (9A, 9B) |
| 10 | Domains as work environments | 3, 6 | eight sessions (10A-10D, 10E-1, 10E-2, 10F, 10G) |
| 11 | UI overhaul | 8, 10 | three sessions (11A-11C) |
| 12 | Install, reset, doctor, MCP setup UI | 1, 6 | two (12A, 12B) |
| 13 | Multi-user and shared spaces | all above | four sessions (13A-13D) |
| C | Capability track (new MCP servers, parallel) | 8.1, 12B | one session per server |

Parallelism: Phase 3 can run any time after Phase 0 and is a good early win.
Phase 6 panels are added incrementally as phases land; the phase-6 commit is
the navigation and information-architecture pass.

### 5.1 Recommended order after Phase 6

| Order | Session(s) | Why now |
| --- | --- | --- |
| 1 | 8.0 deployment safety and staging | Every later phase migrates real data on Ares; backups, a staging instance, and a runbook come first |
| 2 | 8.2 memory-write taint | Real security gap, tiny change |
| 3 | 8A (8.1 per-tool MCP risk, 8.3 reject repro, 8.4 web hardening) | Small, independent, user-visible |
| 4 | 8B-1 then 8B-2 (approval rework) | Biggest UX win; unblocks "edit requests" |
| 5 | 9A Change Model | Small; needed by evals and the router |
| 6 | 12B MCP setup UI, then the first capability-track servers | Domains only prove themselves with real tools; adding a tool must stop requiring file edits |
| 7 | 10A-10D (baseline, routing evidence, entry, soft memory) | The core idea; now has tools to work with |
| 8 | 10E-1, 10E-2 (visibility and correction loop) | Starts producing labelled real-use data |
| 9 | 12A install, reset, doctor | Makes testing from zero routine |
| 10 | 11A-11C UI overhaul | Easier once approvals, settings schema, and domain cards exist |
| 11 | Real-use cycle on Ares, then 10F-10G and 9B (shadow -> enable) | Need real data; not session-completable |
| 12 | 13A-13D multi-user | Largest; prep migration earlier |
| 13 | Phase 7 guard | When LaYa is online |

Ordering rationale (original):

- Modules before everything: every later feature consumes the registry.
- MCP before skills: it forces the hard parts (dynamic discovery, health,
  per-domain visibility, schema budget) and the first MCP server.
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

### Phase 1 - MCP modules, dynamic loading, first MCP server, trust flags

**Status:** complete (2026-11-09); revised 2026-11-15 to remove the
web-specific module kind and config group.

**Goal:** MCP servers become domain-scoped modules that reload without a
process restart, and the agent gets web search through a self-contained
SearXNG-backed MCP server.

**Scope**

- `harness/modules/mcp.py`: replace `McpTools` with a registry-backed
  loader. Keep the existing stdio JSON-RPC client and per-call process
  isolation.
- Manifest fields: `transport` (`stdio` now), `command`, `args`, `env_names`,
  `domains`, `autoapprove`, `risk_default`, `tools_allow`, `tools_deny`,
  `read_only`, `side_effects`, `startup` (`eager|lazy`), `timeout_s`.
- Import standard `mcpServers` JSON, `examples/mcp_servers.yaml`, and
  `tools.mcp_servers_file` into module rows; re-read the file on startup and
  on explicit reload so entry edits are picked up without deleting rows.
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
- Ship the first MCP server: `harness/modules/web/` (self-contained; its own
  `--config` file and neutral `WEB_*` env fallbacks). It registers like any
  other MCP module, with `tool_prefix: ""` so its tool stays `web_search`.
  - `web_search(query, categories?, max_results?)` -> bounded
    `{title, url, snippet}`.
  - Uses SearXNG's JSON API. The first deployment verified 2026-10-28 that
    the local instance's `/search?q=...&format=json` returns JSON; the
    endpoint itself is server config, not harness config.
  - Results marked `untrusted_output=True`, each with its URL as provenance.
  - `web_fetch` is deferred (SSRF + injection surface; see section 9).
- Do **not** add dynamic Python imports.

**Config**

```yaml
modules:
  default_mcp_risk: confirm
  lazy_start: true
tools:
  mcp_servers_file: examples/mcp_servers.yaml
```

The manifest file registers servers; runtime options live in each server's
own config file (for the bundled web server: `WEB_SEARXNG_URL`,
`WEB_MAX_RESULTS`, `WEB_TIMEOUT_S`, `WEB_CATEGORIES`, `WEB_SAFE_SEARCH`, or
`--config`). No `web.*` group exists in the harness config.

**Schema:** reuse v3 tables; `module_tools.side_effects/read_only` populated.

**Tests**

- Fake stdio MCP server with two tools; discovery and round trip.
- A module bound to `domain:A` is invisible in `domain:B`; calling it by
  name is denied, not executed.
- Reload changes the registered toolset without restarting the app.
- Unavailable module recorded and never blocks a turn.
- Schema budget excludes a low-priority module and records why.
- Web MCP server against a local stub HTTP server; bounded output; untrusted
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

**Status:** complete (2026-11-13).

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

**Status:** complete (2026-11-13).

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

`allow_confirm_risk` is reserved and inert in v1: the child toolset is always
low-risk-only, and a confirm attempt returns `blocked`.

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

### Phase 6 - UI rework information architecture

**Status:** complete (2026-11-13).

**Goal:** the UI explains and controls the system instead of accumulating
flat tabs.

**Scope**

- Navigation groups:
  - **Conversation**: chat, task detail drawer, more dynamic chat with status on the working task.
  - **Work**: tasks (+ sub-agent tree) each task provides detailed info on the prompt ran, the response, tool uses, etc; approvals, schedules, inbox,
    automations.
  - **Knowledge**: memories (active/archived/superseded, usage), domains,
    skills, consolidation.
  - **System**: modules (MCP/web health, bindings, reload), tools, events,
    config, backup, guard later.
- Split `/api/state` into per-panel endpoints (`/api/tasks`, `/api/memories`,
  `/api/modules`, `/api/inbox`, ...) and keep a small `/api/summary` for
  header badges. Poll only the active panel; keep the chat poll.
- All data sources (memories, context, persona, skills, etc) can be modified, viewed, and deleted via the UI.
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

**Status:** pending (LaYa is not online yet).

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

**Integration points prepared by Phase 8:** the per-call nonce envelope around
untrusted tool output (8.4), trusted-annotation handling (8.1), and the memory-write
taint rule (8.2). Phase 10F's model routing never sees untrusted text, so the guard
need not cover it.

**Commit:** `Phase 7: optional LaYa-backed injection classification on top of trust policy`

---

### Findings that motivate Phases 8-13

Verified in the code of the 2026-11-15 ZIP:

- **MCP risk is per module, not per tool.** `modules/mcp.py::_module_tools`
  applies one `risk`/`read_only`/`side_effects` to every tool of a server. That
  is why calendar reads need approval: the server also has write tools, so the
  module cannot be `autoapprove`/`read_only`.
- **Non-decision replies are rejected, not used.** While an approval is pending,
  anything that is not a clear yes/no gets "I did not apply that reply..."
  (`runtime._ask_which_approval`). A natural "make it 4pm" is a dead end, which
  is most of the "clumsy" feeling. Only skills have in-place refinement.
- **Reject re-enters the loop with a denied tool result.** The model can answer
  and then re-propose the same call, producing a second, identical "I want to
  run X" note. This is the leading suspect for the repeated message (see 8.3).
- **Taint does not cover memory writes.** `remember`/`update_memory`/
  `retract_memory` have no `side_effects` flag, so after a web result they run
  unescalated. Injected text can become durable memory. Skills are guarded;
  memory is not.
- **Model name is config-only.** `model.name` lives in `config.yaml`; changing
  it is documented as needing a restart. `ModelClient.probe()` already lists
  models, so a dropdown has its data source.

---

### Phase 8 - Approval rework and the quick-fix batch

**Status:** implemented (2026-11-16) in the working tree; staging and
live-model verification still pending. 8.0, 8.1-8.4, 8A, and 8B-1/8B-2 are
complete with deterministic coverage in `tests/test_deploy.py`,
`tests/test_taint_memory.py`, `tests/test_mcp_tool_risk.py`,
`tests/test_web_hardening.py`, `tests/test_approvals_reject.py`,
`tests/test_approvals_rework.py`, and `tests/test_approval_cards_ui.py`.

**Implementation notes.**

- 8.2 chose policy (a): a tainted memory write becomes confirm-risk and the
  approval names the untrusted origin; the row's provenance records
  `tainted: true`.
- 8.1 honours `annotations.readOnlyHint` only for a server with
  `trust_annotations: true`; `tool_risk` overrides risk only; reads stay
  `untrusted_output` so they still taint the run.
- 8.3 confirmed H1 (an identical re-proposal after a rejection created a
  second approval and a duplicate note). The fix is per-run denial memory
  keyed by `(tool, sha256(canonical arguments))`, checked before any approval
  is created and cleared by an explicit new user turn.
- 8.4 sanitises and flags results in the web server and wraps every
  untrusted tool result in a nonce envelope in the runtime.
- 8B uses a revision chain (`revised` / `superseded`, `supersedes_id`)
  rather than in-place mutation, adds structured edits, per-tool
  `summary_template`, approve-and-remember risk overrides, and shared
  approval cards in chat and the panel.

Split into two commits. 8A is small and independent; 8B changes the approval
interaction model.

#### 8.0 - Deployment safety and staging (own session, do first)

**Goal:** make every later phase safe to deploy to Ares, where real memory and
history live. Nothing here changes agent behaviour.

- **Pre-migration backup.** When startup finds a schema version lower than the
  code's, write `backups/pre-migrate-v<old>-to-v<new>-<ts>.zip` first and abort
  with a clear message if the backup fails. Never migrate unbacked-up data.
- **Version visibility.** Embed version and git sha in the image (build arg) and
  report `{version, git_sha, schema_version}` on the authenticated `/api/system`
  and in `harness status`. `/health` stays unauthenticated and minimal.
- **Staging instance.** `docker-compose.stage.yml` (or documented `-p` project
  flags): separate volume, separate `HARNESS_PORT` (not 8080, which SearXNG
  uses), separate token, same model endpoint. One writer per data directory is
  preserved. Used to try a phase, to test a migration on a restored production
  backup, and to reinstall from zero.
- **Runbook** `docs/DEPLOY.md`: build, back up, deploy to staging, run
  `scripts/container_check.py`, run live evals, promote, roll back (restore the
  backup and run the previous image tag; tag images per phase).
- **Portability guards.** `.gitattributes` forcing LF for shell scripts and the
  Dockerfile (a CRLF script authored on Windows fails in the Linux container);
  a cheap test that fails if a shell script contains `\r`.
- **Container test target.** A Docker build target (or documented command) that
  runs the deterministic suite inside the image, so "green on Windows" is
  confirmed on Linux before promotion.

**Tests:** migration writes a pre-migrate backup and aborts when the backup
fails; `/api/system` reports version and schema; the LF test; the suite passes
inside the container target.

**Exit criteria:** the runbook exists and has been followed once end to end on
staging; a version bump leaves a restorable backup.

**Commit:** `Phase 8.0: pre-migration backups, version reporting, staging, and deploy runbook`

#### 8A - Fixes (one commit)

**8.1 Per-tool risk for MCP (calendar reads without approval)**

Hypothesis: honouring the MCP-standard `annotations.readOnlyHint` per tool
removes the approval friction without weakening the trust model.

- Servers declare `annotations: {readOnlyHint: true}` on read tools
  (calendar: `list_calendars`, `list_events`, `get_event`; web: `web_search`).
- `_module_tools` reads the annotation per tool and sets `read_only=True`,
  `side_effects=False`, `risk=low` for those tools only.
- **Annotations are claims, not facts.** Honour them only when the manifest has
  `trust_annotations: true` (set for the bundled web/calendar servers by their
  templates). A third-party server cannot downgrade its own write tools.
- Manifest/UI per-tool override: `tool_risk: {name: low|confirm}` so the user
  can correct a server.
- Keep `untrusted_output=True` on reads: a calendar event body is attacker
  controllable (invites). Reads stay low-risk; the run becomes tainted; later
  writes still escalate. This preserves invariant 4.
- Test first: fake MCP server with one annotated read and one write; read runs
  without approval, write needs it; an un-trusted server's annotation is
  ignored; calendar read taints, subsequent `calendar_create_event` is confirm.

**8.2 Close the memory-write taint gap (security, do before anything else)**

- Mark `remember`, `update_memory`, and `retract_memory` as `side_effects=True`
  (they change durable cross-run state; a retract could erase real memory on
  injected instruction). Leave `update_task_notes` alone: it is per-task scratch
  text, and escalating it would produce approval spam after every web search.
- Policy experiment, pick by test: (a) tainted run -> memory write becomes
  confirm-risk with the untrusted origin named, or (b) tainted run -> memory
  write refused like skills, plus `memory.refused` event. Recommend (a) first
  because "remember this from the page I just read" is a legitimate use.
- Provenance already records run/task; add `tainted: true` to the memory row's
  provenance so the UI and consolidation can show/ignore it.
- Test first: scripted run where a web result says "remember X"; assert no
  memory row appears without an approval that names the URL.

**8.3 Repeated message on reject**

Do not fix until reproduced. Two hypotheses, one test discriminates them:

- H1: the model re-proposes the same tool call after the denial, creating a
  second approval with an identical note.
- H2: the same event emits text from two places (approval note, park note,
  model reply echoing the rejection).
- Test first: scripted model, reject via (a) the Approvals button and (b) the
  chat quick path ("no"); count `messages` rows and approvals created after the
  reject. Whichever fails is the bug.
- Likely fix for H1 regardless: a deterministic **denial memory** per run. After
  a rejection, the same `(tool, args-hash)` in that run returns `denied`
  immediately without creating an approval. The model is not trusted to obey
  "do not try again".

**8.4 Web MCP injection hardening**

Server-side, in `modules/web/server.py`, all deterministic:

1. Normalise every string: NFKC, strip control, zero-width and bidi override
   characters, strip HTML tags, collapse whitespace, then clip.
2. Only `http`/`https` result URLs; drop `javascript:`, `data:`, `file:`;
   optionally drop private-range hosts and userinfo URLs.
3. Heuristic `flags` per result (instruction-override phrases, role/tool
   spoofing like "system:" or "tool_call", "ignore previous", base64 blobs,
   long URLs with query payloads). Flags are labels only, never instructions;
   flagged snippets are replaced by a short placeholder plus the title/URL.
4. Output envelope: results come back as data with a per-call random nonce
   (`<web_results nonce="...">`) and the explicit statement that the content is
   not instructions. The nonce stops content from forging the closing tag.
5. Query hygiene: cap query length (about 200 chars). A tainted run could try to
   smuggle memory content out through the search query (exfiltration channel);
   the cap bounds it. Experiment: also log queries in the event stream.
6. Result count and per-result size already bounded; keep.

Harness-side (generic, benefits every MCP): wrap all `untrusted_output` tool
results in the same nonce delimiter before they enter the transcript
("spotlighting"). Guard (Phase 7) later plugs into the same seam.

- Test first: stub SearXNG returning a corpus of payloads (override phrases,
  hidden unicode, markdown image exfil `![](http://x/?q=...)`, fake tool-call
  JSON). Assert output is sanitised and flagged; assert no tool call proceeds
  without approval. Live scenario `i_live_hostile_web` run against both
  `qwen3.5-9b` and `minicpm5-2B`, measuring `flags_untrusted_input` (the 2B model
  already failed this criterion on hostile inbox, so expect to learn something).

#### 8B - Approval interaction rework (two sessions: 8B-1 backend and decision model; 8B-2 cards, edit form, and tray in the UI)

Goal: approving, rejecting, and *changing* an action feels like a conversation,
not a form.

Decisions to test:

1. **One decision model, four outcomes.** Approve, Reject, Reject with feedback,
   Edit. "Reject with feedback" is the general mechanism; "Edit" is its
   structured cousin.
2. **Non-decision replies become feedback.** While one approval is pending, a
   reply that is not clearly yes/no is treated as revision feedback: the
   approval closes as `revised`, the model receives
   `{status: "revision_requested", feedback: <text>}`, and re-proposes. This
   replaces the "I did not apply that reply" dead end.
   - Risk: a genuinely unrelated message gets swallowed. Experiment: use the
     existing resume-candidate scoring (keyword overlap) to tell "feedback on
     this" from "new topic"; unrelated text starts a new task and leaves the
     approval pending.
3. **Revision chain, not in-place mutation.** Skills today refine the pending
   approval in place. For general tools, test creating a new approval with
   `supersedes_id` and marking the old one `superseded`. This keeps the
   "approvals are immutable" invariant and gives an audit trail. Compare with
   in-place on complexity before choosing; if in-place wins, generalise the
   existing skill path instead.
4. **Direct user edit.** The approval card shows arguments as a form generated
   from the tool's JSON schema. The user edits fields and approves in one step;
   the harness re-validates against the schema, records the diff
   (`approval.edited`), and executes the *edited* arguments. The model sees
   what actually ran.
5. **Cards, not prose notes.** Replace "reply approve/reject, or use the
   Approvals panel" text notes with an inline chat card: summary line, key
   arguments, source/taint reason, buttons (Approve, Edit, Reject, optional
   reason box). Keyboard shortcuts. The text reply path keeps working.
6. **Human summaries.** Optional per-tool `summary_template`
   (calendar: "Create 'Dentist' Tue 16:00-17:00") so cards read naturally and
   fall back to the clipped JSON when absent.
7. **"Always allow" with limits.** Approve-and-remember writes a
   `risk_override` for that tool, but only for tools without `side_effects` in
   tainted context; a tainted run still escalates. Show and revoke these in
   the UI.
8. **Batch and expiry.** Multiple pending approvals listed in one tray with
   approve-all-safe; reminders already exist.

- Test first: a scripted multi-turn scenario per outcome; edit changes executed
  arguments exactly; superseded approvals can never execute; free-text feedback
  resumes the same run once; unrelated message does not consume the approval.

---

### Phase 9 - Provider, model selection, and model router

**Status:** 9A implemented (2026-11-16); 9B proposed.

**Implementation notes (9A).**

- Provider records are configured under `providers`, with a synthesized
  built-in `default` provider for existing single-endpoint configurations.
- Model/provider selection is explicit and has no fallback.
- Config loading validates `providers` records and the `model.allowed` list.
- The catalogue and selection API expose provider reachability, current model,
  and configured context window through `GET /api/models` and `POST /api/model`.
- Runs record `model` and `provider`; each loop pins its snapshot, while a
  resume after an explicit switch records the change with `source: resume`.
- The header picker and Settings card show provider reachability, model state,
  and context window and can apply a selection without a restart.

**9A - Change Model setting**

- `GET /api/models`: advertised models from `models_path`, intersected with an
  optional `model.allowed: []` allowlist (empty = everything advertised).
- `POST /api/model {name}`: validates against that list, writes config, applies
  without a restart (ModelClient reads `model.name` per call; verify by test).
- Record `model` and `provider` on every run (`runs.meta`) so evals, history,
  and the router can attribute behaviour.
- Introduce `providers:` as a list `[{id, base_url, api_key_env, models}]` with
  `model.provider` + `model.name` selecting one. Single provider stays the
  default so existing configs keep working. "Never silently fall back" stays:
  switching is always an explicit user or router decision, recorded.
- UI: dropdown in the header and in Settings; shows reachability and context
  window per model.
- Test first: switch model mid-session; a run in flight keeps its snapshot
  model; invalid names refused; unreachable provider reports cleanly.

**9B - Model router (shadow mode first)**

Hypothesis: a deterministic router can send trusted, short, low-risk requests
to the fast model with no loss in correctness and a clear latency win.

Rules, in order (all deterministic, no model call):

1. Quality path always for: inbox/automation origin, tainted runs, prompts that
   mention credentials/secrets, anything with a confirm-risk tool in the
   snapshot that the message plausibly invokes, multi-action instructions.
2. Fast path candidates: user-origin, trusted, short, resolves to read-only or
   memory/schedule-lookup tools.
3. **Escalate mid-run.** If a fast-path run becomes tainted, proposes a
   confirm-risk tool, or hits a malformed-output retry, re-run that turn on the
   quality model with the same transcript (OpenAI-format transcripts are
   portable). Record `router.escalated` with the reason.

Experiments, in this order. The implementation session delivers shadow mode,
the logging, and the comparison report; the measurements below run on Ares
after deployment, and their results are recorded before enabling the router:

- **Shadow mode:** log what the router *would* choose for every real request for
  a week; no behaviour change. Measure the fast-path share.
- **Swap cost:** if both models are not resident on the P100s, a model swap may
  cost more than it saves. Measure load time first; if it dominates, the router
  is only worth it with both resident.
- **Eval replay:** run the behavior + holdout suites with the router on, compare
  against single-model baselines with the existing `--aggregate` tooling.
- Only then enable it, per-domain or globally, with a UI toggle and a visible
  "answered by" label.

---

### Phase 10 - Domains as work environments (routing, entry, memory, feedback)

**Status:** proposed. Sub-phases 10A-10G, one commit each, in order. Every sub-phase
is a hypothesis plus an experiment; quoted targets are initial and expected to be
revised after each test/refine cycle.

**Owner decisions (2026-11-15):** real domains will arrive over time (Health first;
later home automation, language learning, and others), so the test corpus uses
placeholders. General holds no separate memory. Domains own instructions,
capabilities, tools, skills, and context. A visible "entering <domain>" status
line is wanted.

#### Goal

A domain is not a project folder. It is a **work environment**: when the agent
is "in" Health it has that domain's memories, skills, instructions, and
exclusive tools, and it behaves as one continuous entity that also remembers
who the user is everywhere else.

Two things must both be true:

1. **Entering is reliable.** Work reaches the right domain without the user
   naming it, and a wrong guess is cheap to correct.
2. **Being in a domain adds capability without causing amnesia.** Tools and
   skills may be exclusive; what the agent knows about the user must not be.

#### Findings this phase fixes (verified in code)

| # | Finding | Where |
| --- | --- | --- |
| F1 | Routing is lexical overlap on name+description with no stemming. The 2.0 threshold equals name (1.5) + one description word (0.5), so in practice the message must contain the domain's literal name. Simulated: "nutritional info for chicken tikka masala", "calories in a big mac", "protein in greek yogurt" all route to General; only "my health goals" routes to Health. | `store.Domains.route` |
| F2 | The model fallback is weak: the prompt lists `Name (id)` without descriptions, and the rule only says to call `use_domain` when the domain "is wrong". | `context.py` state block |
| F3 | `use_domain` recomputes the toolset but the context is built once in `start_run`. Memories, skills, and the "Active domain" line stay stale for the rest of the run. | `runtime.start_run` is the only `context.build` call |
| F4 | Memory is a hard partition: a sibling domain's memories are invisible even on an exact keyword hit. A misrouted request loses what the agent knows. | `memory._scope_visible` |
| F5 | A domain-bound tool is invisible elsewhere and nothing tells the model it exists. | `ToolRegistry.schemas_for` exclusions are recorded, not disclosed |
| F6 | No feedback loop: a wrong route leaves no labelled trace, so the router cannot be measured or tuned from real use. | - |

#### Design principles

1. **Exclusive tools and skills, soft memory.** Exclusivity protects the schema
   budget and limits what can run; applied to memory it only causes amnesia.
2. **Deterministic first, model second.** Evidence-based routing stays the
   backbone. A model tie-break is added in shadow mode and never for untrusted
   text.
3. **Entering a domain is one operation** that updates context, tools, and
   state together and records why.
4. **Every correction is data.** A user moving a task becomes a labelled eval
   case and a keyword suggestion.
5. **Domains never widen permissions.** Unchanged invariant. Risk classes,
   approvals, and taint rules ignore the domain.

#### Invariants checked

- Invariant 4 (data, not instructions): domain `instructions` are user-written;
  the model may only propose a change through an approval-gated tool, never in a
  tainted run, and never writes one directly.
- Invariant 6: every new inclusion is a recorded context source
  (`domain:hint`, `domain:briefing`, `memory:cross:<domain>`).
- Invariant 9: all new tests are deterministic; the model tie-break uses a
  scripted classifier.
- Invariant 10: UI stays zero-build.
- Taint: `use_domain` may run in a tainted run (injected text could ask for a
  domain that exposes more tools). That is acceptable only because exposure
  never changes risk: every side-effecting tool in the new domain still
  escalates. This must be tested (10C).

#### Schema (v8, additive, one migration)

```sql
ALTER TABLE domains ADD COLUMN keywords     TEXT NOT NULL DEFAULT '[]';  -- JSON list
ALTER TABLE domains ADD COLUMN examples     TEXT NOT NULL DEFAULT '[]';  -- JSON list of phrases
ALTER TABLE domains ADD COLUMN instructions TEXT NOT NULL DEFAULT '';    -- user-authored, bounded

CREATE TABLE routing_feedback (
  id TEXT PRIMARY KEY,
  task_id TEXT,
  message_text TEXT NOT NULL,
  routed_domain TEXT NOT NULL,
  correct_domain TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '{}',   -- routing breakdown at the time
  source TEXT NOT NULL,                  -- ui | tool | cli
  created_at TEXT NOT NULL
);

CREATE TABLE domain_suggestions (
  id TEXT PRIMARY KEY,
  terms TEXT NOT NULL,                   -- JSON list
  task_ids TEXT NOT NULL,                -- JSON list
  status TEXT NOT NULL CHECK(status IN ('open','created','dismissed')),
  created_at TEXT NOT NULL
);
```

Coordination note: Phase 13 (multi-user) also wants an additive prep migration
(`user_id` columns). Land them in the same version bump or sequence them; do not
interleave two migrations in one commit.

#### Config additions

```yaml
domains:
  routing_threshold: 2.0          # retune from the 10A sweep
  ambiguity_margin: 0.75
  weights:
    name: 1.5
    keyword: 1.0
    example: 0.75
    description: 0.5
    learned_cap: 2.0              # cap on memory/skill/task vocabulary evidence
  stemming: true
  hint_min_score: 0.5             # below threshold but worth hinting to the model
  briefing_chars: 1800
  instructions_max_chars: 1200
  model_routing: off              # off | shadow | on (10F)
  model_routing_timeout_s: 3.0
  status_line: true               # visible "Entering <domain>" chip
  suggestions:
    enabled: true
    min_tasks: 6
    window_days: 30
memory:
  cross_domain: boost             # off | boost | search; chosen by the 10D experiment
  cross_domain_max: 3
  core_profile_chars: 1200
```

---

#### 10A - Baseline: measure routing and recall before changing anything

**Goal:** make the problem a number.

**Scope**
- New eval suites `eval/scenarios/k_routing.yaml` and `k_memory_recall.yaml`.
  - Routing corpus: about 40 labelled messages across 4-5 domains (Health,
    Home automation, Language learning, Clothing, General; placeholders until
    the real domains exist), including the nutrition cases, composite messages
    such as "what should I wear given the weather",
    paraphrases without the domain name, ambiguous messages, and genuine
    General chatter.
  - Memory corpus: facts stored in one domain, asked about from another domain
    and from General, with paraphrases.
- Assertion kind for routing (`routing.domain`) reading the `domain.routed`
  event, if the evaluation package lacks one.
- Metrics: accuracy, General-fallback rate, **false-positive rate** (routed to
  the wrong specific domain; worse than General), and per-domain confusion.
  Memory: recall@k and context-budget share.
- Sweep script: run the router over the corpus for a grid of thresholds and
  margins and write a report.

**Tests:** corpus loads; the baseline report is deterministic.

**Experiment:** record the current numbers. Prediction: near-zero accuracy on
messages that lack the literal domain name.

**Exit criteria:** baseline report committed under `eval/results/`; no
behaviour change.

**Commit:** `Phase 10A: routing and cross-domain recall baseline suites`

---

#### 10B - Routing evidence v2 (deterministic)

**Hypothesis:** richer evidence plus stemming moves routing accuracy from near
zero to most of the corpus without adding model calls.

**Scope**
- Schema v8 domain columns (`keywords`, `examples`, `instructions`).
- `util.stem()`: a small deterministic suffix stripper (plurals, -ing, -ed,
  -al/-ional to the root so "nutritional" matches "nutrition"). Applied to both
  message and domain vocabulary. Unit-tested with a fixed table.
- `Domains.route` scoring terms: name, user keywords, example phrases,
  description (capped), plus **learned vocabulary**:
  - `MemoryManager.domain_affinity(text)`: FTS over active, non-archived
    memories scoped to each domain, plus bound skills and past task objectives,
    normalised and capped by `weights.learned_cap`. A domain that already holds
    "calories" in its memories wins without anyone listing the word.
- General stays a fallback (the existing 0.5x factor), but a clear single
  winner above threshold always beats it.
- The `domain.routed` event carries a **per-term breakdown** (which words
  matched which signal) so every decision is explainable.
- `create_domain` requires `keywords` and `examples` (the model proposes, the
  approval shows them); the UI/API edit them for existing domains. Empty lists
  remain valid for old rows.
- Retune `routing_threshold` and `ambiguity_margin` from the 10A sweep.

**Tests**
- The nutrition cases route to Health with a Health domain that has sensible
  keywords; with only name+description they route better than baseline.
- Learned vocabulary: a domain with relevant memories wins with no keywords.
- No cross-domain false positives on the General chatter set.
- Stemming table; v7 -> v8 migration opens an existing database.
- Routing never creates a domain (existing guarantee).

**Targets (initial):** routing accuracy >= 85% on the corpus, false-positive
rate <= 5%.

**Commit:** `Phase 10B: routing evidence v2 with keywords, stemming, and learned vocabulary`

---

#### 10C - Entering a domain: briefing, hints, and tool discovery

**Hypothesis:** if entering a domain delivers its context in the same step,
the model behaves as if it were "in" the environment.

**Scope**
- **Domain instructions block:** when a run is in a domain, the context builder
  adds its `instructions` (bounded by `instructions_max_chars`) as a recorded
  source. User-authored only.
- **Domain list with descriptions** in the state block (bounded), replacing
  `Name (id)`-only lines.
- **Routing hint:** when routing lands in General but a domain scored between
  `hint_min_score` and the threshold, add one line: "Possible domain: Health
  (matched: calories, protein). Call use_domain if the work belongs there."
  Recorded as `domain:hint`.
- **`use_domain` returns a briefing:** a bounded result (`briefing_chars`) with
  the domain description, instructions, its top memories for the task
  objective, active skill names and one-line descriptions, and the tools that
  just became available. The toolset recompute already exists.
  - Experiment: compare (a) briefing inside the tool result against (b)
    rewriting the system message in the transcript. Prefer (a) first because it
    works with any chat template and keeps the persisted transcript append-only.
    Choose by scripted and live scenario results.
- **Tool discovery line:** when schema selection excludes tools for domain
  reasons, add one bounded line naming them and their domain: "Tools in other
  domains: calendar (Planning). use_domain to enter." Names only, no schemas.
  This is the cheap answer to F5; a full `find_tools` meta-tool stays deferred.
- **Taint safety:** `use_domain` stays allowed in a tainted run, and a test
  proves tools exposed by a mid-run switch still escalate side effects.
- Sub-agents inherit the parent's *current* task domain; a test pins this.
- **Visible "entering" status line (owner request).** Whenever the active domain
  changes (routing at task start, `use_domain`, or a user correction) the
  runtime records a `domain.entered` event and one compact visible status
  message, for example "Entering Health - matched: calories, protein". General
  never shows one, a task that stays in its domain shows it once, and
  `domains.status_line: false` silences it. The UI renders it as a small chip,
  not a chat bubble.
- **Two candidates in the hint.** Composite requests ("what should I wear given
  the weather" touches Clothing and general weather) list up to two possible
  domains; the briefing for the entered domain may pull the second's top
  memories through the soft-memory path (10D).

**Tests**
- After `use_domain`, the next model turn sees the briefing and the new tools;
  the old domain's tools are gone; `toolset.changed` is recorded.
- A misrouted request with the hint present: the scripted model calls
  `use_domain` and then completes with the right memory visible.
- Tainted run switches domain: side-effecting tools in the new domain still
  need approval and name the untrusted source.
- Discovery line is bounded and lists names only.
- The status line appears once per domain change, never for General, and is silenced by config.

**Commit:** `Phase 10C: domain entry briefing, routing hints, and cross-domain tool discovery`

---

#### 10D - Soft memory and a core profile

**Hypothesis:** letting memory span domains with the current domain boosted
improves recall without noticeably polluting context. This is the step that
decides whether the single-entity goal is reachable.

**Scope**
- `memory.cross_domain`:
  - `off`: today's behaviour.
  - `boost`: all active, non-archived memories are eligible; scope priority is
    current domain > global > other domains. Other-domain memories enter
    context only above a lexical relevance floor (never as no-hit fillers),
    capped by `cross_domain_max`, and labelled `from: Health`.
  - `search`: context stays as today, but `search_knowledge` searches every
    domain by default and labels the origin.
- Keep the visibility rule in one place (`_scope_visible`) so Phase 13 can add
  the per-user filter there. A domain is a relevance boundary, not a security
  boundary.
- **Core profile:** pinned `global` memories form an always-on block
  (`core_profile_chars`), editable in the UI. This is the part that makes the
  agent know the user in every domain, including General.
- Memory writes: sharpen the `remember` guidance so facts about the user
  (preferences, constraints, identity) default to `global`, and domain facts to
  the domain.
- Record `memory:cross:<domain>` sources with a reason.
- **General has no memory silo of its own (owner decision).** General is the
  unscoped interface: rows scoped to `domain:general` are treated as `global` at
  read time (no data rewrite), and new memories saved from General default to
  `global`. Domain-specific memories still surface in General through the soft
  path when relevant, and global tools (web, weather) stay available inside
  domains. That is what makes "what should I wear based on the weather" work from
  General (Clothing memories surface) and from Clothing (the global weather tool
  is present).

**Experiment:** run `k_memory_recall` in `off`, `search`, and `boost`; compare
recall@k and context-budget share; choose the default from the numbers.

**Tests**
- Cross-domain fact recalled from General in `boost`, labelled with its origin.
- No other-domain filler without a lexical hit.
- Archived and retracted rows never cross over.
- Core profile always present, bounded, and recorded as a source.
- Composite scenario: Clothing memories and a global weather tool combine for one answer, from General and from Clothing.
- `domain:general` rows are readable as global; new memories from General are saved as global.
- Consolidation unaffected (ignores archived rows).

**Commit:** `Phase 10D: soft cross-domain memory and an always-on core profile`

---

#### 10E - Routing visibility and the correction loop (two sessions: 10E-1 backend, API, CLI, export; 10E-2 UI)

**Goal:** wrong routes are visible and fixable in one click, and every fix feeds
the eval suite.

**Scope**
- Task header: domain chip with a "why" popover (candidates, scores, matched
  terms, hint shown, model tie-break result if any).
- `POST /api/tasks/{id}/domain {domain_id, reason}`: moves the task, records
  `domain.corrected`, writes a `routing_feedback` row with the evidence. A
  correct move also offers up to three **suggested keywords** (message terms
  not already in the domain); the user ticks which to add. Nothing is added
  automatically.
- `POST /api/domains/route-preview {text}`: returns scores and breakdown with
  no side effects, for a "test routing" box.
- Domains panel as an environment card: description, keywords, examples,
  instructions editor, counts (memories, skills, bound modules, tasks, last
  used), routing stats (routed here / corrected away / corrected in), archive.
- CLI/API: `domains --export-feedback` writes `k_routing`-format scenarios from
  `routing_feedback`, closing the test/refine loop.
- Core profile editor (pinned global memories) in the Knowledge group.
- **Per-domain instructions (owner decision: yes).** The environment card edits
  `instructions`, alongside the domain's memories, skills, module bindings, and
  tools. The agent may *propose* an instruction change through a confirm-risk
  `edit_domain` tool (approval shows the full bounded text; the same pattern as
  skills) and is refused in a tainted run. Only an approved change is written.

**Tests**
- Correction moves the task and context for the next run, records feedback, and
  never changes permissions.
- Export produces scenarios the eval loader accepts.
- `route-preview` has no side effects (no events, no tasks).
- API contract tests per new endpoint; panel smoke test.

**Commit:** `Phase 10E: routing visibility, corrections, and domain environment cards`

---

#### 10F - Model-assisted tie-break (shadow first)

**Hypothesis:** when deterministic evidence is weak, one tiny constrained call
picks better than falling back to General, at acceptable latency.

**Scope**
- `domains.model_routing: off | shadow | on`.
- Trigger: best deterministic score below threshold or an ambiguous margin, and
  at least one non-General active domain.
- One call, same pattern as the quick-confirm classifier: names, descriptions,
  and keywords of active domains plus the message; output is one domain id or
  `general`; small `max_tokens`, no thinking, `model_routing_timeout_s`.
  Invalid output, timeout, or an unknown id means General (today's behaviour).
- Uses the fast model when the Phase 9B router exists, otherwise the main model.
- **Trusted input only.** Inbox and automation text never decides the domain
  (attacker-controlled text would pick which tools appear); those use the
  automation's configured domain or deterministic routing.
- The output is a label, never injected as instructions.
- `shadow`: record the model's choice on `domain.routed` without acting. Compare
  with corrections and the 10A corpus.

**Tests:** scripted classifier; invalid/timeout/unknown id -> General; shadow
never changes the route; untrusted origin never calls the classifier.

**Experiment (post-deploy, on Ares):** run shadow for about a week of real use, then replay on the
corpus; enable only if it beats deterministic routing on false-positive rate as
well as accuracy.

**Commit:** `Phase 10F: optional model tie-break for weak domain evidence`

---

#### 10G - Domain suggestions from usage

**Goal:** the creation threshold stays high, but the user is nudged when General
is clearly hiding a domain.

**Scope**
- Heartbeat pass (deterministic): over General tasks in `window_days`, find term
  sets shared by at least `min_tasks` tasks and not covered by any domain.
- Write a `domain_suggestions` row and one visible note: proposed name, keywords,
  and the tasks involved, with a create button that opens the domain form
  pre-filled. **Never auto-creates.** Dismissals are remembered so the same
  cluster is not re-suggested.
- Optional: suggest archiving a domain unused for 60 days.

**Tests:** cluster detection on a fixed corpus; dismissed clusters stay
dismissed; no suggestion when a domain already covers the terms; bounded per tick.

**Commit:** `Phase 10G: usage-based domain suggestions`

---

#### Test/refine cycle after implementation

(Post-deploy work on Ares, not part of any implementation session. It needs at
least two domain-bound capability-track servers in daily use first.)

After 10G, repeat on real use:

1. Use the harness normally for a week with 3-4 domains and real MCPs.
2. Correct every wrong route in the UI (feeds `routing_feedback`).
3. Weekly: export feedback to scenarios, rerun `k_routing` and
   `k_memory_recall`, and read the confusion table.
4. Adjust one variable at a time: weights, threshold, `cross_domain` mode,
   keywords, model tie-break. Record the before/after in the decision log.
5. Promote any hard-won failure case to the permanent suite.

Initial targets (revise freely): routing accuracy >= 85%, false-positive <= 5%,
General-fallback only on genuinely domainless messages, cross-domain recall@5 no
worse than in-domain recall, and added context cost from cross-domain memory
under 10% of the memory budget.

---

### Phase 11 - UI overhaul (settings, discoverability, approvals)

**Status:** proposed. Three sessions: 11A typed settings schema and generated forms;
11B navigation regroup plus approval tray and domain cards integration; 11C `app.js`
ES-module split and per-panel smoke tests (screenshots via Playwright).

Observed from the structure: one 2,000-line `app.js`, config edited as whole
raw groups, many features reachable only by knowing they exist.

- **Typed settings.** Add schema metadata next to config defaults (type, label,
  help, range, restart-required). Generate forms from it, with search and
  "modified from default" markers. Replace raw group editing.
- **Fewer, clearer destinations:** Chat, Work (tasks + approvals + inbox),
  Knowledge, Connections (MCP/skills), Settings. Demote read-only diagnostics
  (events, tools) to a System/Advanced area.
- **Approvals inline in chat** (8B) and a pending-count badge everywhere.
- **First-run checklist / health page** ("model reachable, web module
  configured, calendar connected, backup taken") that doubles as the doctor
  output from Phase 12.
- **Zero-build, but modular:** split `app.js` into native ES modules per panel;
  no bundler, still invariant 10.
- **Process:** screenshot every panel with Playwright, list hidden or
  underworked features, then design a pass with the frontend-design skill
  before touching code. Test first: a smoke test per panel that loads without
  console errors, and the existing `test_ui_panels` contract tests.

---

### Phase 12 - Install, reset, doctor, and MCP setup UI

**Status:** 12B implemented (2026-11-16); 12A remains proposed.

**12A - Docker-first install, reset, and doctor**
- The deployed form stays a container on Ares; the goal is that setup from zero
  needs no hand-edited files and no compose file. Document a single
  `docker run` (image, volume, port, env) as the compose-free path; Compose stays
  optional.
- First-run web wizard when no config exists: set the token, enter the model
  endpoint with a live probe and model pick, timezone, optional modules.
  `harness init --non-interactive` reads the same values from `HARNESS_*`
  environment variables so the container entrypoint can bootstrap itself.
- `harness reset [--keep-config] --yes`: takes a backup first, then wipes. In
  Docker: `docker compose exec harness python -m harness reset`; on staging,
  deleting the stage volume is the reset.
- `harness doctor` (also runs inside the container): Python version, data volume
  writable by the container user (UID 10001), endpoint reachable *from inside the
  container*, port in use (8080 collision with SearXNG), writer lock, module
  health, disk.
- Optional, low priority: native installers (pipx entry point, systemd unit,
  Windows task). Windows development already works with `pip install -e`.
- Test first: scripted smoke (fresh data dir -> init -> start -> `/health` ->
  one message -> reset) in the Windows session; the same smoke runs in the Linux
  container target at promotion.

**12B - MCP management UI**

**Status:** implemented (2026-11-16). The authenticated backend supports
server setup, connection previews, per-tool risk, private env sidecars, and
bundled web/calendar schema templates, and the Modules panel wires them into
add/edit/remove forms, a connection preview, a per-tool risk control, private
environment inputs, and template forms.

- Add/edit/remove server forms in Modules: command, args, env names, domains.
- "Test connection": launch, `initialize` + `tools/list`, show tools,
  annotations, and the stderr tail, before saving.
- Per-tool risk table (low/confirm, read-only, side effects) writing
  `tool_risk` from 8.1.
- Secret inputs write to the private sidecar; the UI never displays values
  (invariant 11).
- Bundled templates for web and calendar render a form from a
  `config.schema.json` each ships, writing `modules/mcp/<id>.json`.
- **Decision needed:** a UI that can create stdio servers can launch arbitrary
  commands for anyone holding the token. Editing `mcp.json` has the same effect
  today, but a UI widens the exposure. Test two mitigations: command allowlist
  (`python`, `node`, `npx`, `uvx`, bundled servers) with an explicit override,
  and a re-confirm step with an `module.created` event.

---

### Phase 13 - Multi-user and shared spaces

**Status:** proposed; do last. Four sessions: 13A prep migration (`user_id` columns, no behaviour
change); 13B identity, per-user tokens, roles; 13C scopes and SQL-layer isolation with
the leak scenario; 13D shared spaces and approval ownership.

Do this last; it touches every table. Prepare cheaply earlier.

- **Prep (can ride along with Phase 10):** additive migration adding
  `user_id TEXT NOT NULL DEFAULT 'owner'` to messages, tasks, memories,
  domains, schedules, approvals, inbox_events. No behaviour change.
- **Identity:** `users` and hashed per-user API tokens; roles
  (owner, member). Channel identities (e.g. a messaging pusher) map to users.
- **Scopes:** extend memory/domain scope from `global|domain` to
  `private|space`. A *space* (household) has members; modules and memories can
  be bound to a space. Home automation = a space-bound MCP module.
- **Isolation at the SQL layer,** not in prompts: every read in context
  building, `search_knowledge`, and history search filters by the actor's
  visible users/spaces.
- **Approvals belong to the actor.** A roommate's task cannot be approved by
  you, and a shared-space action may need the module owner's approval.
- **Trust question to test:** content authored by another user inside a shared
  space is trusted as a person but is third-party text for the agent; test
  whether it should taint for side effects.
- **Concurrency:** the single `RLock` serialises all users on one model
  (about 27 tok/s on the quality path). Measure queue latency with two active
  users; the router (9B) is the first mitigation, parallel runs stay deferred.
- Test first: an adversarial leak scenario in the eval suite (user B asks for
  user A's private memory by paraphrase); a shared-space round trip; approval
  ownership.

---

### Capability track (parallel to Phases 8-13)

**Why:** domains only prove themselves with real tools, and today the agent has
calendar and web search. Each item is one self-contained stdio MCP server
following the web/calendar pattern: its own config file, bounded output, read
tools annotated (8.1), read-only first, output untrusted. One session per server,
each with an example manifest and tests against a local stub. After 8.1 and 12B
no harness core change should be needed.

**Candidates (owner picks and orders; examples, not commitments):**

1. Home automation (e.g. Home Assistant): entity state reads, service calls as
   confirm-risk; bound to a Home domain; also the shared-space case for Phase 13.
2. Nutrition lookup (a food-composition database API), bound to Health: the exact
   scenario that exposed the routing gap, and it exercises skills and delegation.
3. Weather (global, read-only): the Clothing composite example.
4. Language learning (dictionary and spaced-repetition store), bound to a
   Language domain: a memory- and skill-heavy domain.
5. Notes/tasks, and mail read-only through an inbox pusher.

**Gate:** the Phase 10 real-use cycle, 10F, 10G, and the 9B router measurements
start only after at least two domain-bound servers have a week of daily use.

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
approval.edited / approval.revised / approval.superseded
memory.refused / module.created / router.escalated
model.changed
domain.entered / domain.corrected / domain.suggested
```

### 7.3 Tests and fakes

- Fake MCP stdio server in `tests/` for discovery, reload, failure.
- Local HTTP stub for SearXNG; the web MCP server must not require the LAN.
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

### 7.6 Prompting each phase

One Claude Code session per phase or sub-phase, run to completion. A good
session prompt is short and points at the plan instead of pasting it:

> Implement <phase/sub-phase> from LONGTERMPLAN.md. Read section 2 (invariants),
> sections 7.1-7.4, and only that phase; read CODEMAP.md for the code layout.
> Treat its "test first" items as the acceptance tests and write them first.
> Do not start any other phase. Update README.md, CODEMAP.md, this plan's status
> and decision log, and make one commit with the stated message. If an experiment
> contradicts the stated hypothesis, stop and report instead of choosing silently.

After a phase lands: run the evals, record the numbers, and update the decision
log before starting the next one.

### 7.7 Workflow: sessions, Windows development, Ares deployment

- **Where things run.** Development and Claude Code sessions run on the Windows
  machine. The deployed instance runs in a container on Ares (192.168.50.40), with
  state in the `harness-data` volume. A session cannot touch Ares or the real
  data, so a session's definition of done must not depend on them.
- **Session deliverables:** code, deterministic tests, docs, and one commit. Done
  means the deterministic suite is green on Windows, new scenarios pass in
  deterministic eval mode, and docs are updated.
- **Post-deploy validation** is separate work, recorded after deployment:
  live evals against the Ares model (reachable from Windows), shadow-mode
  measurements, router swap cost, and the real-use test/refine cycles. A later
  phase that depends on such a result waits for a one-paragraph note in the
  decision log.
- **Promotion checklist** (from the 8.0 runbook): back up production, build the
  image, run the deterministic suite inside the container target, start the
  staging instance, run `scripts/container_check.py` and live evals, then deploy
  to production and keep the previous image tag for rollback.
- **Windows-to-Linux portability rules:** LF endings enforced by
  `.gitattributes`; `pathlib` and no hard-coded separators; manifests use
  `${data_dir}` and the container's `python`, with machine-local overrides kept
  out of git; a test that needs signals or POSIX file modes is marked and runs in
  the container target; new files must be writable by the container user.
- **Migrations on real data** always follow an automatic pre-migration backup
  (8.0); a failed migration restores from it rather than patching in place.

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
| Port 8080 collision with SearXNG | Web search unreachable or harness unreachable | Document `HARNESS_PORT`; configure the web server's own URL | 1 |
| UI rewrite churn | Lost time | Static split in Phase 0; panels incremental; no framework | 0, 6 |
| Memory write after untrusted content becomes durable | Persistent injection | `remember`/`update_memory` count as side effects under taint (8.2) | 8 |
| Annotated MCP read tool is actually a write | Unapproved side effect | Honour `readOnlyHint` only for trusted modules; per-tool override | 8 |
| Richer domain routing causes wrong-domain false positives | Wrong tools and skills | Separate false-positive metric; margin rule; correction loop | 10 |
| Soft memory pollutes context | Worse answers | Lexical floor, per-run cap, labelled origin, experiment-chosen default | 10 |
| Untrusted text steers the domain | Tool exposure | No model routing for untrusted input; taint escalation holds after a switch | 10 |
| UI can add stdio MCP servers | Command execution by token holder | Command allowlist or re-confirm; `module.created` event | 12 |
| Router sends adversarial input to the 2B model | Missed injection | Quality path for tainted/inbox input; escalate mid-run; shadow first | 9 |
| Multi-user leaks private memory | Privacy | SQL-layer visibility filter; adversarial leak scenario | 13 |
| Tests green on Windows but fail in the Linux container | Broken deploy | Container test target; LF enforcement; portability rules (7.7) | 8.0 |
| Schema migration damages production memory | Lost history | Automatic pre-migration backup; staging rehearsal on a restored backup; image tags for rollback | 8.0, all |
| A session's success depends on real usage | Session can never finish | Split in-session deliverables from post-deploy validation (7.7) | all |
| Domains tested without real tools | Misleading conclusions | Capability track gate before the real-use cycle | 10 |

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
| 2026-11-13 | Per-run access dedupe is a durable `memory_access(memory_id, run_id)` row, not in-memory state | Restarts and repeated context builds must never inflate usage counts |
| 2026-11-13 | `include_archived` restores every row it returns, with `changed_by=retrieval` | Explicit retrieval is the only un-archive path, and it must be auditable |
| 2026-11-13 | The archive index is created after the additive v4 columns, not in the base schema | A v3 database must upgrade without the base script touching a missing column |
| 2026-11-13 | A child is a normal task/run with parent ids, not a separate execution model | Reuses exact approvals, transcripts, limits, and crash recovery with one `RLock` |
| 2026-11-13 | A child gets the goal, the parent-supplied brief, and domain memories, never the parent transcript | Isolation keeps the child bounded and prevents conversation-wide leakage |
| 2026-11-13 | Child confirm-risk tools return `blocked` and end the child run | A sub-agent must never create an approval or leave the parent waiting |
| 2026-11-13 | Same normalized goal reuses the existing child by `parent_task_id:goal_hash` | Prevents duplicate children and duplicate side effects |
| 2026-11-13 | Child budgets are recorded on `tasks.meta` and applied by `_loop` | A config change mid-delegation cannot silently widen a running child |
| 2026-11-13 | Cancelling a parent cancels its open children | A cancelled tree must not leave resumable child work behind |
| 2026-11-13 | Add `automation_fires` as a third v6 table | Durable fire history is the visible audit trail and the hourly loop breaker; in-memory counters would reset on restart |
| 2026-11-13 | Content dedupe applies only when no enabled automation matches, and only against events already out of the `new` queue | Rule-driven recurring mail must reach its cooldown/cap, while two pending copies must not dedupe each other |
| 2026-11-13 | Quiet hours leave events `new` instead of consuming or ignoring them | Postponing triage must not lose an external wake-up |
| 2026-11-13 | Hitting `max_per_hour` disables the rule and writes one visible note | The breaker must survive restarts and stop an email-storm loop without spamming chat |
| 2026-11-13 | Schedule bounds are enforced in `Scheduler.create_for_task`, not the tool handler | Direct API callers must not bypass the per-task cap or chain-depth limit |
| 2026-11-13 | Schedule chains carry `schedule_depth` in schedule/task meta and the parent task id in schedule meta | A scheduled task can be bounded without adding mutable state to the schedule row |
| 2026-11-13 | Inbox triage shares the scheduler run budget and runs before due schedules | Urgent external work wins the tick while background model work stays bounded |
| 2026-11-13 | The agent may refine only skills it created (`origin=agent`, `created_by=model|builder`) | Self-built procedures can improve without letting the model rewrite imported or user-authored instructions |
| 2026-11-13 | User UI/API revisions keep the original ownership markers | An agent skill edited by the user remains agent-owned, while an imported or user-created skill stays read-only to the agent |
| 2026-11-13 | A same-run skill write may refine the already-pending approval in place; another run may not | Lets the agent fine-tune a proposal before the user decides without stacking approvals or widening authorization |
| 2026-11-13 | Refining an approval updates its arguments and the awaiting execution row, keeping the original tool and id | The recorded audit matches the action that will actually execute, and one decision still covers exactly one request |
| 2026-11-15 | MCP tool annotations are honoured only for modules with `trust_annotations` | A server must not downgrade its own write tools |
| 2026-11-15 | Memory writes count as side effects under taint | Otherwise injected text becomes durable memory |
| 2026-11-15 | Non-decision replies to a pending approval become revision feedback | The "did not apply that reply" dead end is the main approval friction |
| 2026-11-15 | Router starts in shadow mode | Swap cost and escalation accuracy are unmeasured |
| 2026-11-15 | Add `user_id` columns before building multi-user | Avoids a rewrite later; zero behaviour change |
| 2026-11-15 | Tools and skills stay domain-exclusive; memory becomes soft | Exclusivity limits capability, not knowledge; amnesia breaks the single-entity goal |
| 2026-11-15 | Routing evidence adds keywords, stemming, and learned vocabulary before any model call | Deterministic, explainable, measurable |
| 2026-11-15 | `use_domain` returns a briefing; the system message is not rewritten (pending experiment) | Keeps the transcript append-only and template-agnostic |
| 2026-11-15 | Model domain tie-break starts in shadow and never sees untrusted text | Domain choice controls which tools appear |
| 2026-11-15 | Routing corrections create labelled eval cases | Real usage is the only trustworthy corpus |
| 2026-11-15 | Domain `instructions` are user-written; the agent may only propose via approval, never in a tainted run | Preserves "model proposes, harness decides" |
| 2026-11-15 | General has no memory silo; `domain:general` reads as global | General is the unscoped interface (owner decision) |
| 2026-11-15 | Show a visible "Entering <domain>" status line on domain changes | Owner request; makes routing legible |
| 2026-11-15 | One Claude Code session per phase or sub-phase; sessions deliver code, tests, and docs only | The session cannot reach Ares; real-use validation is separate |
| 2026-11-15 | Deployment safety (pre-migration backup, staging, runbook) is the first session | Every later phase migrates real data on Ares |
| 2026-11-15 | Version and git sha are reported on the authenticated API, not `/health` | Keep the unauthenticated endpoint minimal |
| 2026-11-15 | Add a capability track and gate the real-use domain cycle on it | Domains need real tools to be judged |
| 2026-11-15 | `update_task_notes` is not treated as a side effect under taint | Per-task scratch; escalation would cause approval spam |
| 2026-11-15 | 12A is Docker-first; native installers are optional | The deployed form is a container on Ares |
| 2026-11-16 | Synthesize the built-in `default` provider from `model.*`; additional providers are opt-in through `model.provider` | Existing single-endpoint configs keep working while multi-provider selection remains explicit |
| 2026-11-16 | Pin a run's model/provider snapshot for the whole loop invocation; a resume after a switch records `model.changed` with `source: resume` | A run in flight stays reproducible while a later resumption follows the newly selected model |
| 2026-11-16 | An unreachable provider may accept only a name named by a configured allowlist | Reachability cannot be verified, so the static allowlist is the only bounded authorization |
| 2026-11-16 | Use the dedicated `POST /api/model` for switching rather than making the `model` config group editable | Selection needs validation, attribution, and an explicit no-fallback contract |
| 2026-11-16 | Phase 12B uses a command allowlist plus explicit confirmation, with schema-driven bundled templates | A token holder can otherwise launch arbitrary commands; confirmation and the allowlist bound that exposure while templates keep common servers easy to install |

---

## 11. Definition of done

The plan is complete when:

1. A user can install a module (skill, MCP, web), bind it to a domain, and
   see its health and schema budget in the UI without editing code.
2. Web search works, and a web result cannot cause a side effect without an
   approval that names the source.
3. The agent can propose a skill from a successful task, refine its own
   skills, and the user can review, activate, disable, and roll back them.
4. Memory usage determines retention; archived memory can be found again.
5. An external app can POST to `/api/inbox` and get exactly one durable task,
   with loop protection and no unapproved side effect.
6. A complex task can delegate bounded sub-work and recover honestly from a
   crash.
7. Launching the guard is a config change that adds friction without
   changing the trust policy.
8. The full deterministic suite passes, the ten acceptance scenarios still
   pass, and each phase has its own commit.
9. A pending action can be approved, rejected with feedback, or edited without
   restarting the request, and a rejected call cannot loop back.
10. Domain routing is measured on a labelled corpus, corrections feed it, and
    entering a domain is visible and brings its memories, skills, and tools.
11. The harness can be installed, reset, and extended with MCP servers without
    editing files by hand.
12. Every phase is rehearsed on a staging instance from a restored backup before it
    reaches production, and a failed deploy can be rolled back.

