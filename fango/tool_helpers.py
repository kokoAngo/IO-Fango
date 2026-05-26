"""Shared tool-side helpers: agent resolution + serialization."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from .auth import require_agent
from .models import Agent


def auth() -> Agent:
    """Resolve the current agent for an MCP tool call; raises AuthError if missing."""
    return require_agent()


def dump(obj: Any) -> Any:
    """JSON-safe representation. Dataclasses → dict; lists/dicts recurse."""
    if obj is None:
        return None
    if is_dataclass(obj):
        return {k: dump(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [dump(x) for x in obj]
    if isinstance(obj, tuple):
        return [dump(x) for x in obj]
    if isinstance(obj, dict):
        return {k: dump(v) for k, v in obj.items()}
    return obj
