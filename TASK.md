> **Temporary file — audit only.** This run is an architectural audit of the
> uncommitted v2 refactor, not the implementation task described below. The text
> below is the refactor specification supplied by the user, preserved verbatim as
> a reference for the audit. No implementation changes were made in this run, and
> this file is intentionally left uncommitted; it is safe to delete.

---

# TASK: Refactor the Personal Assistant Harness into a Small, Complete, Two-Week-Ready System

## Mandatory workflow — follow this in order

1. **Read `README.md` and `CODEMAP.md` first.** Understand the documented purpose, existing architecture, repository structure, setup instructions, and known limitations before making changes. If either file is missing, record that in `TASK.md` and inspect the repository to establish the missing context.
2. **Create `TASK.md` at the repository root before modifying any existing files.** Use it as the live execution plan. Record the objectives, initial repository findings, implementation phases, important decisions, acceptance criteria, and outstanding issues. Update it throughout the task as findings change and work is completed.
3. Inspect the existing implementation, tests, evaluations, configuration, documentation, and deployment workflow before deciding what to retain, adapt, replace, or remove.
4. Implement the refactor, run the relevant tests and acceptance scenarios, update documentation, and verify the resulting repository.
5. **Delete `TASK.md` only at the very end**, after implementation, testing, documentation, and final verification are complete. Do not delete it early or leave it in the final repository. If work cannot be completed, do not pretend it is complete: report the blocker and remaining work rather than deleting the task tracker to imply success.

Do not stop after producing an audit or design proposal. Proceed through implementation and verification. Do not ask for confirmation for routine implementation decisions that can be resolved from the requirements and repository. If a genuinely blocking ambiguity or external dependency prevents progress, document it and continue with independent work.

---

## 1. Mission

Refactor the existing personal assistant harness into the **smallest complete implementation of the intended system**, capable of operating reliably for a two-week evaluation without needing architectural changes.

This is not a throwaway MVP that will need to be rewritten immediately afterward. It must include the core capabilities required for sustained use: persistent state, resumable work, memory, context construction, tools, approvals, scheduled tasks, heartbeat activity, recovery, and usable interfaces.

The harness should behave like a persistent personal assistant that can reason, plan, use tools, observe results, correct mistakes, manage long-running work, and initiate bounded work without a fresh user message.

The model is the reasoning engine. The harness is responsible for execution, persistence, validation, context assembly, scheduling, permissions, recovery, and observability.

### Primary objective

Build a stable harness that can remain architecturally fixed during a two-week test while the assistant improves its effectiveness through accumulated memories, domain knowledge, skills, procedures, MCP tools, and configuration.

The evaluation should help distinguish failures caused by:
- Model limitations.
- Missing or poorly retrieved context.
- Inadequate memories or procedures.
- Tool or MCP limitations.
- Harness bugs or architectural deficiencies.

Do not respond to every weakness by adding another subsystem or rewriting the architecture.

## 2. Operating principles

Treat these as binding design constraints:

- Prefer straightforward, explicit Python code over elaborate abstractions.
- Reuse validated existing work where appropriate, but do not preserve bad architecture merely because it already exists.
- Use one model, one agent runtime, one persistent interaction history, and one authoritative state store.
- Do not introduce microservices, distributed queues, a separate vector database, multiple LLMs, or other infrastructure without a demonstrated requirement.
- Avoid speculative frameworks and empty extension points. Add seams only where a foreseeable change genuinely warrants them.
- Do not make the agent dependent on manual project switching, manual conversation selection, or the user remembering what was discussed previously.
- Favor explicit state transitions and recoverable execution over clever prompting.
- Treat scheduled work, heartbeat activity, memory, and task resumption as core functionality rather than optional future enhancements.
- Keep the user-facing experience simple while making the underlying system inspectable.
- Do not introduce user-specific integrations or tools into the default installation. External capabilities should be configured through MCP servers and explicitly installed or configured skills.
- Do not introduce shell or arbitrary command execution as a tool.
- Do not silently modify the architecture during the two-week evaluation to compensate for ordinary model weaknesses.

When there is a choice between a complex, theoretically extensible design and a simpler design that satisfies the actual requirements, choose the simpler design.

---

## 3. Phase one: repository audit and preservation plan

After reading `README.md` and `CODEMAP.md` and creating `TASK.md`, inventory the repository.

Inspect, as applicable:
- Application entry points and runtime loops.
- Existing prompts and model integrations.
- Tool and MCP integrations.
- Domain, session, task, and history implementations.
- Memory and context-retrieval code.
- Scheduler and heartbeat functionality.
- Approval and permission handling.
- Web UI and terminal interface.
- Configuration and deployment files.
- Persistence and recovery mechanisms.
- Tests, regression suites, evaluation datasets, fixtures, and mock integrations.
- Documentation and operational instructions.

Build a concise map of the current architecture. Identify the main sources of unnecessary complexity, duplicated behavior, conflicting sources of truth, incomplete functionality, and fragile coupling.

Classify existing components and tests into three groups:

1. **Retain:** Useful, correct behavior or evaluations that remain compatible with the target architecture.
2. **Adapt:** Valuable functionality that needs changes to fit the new design.
3. **Replace or remove:** Redundant, obsolete, misleading, or structurally incompatible functionality.

Preserve the useful knowledge contained in existing tests and evaluations even when their implementation must be replaced. Do not delete a test merely because it currently fails or is inconvenient.

Before removing existing functionality, establish what behavior it provides and whether that behavior is required by the target system.

Use the audit to determine the smallest coherent replacement. Do not maintain two competing agent runtimes indefinitely. Where a temporary migration path is necessary, make it short-lived and remove it once the replacement is verified.

Update `TASK.md` with findings and the implementation plan before beginning the main refactor.

---

## 4. Target architecture

Build one Python application, deployed in one Docker container, with a single shared runtime and state store.

The intended logical structure is:

- **Interfaces:** Web UI and terminal CLI.
- **Agent runtime:** The single reusable execution loop.
- **Context manager:** Builds and retrieves the context needed for each model turn.
- **Domain and task management:** Organizes persistent work and resumes unfinished tasks.
- **Memory manager:** Captures, retrieves, and consolidates memories.
- **Tool execution:** Discovers configured capabilities, validates calls, checks permissions, executes tools, and records results.
- **Scheduler and heartbeat:** Initiates normal agent work at the appropriate time.
- **Persistence:** SQLite authoritative state plus readable, versioned artifacts where appropriate.
- **Observability:** Exposes events, task state, tool activity, context sources, approvals, and failures.

These are logical responsibilities, not a mandate to create a Python package or service for every bullet. Group closely related code when that keeps the implementation smaller and easier to maintain.

All interfaces and triggers must use the same underlying runtime. The web interface and terminal must not have separate implementations of agent reasoning, tool execution, or state management.

### One model endpoint

Use a single configurable OpenAI-compatible model endpoint:

`http://192.168.50.40:8181/`

This is the current endpoint for the local Qwen3.5 9B model. It is the only intended model backend for this iteration.

Requirements:
- Centralize the endpoint configuration.
- Use the endpoint's actual API conventions, including the correct chat-completion path.
- Verify network connectivity and API behavior before relying on it.
- Test ordinary responses and the structured/tool-call behavior required by the runtime.
- Handle timeouts, malformed responses, connection failures, and model unavailability explicitly.
- Never silently switch to another model or a hosted provider.
- Use deterministic mocked model responses for most automated tests so tests do not depend on network or model availability.
- Keep real endpoint tests identifiable as integration tests.

Do not assume the endpoint is reachable or its tool-call format works until verified. If connectivity cannot be established, continue implementation using mocks, record the exact blocker, and provide a clear integration test that can be rerun when the endpoint is available.

Do not introduce separate routing, embedding, summarization, or approval-classification models. A specialized approval-classification call may use the same model with a small, constrained context.

---

## 5. Unified interaction history and agent runtime

All user inputs and agent-initiated work must enter the same underlying interaction system:

- Web UI messages.
- Terminal messages.
- Scheduled prompts.
- Heartbeat-initiated work.
- Future input adapters.

There must not be a separate agent loop or independent conversation history for each input source.

Maintain one continuous, durable event/history stream. Events may record their source, timestamp, associated domain, task, and correlation identifiers. This metadata must not fragment the interaction into unrelated histories.

Not every internal event needs to become a visible chat bubble. Heartbeat checks and maintenance events should remain auditable without flooding the ordinary conversation UI.

### Shared execution loop

Implement one reusable, bounded agent loop:

1. Understand the current request or initiated objective.
2. Retrieve or request relevant context.
3. Determine the next action.
4. Produce a response or propose a tool call.
5. Validate and execute the action through the harness.
6. Observe and record the result.
7. Decide whether to continue, wait, retry, or finish.
8. Persist enough state to resume safely when necessary.

The model may decide its next step, ask for additional context, choose tools, and revise its plan based on observations. It must not bypass the harness to execute actions.

The loop must support:
- Multiple sequential tool calls.
- Observation-driven corrections.
- Explicit completion.
- Waiting for user input or approval.
- Recoverable failures.
- Resumption after process or container restart.
- Configurable limits on iterations, elapsed time, tool calls, and other relevant resources.

Do not assume that the model will always produce valid tool calls, recognize completion, or recover from a failure by itself.

Persist execution state and tool outcomes before advancing whenever necessary to support reliable recovery.

Use an explicit state machine for important task and approval transitions rather than relying solely on prompt instructions.

---

## 6. Domains, tasks, and automatic resumption

Use **domains** rather than conventional user-managed projects as the primary organizational abstraction.

A domain represents a persistent area of work with its own relevant:
- Knowledge and readable artifacts.
- Memories.
- Tasks and unfinished work.
- History references.
- Domain-specific skills and configured tools.
- Procedures and decisions.

Global information remains available across domains.

### Automatic domain routing

The user should not have to select a domain for each message.

The agent should normally determine the relevant domain using the current message, existing task state, history, and retrieved context. Ask for clarification only when ambiguity would materially affect the correctness, safety, or usefulness of the result.

Domain creation must have a high threshold. A new topic or isolated question does not automatically justify a new domain.

When a message plausibly continues an existing task, prefer resuming that task over creating a new one. Create a new domain only when there is evidence of a durable, distinct area of work that warrants its own knowledge and lifecycle.

Record domain-routing decisions and their supporting context so they can be inspected and evaluated.

### Task-oriented resumable sessions

Users should not have to manage a collection of unrelated chats to continue work.

Maintain explicit task/session records associated with domains. A task should contain enough structured state to identify:
- Its objective.
- Its domain.
- Its status.
- Relevant history and artifacts.
- Important decisions and completed steps.
- Outstanding work and blockers.
- Any pending tool action or approval.
- Its last meaningful activity and resumption information.

Automatically resume the most relevant unfinished task when appropriate.

Do not blindly resume every unfinished task. Select based on relevance to the current input and task state, and ask when competing candidates are materially ambiguous.

The user-facing interface may display the active domain and current task, but this is a visibility and control feature—not a requirement for manual switching.

---

## 7. Context construction and retrieval

Context construction is a first-class subsystem, not an incidental concatenation of recent messages.

Build the initial context from relevant sources such as:
- Global persona and operating principles.
- User identity and applicable safety constraints.
- The current request or trigger.
- Relevant domain information.
- The active or resumed task.
- Relevant recent history and prior decisions.
- Relevant global and domain memories.
- Applicable procedures and skills.
- Available tools and their descriptions.
- Pending approvals or other state required to continue work.

The precise composition should be driven by relevance and the current task, not by a fixed requirement to include every category in every turn.

### Collaborative retrieval

The harness performs initial retrieval using structured state, task relationships, domain boundaries, keyword search, and relevance rules. The model may request additional context when it identifies a missing fact or reference.

Support an explicit context-retrieval action that returns bounded, relevant information and its provenance. Do not give the model unrestricted access to the entire database simply to avoid designing retrieval.

Use context budgets and prioritize relevant information. Do not send the complete conversation history or every memory on every turn.

### Initial retrieval implementation

Start with:
- Structured SQLite queries for tasks, state, and known relationships.
- SQLite FTS5 for keyword/full-text retrieval where supported.
- Scope, recency, and relevance filtering.
- Explicit links between domains, tasks, events, memories, and artifacts.

Do not add a vector database, embedding model, or separate semantic retrieval service in this iteration. Make the retrieval implementation replaceable at the boundary where it is genuinely useful, but do not build speculative infrastructure.

Include evaluation cases where the correct information is old, buried in history, stored in a different artifact, or available only through a relevant domain memory. A system that works only when the answer is in the most recent messages is insufficient.

Record the context sources used for important decisions and tool calls. This must be inspectable without exposing private internal chain-of-thought.

---

## 8. Memory system and consolidation

Implement global, domain-specific, and temporal/task-specific memory scopes.

Support useful memory categories, including:
- Facts.
- User preferences.
- Decisions and rationale summaries.
- Procedures and lessons learned.
- Important constraints.
- Open questions.
- Relevant historical context.
- Temporary information tied to an active task.

Memory should be captured dynamically during ordinary work. Explicit user instructions to remember something must receive high priority.

Do not save every message as a permanent memory. The system should distinguish durable information from transient conversation details.

### Storage model

Use a hybrid of structured records and readable artifacts.

**SQLite is authoritative for structured state**, including memory identity, scope, type, provenance, status, timestamps, revision information, and references to artifacts.

Use readable Markdown or other simple text artifacts for longer notes, summaries, procedures, and knowledge that benefits from direct inspection or editing.

Avoid two writable sources of truth. If an artifact is referenced by a database record, maintain an unambiguous identity and revision relationship. Define how updates, deletions, and inconsistencies are handled.

### Controlled memory updates

The model can propose new memories and modifications. The harness validates and persists them.

Support:
- Creating memories.
- Updating and correcting existing memories.
- Merging duplicates.
- Resolving or flagging contradictions.
- Promoting useful temporary information into longer-lived memory.
- Expiring or archiving stale information.
- Removing irrelevant or low-value information.
- Maintaining provenance and revision history.

Do not let the model silently overwrite the entire memory store or bypass validation.

### Consolidation

Provide a deliberate consolidation operation that can run manually at first and later be triggered during off-hours.

Consolidation should review accumulated events and memories, identify useful information, remove noise, merge duplicates, detect contradictions, update summaries, and promote or expire temporary knowledge.

Use the same model endpoint. Consolidation must be bounded and recoverable.

Produce inspectable proposed changes or a change log. Apply updates through controlled harness operations so the user can determine what changed and why.

Do not make nightly consolidation a prerequisite for ordinary memory capture or correct task resumption.

---

## 9. Scheduler and heartbeat: core requirements

Scheduled work and heartbeat activity are essential features and must be implemented in the initial complete system.

### Scheduled tasks are normal prompts

A scheduled task is a durable instruction that is submitted to the ordinary agent runtime at its scheduled time.

It is not a separate agent, separate model, or separate execution engine.

When triggered, it should have the same configured tools, permissions, context retrieval, task management, and bounded execution behavior as an equivalent interactive request.

Support:
- One-time and recurring schedules as appropriate to the existing product requirements.
- Explicit timezone handling.
- Durable persistence across container restarts.
- Due-time calculation and missed-run recovery.
- Retry behavior for transient failures.
- Idempotent occurrence tracking to prevent duplicate prompt submissions.
- Visibility into the schedule, next run, last run, and outcome.
- Editing, disabling, and removing schedules.

Record each scheduled occurrence before or atomically with submitting its work so restart recovery does not blindly enqueue the same occurrence again. Define the behavior for crashes between scheduling, submission, and execution.

Do not claim that arbitrary external side effects are exactly-once. Use durable action records, idempotency where available, and explicit uncertainty handling when a crash leaves an action's outcome unknown.

### Approval during scheduled work

Scheduled prompts inherit the ordinary permission and approval policy.

If the agent reaches an action requiring approval:
- Persist the task and its execution state.
- Record the exact pending action and parameters.
- Mark the task as waiting for approval.
- Notify or expose the pending approval through the normal UI.
- Resume the same task when approval is received.
- Do not restart the entire scheduled prompt and accidentally repeat completed actions.

If approval arrives after the original scheduled time, resume the waiting task instead of generating an unintended duplicate run.

### Heartbeat

Heartbeat is another trigger for the same runtime, not a separate autonomous agent.

At a configurable interval, it may examine relevant pending work such as:
- Due tasks and reminders.
- Outstanding approvals.
- Alerts or configured status information.
- Unfinished work that warrants attention.
- Other explicitly configured maintenance objectives.

It should then submit an appropriate bounded objective to the normal agent loop when useful.

Avoid generating a user-visible message for every heartbeat check. Record meaningful checks, decisions, initiated work, failures, and state changes.

Prevent overlapping heartbeat runs and uncontrolled repeated work. Persist enough state to recover after restart. Do not create an always-running high-frequency model loop that burns resources without a clear purpose.

The scheduler's job is durable timing and prompt submission. The runtime's job is reasoning and execution.

---

## 10. Approvals and constrained confirmation

Implement a clear approval lifecycle for actions that require user authorization.

An approval must be tied to a specific pending action, including the relevant tool, parameters, intended effect, and user or identity scope.

A generic “yes” must never grant broad or reusable authority.

### Quick confirmation path

Support a small, fast confirmation path for short responses such as “yes,” “no,” “approve,” or “cancel.”

It may use the same model with minimal context, but it must only classify the user's response as:
- Affirmative.
- Negative.
- Ambiguous or unrelated.

It must not execute tools or independently decide what action is authorized.

The runtime should associate the classification with the exact pending approval and verify that the approval is still valid and unchanged before executing the action.

Ambiguous responses must not approve an action. If there is no applicable pending approval, an affirmative message must not be treated as general permission.

Approval state must survive restarts. Cancellation, rejection, expiration if configured, and successful execution must have explicit outcomes.

---

## 11. MCPs, skills, tools, and permissions

Most external capabilities should come from MCP servers. The default application must not bundle a collection of tools useful only for one particular user's homelab.

The harness should support configured MCP connections and explicitly installed or configured skills. Avoid inventing an elaborate plugin ecosystem if straightforward configuration and clear interfaces are sufficient.

### Tool execution

The model proposes tool calls. The harness:
1. Resolves the requested tool against the currently configured and allowed tools.
2. Validates arguments against the tool's schema.
3. Applies permission and risk checks.
4. Requests approval when required.
5. Executes the tool.
6. Captures the result, errors, and relevant metadata.
7. Returns a bounded observation to the model.
8. Persists the outcome for recovery and inspection.

Handle unknown tools, invalid arguments, timeouts, failed connections, malformed responses, and tool errors without crashing the entire runtime.

Keep tool schemas and result formatting clear to the model. Prevent unbounded tool outputs from consuming the context budget.

### No shell access

Do not provide:
- Bash or shell tools.
- Arbitrary command execution.
- Generic code-execution tools.
- A generic tool that can indirectly recreate unrestricted shell access.

This restriction applies to bundled functionality and default configuration. Do not circumvent it through a convenience wrapper.

### Permission boundaries

Implement explicit tool allowlists and configurable risk/approval policy. At minimum, distinguish low-risk preauthorized actions from actions that require approval because of their consequences.

The harness—not the model—enforces permissions.

MCP results, retrieved documents, skills, and other external content are untrusted data. They must not override system rules or expand the model's permissions.

Skills may eventually be proposed or created from successful recurring procedures. Treat skills as untrusted executable instructions, not as inherently safe capabilities. Validate their contents, permitted tools, and effects before enabling them.

Record tool calls, arguments where appropriate, permission decisions, approval outcomes, results, failures, and execution identifiers. Avoid logging credentials or unnecessary sensitive content.

---

## 12. Persona and configuration

Implement one configurable global persona that represents the assistant to the user.

Configuration should support, as appropriate:
- Persona name.
- Purpose and responsibilities.
- Principles and values.
- Personality and conversational style.
- Response preferences.
- Configurable user-facing identity or speaker labels.

Keep stable internal roles and event types independent of display names. Changing a persona name must not corrupt historical attribution or database relationships.

Do not create multiple user-visible agent personas. Internal components are implementation responsibilities, not separate characters.

Avoid hardcoding personality or operational policy throughout the application. Keep stable system constraints in the appropriate code/configuration boundary and user-customizable persona content in inspectable configuration.

Do not require a final assistant name to be chosen during this refactor.

---

## 13. Persistence and Docker deployment

Use SQLite as the authoritative state store.

Persist at least the state needed for:
- Interaction events and history references.
- Domains and tasks.
- Memories and memory revisions.
- Schedules and individual occurrences.
- Heartbeat/run state.
- Tool execution records.
- Pending approvals.
- Relevant configuration and references to artifacts.

Use database transactions and appropriate constraints for important state transitions. Include a migration/versioning strategy that is proportionate to this application's size.

Readable artifacts should live in a predictable persistent directory. Use explicit references from SQLite and document the backup/restore relationship between the database and artifacts.

### Docker requirements

The application should run as one Docker container with:
- Web UI and API.
- Shared agent runtime.
- Terminal interface connecting to the same runtime.
- Scheduler and heartbeat.
- Memory and context management.
- Persistent SQLite and artifact storage via mounted volumes.
- Configuration for the model endpoint and MCPs.
- Health checks and useful startup diagnostics.
- Graceful shutdown and recovery.

Do not add Redis, a message broker, a separate database service, or additional application containers without a demonstrated need.

The model endpoint is external to this container. Do not bundle a model server into the harness.

Ensure scheduled work and pending state survive container restarts. Handle interrupted work explicitly. Do not start a second competing scheduler when the process restarts or multiple interface connections are opened.

Provide a documented, practical installation and upgrade workflow. Include a backup and restore procedure for persistent state.

---

## 14. Web UI and terminal interface

Implement both interfaces in the initial complete version.

### Web UI

Provide a familiar, simple chat-style interface as the default view.

The user should be able to:
- Send messages and see responses.
- See the active domain and relevant task.
- Inspect ongoing work and its status.
- Approve or reject pending actions.
- View schedules and heartbeat status.
- Inspect memories and relevant knowledge.
- Inspect configured MCPs, tools, and skills.
- Review activity, failures, and tool execution results.
- View context-source references and important decisions.
- Access configuration and maintenance functions.

Keep the default screen uncluttered. Advanced state and debugging details can live in expandable panels or dedicated pages.

### Terminal interface

Provide a practical CLI for interacting with the same running application.

It must not launch an independent model loop or maintain separate persistent history. It should send messages to the same runtime and expose useful inspection, configuration, scheduling, and troubleshooting commands.

Document how to use it in development and deployment.

### Observability

Make important system behavior inspectable:
- What objective was submitted.
- Which domain/task was selected.
- Which context sources were retrieved.
- Which tools were considered or called.
- What action was proposed and whether it passed validation.
- Whether approval was required and its outcome.
- What results or errors were observed.
- Why the task finished, paused, or failed, using a concise decision summary.
- Which memories or artifacts were created or changed.

Do not expose raw hidden chain-of-thought. Provide concise explanations, recorded decisions, and evidence of actual operations.

Clearly distinguish observed events and tool results from model-generated explanations. The UI must reflect persisted system state rather than becoming a separate source of truth.

---

## 15. Bounded autonomy, recovery, and failure handling

The assistant must be capable of autonomous work without being allowed to run indefinitely.

Implement configurable limits for execution duration, iterations, tool calls, and other meaningful resources. Avoid scattered, hardcoded limits. Defaults should be documented and configurable, with safe behavior when limits are reached.

When a limit is reached, the system should:
- Persist the current state.
- Record why execution stopped.
- Mark the task as paused, waiting, or failed as appropriate.
- Preserve outstanding approvals and completed results.
- Allow an appropriate future trigger to resume it.

Distinguish retryable failures from permanent failures. Use bounded retries and avoid repeating irreversible actions merely because an observation was lost.

Handle:
- Model endpoint outages.
- MCP timeouts and failures.
- Malformed model output.
- Invalid tool arguments.
- Container restarts.
- Interrupted agent runs.
- Interrupted consolidation.
- Missed scheduled occurrences.
- Duplicate trigger attempts.
- Unknown outcomes after interrupted external actions.
- Missing or corrupt artifacts.

Use structured logs and durable event records for important state transitions. Do not make a separate observability infrastructure a prerequisite.

---

## 16. Future evolution without premature implementation

The architecture should leave practical room for future capabilities, but implement only the current requirements.

Potential future additions include:
- Multiple users with separate identity, safety, memory, and authorization scopes.
- Internal multi-agent orchestration hidden behind the same user-facing persona.
- Additional input/output adapters.
- More advanced semantic retrieval.
- Controlled self-improvement and prompt evaluation.
- More sophisticated notifications and integrations.

Do not implement these future systems now unless a small, concrete architectural decision is necessary to avoid a foreseeable breaking change.

Do not build empty interfaces, unused configuration layers, or placeholder subsystems just to claim extensibility.

### Self-improvement boundary

The assistant may eventually propose improvements to its persona, prompts, procedures, or skills. It must not silently rewrite its production instructions or bypass permissions.

A future improvement workflow should be able to version proposed changes, evaluate them against regression scenarios, compare results, and roll back. For this iteration, it is sufficient to preserve a clean separation between editable configuration/content and the stable execution and safety code.

The initial system should improve primarily through better use of its available capabilities, not by rewriting its own core.

---

## 17. Testing and evaluation

The existing repository contains tests and evaluations that may need substantial modification. Audit them carefully and preserve valuable behavioral coverage.

Build a compact but meaningful regression suite around the target architecture. Use deterministic fixtures and mocked model/MCP responses for most tests. Add real endpoint integration tests for behavior that cannot be reliably simulated.

Do not claim a test passed unless it was actually run and passed. Report skipped tests and their reasons.

### Required test categories

#### A. Core runtime
- A normal user message produces a response.
- A tool call is validated, executed, observed, and recorded.
- Multiple tool calls occur in sequence when appropriate.
- The model can recover from a tool failure without losing state.
- Malformed model responses and unknown tools fail safely.
- Execution budgets stop runaway loops.

#### B. Unified interfaces and history
- A message from the web UI and a message from the terminal use the same runtime and state.
- Scheduled prompts enter the same history/runtime.
- Heartbeat-initiated work enters the same runtime.
- Events retain source metadata without creating separate agent histories.
- Internal maintenance activity does not flood the ordinary chat view.

#### C. Domains and resumption
- A new request is routed to a relevant existing domain when appropriate.
- A new domain is not created for every new topic.
- An unfinished task resumes from its persisted state without requiring the entire transcript.
- Ambiguous competing tasks trigger clarification when it materially matters.
- Domain-specific context does not leak indiscriminately into unrelated work.

#### D. Context and memory
- Explicit remember requests create the appropriate durable memory.
- Relevant old memories can be retrieved after later messages.
- Global and domain-specific scopes are respected.
- Context retrieval identifies its sources.
- Context budgets prevent unbounded history injection.
- Consolidation can merge duplicates, identify contradictions, and propose corrections.
- Memory changes preserve provenance and revision information.
- An artifact/database inconsistency is detected or handled explicitly.

#### E. Scheduling and heartbeat
- A scheduled prompt is submitted at the correct time and timezone.
- Schedules survive restarts.
- Missed runs and retry behavior follow documented policy.
- Repeated scheduler ticks do not duplicate a scheduled occurrence.
- Heartbeat runs do not overlap uncontrollably.
- Heartbeat work uses the same agent runtime.
- A scheduled task waiting for approval resumes the original task after approval.
- Restart recovery does not repeat completed actions unnecessarily.

#### F. Approval and permissions
- A pending action requires approval when policy says it should.
- A generic affirmative message without a matching pending approval grants no permission.
- An approval is bound to the exact action and parameters.
- Ambiguous responses do not approve.
- Rejection and cancellation prevent execution.
- A changed or expired pending action cannot be approved using stale authorization.
- The quick confirmation path cannot execute tools.
- Untrusted tool output cannot expand permissions.
- Shell or arbitrary command execution is unavailable.

#### G. Recovery and deployment
- The application restarts with domains, memories, tasks, schedules, and approvals intact.
- An interrupted run can resume safely.
- Unknown external-action outcomes are not treated as confirmed success.
- Graceful shutdown preserves state.
- Backup and restore procedures work.
- Web and terminal interfaces can be used concurrently without creating independent state or duplicate scheduler workers.

#### H. Real model endpoint
Attempt to verify `http://192.168.50.40:8181/` and test the actual API behavior.

Check normal completion, the required tool-call protocol, multi-step tool interaction, and representative structured outputs. Record the model/server behavior and any compatibility constraints. Do not assume that OpenAI-compatible means every feature behaves identically.

Use mocks for deterministic regression tests even when real integration tests succeed.

### End-to-end acceptance scenarios

In addition to unit tests, create repeatable scenario tests for at least these workflows:

1. The user establishes a preference, returns later, and the assistant retrieves it correctly.
2. A multi-step task is interrupted by a restart and resumes without repeating completed work.
3. A scheduled task starts, reaches an approval requirement, waits, and resumes after approval.
4. A heartbeat detects relevant pending work and initiates bounded execution.
5. The assistant uses information from an older history event or artifact rather than hallucinating or requiring the user to repeat it.
6. The web UI and CLI can exchange messages while reflecting the same persisted task and history state.
7. A failed model endpoint or MCP call produces an inspectable failure and recoverable state.
8. Consolidation proposes meaningful memory changes that can be inspected and traced.
9. A repeated trigger after a restart does not duplicate a completed scheduled occurrence.
10. A permission-sensitive action cannot be executed through an indirect tool or a generic approval.

The acceptance suite should be small enough to run regularly and meaningful enough to catch architectural regressions.

---

## 18. Two-week operational readiness

Before declaring the refactor complete, evaluate whether the system can realistically be run for two weeks without architectural intervention.

It must support:
- Persistent state across restarts.
- A single interaction history and shared runtime.
- Automatic domain routing and task resumption.
- Dynamic memory capture and later retrieval.
- Manual, inspectable consolidation.
- Durable scheduled prompts.
- Heartbeat-triggered work.
- Approval pauses and resumption.
- Bounded autonomous execution.
- Tool/MCP failure recovery.
- Web and terminal access.
- Useful operational logs and state inspection.
- Backup and restore.
- Configuration without editing core code for ordinary changes.

Do not interpret “two-week ready” as permission to claim two weeks of real operation have already been observed. It means the system and its acceptance tests are ready for that evaluation.

Once the two-week evaluation begins, prefer recording defects and classifying them as model, retrieval, skill, tool, configuration, or harness issues. Make only critical safety fixes or necessary bug fixes during the evaluation; avoid routine architectural changes.

---

## 19. Documentation and final repository state

Update `README.md` to accurately describe the implemented system, its requirements, installation, configuration, operation, limitations, testing, and recovery.

Update `CODEMAP.md` to accurately represent the final architecture and repository layout. Remove stale descriptions of components that no longer exist.

Document:
- Model endpoint configuration and verification.
- Docker deployment and persistent volume setup.
- MCP configuration and tool permissions.
- Web and terminal usage.
- Domain and task behavior.
- Memory and consolidation behavior.
- Scheduling, timezones, missed runs, and heartbeat behavior.
- Approval and recovery workflows.
- Logging and troubleshooting.
- Backup and restore.
- Running unit, regression, scenario, and endpoint integration tests.
- Known limitations and incomplete external integrations.

Remove obsolete code, tests, dependencies, configuration, and documentation when they no longer serve a purpose. Do not leave dead implementations, duplicate runtime paths, or misleading compatibility wrappers merely to avoid deleting files.

Do not remove useful user data or overwrite existing persistent state as part of the refactor without a safe, documented migration or explicit necessity. If the repository contains actual user data or secrets, preserve them and avoid printing secrets into logs or documentation.

Before finishing:
1. Run the relevant automated tests.
2. Run linting, type checking, or other established quality checks where available and useful.
3. Verify Docker build and startup behavior where the environment supports it.
4. Verify that the documented commands match the final implementation.
5. Check for stale references, dead code, duplicate implementations, and prohibited shell-execution pathways.
6. Review the final changes against every mandatory requirement in this prompt.
7. Update `TASK.md` with the final verification results and any limitations.
8. Delete `TASK.md` as the final repository modification.
9. Confirm that `TASK.md` is absent and that the final repository contains the updated documentation and implementation.
10. Provide a final report summarizing the architecture, retained and removed components, tests actually run and their outcomes, endpoint connectivity status, deployment instructions, and any remaining blockers.

If an external service, hardware dependency, or unavailable environment prevents a test, distinguish that from a test failure and provide the exact command needed to verify it later.

Do not declare completion while a known critical safety, persistence, approval, or scheduler correctness issue remains unresolved.

---

## 20. Final decision rule

When making implementation decisions, repeatedly ask:

**Is this necessary for the intended assistant to operate reliably for two weeks, or is it complexity for a hypothetical future?**

Implement the former. Defer the latter.

The desired result is not the largest or most sophisticated agent framework. It is a compact, persistent, inspectable personal assistant whose runtime stays stable while its capabilities, knowledge, and effectiveness grow through real use. 