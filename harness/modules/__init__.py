"""Capability modules: skills, MCP servers, and web as one record shape.

Phase 1 makes the registry authoritative for MCP and web modules:
``load_all`` imports legacy MCP files, installs the system web module, and
reloads enabled modules. Skills remain directory-based until Phase 2.
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
