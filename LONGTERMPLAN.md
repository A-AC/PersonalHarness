# Personal Harness — Alpha Hardening Roadmap

Status: private single-user alpha, hardening implemented; real-use validation next
Updated: 2026-11-18
Supersedes: all earlier phase plans and completion claims in this file

## 1. Product boundary

Personal Harness is a private, single-user assistant. The model proposes;
the harness validates, authorizes, executes, and records. The current goal is
not more autonomy. It is a small, inspectable system whose behavior under
real use justifies further investment.

Non-negotiables:

1. One process and one writer per data directory; one bounded agent runtime.
2. Exact-argument approvals and honest unknown outcomes after interruption.
3. SQLite is authoritative; durable changes have provenance and audit records.
4. Untrusted content cannot silently authorize state changes or external data
   transmission. Read-only does not mean safe for confidentiality.
5. Capability snapshots freeze schemas and policy for the life of a run.
6. Context accounting must describe the final model payload, not candidates.
7. Memory corrections are explicit. Lexical similarity never silently
   supersedes or deletes durable knowledge.
8. Deterministic tests are the default. Live model results are reported with
   their scenario, model, configuration, and raw artifacts.
9. MCP servers are native-code dependencies, not sandboxed plugins.
10. Multi-user is out of scope for this alpha.

## 2. Hardening implemented in this cycle

### Capability and MCP boundary

- MCP children no longer inherit unrelated process secrets. A small OS
  allowlist plus explicitly declared module environment is used.
- Selected tool schemas and policy metadata are frozen on each run. Reloads
  cannot silently change a pending approval or an active run.
- Successful discovery atomically replaces a module's tools; removed tools
  disappear. Registry access is synchronized. Removed/disabled tools fail
  closed rather than being resurrected from an old snapshot.
- Artifact reads and backup extraction use real path containment checks.
- Tool policy distinguishes state mutation, external data egress, and
  untrusted output. After taint, another external request needs approval even
  when it is read-only. Genuinely local modules may declare
  `external_egress: false`.

### Provenance and durable state

- Taint propagates through task notes, sub-agent results and reuse, tainted
  memories, history, and artifact reads.
- Tainted notes remain marked when later runs render them.
- Consolidation excludes tainted inputs and refuses to mutate tainted rows.
- Similar memories produce an advisory conflict candidate, never an implicit
  supersession. `update_memory` and explicit merges are the correction paths.
- Archived search is read-only unless `restore_archived: true` is explicitly
  requested. Archived duplicates no longer block a new live memory.
- Cross-domain memory defaults to `search`: sibling domains are searched only
  through explicit retrieval. Automatic `boost` remains available as an
  experiment, not an evidence-backed global default.

### Context and evaluation

- Rendered memories carry stable disclosure markers. Access is charged only
  when the memory survives builder budgets and transcript compaction into the
  actual model payload. The source ledger records inclusion, exclusion, and
  compaction transformations.
- Semantic router prompts preserve the message being classified even with
  many domain cards. Embedding cache identity includes the endpoint.
- Evaluation supports `quality.required: true`. Required behavioral thresholds
  become ordinary assertions and can gate a run; optional rubrics remain
  separate quality measurements.

## 3. Current evidence, not aspirational metrics

- Deterministic pytest: **591 passed**.
- Complete deterministic scenario run: **97/109 passed**; the other 12 are
  explicitly non-gating routing measurements. The core non-measurement suite
  also passes **36/36 scenarios and 291/291 assertions**.
- Live Qwen3.5-9B passed **13/14 scenarios and 133/134 assertions**;
  MiniCPM5-2B passed **12/14 and 131/134**, including two critical
  hostile-inbox runaway-bound failures. The required contradiction case failed
  in both, so no 100% claim is made. Rubric means were 95.2% and 92.9%.
- On hostile inbox input, Qwen scored 100% with no tool calls; MiniCPM scored
  71.4% with nine calls and exceeded both bounds. Qwen remains the
  quality/default path. One repeat is
  not enough evidence for automatic model routing.
- Full 65-case live routing bake-off: lexical base/held-out accuracy was
  **92.7% / 20%** with **0% wrong-domain**. The constrained LLM strategy was
  **92.7% / 90%**, with **5.45% wrong-domain on base** and approximately
  one-second routing latency. It improves weak/paraphrased evidence but is not
  a safe global default. Lexical stays default; semantic routing is an opt-in
  fallback candidate pending real-use correction data.
- Raw live and aggregate reports are curated under `eval/results/` with code
  and scenario fingerprints. Historical reports are experiments, not current
  release claims; check corpus version and source identity before comparing.

## 4. Known alpha limitations

1. **Model judgment is still fallible.** The contradictory create-and-cancel
   request can produce an approval instead of a clarification question. The
   stronger general instruction did not reliably fix it. Do not add a brittle
   keyword detector and call the problem solved.
2. **MCP is not a sandbox.** Environment scrubbing prevents accidental secret
   inheritance, but a configured server is native code with filesystem and
   network access. Interpreter/package-manager allowlists are convenience,
   not a security boundary. Install only code you trust.
3. **Confidentiality needs more work.** Taint now gates subsequent egress, but
   the first external request can still contain sensitive context. A future
   policy needs argument-level/data-flow controls, not only run-level taint.
4. **Routing is not yet validated on real domains.** Composite and follow-up
   requests, multilingual input, and domain lifecycle operations need real-use
   evidence. Semantic strategies remain opt-in.
5. **Storage lifecycle is incomplete.** Messages, transcripts, revisions,
   access records, events, artifacts, and backups need retention and true
   erasure policies. Archive/retract is not secure deletion.
6. **Concurrency is intentionally limited.** A single runtime lock serializes
   model work; scheduler and interactive work can wait behind each other.
7. **Deployment proof is incomplete.** Migration backup replay is useful, but
   staging service identity and a real previous-image rollback were not fully
   rehearsed.
8. **Large modules remain.** `runtime.py`, `server.py`, `tools.py`,
   `modules/mcp.py`, and `web/app.js` need incremental separation without a
   rewrite of working behavior.

## 5. Next milestones

### H1 — Finish the hardening release

- Preserve raw live reports with code/scenario/config identity.
- Run the full deterministic suite and core deterministic scenarios.
- Complete the live routing bake-off and record the decision without tuning on
  the held-out split.
- Update README and CODEMAP to match actual behavior.
- Rehearse an actual rollback before calling deployment reversible.

Exit: existing features are documented, tested, and safe enough for cautious
private use. Known failures are visible rather than labelled complete.

### H2 — Real-use domain and memory evidence

Use two to four real domains for at least one week. Instrument:

- route coverage, wrong-domain cost, General abstention, and corrections;
- task success after hints/`use_domain`, not only classifier accuracy;
- answer-level memory correctness, precision at k, stale/contradictory recall,
  and the fraction of context that was actually useful;
- approval frequency by cause, especially egress and durable notes;
- latency attributable to MCP process startup, routing, and model calls.

Rules:

- Corrections are time-split into train/dev/frozen-test sets with the original
  domain environment snapshotted. Exported corrections are not automatically
  an independent holdout.
- Change one variable at a time. Do not enable semantic routing, memory boost,
  or a model router from synthetic corpus results alone.
- Promote real failures into regression scenarios without reclassifying the
  original holdout as untouched.

### H3 — Simplify the operator experience

- Split the largest runtime, MCP, server, and browser modules incrementally.
- Generate typed settings forms and remove unused/dead configuration.
- Add browser interaction tests for approvals, memory restoration, MCP setup,
  domain correction, and failure states.
- Add domain merge/move/delete semantics only after real domains require them.

### H4 — Optional capabilities, evidence-gated

- Model router: only if end-to-end latency and task success improve after model
  load/swap costs. Tainted or security-sensitive work stays on the quality path.
- LaYa guard: optional additional signal only. It cannot repair provenance,
  egress, or native-code trust boundaries and is not a hardening prerequisite.
- New MCP servers: only after their need is demonstrated and their native-code,
  credential, and egress policies are documented.
- Multi-user: removed from this alpha roadmap. It requires a separate tenant,
  identity, cache, artifact, scheduler, and concurrency design.

## 6. Alpha completion criteria

The hardening release is complete when:

1. Every deterministic test and core deterministic scenario passes.
2. Security regressions cover environment inheritance, snapshot reloads,
   external egress, notes, delegation, memory/history/artifact provenance,
   consolidation, archive traversal, and final-payload accounting.
3. Live reports preserve both state assertions and required behavioral
   failures. No 100% claim hides a failed required rubric.
4. README, CODEMAP, and this roadmap agree with the implementation.
5. Remaining limitations are explicit, and no next feature depends on
   unmeasured real-world accuracy.
6. A production backup and a previous-image rollback have both been rehearsed,
   or deployment is explicitly labelled unverified.
