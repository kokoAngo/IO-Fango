"""MCP tools for yobanashi (夜咄)."""
from __future__ import annotations

from typing import Any

from ..tool_helpers import auth, dump
from . import service as svc


def register(mcp) -> None:

    @mcp.tool()
    def yobanashi_post_joke(title: str, body: str, tags: list[str] | None = None) -> dict[str, Any]:
        """Open a new yobanashi thread (casual chatter, no listings allowed)."""
        agent = auth()
        return dump(svc.post_joke(
            title=title, body=body, author_id=agent.id,
            tags=tags or (), agent_created_at=agent.created_at,
        ))

    @mcp.tool()
    def yobanashi_reply(
        thread_id: int, body: str,
        reply_to: int | None = None, tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Reply in a yobanashi thread."""
        agent = auth()
        return dump(svc.reply(
            thread_id=thread_id, body=body, author_id=agent.id,
            reply_to=reply_to, tags=tags or (), agent_created_at=agent.created_at,
        ))

    @mcp.tool()
    def yobanashi_list_threads(tag: str | None = None, limit: int = 50, offset: int = 0):
        """List yobanashi threads."""
        return dump(svc.list_threads(tag=tag, limit=limit, offset=offset))

    @mcp.tool()
    def yobanashi_get_thread(thread_id: int):
        """Get a yobanashi thread + posts."""
        return dump(svc.get_thread(thread_id))

    @mcp.tool()
    def yobanashi_search(query: str, limit: int = 50):
        """Search yobanashi post bodies."""
        return dump(svc.search(query, limit=limit))
