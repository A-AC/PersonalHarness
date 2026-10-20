#!/usr/bin/env bash
# Overnight variant runs. Rerun-safe: skips tags that already have results.
cd /home/alx/assistant-prototype || exit 1

PYTHON="${PYTHON:-python}"
REPEATS="${REPEATS:-5}"
RESULTS_DIR="suite_results"
mkdir -p logs "$RESULTS_DIR"

# Start every run from the baseline config, regardless of the shell's env.
unset THINKING_BUDGET RESPONSE_FORMAT RETRIES TEMPERATURE SEED MAX_ITERATIONS MAX_TOKENS

# Abort early if the model server isn't up.
if ! curl -s -o /dev/null --max-time 5 http://127.0.0.1:8181/health; then
  echo "llama-server not reachable on :8181, aborting." >&2
  exit 1
fi

run() {
  local tag="$1"; shift
  if [ -f "$RESULTS_DIR/$tag.json" ]; then
    echo "[$(date '+%F %T')] SKIP  $tag (already done)"
    return
  fi
  echo "[$(date '+%F %T')] START $tag  ($*)"
  env "$@" "$PYTHON" qwen_agent_suite.py --repeats "$REPEATS" --tag "$tag" > "logs/$tag.log" 2>&1
  local rc=$?
  echo "[$(date '+%F %T')] END   $tag  exit=$rc"
}

# 0. Baseline
run baseline

# 1. Thinking budget
run think_0     THINKING_BUDGET=0
run think_64    THINKING_BUDGET=64
run think_512   THINKING_BUDGET=512
run think_1024  THINKING_BUDGET=1024
run think_unset THINKING_BUDGET=off

# 2. Output constraint
run schema      RESPONSE_FORMAT=json_schema

# 3. Bounded retry
run retry_1     RETRIES=1
run retry_2     RETRIES=2

# 4. Temperature
run temp_0      TEMPERATURE=0
run temp_06     TEMPERATURE=0.6
run temp_10     TEMPERATURE=1.0

# 5. Iteration limit
run iter_3      MAX_ITERATIONS=3
run iter_8      MAX_ITERATIONS=8

# 6. Output length cap
run maxtok_256  MAX_TOKENS=256
run maxtok_512  MAX_TOKENS=512

# 7. Fixed-seed reproducibility (3 repeats each)
REPEATS=3 run seed_42a SEED=42
REPEATS=3 run seed_42b SEED=42

# Comparisons against the baseline
{
  for t in think_0 think_64 think_512 think_1024 think_unset schema retry_1 retry_2 \
           temp_0 temp_06 temp_10 iter_3 iter_8 maxtok_256 maxtok_512; do
    echo "=============== baseline vs $t ==============="
    "$PYTHON" qwen_agent_suite.py --compare baseline "$t"
  done
  echo "=============== seed_42a vs seed_42b ==============="
  "$PYTHON" qwen_agent_suite.py --compare seed_42a seed_42b
} > compare_all.txt 2>&1

echo "[$(date '+%F %T')] ALL DONE. See compare_all.txt and logs/"
