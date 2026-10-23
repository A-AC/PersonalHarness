# Latency and failure experiments

Measured on the live llama.cpp server (`qwen3.5-9b`,
`http://192.168.50.40:8181/v1/chat/completions`) on 2026-10-23. The fixed set
is `harness/eval.py`'s 21 scenarios, including reconstructions of the reported
failures (`remember_name`, `failed_remember_honesty`, `retry_pressure`) plus
the planning checks (`plan_short_single`, `plan_two_requests`). Each config ran
the whole set twice: 42 scenario runs, 44 user turns.

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

## Recommended defaults

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

## Caveats

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
