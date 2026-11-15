# Calendar MCP server (CalDAV/Baikal)

A standalone stdio MCP server over CalDAV. It imports nothing from the
harness and needs no harness code changes: register it like any other MCP
server through `data/mcp.json` (standard `mcpServers` shape) or
`tools.mcp_servers_file` (see `examples/mcp.json` and
`examples/mcp_servers.yaml`).

Only CalDAV is supported. Every request is rooted at the configured DAV
collection; event hrefs come from discovery, so the model cannot fetch an
arbitrary URL. VEVENT only: no invitations, free/busy, VTODO, or
single-instance recurrence edits.

## Tools

| Tool | Effect |
| --- | --- |
| `calendar_list_calendars` | Discover calendars (name, slug, href, owner, shared, read-only) |
| `calendar_list_events` | List events in a range; optional `query` filter and `limit` |
| `calendar_get_event` | Fetch one event by `uid` |
| `calendar_create_event` | Create a one-off VEVENT (confirm-risk when registered normally) |
| `calendar_update_event` | Change summary/description/location/times, ETag-guarded |
| `calendar_delete_event` | Delete by UID, ETag-guarded |

The harness prefixes generic MCP tool names with the module name by default,
so a server entry named `calendar` exposes
`mcp_calendar_calendar_list_events`, `mcp_calendar_calendar_create_event`,
and so on. Set `tool_prefix: ""` in the entry to register the server's own
names (`calendar_list_events`, ...). `tools_allow`/`tools_deny` entries always
use the unprefixed names above.

## Configuration

Credentials live in a JSON or TOML file passed with `--config`, never in
environment variables, module rows, `config.yaml`, or SQLite. Copy
`calendar.example.json` and edit it:

```json
{
  "url": "http://127.0.0.1:8383/dav.php",
  "username": "your-baikal-user",
  "password": "replace-me",
  "calendar": "personal",
  "organizer": "",
  "timezone": "system",
  "max_results": 100,
  "max_range_days": 366,
  "timeout_s": 10,
  "read_only": false
}
```

| Key | Meaning |
| --- | --- |
| `url` | DAV collection, e.g. `http://host:8383/dav.php` (required) |
| `username` / `password` | CalDAV Basic-auth credentials (required) |
| `calendar` | Default calendar display name or URL slug |
| `organizer` | Optional `ORGANIZER` value used on create |
| `timezone` | IANA zone; `system`/`local` uses the host clock |
| `max_results` | Events per query (1-500, default 100) |
| `max_range_days` | Query range cap (default 366) |
| `timeout_s` | HTTP timeout per request (default 10) |
| `read_only` | `true` hides and refuses the write tools |

File values win. Neutral `CALENDAR_*` variables of the same meaning
(`URL`, `DAV_URL`, `USER`, `PASSWORD`, `NAME`, `ORGANIZER`, `TIMEOUT_S`,
`MAX_RESULTS`, `MAX_RANGE_DAYS`, `READ_ONLY`, `TZ`) exist only as a fallback
for standalone/scripted runs when no file is supplied; legacy
`HARNESS_CALENDAR_*` names are still accepted. Keep the file private:
it contains a clear-text password (`chmod 600`, owned by the process user).

Check credentials and discovery without starting the JSON-RPC loop:

```bash
python harness/modules/calendar/server.py \
  --config harness/modules/calendar/calendar.example.json --probe
```

## Registering with the harness

Copy `calendar.example.json` to `<data_dir>/modules/mcp/calendar.json` and edit
it. Then add this entry to `data/mcp.json` (the standard `mcpServers` shape,
auto-detected) or to the file named by `tools.mcp_servers_file`:

```json
{
  "mcpServers": {
    "calendar": {
      "type": "stdio",
      "command": "python",
      "args": [
        "harness/modules/calendar/server.py",
        "--config",
        "${data_dir}/modules/mcp/calendar.json"
      ],
      "tool_prefix": "",
      "risk_default": "confirm",
      "side_effects": true,
      "startup": "lazy",
      "timeout_s": 60
    }
  }
}
```

`${data_dir}` expands to the absolute data directory, so the same entry works
locally and in Docker. The import runs on startup and creates the
`mcp:calendar` module row once. After editing the entry, apply the change with
`python -m harness modules --reload` (reload also re-reads the file).

The generic MCP loader has one risk class per module, so choose a policy:

- **Reads only:** set `read_only: true` in the calendar config and, in the
  entry, `autoapprove: true`, `read_only: true`, `risk_default: low`. The
  server hides the write tools and the harness treats the rest as read-only,
  so reads do not need approval even after untrusted output taints the run.
- **Reads and writes:** keep the entry above (`risk_default: confirm`,
  `side_effects: true`) so every call needs approval, or set
  `autoapprove: true`/`risk_default: low` to auto-approve calls; after an
  untrusted result taints the run, side-effecting calls still need approval.

Restrict the surface with `tools_allow` (unprefixed names) when only some
operations are wanted, e.g. `calendar_list_calendars`,
`calendar_list_events`, `calendar_get_event`.

In Docker, mount the file read-only and use the container paths:

```yaml
    volumes:
      - harness-data:/data
      - ./calendar.json:/data/modules/mcp/calendar.json:ro
```

The image runs as UID 10001, so a host bind mount must be readable by that
user (`sudo chown 10001:10001 calendar.json && chmod 600 calendar.json`).
Prefer HTTPS to Baikal, or a trusted LAN/VPN: Basic auth sends the password
on every request.

## Safety notes

- Read results are low-risk/read-only; writes are confirm-risk and
  side-effecting under the recommended registration, and all results are
  untrusted data.
- Updates/deletes first find the record by exact UID and send `If-Match`, so
  an unknown UID never targets another event and a concurrent edit returns
  HTTP 412 instead of being clobbered.
- Responses are capped (4 MB HTTP body, 256 KB ICS, 500 events, clipped
  summary/description/location) and unknown custom `TZID` values fall back to
  the host timezone for display; writes are UTC.
- Recurring events are handled as a series: updates and deletes affect every
  occurrence.
