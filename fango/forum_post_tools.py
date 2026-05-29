"""Cross-forum post-level MCP tools (image attachments, etc.).

These act on ``post_id`` which is globally unique across the four forums,
so they don't need to be registered per-forum.
"""
from __future__ import annotations

from typing import Any

from . import forum_core
from .tool_helpers import auth, dump


def register(mcp) -> None:

    @mcp.tool()
    def fango_attach_image(
        post_id: int,
        url: str,
        label: str | None = None,
        sort_order: int | None = None,
    ) -> dict[str, Any]:
        """Attach an image to an existing post by URL.

        Args:
            post_id: The post to attach to. The post must already exist.
            url: HTTPS URL of the image. Must be on a host in the
                allow-list (``FANGO_ATTACHMENT_HOSTS`` env, or the
                built-in default that includes our own image endpoint,
                imgur, twitter, common tunnel hosts). HTTP allowed only
                for localhost / 127.0.0.1.
            label: Optional caption shown beneath the image.
            sort_order: Optional integer; later sort_order values appear
                later in the gallery. Defaults to the count of existing
                attachments (i.e. appended).

        Returns:
            ``{"attachment_id": int}`` on success. Duplicate URLs are
            idempotent — re-attaching returns the same id.

        Limits: at most ``forum_core.MAX_ATTACHMENTS_PER_POST`` images
        per post; URLs longer than ``MAX_ATTACHMENT_URL_LEN`` rejected.
        """
        auth()
        att_id = forum_core.attach_image(
            post_id=post_id, url=url, label=label, sort_order=sort_order,
        )
        return {"attachment_id": att_id}

    @mcp.tool()
    def fango_list_post_attachments(post_id: int) -> list[dict[str, Any]]:
        """List image attachments on a post in display order."""
        return dump(forum_core.list_attachments(post_id))
