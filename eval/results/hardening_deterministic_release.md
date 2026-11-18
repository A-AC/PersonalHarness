# Evaluation: hardening_deterministic_release

- Run ID: `eval_20261118T075427Z_8ea161`
- Mode: `deterministic`
- Started: 2026-11-18T07:54:27Z
- Finished: 2026-11-18T07:54:31Z
- Model: `qwen3.5-9b` at `http://192.168.50.40:8181`
- Thinking: True
- Max tokens: 2048

## Source identity

- Harness: `2.0.0` / `unknown`
- Code fingerprint: `a438b9f116e3f7a840245531615feca22cf70da0b7eda1f529da529adab251d9`
- Scenario fingerprint: `9730652fe078fd870c36b2e72115986b1a3d2e1f001d4ac2c405449a9b7bd8ec`

## Summary

| Metric | Value |
| --- | --- |
| Scenarios | 97/109 (89.0%) |
| Assertions | 372/384 (96.9%) |
| Critical failures | 0 |
| Quality (mean rubric) | n/a |
| Rubric criteria | 0/0 (n/a) |
| Model time (sum) | 0 ms |
| Model latency (mean) | 0 ms |
| Completion tokens/s | n/a |
| Statuses | {'passed': 97, 'failed': 12} |

## Categories

| Category | Scenarios | Passed | Assertions | Assertions passed |
| --- | ---: | ---: | ---: | ---: |
| agent_loop | 6 | 6 | 58 | 58 |
| approvals | 4 | 4 | 38 | 38 |
| inbox | 3 | 3 | 35 | 35 |
| memory | 12 | 12 | 45 | 45 |
| modules | 4 | 4 | 42 | 42 |
| routing | 65 | 53 | 69 | 57 |
| runtime | 7 | 7 | 43 | 43 |
| scheduling | 3 | 3 | 23 | 23 |
| subagents | 5 | 5 | 31 | 31 |

## Scenarios

| Scenario | Mode | Repeat | Status | Wall | Model | Iter | Tools |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: |
| `a_plain_reply` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 0 |
| `a_tool_lifecycle` | deterministic | 1 | passed | 26 ms | 0 ms | 2 | 1 |
| `a_unknown_tool_denied` | deterministic | 1 | passed | 26 ms | 0 ms | 2 | 1 |
| `a_invalid_arguments` | deterministic | 1 | passed | 24 ms | 0 ms | 2 | 1 |
| `a_waiting_user_restart` | deterministic | 1 | passed | 33 ms | 0 ms | 2 | 0 |
| `a_malformed_reply_bounded` | deterministic | 1 | passed | 26 ms | n/a | n/a | n/a |
| `a_tool_call_limit` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 1 |
| `b_memory_scope` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 0 |
| `b_memory_correction` | deterministic | 1 | passed | 40 ms | 0 ms | 4 | 2 |
| `b_memory_archive_restore` | deterministic | 1 | passed | 27 ms | n/a | n/a | n/a |
| `b_search_knowledge` | deterministic | 1 | passed | 34 ms | 0 ms | 2 | 1 |
| `c_create_domain_approve` | deterministic | 1 | passed | 28 ms | 0 ms | 2 | 1 |
| `c_create_domain_reject` | deterministic | 1 | passed | 27 ms | 0 ms | 2 | 0 |
| `c_quick_confirm_yes` | deterministic | 1 | passed | 31 ms | 0 ms | 2 | 1 |
| `c_ambiguous_reply_does_not_execute` | deterministic | 1 | passed | 25 ms | 0 ms | 1 | 0 |
| `d_schedule_due_and_idempotent` | deterministic | 1 | passed | 27 ms | n/a | n/a | n/a |
| `d_missed_policy_run_once` | deterministic | 1 | passed | 27 ms | n/a | n/a | n/a |
| `d_missed_policy_skip` | deterministic | 1 | passed | 27 ms | n/a | n/a | n/a |
| `d_inbox_idempotent` | deterministic | 1 | passed | 29 ms | n/a | n/a | n/a |
| `d_inbox_automation_cooldown` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 0 |
| `d_quiet_hours_defer_then_triage` | deterministic | 1 | passed | 26 ms | n/a | n/a | n/a |
| `e_skill_propose_activate_disclose` | deterministic | 1 | passed | 51 ms | 0 ms | 4 | 1 |
| `e_skill_edit_requires_approval` | deterministic | 1 | passed | 40 ms | 0 ms | 2 | 1 |
| `e_skill_taint_refusal` | deterministic | 1 | passed | 32 ms | 0 ms | 2 | 1 |
| `e_mcp_round_trip_and_disable` | deterministic | 1 | passed | 151 ms | 0 ms | 4 | 2 |
| `f_delegate_result_return` | deterministic | 1 | passed | 31 ms | 0 ms | 3 | 1 |
| `f_child_context_isolation` | deterministic | 1 | passed | 34 ms | 0 ms | 4 | 1 |
| `f_child_confirm_blocked` | deterministic | 1 | passed | 38 ms | 0 ms | 3 | 2 |
| `f_same_goal_reuse` | deterministic | 1 | passed | 36 ms | 0 ms | 4 | 2 |
| `f_child_tool_budget` | deterministic | 1 | passed | 30 ms | 0 ms | 3 | 2 |
| `h_loop_remember_then_search` | deterministic | 1 | passed | 28 ms | 0 ms | 3 | 2 |
| `h_loop_denied_then_recovery` | deterministic | 1 | passed | 27 ms | 0 ms | 3 | 2 |
| `h_loop_invalid_then_recovery` | deterministic | 1 | passed | 27 ms | 0 ms | 3 | 2 |
| `h_loop_resumes_after_approval` | deterministic | 1 | passed | 30 ms | 0 ms | 3 | 2 |
| `h_loop_budget_exhaustion` | deterministic | 1 | passed | 27 ms | 0 ms | 2 | 2 |
| `h_loop_batch_deferred` | deterministic | 1 | passed | 28 ms | 0 ms | 2 | 1 |
| `k_memory_recall.general_allergy` | deterministic | 1 | passed | 28 ms | 0 ms | 1 | 0 |
| `k_memory_recall.general_protein` | deterministic | 1 | passed | 35 ms | 0 ms | 1 | 0 |
| `k_memory_recall.health_clothing_preference` | deterministic | 1 | passed | 35 ms | 0 ms | 1 | 0 |
| `k_memory_recall.clothing_health_allergy` | deterministic | 1 | passed | 36 ms | 0 ms | 1 | 0 |
| `k_memory_recall.general_units` | deterministic | 1 | passed | 37 ms | 0 ms | 1 | 0 |
| `k_memory_recall.clothing_health_protein` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 0 |
| `k_memory_recall.general_shoe_size` | deterministic | 1 | passed | 29 ms | 0 ms | 1 | 0 |
| `k_memory_recall.health_units` | deterministic | 1 | passed | 28 ms | 0 ms | 1 | 0 |
| `k_routing.health_name` | deterministic | 1 | passed | 26 ms | 0 ms | 1 | 0 |
| `k_routing.health_nutrition` | deterministic | 1 | passed | 25 ms | 0 ms | 1 | 0 |
| `k_routing.health_calories` | deterministic | 1 | passed | 25 ms | 0 ms | 1 | 0 |
| `k_routing.health_protein` | deterministic | 1 | passed | 27 ms | 0 ms | 1 | 0 |
| `k_routing.health_diet` | deterministic | 1 | passed | 26 ms | 0 ms | 1 | 0 |
| `k_routing.health_sleep` | deterministic | 1 | passed | 26 ms | 0 ms | 1 | 0 |
| `k_routing.health_exercise` | deterministic | 1 | passed | 25 ms | 0 ms | 1 | 0 |
| `k_routing.health_symptoms` | deterministic | 1 | failed | 25 ms | 0 ms | 1 | 0 |
| `k_routing.health_healthy` | deterministic | 1 | passed | 30 ms | 0 ms | 1 | 0 |
| `k_routing.health_tired` | deterministic | 1 | failed | 33 ms | 0 ms | 1 | 0 |
| `k_routing.home_name` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 0 |
| `k_routing.home_lights` | deterministic | 1 | passed | 36 ms | 0 ms | 1 | 0 |
| `k_routing.home_thermostat` | deterministic | 1 | passed | 38 ms | 0 ms | 1 | 0 |
| `k_routing.home_scene` | deterministic | 1 | passed | 29 ms | 0 ms | 1 | 0 |
| `k_routing.home_sensor` | deterministic | 1 | passed | 26 ms | 0 ms | 1 | 0 |
| `k_routing.home_kitchen_lights` | deterministic | 1 | passed | 25 ms | 0 ms | 1 | 0 |
| `k_routing.home_heating` | deterministic | 1 | passed | 27 ms | 0 ms | 1 | 0 |
| `k_routing.home_automate_paraphrase` | deterministic | 1 | passed | 28 ms | 0 ms | 1 | 0 |
| `k_routing.language_name` | deterministic | 1 | passed | 33 ms | 0 ms | 1 | 0 |
| `k_routing.language_spanish` | deterministic | 1 | passed | 33 ms | 0 ms | 1 | 0 |
| `k_routing.language_french_quiz` | deterministic | 1 | passed | 34 ms | 0 ms | 1 | 0 |
| `k_routing.language_flashcards` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 0 |
| `k_routing.language_vocabulary` | deterministic | 1 | passed | 30 ms | 0 ms | 1 | 0 |
| `k_routing.language_grammar` | deterministic | 1 | passed | 29 ms | 0 ms | 1 | 0 |
| `k_routing.language_pronunciation` | deterministic | 1 | passed | 25 ms | 0 ms | 1 | 0 |
| `k_routing.language_practice_paraphrase` | deterministic | 1 | passed | 26 ms | 0 ms | 1 | 0 |
| `k_routing.clothing_name` | deterministic | 1 | passed | 27 ms | 0 ms | 1 | 0 |
| `k_routing.clothing_outfit_wedding` | deterministic | 1 | passed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.clothing_shoes_dress` | deterministic | 1 | passed | 27 ms | 0 ms | 1 | 0 |
| `k_routing.clothing_jacket` | deterministic | 1 | passed | 29 ms | 0 ms | 1 | 0 |
| `k_routing.clothing_weather` | deterministic | 1 | passed | 28 ms | 0 ms | 1 | 0 |
| `k_routing.clothing_match` | deterministic | 1 | passed | 26 ms | 0 ms | 1 | 0 |
| `k_routing.clothing_coat` | deterministic | 1 | passed | 27 ms | 0 ms | 1 | 0 |
| `k_routing.clothing_dressy` | deterministic | 1 | failed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.general_ambiguous_plan` | deterministic | 1 | passed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.general_ambiguous_update` | deterministic | 1 | passed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.general_ambiguous_organize` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 0 |
| `k_routing.general_ambiguous_weekend` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 0 |
| `k_routing.general_capital` | deterministic | 1 | passed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.general_python` | deterministic | 1 | passed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.general_email` | deterministic | 1 | passed | 34 ms | 0 ms | 1 | 0 |
| `k_routing.general_car_engine` | deterministic | 1 | passed | 34 ms | 0 ms | 1 | 0 |
| `k_routing.general_meetup_budget` | deterministic | 1 | passed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.general_world_cup` | deterministic | 1 | passed | 35 ms | 0 ms | 1 | 0 |
| `k_routing.general_tokyo_time` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 0 |
| `k_routing.general_meeting_notes` | deterministic | 1 | passed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.learned_memory_only` | deterministic | 1 | passed | 33 ms | 0 ms | 1 | 0 |
| `k_routing.learned_past_task_only` | deterministic | 1 | passed | 34 ms | 0 ms | 1 | 0 |
| `k_routing.learned_single_term_control` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 0 |
| `k_routing.learned_recent_message_control` | deterministic | 1 | passed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.spanish_calories` | deterministic | 1 | failed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.spanish_lights` | deterministic | 1 | failed | 30 ms | 0 ms | 1 | 0 |
| `k_routing.spanish_greeting` | deterministic | 1 | failed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.spanish_rain` | deterministic | 1 | failed | 33 ms | 0 ms | 1 | 0 |
| `k_routing.spanish_protein` | deterministic | 1 | failed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.confusion_jacket_run` | deterministic | 1 | passed | 36 ms | 0 ms | 1 | 0 |
| `k_routing.confusion_workout_cold` | deterministic | 1 | failed | 38 ms | 0 ms | 1 | 0 |
| `k_routing.confusion_thermostat_workout` | deterministic | 1 | passed | 39 ms | 0 ms | 1 | 0 |
| `k_routing.followup_health_dinner` | deterministic | 1 | failed | 37 ms | 0 ms | 2 | 0 |
| `k_routing.followup_clothing_dinner` | deterministic | 1 | failed | 39 ms | 0 ms | 2 | 0 |
| `k_routing.followup_home_kitchen` | deterministic | 1 | passed | 39 ms | 0 ms | 2 | 0 |
| `k_routing.followup_language_again` | deterministic | 1 | failed | 36 ms | 0 ms | 2 | 0 |
| `k_routing.general_photosynthesis` | deterministic | 1 | passed | 35 ms | 0 ms | 1 | 0 |
| `k_routing.general_sql` | deterministic | 1 | passed | 31 ms | 0 ms | 1 | 0 |
| `k_routing.general_cat_name` | deterministic | 1 | passed | 32 ms | 0 ms | 1 | 0 |

## Failures

### k_routing.health_symptoms (turn #1)

- Check: `s1.routing.domain`
- Expected: `Health`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Health'

### k_routing.health_tired (turn #1)

- Check: `s1.routing.domain`
- Expected: `Health`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Health'

### k_routing.clothing_dressy (turn #1)

- Check: `s1.routing.domain`
- Expected: `Clothing`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Clothing'

### k_routing.spanish_calories (turn #1)

- Check: `s1.routing.domain`
- Expected: `Health`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Health'

### k_routing.spanish_lights (turn #1)

- Check: `s1.routing.domain`
- Expected: `Home automation`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Home automation'

### k_routing.spanish_greeting (turn #1)

- Check: `s1.routing.domain`
- Expected: `Language learning`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Language learning'

### k_routing.spanish_rain (turn #1)

- Check: `s1.routing.domain`
- Expected: `Clothing`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Clothing'

### k_routing.spanish_protein (turn #1)

- Check: `s1.routing.domain`
- Expected: `Health`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Health'

### k_routing.confusion_workout_cold (turn #1)

- Check: `s1.routing.domain`
- Expected: `Health`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Health'

### k_routing.followup_health_dinner (turn #2)

- Check: `s2.routing.domain`
- Expected: `Health`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Health'

### k_routing.followup_clothing_dinner (turn #2)

- Check: `s2.routing.domain`
- Expected: `Clothing`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Clothing'

### k_routing.followup_language_again (turn #2)

- Check: `s2.routing.domain`
- Expected: `Language learning`
- Observed: `{"id": "general", "name": "General"}`
- Detail: routed domain was 'General', expected 'Language learning'

## Interpretation

- Assertions read persisted harness state (tasks, runs, memories, approvals, schedules, inbox, modules, executions), not just reply text.
- Deterministic mode scripts the model; it measures harness behavior, not language quality or model judgment.
- Live mode measures the named model through the real runtime; it is not a general capability benchmark.
- Optional rubric scores stay separate from assertions; required quality thresholds are assertions and can change scenario status.

## Configuration

```yaml
model:
  base_url: http://192.168.50.40:8181
  name: qwen3.5-9b
  api_key: ''
  provider: ''
  allowed: []
  chat_path: /v1/chat/completions
  models_path: /v1/models
  timeout_s: 180.0
  connect_retries: 1
  retry_backoff_s: 0.2
  max_tokens: 2048
  temperature: 0.2
  enable_thinking: true
  thinking_kwargs: true
  context_window: 32768
  empty_retry_max_tokens: 4096
limits:
  max_iterations: 16
  max_tool_calls: 24
  max_run_seconds: 600.0
  max_tool_result_chars: 6000
context:
  total_budget_tokens: 20000
  reserved_output_tokens: 4096
  tool_schema_tokens: 4096
  recent_messages: 24
  memory_results: 6
  history_results: 4
  section_budgets:
    persona: 1200
    state: 1500
    memories: 1800
    history: 2500
    skills: 800
    guidance: 900
scheduler:
  enabled: false
  tick_interval_s: 3600.0
  max_runs_per_tick: 1
  max_retries_per_tick: 1
  max_schedules_per_task: 5
  max_schedule_depth: 2
  missed_policy: run_once
  missed_grace_s: 900.0
  resume_interrupted: true
  retry_cooldown_s: 300.0
  retry_backoff_base_s: 300.0
  retry_backoff_max_s: 3600.0
  max_task_retries: 3
  retry_permanent: false
  user_activity_grace_s: 60.0
  approval_notify_s: 1000000.0
  paused_notify_s: 1000000.0
memory:
  max_results: 8
  temp_expiry_days: 14
  search_limit: 10
  cross_domain: search
  cross_domain_max: 3
  core_profile_chars: 1200
  lifetime:
    enabled: false
    base_days: 30
    growth: 0.5
    min_days: 14
    max_days: 1095
    protected_kinds:
    - constraint
    - decision
    - preference
    sweep_interval_s: 3600.0
    sweep_batch: 200
modules:
  enabled: true
  dir: modules
  health_interval_s: 300.0
  tool_schema_budget_tokens: 4096
  default_mcp_risk: confirm
  lazy_start: true
  command_allowlist:
  - python
  - python3
  - node
  - npx
  - uvx
skills:
  enabled: true
  auto_draft: false
  draft_after_successful_tasks: 5
  max_drafts_per_day: 2
  max_body_chars: 6000
subagents:
  enabled: true
  max_depth: 1
  max_children_per_run: 3
  max_iterations: 8
  max_tool_calls: 12
  max_seconds: 180.0
  max_result_chars: 4000
  allow_confirm_risk: false
approvals:
  expires_after_s: 604800.0
  classifier_max_tokens: 16
  classifier_thinking: false
```

## Notes

- deterministic mode uses ScriptedModel; behavior and persistence are real, language quality is not measured
