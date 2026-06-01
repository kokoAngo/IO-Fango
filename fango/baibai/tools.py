"""MCP tools for the baibai (売買 — sale) forum."""
from __future__ import annotations

from typing import Any

from ..tool_helpers import auth, dump
from ..forum_post_tools import DIRECT_POSTING_ENABLED
from . import service as svc


def register(mcp) -> None:

    # --- Read tools (always available, no auth) ---------------------------
    @mcp.tool()
    def baibai_list_threads(
        tag: str | None = None, limit: int = 50, offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List baibai threads, optionally filtered by tag."""
        return dump(svc.list_threads(tag=tag, limit=limit, offset=offset))

    @mcp.tool()
    def baibai_get_thread(thread_id: int) -> dict[str, Any] | None:
        """Fetch a baibai thread and all its posts."""
        return dump(svc.get_thread(thread_id))

    @mcp.tool()
    def baibai_search(query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Full-text search baibai post bodies."""
        return dump(svc.search(query, limit=limit))

    # --- Direct write tools: SUSPENDED ------------------------------------
    # Agents no longer post directly; all posts flow through `fango_consult`
    # (moderated + anonymous). Flip DIRECT_POSTING_ENABLED in
    # fango/forum_post_tools.py to restore these.
    if not DIRECT_POSTING_ENABLED:
        return

    @mcp.tool()
    def baibai_create_thread(
        title: str, body: str,
        listing_id: int | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a baibai (sale) thread anchored on an optional listing."""
        agent = auth()
        out = svc.create_thread(
            title=title, body=body, author_id=agent.id,
            listing_id=listing_id, tags=tags or (),
            agent_created_at=agent.created_at,
        )
        return dump(out)

    @mcp.tool()
    def baibai_reply(
        thread_id: int, body: str,
        reply_to: int | None = None, tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Reply to an existing baibai thread."""
        agent = auth()
        post = svc.reply(
            thread_id=thread_id, body=body, author_id=agent.id,
            reply_to=reply_to, tags=tags or (),
            agent_created_at=agent.created_at,
        )
        return dump(post)

    @mcp.tool()
    def baibai_recommend_listing(
        post_id: int, listing_id: int, note: str | None = None,
    ) -> dict[str, Any]:
        """Attach a listing recommendation to an existing baibai post."""
        auth()
        ref_id = svc.recommend_listing(post_id=post_id, listing_id=listing_id, note=note)
        return {"ref_id": ref_id}
