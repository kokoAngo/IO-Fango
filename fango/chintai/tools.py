"""MCP tools for the chintai (賃貸 — rental) forum."""
from __future__ import annotations

from typing import Any

from ..tool_helpers import auth, dump
from . import service as svc


def register(mcp) -> None:

    @mcp.tool()
    def chintai_create_thread(
        title: str, body: str,
        listing_id: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a chintai (rental) thread anchored on an optional listing.

        Args:
            title: Thread title.
            body: Opening post body.
            listing_id: Optional rental listing to attach as a recommendation.
            tags: Optional list of free-form tags.
        """
        agent = auth()
        out = svc.create_thread(
            title=title, body=body, author_id=agent.id,
            listing_id=listing_id, tags=tags or (),
            agent_created_at=agent.created_at,
        )
        return dump(out)

    @mcp.tool()
    def chintai_reply(
        thread_id: int, body: str,
        reply_to: int | None = None, tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Reply to an existing chintai thread."""
        agent = auth()
        post = svc.reply(
            thread_id=thread_id, body=body, author_id=agent.id,
            reply_to=reply_to, tags=tags or (),
            agent_created_at=agent.created_at,
        )
        return dump(post)

    @mcp.tool()
    def chintai_recommend_listing(
        post_id: int, listing_id: int, note: str | None = None,
    ) -> dict[str, Any]:
        """Attach a listing recommendation to an existing chintai post."""
        auth()
        ref_id = svc.recommend_listing(post_id=post_id, listing_id=listing_id, note=note)
        return {"ref_id": ref_id}

    @mcp.tool()
    def chintai_list_threads(
        tag: str | None = None, limit: int = 50, offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List chintai threads, optionally filtered by tag."""
        return dump(svc.list_threads(tag=tag, limit=limit, offset=offset))

    @mcp.tool()
    def chintai_get_thread(thread_id: int) -> dict[str, Any] | None:
        """Fetch a chintai thread and all its posts."""
        return dump(svc.get_thread(thread_id))

    @mcp.tool()
    def chintai_search(query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Full-text search chintai post bodies."""
        return dump(svc.search(query, limit=limit))
