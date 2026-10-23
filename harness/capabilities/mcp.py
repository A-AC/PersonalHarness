"""Optional stdio MCP integration.

The harness never hardcodes an integration. A YAML file (path from settings)
declares the servers, their launch command, and the permission level for each
tool. Manifests are discovered at startup through the official MCP SDK.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from harness.constants import ExecutionStatus, PermissionLevel
from harness.contracts.capabilities import Manifest, OperationSpec


class McpConfigError(ValueError):
    pass


@dataclass
class McpServerSpec:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    permissions: dict[str, str] = field(default_factory=dict)
    default_permission: str = PermissionLevel.WRITE.value


def load_server_specs(path: Path) -> list[McpServerSpec]:
    if not path or not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise McpConfigError("MCP config must be a mapping with a servers list")
    rows = raw.get("servers") or []
    if not isinstance(rows, list):
        raise McpConfigError("MCP config servers must be a list")
    specs: list[McpServerSpec] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("name") or not row.get("command"):
            raise McpConfigError("each MCP server needs name and command")
        name = str(row["name"]).strip().lower()
        if not name.replace("-", "").replace("_", "").isalnum():
            raise McpConfigError(f"unsafe MCP server name: {name}")
        if name in seen:
            raise McpConfigError(f"duplicate MCP server name: {name}")
        seen.add(name)
        permissions = {str(k): str(v) for k, v in (row.get("permissions") or {}).items()}
        for value in permissions.values():
            if value not in {item.value for item in PermissionLevel}:
                raise McpConfigError(f"invalid permission level: {value}")
        default_permission = str(row.get("default_permission") or PermissionLevel.WRITE.value)
        if default_permission not in {item.value for item in PermissionLevel}:
            raise McpConfigError(f"invalid default_permission: {default_permission}")
        specs.append(
            McpServerSpec(
                name=name,
                command=str(row["command"]),
                args=[str(item) for item in (row.get("args") or [])],
                env={str(k): str(v) for k, v in (row.get("env") or {}).items()},
                permissions=permissions,
                default_permission=default_permission,
            )
        )
    return specs


def _type_name(schema: dict[str, Any]) -> str:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), "string")
    return {
        "string": "string",
        "number": "number",
        "integer": "number",
        "boolean": "boolean",
    }.get(str(kind), "string")


def manifest_from_tools(spec: McpServerSpec, tools: list[Any]) -> Manifest:
    operations: list[OperationSpec] = []
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
        properties = schema.get("properties") or {}
        inputs = {str(name): _type_name(value) for name, value in properties.items() if isinstance(value, dict)}
        required = [str(item) for item in (schema.get("required") or [])]
        operations.append(
            OperationSpec(
                name=str(tool.name),
                description=str(tool.description or ""),
                permission=spec.permissions.get(str(tool.name), spec.default_permission),
                inputs=inputs,
                required=required,
                idempotent=False,
            )
        )
    return Manifest(
        name=f"mcp_{spec.name}",
        purpose=f"MCP server {spec.name}",
        source=f"mcp:{spec.name}",
        operations=operations,
    )


async def _list_tools_async(spec: McpServerSpec, timeout: float) -> list[Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=spec.command, args=list(spec.args), env=dict(spec.env) or None)

    async def run() -> list[Any]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return list(result.tools)

    return await asyncio.wait_for(run(), timeout=timeout)


def load_mcp_manifests(path: Path, timeout: float) -> tuple[dict[str, Manifest], dict[str, McpServerSpec]]:
    manifests: dict[str, Manifest] = {}
    specs: dict[str, McpServerSpec] = {}
    for spec in load_server_specs(path):
        try:
            tools = asyncio.run(_list_tools_async(spec, timeout))
            manifest = manifest_from_tools(spec, tools)
        except Exception as exc:  # an unavailable server must not crash the harness
            manifest = Manifest(
                name=f"mcp_{spec.name}",
                purpose=f"MCP server {spec.name} (unavailable: {exc})",
                source=f"mcp:{spec.name}",
                operations=[],
                available=False,
            )
        manifests[manifest.name] = manifest
        specs[manifest.name] = spec
    return manifests, specs


async def _call_tool_async(spec: McpServerSpec, tool: str, arguments: dict[str, Any], timeout: float) -> Any:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=spec.command, args=list(spec.args), env=dict(spec.env) or None)

    async def run() -> Any:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool, arguments)

    return await asyncio.wait_for(run(), timeout=timeout)


def call_mcp_tool(spec: McpServerSpec, tool: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        result = asyncio.run(_call_tool_async(spec, tool, arguments, timeout))
    except asyncio.TimeoutError:
        return {
            "status": ExecutionStatus.UNKNOWN,
            "data": {},
            "message": "MCP call timed out after the request may have been sent",
            "error_category": "timeout_unknown",
        }
    except Exception as exc:
        return {"status": ExecutionStatus.FAILED, "data": {}, "message": str(exc), "error_category": "mcp_error"}
    texts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            texts.append(str(text))
    text = "\n".join(texts)
    if getattr(result, "is_error", False):
        return {
            "status": ExecutionStatus.FAILED,
            "data": {"content": text, "structured": getattr(result, "structured_content", None)},
            "message": text or "MCP tool returned an error",
            "error_category": "tool_error",
        }
    return {
        "status": ExecutionStatus.SUCCEEDED,
        "data": {"content": text, "structured": getattr(result, "structured_content", None)},
        "message": "ok",
    }
