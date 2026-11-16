# Web search MCP server (SearXNG)

A standalone stdio MCP server over SearXNG's JSON API. It imports nothing
from the harness and needs no harness code changes.

Only `web_search` is provided. `web_fetch` is deliberately absent: fetching
arbitrary URLs is an SSRF and prompt-injection surface and is deferred until
it can be allowlisted and guarded.

## Configuration

All settings belong to this server. Copy `web.example.json` and point the MCP
entry at it with `--config`:

```json
{
  "searxng_url": "http://127.0.0.1:8080",
  "max_results": 5,
  "timeout_s": 10,
  "categories": "general",
  "safe_search": 1,
  "block_private_hosts": true
}
```

| Key | Meaning |
| --- | --- |
| `searxng_url` | SearXNG base URL (required) |
| `max_results` | Results per query (1-10, default 5) |
| `timeout_s` | HTTP timeout per request (default 10) |
| `categories` | Default SearXNG category (default `general`) |
| `safe_search` | 0, 1, or 2 (default 1) |
| `block_private_hosts` | Drop result URLs on private/loopback hosts (default true) |

A TOML file with the same keys works too. Neutral `WEB_SEARXNG_URL`,
`WEB_MAX_RESULTS`, `WEB_TIMEOUT_S`, `WEB_CATEGORIES`, `WEB_SAFE_SEARCH`, and
`WEB_BLOCK_PRIVATE_HOSTS` environment variables override the file when set
(container/secret friendly); legacy `HARNESS_WEB_*` names are still accepted.
The file supplies the defaults.

Check configuration without starting the JSON-RPC loop:

```bash
python harness/modules/web/server.py \
  --config harness/modules/web/web.example.json --probe
```

## Registering with the harness

Copy `web.example.json` to `<data_dir>/modules/mcp/web.json` and edit it. The
harness knows only the MCP entry below; it never reads the config file and
stores no SearXNG URL of its own. Add it to `data/mcp.json` (the standard
`mcpServers` shape, auto-detected) or to the file named by
`tools.mcp_servers_file` (see `examples/mcp.json` and
`examples/mcp_servers.yaml`):

```json
{
  "mcpServers": {
    "web": {
      "type": "stdio",
      "command": "python",
      "args": [
        "harness/modules/web/server.py",
        "--config",
        "${data_dir}/modules/mcp/web.json"
      ],
      "tool_prefix": "",
      "tools_allow": ["web_search"],
      "risk_default": "low",
      "autoapprove": true,
      "trust_annotations": true,
      "read_only": true,
      "side_effects": false,
      "startup": "lazy",
      "timeout_s": 60
    }
  }
}
```

`${data_dir}` expands to the absolute data directory, so the same entry works
locally and in Docker. `tool_prefix: ""` registers `web_search` verbatim; omit
it to get the default `mcp_web_web_search`. Every result is untrusted data.
After one enters a run, a side-effecting tool still needs approval even if it
was auto-approved.

In Docker, mount the config read-only and use the container data path
(`/data/modules/mcp/web.json` via `${data_dir}`):

```yaml
    volumes:
      - harness-data:/data
      - ./web.json:/data/modules/mcp/web.json:ro
```

## Safety notes

- Results are capped (10 results, 200-char titles, 500-char URLs, 400-char
  snippets) and marked untrusted.
- Text is normalised (NFKC, control/zero-width/bidi characters and HTML tags
  removed, whitespace collapsed). Only `http`/`https` result URLs survive;
  `javascript:`, `data:`, `file:`, userinfo URLs, and (by default) private or
  local hosts are dropped.
- Each result carries heuristic `flags` (instruction override, role/tool
  spoofing, encoded payload, exfiltration request, urgency, hidden markup or
  unicode, suspicious URL). A flagged snippet is replaced with a short
  placeholder; the flag is a label, never an instruction.
- Output is structured data with a per-call `nonce`, a `notice`, and
  `flagged`/`dropped` counts. Queries are capped at 200 characters before the
  SearXNG request. The harness wraps every untrusted tool result in its own
  nonce-delimited envelope before it enters the transcript.
- The server never fetches a URL supplied by the model; it only queries the
  configured SearXNG instance.
