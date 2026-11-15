"""Capability modules: skills and MCP servers as one record shape.

The registry is authoritative for both: ``load_all`` imports configured MCP
servers, reloads enabled modules, and imports skills. Skills contribute
disclosure text only; MCP servers contribute tools.
"""

from .registry import ModuleRegistry, load_all
from .types import (
    KINDS,
    ORIGINS,
    SCOPES,
    STATUSES,
    Module,
    ModuleBinding,
    ModuleTool,
)

__all__ = [
    "KINDS",
    "ORIGINS",
    "SCOPES",
    "STATUSES",
    "Module",
    "ModuleBinding",
    "ModuleRegistry",
    "ModuleTool",
    "load_all",
]
