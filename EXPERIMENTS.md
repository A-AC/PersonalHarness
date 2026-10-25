# Latency and failure experiments

Measured on the live llama.cpp server (`qwen3.5-9b`,
`http://192.168.50.40:8181/v1/chat/completions`) on 2026-10-23. The first set
was `harness/eval.py`'s 21 scenarios, including reconstructions of the reported
failures (`remember_name`, `failed_remember_honesty`, `retry_pressure`) plus
the planning checks (`plan_short_single`, `plan_two_requests`). Each config ran
the whole set twice: 42 scenario runs, 44 user turns.

> The V1.1 real-use set adds long-history and scripted honesty scenarios. Its
> before/after and prompt/decoding experiments are in
> [Real-use reliability (V1.1)](#real-use-reliability-v11) below. The 21-scenario
> latency table above remains the evidence for the earlier tuning defaults.

Settings for every config live in `scripts/experiments.py`; raw results and
per-run traces are in `experiments/results/<config>.json`.

## Instrumentation

Every model-call trace now stores `duration_ms`, `time_to_first_token_ms`,
`prompt_tokens`, `completion_tokens`, `visible_output_tokens` (reasoning
stripped), `reasoning_tokens`, and `thinking_produced`. Every turn writes a
`role="turn"` trace with a stage timeline: session, structure check, plan
prompt/model/parse, task creation, prompt build, each decision model call,
parse/validation, execution, and response. Execution stage entries contain the
full envelope (`status`, `error_category`, `message`, data/evidence) and the
trace view renders it inline. Metrics are computed from these traces by
`harness/eval.py`.

## Metrics

- **Eval**: scenario runs whose deterministic checks all pass.
- **Turn p50/p95**: wall-clock time for one `handle_turn`, including every
  model call and execution in that turn.
- **Call p50/p95**: one model HTTP call, from request start to last streamed
  chunk.
- **TTFT**: request start to first SSE chunk.
- **Tool success**: succeeded capability attempts / all attempts.
- **Thinking**: share of model calls that produced reasoning tokens.
- **Completion/visible**: completion tokens come from server usage; visible
  output is estimated from the reasoning-stripped content with
  `context.chars_per_token`. Reasoning is the server's `reasoning_content`
  estimate, or the completion/visible gap when the server hides it.

## Results

| config | eval | tool success | turn p50 | turn p95 | call p50 | call p95 | TTFT p50 | thinking | completion | visible |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 88% (37/42) | 0.793 | 11,892 | 16,536 | 4,135 | 5,864 | 554 | 1.00 | 105.0 | 44.7 |
| E1 no thinking | 90% (38/42) | 0.818 | 6,108 | 10,512 | 2,175 | 3,983 | 562 | 0.00 | 45.0 | 48.4 |
| E1b budget 16 | 86% (36/42) | 0.818 | 7,669 | 10,866 | 2,562 | 4,276 | 551 | 1.00 | 58.3 | 44.2 |
| E2 heuristic plan | 90% (38/42) | 0.824 | 4,010 | 8,851 | 2,324 | 3,582 | 569 | 0.00 | 52.7 | 58.2 |
| E3 retry cap | 95% (40/42) | 0.824 | 4,038 | 8,862 | 2,346 | 3,617 | 571 | 0.00 | 52.5 | 57.5 |
| recommended (thinking on) | 93% (39/42) | 0.824 | 7,450 | 19,951 | 4,115 | 5,447 | 586 | 1.00 | 106.9 | 50.0 |

`baseline` = thinking on (`thinking_budget_tokens=64`), LLM planning, no retry
cap. Each later config changes one lever relative to the previous row;
`recommended (thinking on)` is the `recommended_think` config: thinking on +
heuristic planning + retry cap, i.e. the proposed defaults.

## E1: disable thinking

**Controlling setting:** `models.interactive.enable_thinking` (false), with
`models.interactive.stream=true`. The config still carries
`thinking_budget_tokens=64`, but the chat-template kwarg disables reasoning.

Disabling thinking cut turn p50 by 49% (11,892 → 6,108 ms), call p50 by 47%
(4,135 → 2,175 ms), and mean completion tokens from 105 to 45. Eval moved from
88% to 90%, but that is not evidence of "no accuracy loss": without thinking
the model systematically invented a target for `status_missing_target` (0/2; it
called `mock.status` with target `"mock"` instead of asking which target), and
it lost one `retry_pressure` run by asking for the NAS address after the failed
execution. Baseline's losses were different: `followup_echo` 0/2 (answered the
value instead of echoing it), one `retry_pressure` run, and two flaky
unsupported/injection runs. The systematic missing-argument regression is the
decisive signal.

A middle setting, **E1b** (`thinking_budget_tokens=16`, thinking enabled),
was worse: 86%, with `status_missing_target` 0/2 and
`failed_remember_honesty` 0/2 (it asked the user whether to store the memory
after the failed execution). A small non-zero budget did not recover
clarification behavior.

**Recommendation:** do not disable thinking by default. Keep the current
thinking-on default and document thinking-off as an optional latency preset
with a known missing-argument regression.

## E2: heuristic task planning

**Controlling settings:** `tasks.planning_mode=heuristic`,
`tasks.heuristic_max_chars=160`, `tasks.heuristic_max_words=24`,
`tasks.heuristic_split_markers=then|and also|after that|finally|;|, and`.

Relative to E1 (thinking already off), heuristic planning reduced turn p50 by
34% (6,108 → 4,010 ms) and p95 by 16% (10,512 → 8,851 ms) with the same eval
score (90%). Model calls fell from 121 to 84 (-31%) because short single
requests no longer pay a planner call. `plan_short_single` and
`plan_two_requests` both passed 2/2, so task-split accuracy did not regress.

**Recommendation:** enable `tasks.planning_mode=heuristic` by default.

## E3: retry cap after non-retryable failure

**Controlling setting:** `loop.stop_after_nonretryable_failure=true`. Retryable
error categories from the operation manifest are still allowed.

Relative to E2, the cap raised eval from 90% to 95% (38 → 40 of 42 runs) by
fixing `retry_pressure` 0/2 → 2/2. Every run made exactly one failed
`harness_memory.remember` attempt and the user got an honest failure message
instead of a question about internal memory type/scope. Turn p50/p95 were
unchanged (4,010/8,851 → 4,038/8,862 ms) and model calls stayed at 84.

**Recommendation:** enable `loop.stop_after_nonretryable_failure=true` by
default.

## E4: cold start

No harness setting controls this; the probe talks to the router directly
(`scripts/cold_start_probe.py`).

- **Idle (180 s):** the model stayed `loaded` before and after idle. The first
  call took 367 ms (TTFT 331 ms) versus a warm p50 of 334 ms (TTFT 297 ms), so
  idle itself costs only ~30 ms and does not evict the model.
- **Forced unload:** `POST /models/unload` returned 200. The next call took
  2.97 s in one trial and 7.31 s in another (TTFT 2.93 s / 7.28 s), versus
  0.34 s warm. Model reload is the real cold-start cost.
- **Eviction risk:** the router reports `max_instances=1` and
  `models_autoload=true`, so any request for a different model evicts
  `qwen3.5-9b`, and the next interactive request pays the 3-7 s reload (plus
  queueing). This is the plausible cause of the observed 51 s "hello": a
  reload or another client on the single slot, not idle time by itself.
- **Queueing:** two simultaneous small requests took 346 ms and 486 ms with a
  497 ms wall time, versus 334 ms warm solo. The slot serializes clients, so a
  long generation ahead of an interactive turn adds its full duration.

**Recommendation:** no harness change. Keep one model resident and avoid
requesting other models or running long jobs while an interactive turn is in
flight. If a cold start recurs, check `/v1/models` status before blaming the
harness; `/models/unload` can reproduce the reload penalty deterministically.

## Recommended defaults (V1 latency tuning)

The proposed default is **thinking on (budget 64) + heuristic planning + retry
cap**, i.e. the `recommended_think` row. It raises eval from 88% to 93% and cuts
turn p50 from 11,892 to 7,450 ms (-37%). Its p95 is worse in this sample
(16,536 → 19,951 ms) because one run split `chain_echo_add` into three tasks
and re-executed echo/add (10 calls, 39.6 s). Excluding that run, p95 is
15,678 ms, below baseline's 16,536 ms; the median is the more stable comparison
at this sample size.

Settings to enable by default:

```yaml
tasks:
  planning_mode: heuristic
loop:
  stop_after_nonretryable_failure: true
models:
  interactive:
    enable_thinking: true
    thinking_budget_tokens: 64
    stream: true
```

Thinking is deliberately left enabled. Thinking-off is a useful **fast mode**
(`models.interactive.enable_thinking: false`): E3 reached 95% at a 4,038 ms p50,
but failed `status_missing_target` 0/2 in both E1 and E3 by inventing a target
instead of asking. That is the same class of behavior as the reported
"invented type/scope" failure, so it is not a safe default. E1b showed that a
16-token reasoning budget is not a working compromise.

## Caveats (V1 latency tuning)

- Two repeats per scenario; the live model is stochastic and one scenario
  (`followup_echo`) was flaky, so one-scenario differences are weak evidence.
- The fixed set is small and aimed at the reported failures, not a general
  benchmark.
- Latency is end-to-end through the llama.cpp router; an external client using
  the single slot would add queue time. Duplicate harness processes were
  stopped before the reported run.
- `recommended_think`'s p95 includes a single 39.6 s outlier where the LLM
  planner created three independent tasks for `chain_echo_add` and re-ran both
  operations; p50 is reported alongside p95 for that reason.

## Real-use reliability (V1.1)

Measured on the same live endpoint on 2026-10-23. The V1.1 suite is the
original 21 scenarios plus real-use and scripted-honesty scenarios: 29
scenarios, 58 runs, 64 turns at two repeats. Each real-use run starts with a
12-pair history (24 messages, beyond the 20-message window) and four active
memories, so the model cannot rely on a fresh context. The required real-use
cases are remembering a name, remembering NAS details, replying to "i don't
know, figure it out", and resuming a task after an ask. A fifth case disables
the memory source policy to test a failed remember. The scripted cases drive the
real loop, validator, and response guards with a failed tool and an unknown
outcome, because a live timeout cannot be produced on demand.

### Before and after

| config | eval | real-use | scripted honesty | malformed | honesty rejects | tool success | turn p50 | turn p95 | call p50 | call p95 | TTFT p50 | completion | visible |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| before (gated switches off) | 86% (50/58) | 9/10 (7/8 required) | 0/4 | 0.000 | 0 | 0.760 | 7,607 | 16,353 | 4,264 | 5,616 | 580 | 100.8 | 48.1 |
| after (strict + full + guards) | 100% (58/58) | 10/10 (8/8 required) | 4/4 | 0.000 | 4 | 0.708 | 8,170 | 15,880 | 4,452 | 5,762 | 982 | 96.4 | 47.5 |

The before arm is a controlled ablation, not a reproduction of V1: it keeps
the new full prompt and the always-on multi-field validation/manifest defaults,
and turns off only the gated switches (`coerce_optional_arguments`,
`disclose_enum_values`, `reject_placeholders`, `honesty_guard`,
`internal_schema_ask_guard`, `include_time`) plus the strict schema. It
therefore understates the true V1-to-V1.1 gain. The original 21-scenario V1
baseline was 88% (37/42). The after arm is the catalog-default configuration,
written out explicitly in `scripts/experiments.py`.

Tool success includes the deliberately failing scenarios (failed remember,
hard failure, unknown write), so it is not a standalone quality metric. Use the
eval and honesty columns for reliability.

### What failed before, and why

| failure | before | after | cause and fix |
|---|---|---|---|
| Invented memory scope | `retry_pressure` 0/2: the call failed with `invalid_arguments` (`invalid scope "personal"; allowed: ...`); #1 was honest and completed, #2 blocked without an honest failure | 2/2 | Enum values are disclosed inline; `type`/`scope` are optional hints, invalid values are coerced or omitted, and scope is derived from the task. An earlier baseline repeat also failed `remember_name` once with scope `"user"`. |
| Literal placeholder as data | `real_ambiguous` 1/2: executed `mock.echo` with `{"echo": "<text>"}` | 2/2 ask for the real value | `capabilities.reject_placeholders` rejects an unresolved `<...>` in a required value; the full prompt says an angle-bracket placeholder is not a value. |
| Success claim after a failed tool | `scripted_failed_tool_no_claim` 0/2: "I've saved" | 2/2 | `loop.honesty_guard` rejects a respond/end that claims success after a failed/denied/unavailable execution and retries with the exact failure. |
| Success claim after an unknown outcome | `scripted_unknown_outcome_honesty` 0/2: "I've saved" | 2/2 | The guard requires "unknown" or "uncertain" when the last status is unknown. |
| Asks the user for internal schema | `real_no_internal_schema_question` 2/2 (the live failure did not reproduce) | 2/2 | `loop.internal_schema_ask_guard` plus the prompt rule. This is regression coverage, not a measured before failure. |
| Flaky follow-up | `followup_echo` 1/2: no echo call on turn 2 | 2/2 | Sampling noise in this sample; the resume rule is present in both arms. |

The final before rerun is 50/58 (86%). The earlier 47/58 run used an eval check
that counted a blocked ask as a success claim; the final check exempts a blocked
ask, and the rerun passed `unsupported_email` 2/2. Normal sampling variation
accounts for the rest (`remember_name`, `injection_echo`, and `real_ambiguous`
differed between the two before runs).

### Why the failed remember produced "noted"

The V1 decision prompt already contained the failed execution envelope; the
trace shows the `status`, `error_category`, and message in the prompt, so
missing context was not the cause. The model ignored the envelope and emitted a
success claim. The scripted cases reproduce that deterministically (0/4
before). The fix is two-layered: explicit envelope rules in the full prompt,
and `loop.honesty_guard`, which rejects the claim and forces one retry with the
exact failure. After: 4/4, with 4 honesty rejections. Unknown outcomes must be
stated as unknown or uncertain.

### Prompt: full vs trimmed

| variant | eval | real-use | turn p50 | turn p95 | completion | visible | failures |
|---|---|---|---|---|---|---|---|
| full | 100% (58/58) | 10/10 (8/8 required) | 8,170 | 15,880 | 96.4 | 47.5 | none |
| trimmed | 93% (54/58) | 8/10 (8/8 required) | 7,060 | 14,410 | 84.2 | 28.8 | `retry_pressure` #1, `suggest_domain` #1, `real_failed_remember` #1/#2 |

Trimmed is 13.6% faster at p50 and 9.3% faster at p95, and cuts mean completion
tokens by 12.7%, but it loses 4 runs. Two failures are exact-operation naming:
it called `remember.write` and `suggest_domain.suggest_domain` instead of the
registered operations. Two are honest-failure handling: `real_failed_remember`
blocked without saying the remember failed. Keep the full prompt; the
reliability gain outweighs the latency saving.

### Decoding: strict vs loose vs free-form

The subset is 16 scenarios (32 runs, two repeats) from the categories
`valid_call`, `missing_arg`, `memory`, `real_use`, `internal_schema`, and
`tool_failure`.

| mode | eval | real-use | malformed | parse failures | turn p50 | turn p95 | call p50 | call p95 | TTFT p50 | completion | failures |
|---|---|---|---|---|---|---|---|---|---|---|---|
| strict json_schema | 100% (32/32) | 10/10 (8/8 required) | 0.000 | 0 | 9,101 | 11,263 | 4,722 | 5,764 | 1,078 | 106.8 | none |
| loose json_schema | 97% (31/32) | 9/10 (7/8 required) | 0.000 | 0 | 8,462 | 10,703 | 4,738 | 5,781 | 1,061 | 106.3 | `real_ambiguous` #1 |
| free-form text | 91% (29/32) | 7/10 (5/8 required) | 0.103 | 7 | 9,334 | 14,943 | 4,726 | 5,771 | 1,081 | 104.2 | `real_remember_nas` #1, `real_resume_after_ask` #1/#2 |

Free-form text produced 7 parse failures in 68 model calls (10.3% malformed) and
lost the NAS and resume cases; strict and loose json_schema had no malformed
output. Free-form p95 was 14,943 ms versus 11,263 ms for strict (+33%). Strict
p50 was 7% higher than loose (9,101 vs 8,462 ms), but loose failed
`real_ambiguous` #1 by neither asking nor stating a limitation. Keep
`response_format: json_schema` and `action_schema: strict`.

### Recommended V1.1 defaults

```yaml
models:
  interactive:
    response_format: json_schema
    action_schema: strict
context:
  prompt_variant: full
  include_time: true
capabilities:
  coerce_optional_arguments: true
  disclose_enum_values: true
  reject_placeholders: true
loop:
  honesty_guard: true
  internal_schema_ask_guard: true
```

These are the catalog defaults; `tests/test_settings.py` asserts the strict
schema default. The existing thinking-on, heuristic-planning, and retry-cap
defaults from the V1 experiments are unchanged.

### Caveats

- The before arm is an ablation with the new prompt and always-on validation
  still present, so it understates the true V1-to-V1.1 improvement. It is not a
  time machine.
- Two repeats per scenario; the live model is stochastic. `followup_echo` and
  `real_ambiguous` differed between before runs, so one-scenario differences are
  weak. The deterministic scripted honesty result (0/4 to 4/4) is the strongest
  single comparison.
- The live "what type of memory?" failure did not reproduce in the before arm
  (2/2), so that eval is a regression guard rather than measured evidence.
- Latency is end-to-end on the single-slot llama.cpp server, and the arms ran
  sequentially rather than interleaved. The after arm includes 4 honesty-guard
  retries; p50 is 7% higher, p95 is slightly lower in this sample, and TTFT
  moved from 580 to 982 ms. Treat latency as descriptive, not a controlled
  causal result.
- Tool success includes deliberate failures; do not read it as a quality score.
- `context.include_time` was bundled into the after arm and was not isolated.
- The trimmed run was measured just before a later eval-check tweak that exempts
  a blocked ask from the no-success-claim rule; it passed `unsupported_email`
  2/2, so the prompt comparison is unaffected.

## Memory lifecycle (V2)

Measured on the live endpoint on 2026-10-25 with the same subset and config
before and after replacing the V1 lifecycle:

```powershell
python -m harness --data-dir <dir> eval --tag <tag> --repeats 2 \
  --only memory,real_use,internal_schema,tool_failure
```

Both arms used `models.interactive.model_name=qwen3.5-9b` and
`models.background.model_name=qwen3.5-9b`. Raw results are in
`experiments/results/memory_lifecycle_before.json` and
`experiments/results/memory_lifecycle_after.json`.

| arm | eval | memory | real-use | internal-schema | tool-failure | model calls | capability calls | tool success | turn p50 | turn p95 | call p50 | call p95 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| before (V1 lifecycle) | 100% (22/22) | 4/4 | 10/10 | 2/2 | 6/6 | 47 | 21 | 0.571 | 9,652 | 10,984 | 4,936 | 5,735 |
| after (V2 lifecycle) | 100% (22/22) | 4/4 | 10/10 | 2/2 | 6/6 | 47 | 21 | 0.571 | 9,716 | 11,778 | 5,096 | 6,586 |

Functional metrics are unchanged: same pass rate, same 47 model calls (no
extra call for memory), same 21 capability calls, and the same deliberate tool
failures. The subset is already at ceiling, so it verifies no regression rather
than an accuracy gain. Turn p50 moved +0.7% and p95 +7.2%, while call p50/p95
moved +3.2%/+14.8%; this is live-model timing noise (the after arm also ran
later on the single-slot server), not a lifecycle cost.

The lifecycle itself is covered deterministically:

- `tests/contracts/test_memory_contract.py`: all-wrong hints, no hints,
  duplicate, explicit conflict replacement, retract, source integrity.
- `tests/test_consolidation.py`: extraction with provenance/coercion, conflict
  by recency, run report, and rollback of added/merged/retyped changes.
- `tests/test_memory_retrieval.py`: 6/6 (100%) top-1 hit rate on the fixed
  question set, including "what is my NAS called?", plus the recency tie-break.
- `tests/test_web.py`: retired approve endpoint returns 400, unknown memory/run
  ids return 404, and rollback works from the memory page.
- `tests/test_executor.py`: bad memory hints are coerced even when general
  optional coercion is off, and notes name the rejected value and actual
  fallback.

Removed in V2: `proposed`/`rejected` memory states and the approval queue,
the approve endpoint, inferred-vs-explicit approval gating, `auto_commit`
settings, and model-supplied provenance for explicit remembers. Inferred
memories now come only from consolidation, are active with `origin=inferred`,
and are undoable individually or by run.

Caveat: the live eval set is a regression guard, not a discriminator for this
change; two repeats and single-slot timing make the latency deltas descriptive
only. The deterministic lifecycle tests are the evidence for the new behavior.

## Bounded history and time awareness (V1.2)

Measured on the same live endpoint on 2026-10-25 with `qwen3.5-9b`. Every arm
uses the V1.1 defaults; the only difference is `context.history.strategy`. The
graded subset is the five `time` scenarios plus the five `real_use` scenarios,
two repeats each: 20 runs and 22 turns per arm. The eval seeds the history
before the graded turn, calls `summarize_pending(force=True)` between turns, and
sets `context.history.background_summary=false` so summary latency cannot enter
the turn metrics. Raw results are in
`experiments/results/history_{full,summary,segment}.json`.

Metric: scenario pass rate, time-scenario pass rate, mean prompt tokens per
model call, and turn p50/p95. The time set covers “what did I tell you
yesterday?”, “how long ago did I mention X?”, resuming after a long gap, an old
changeable-state memory, and a fact that is older than the history budget.

| arm | eval | time | real-use | mean prompt tokens | turn p50 | turn p95 | model calls |
|---|---|---|---|---|---|---|---|
| full | 85% (17/20) | 8/10 | 9/10 | 2,314.8 | 7,172 | 12,585 | 35 |
| summary (default) | 95% (19/20) | 10/10 | 9/10 | 2,081.3 | 7,982 | 11,757 | 36 |
| segment | 95% (19/20) | 10/10 | 9/10 | 2,053.9 | 6,880 | 12,033 | 34 |

Summary and segment cut mean prompt tokens by 10.1% and 11.3% versus full,
respectively (about 230 tokens per call), while both raise the time score from
8/10 to 10/10. The absolute latency differences are not a consistent ordering:
summary is slower at p50 and faster at p95 than full, segment is faster at both,
and the arms ran sequentially on a single model slot. Treat latency as
descriptive noise.

### The discriminating case: a fact beyond the budget

Four extra repeats of `time_fact_beyond_history_budget` isolate the mechanism.
The durable fact (“the spare key is in the blue drawer”) is 30 hours old; eight
recent filler turns overflow the 800-token history budget.

| arm | budget-scenario runs | eval | observed behavior |
|---|---|---|---|
| full | 6 (2 in the main set + 4 extra) | 0/6 | The contiguous newest window drops the old fact; the model cannot answer. |
| summary | 6 | 6/6 | The old fact is summarized and carried; the answer survives. |
| segment | 6 | 6/6 | The 30-hour gap starts a new segment; the previous segment summary carries the fact. |

This is the accuracy evidence for bounded history: full history is not “more
context” once the budget is exceeded; it is an arbitrary recent window. Summary
and segment preserve the durable fact while using fewer prompt tokens.

An earlier full-history run passed this scenario 4/4 because the selector kept a
tiny old message after skipping larger middle messages. That was changed to a
contiguous newest window; cherry-picking small old rows made gaps look shorter
than they were and hid the real cost of full history.
`tests/test_history.py::test_full_history_keeps_a_contiguous_newest_window`
locks the change.

### Flaky real-use scenario

`real_ambiguous_figure_it_out` (the model must not pass “figure it out” as a
tool argument) is not history-sensitive and was flaky across arms. In the main
two-repeat set each arm passed 1/2. In six extra repeats, summary passed 6/6,
full 6/6, and segment 3/6; an earlier six-repeat run had full 3/6, summary 6/6,
and segment 4/6. The ordering reverses, so this scenario is not used to choose a
strategy. Raw runs are in `experiments/results/history_*__ambiguous.json`.

### Choice

Keep `context.history.strategy=summary` as the default.

- It matches segment on every time scenario and on the budget discriminator
  (6/6), and matches it on real use (9/10).
- It stores one rolling segment instead of one file per gap and keeps older
  facts in that rolling summary. Segment currently carries only the immediately
  previous segment summary; older segment summaries remain stored and viewable
  but are not in the prompt, which is a worse fit for a long-lived assistant.
- Prompt-token use is effectively tied (2,081 vs 2,054 mean tokens; less than
  1.5% apart).

`segment` remains an opt-in strategy for conversations where gap-scoped
summaries are preferred. `full` is kept for comparison and small histories, but
it is not a safe default once the history budget is exceeded.

Time awareness itself is covered deterministically by
`tests/test_history.py` (stored timestamps, compact markers, gap labels,
chronological ordering, summary-prefix carry-forward, missing/failed summary
fallback, segment boundaries, budget cap, trace inclusion, and the `/history`
page) and by `tests/test_context.py` (local current-time block, task created
marker, old-memory age marker). `tests/test_web.py` verifies that messages
dropped from the prompt are still listed on `/history`.

Caveats:

- Two repeats on the main set. The budget discriminator was repeated six times
  in total and is deterministic in mechanism, but the live model is stochastic.
- The eval turns background summary generation off and calls summarization
  explicitly between turns. The default path starts a daemon thread after the
  response; its off-path behavior is covered by unit tests, not by the live
  latency numbers.
- Prompt tokens cover the whole context (instructions, persona, task,
  capabilities, and history), not the history section alone. The history
  section itself is capped at `context.budgets.history` (800 tokens).
- Segment summaries are scoped to their segment by design. If older-than-
  previous-segment recall becomes a requirement, segment needs a cumulative
  carry-forward or budgeted inclusion of earlier summaries; summary already
  provides that.


## Web chat read path (UI)

Measured on 2026-10-25 with the scripted adapter, so the numbers isolate
harness/UI work from model latency. Each arm used 200 scripted turns
(`plan` + `respond`), producing 600 traces and 200 interactions; `/chat` was
fetched three times through the ASGI app and the median reported.

- **Metric**: `/chat` HTML response time at 200 messages.
- **Before**: one `TraceStore.list_for(interaction_id=...)` per visible
  message. Each call reads every trace file, so the page did 200 scans
  (3,529 ms median; range 3,499-3,536 ms).
- **After**: `DefaultTurnService.detail_many` loads the recent
  `ui.chat_trace_scan=500` traces once, groups them by interaction, and
  falls back to a per-message scan only for older focused messages. The same
  page took 83 ms median (range 80-91 ms), about 42x faster.

The fallback is selectable with `ui.chat_trace_scan=0`; that reproduces the
before arm. Submitting through `DefaultTurnService` (which also builds the
completed event payload) cost about 101 ms/turn versus 73 ms/turn for the
bare loop at 40 turns, so the read model adds roughly 28 ms per turn at that
history size. The scripted turns run locally, so these numbers include disk
trace reads and HTML rendering but no model or network time.
