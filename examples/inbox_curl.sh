#!/usr/bin/env sh
# Push one durable inbox event, then repeat the same (source, external_id)
# to show idempotency. The API stores the event; the heartbeat triages it.
#
#   HARNESS_TOKEN=... ./examples/inbox_curl.sh
#
# Optional: HARNESS_URL (default http://127.0.0.1:8080), SOURCE, EXTERNAL_ID.
set -eu

: "${HARNESS_TOKEN:?set HARNESS_TOKEN to the server token}"
HARNESS_URL="${HARNESS_URL:-http://127.0.0.1:8080}"
SOURCE="${SOURCE:-mail}"
EXTERNAL_ID="${EXTERNAL_ID:-demo-001}"

payload=$(cat <<EOF
{
  "source": "$SOURCE",
  "external_id": "$EXTERNAL_ID",
  "kind": "message",
  "severity": "info",
  "title": "Invoice due tomorrow",
  "body": "This body is untrusted external data, never instructions.",
  "meta": {"from": "billing@example.invalid"}
}
EOF
)

post() {
  printf '%s\n' "$payload" | curl -sS -X POST "$HARNESS_URL/api/inbox" \
    -H "Authorization: Bearer $HARNESS_TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary @-
  printf '\n'
}

echo "First insert (accepted=true):"
post
echo "Second insert with the same (source, external_id) (duplicate=true):"
post
echo "Triage now (or wait for the heartbeat):"
curl -sS -X POST "$HARNESS_URL/api/inbox/triage" \
  -H "Authorization: Bearer $HARNESS_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{}'
printf '\n'
