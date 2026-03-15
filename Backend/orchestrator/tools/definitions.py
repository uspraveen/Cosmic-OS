"""Compatibility wrapper for orchestrator tool definitions."""
from __future__ import annotations

from typing import Any

from .registry import get_local_tool_definitions

ORCHESTRATOR_TOOLS: list[dict[str, Any]] = get_local_tool_definitions()


def get_tool_definitions() -> list[dict[str, Any]]:
    """Return a copy of the local orchestrator tool definitions."""
    return get_local_tool_definitions()
