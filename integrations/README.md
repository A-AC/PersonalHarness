# Integrations: inbox pushers

There is deliberately **no mailbox, IMAP, Telegram, Matrix, or webhook adapter
inside the harness process**. The durable contract is the authenticated inbox
API. A mailbox watcher or messaging bridge is a separate **pusher**: its own
process, credentials, reconnect logic, and lifecycle.

This keeps the one-runtime invariant intact: a pusher can only store an event;
the harness decides whether and how to act on it.

## Contract

```
POST /api/inbox
Authorization: Bearer <server.token>
Content-Type: application/json

{
  "source": "mail",
  "external_id": "message-42",
  "kind": "message",
  "severity": "info",
  "title": "Invoice due tomorrow",
  "body": "...",
  "meta": {"from": "billing@example.invalid"}
}
```

- `source` (required): stable pusher name, e.g. `mail`, `telegram`, `matrix`.
- `external_id` (required): stable id from the source. `(source,
  external_id)` is the **idempotency key**; retries must reuse it.
- `title` (required), `body` (optional): bounded external content. Bodies
  over `inbox.max_body_chars` are rejected with `413`, never truncated.
- `kind` (default `event`): free-form bounded label for rule matching.
- `severity` (default `info`): display metadata only.
- `meta` (optional JSON object): bounded pusher metadata, not instructions.

Success returns `{accepted, id, duplicate, event}`. A duplicate returns the
original id with `accepted: false, duplicate: true`; do **not** treat it as an
error and do not retry the same event with a different id.

Rejected requests return `400` (malformed/missing fields) or `413`
(oversized). Retry transport failures and `5xx`; do not retry `4xx` unchanged.

## Trust and side effects

Every stored event has `trust: "untrusted"`. The title/body are data, never
instructions. A task derived from an event carries `tainted_sources`, so after
the content enters the run, any side-effecting tool is escalated to
confirm-risk and waits for explicit approval. Read-only work may continue.

A pusher cannot approve actions, widen tool permissions, or bypass rules.

## Rules

Rules are declarative: match `source` and/or `kind`, optional keyword/regex
filters, then choose one action.

```http
POST /api/automations
Authorization: Bearer <server.token>
Content-Type: application/json

{"name":"billing mail","source":"mail","kind":"message",
 "match_keywords":["invoice"],"action":"task","cooldown_s":300,
 "max_per_hour":6}
```

Actions:

- `task`: create a normal task (`origin='inbox:<source>'`) and run it within
  the heartbeat's shared model budget.
- `notify`: write one visible system note; no model call.
- `ignore`: record the event and take no action.

An event no enabled rule owns uses `automations.default_action` (default
`notify`). Identical unclaimed content is deduped for
`inbox.dedupe_window_days`.

## Loop safety

- `cooldown_s` suppresses a rule's repeated fires inside the window.
- `automations.max_per_hour` and the rule's `max_per_hour` form an effective
  hourly cap. Hitting it disables the rule, records `automation.disabled`, and
  writes exactly one visible note. Re-enable the rule after the source is
  under control.
- `inbox.quiet_hours` (local time, e.g. `"22:00-07:00"`) leaves events `new`
  so a later heartbeat triages them. It never drops an event.
- `inbox.max_events_per_tick` bounds how many events one pass examines.

Pushers should still bound their own retry/backoff. The harness provides
durable idempotency and loop breakers, not a queue-consumer protocol.

## Suggested pusher shape

1. Authenticate to the source with credentials only the pusher knows.
2. Normalize each item into the contract above.
3. Derive `external_id` from the source's stable id.
4. `POST` with a bounded retry/backoff; treat `duplicate: true` as success.
5. Persist a cursor/checkpoint so a restart resumes safely.
6. Never send commands, approvals, or tool arguments through the body.

See `examples/inbox_curl.sh` for a runnable request and duplicate check.
