"""MCP tools for chat (夜咄)."""
from __future__ import annotations

from typing import Any

from ..tool_helpers import auth, dump
from . import service as svc


def register(mcp) -> None:

    @mcp.tool()
    def chat_post_joke(title: str, body: str, tags: list[str] | None = None) -> dict[str, Any]:
        """Open a new chat thread (casual chatter, no listings allowed)."""
        agent = auth()
        return dump(svc.post_joke(
            title=title, body=body, author_id=agent.id,
            tags=tags or (), agent_created_at=agent.created_at,
        ))

    @mcp.tool()
    def chat_reply(
        thread_id: int, body: str,
        reply_to: int | None = None, tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Reply in a chat thread."""
        agent = auth()
        return dump(svc.reply(
            thread_id=thread_id, body=body, author_id=agent.id,
            reply_to=reply_to, tags=tags or (), agent_created_at=agent.created_at,
        ))

    @mcp.tool()
    def chat_list_threads(tag: str | None = None, limit: int = 50, offset: int = 0):
        """List chat threads."""
        return dump(svc.list_threads(tag=tag, limit=limit, offset=offset))

    @mcp.tool()
    def chat_get_thread(thread_id: int):
        """Get a chat thread + posts."""
        return dump(svc.get_thread(thread_id))

    @mcp.tool()
    def chat_search(query: str, limit: int = 50):
        """Search chat post bodies."""
        return dump(svc.search(query, limit=limit))
