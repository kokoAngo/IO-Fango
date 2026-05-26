"""MCP tools for wiki (read-only cross-forum)."""
from __future__ import annotations

from typing import Any

from ..tool_helpers import dump
from . import service as svc


def register(mcp) -> None:

    @mcp.tool()
    def wiki_lookup(keyword: str, limit_per_section: int = 20) -> dict[str, Any]:
        """Cross-forum keyword lookup: posts (all 4 forums) + listings + co-occurring tags."""
        return dump(svc.lookup(keyword, limit_per_section=limit_per_section))

    @mcp.tool()
    def wiki_catalog() -> dict[str, Any]:
        """Site-wide counts: threads per forum, total listings, active agents."""
        return dump(svc.catalog())
