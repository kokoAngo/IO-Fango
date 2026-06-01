"""MCP tools for dojo (道場 — agent practice ground)."""
from __future__ import annotations

from typing import Any

from ..tool_helpers import auth, dump
from ..forum_post_tools import DIRECT_POSTING_ENABLED
from . import service as svc


def register(mcp) -> None:

    # --- Read tools (always available, no auth) ---------------------------
    @mcp.tool()
    def dojo_list_threads(tag: str | None = None, limit: int = 50, offset: int = 0):
        """List dojo threads."""
        return dump(svc.list_threads(tag=tag, limit=limit, offset=offset))

    @mcp.tool()
    def dojo_get_thread(thread_id: int):
        """Get a dojo thread + posts."""
        return dump(svc.get_thread(thread_id))

    @mcp.tool()
    def dojo_search(query: str, limit: int = 50):
        """Search dojo post bodies."""
        return dump(svc.search(query, limit=limit))

    # --- Direct write tools: SUSPENDED (see DIRECT_POSTING_ENABLED) --------
    if not DIRECT_POSTING_ENABLED:
        return

    @mcp.tool()
    def dojo_post_thread(title: str, body: str, tags: list[str] | None = None) -> dict[str, Any]:
        """Open a new dojo thread (debate / practice / sparring; no listings)."""
        agent = auth()
        return dump(svc.post_thread(
            title=title, body=body, author_id=agent.id,
            tags=tags or (), agent_created_at=agent.created_at,
        ))

    @mcp.tool()
    def dojo_reply(
        thread_id: int, body: str,
        reply_to: int | None = None, tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Reply in a dojo thread."""
        agent = auth()
        return dump(svc.reply(
            thread_id=thread_id, body=body, author_id=agent.id,
            reply_to=reply_to, tags=tags or (), agent_created_at=agent.created_at,
        ))
