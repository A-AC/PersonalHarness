"""Capability modules: skills, MCP servers, and web as one record shape.

Phase 0 adds the package, record types, and the ``load_all`` seam. The
existing skill-directory and MCP-file loaders remain authoritative until
Phase 1 replaces them through the registry.
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
