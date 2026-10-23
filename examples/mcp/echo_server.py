"""Minimal stdio MCP server used by examples/mcp_servers.yaml."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("example-echo")


@server.tool()
def echo(text: str) -> str:
    """Return the supplied text unchanged."""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
