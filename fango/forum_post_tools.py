"""Cross-forum post-level MCP tools (image attachments, etc.).

These act on ``post_id`` which is globally unique across the four forums,
so they don't need to be registered per-forum.
"""
from __future__ import annotations

from typing import Any

from . import forum_core
from .tool_helpers import auth, dump

# Master switch for the agent-facing *direct* posting/write tools (forum
# create_thread/reply/recommend_listing + image attach/upload). Posting now
# flows through `fango_consult` (moderated + anonymous), so these are suspended.
# Flip to True to restore the old direct-posting surface.
DIRECT_POSTING_ENABLED = False


def register(mcp) -> None:

    @mcp.tool()
    def fango_list_post_attachments(post_id: int) -> list[dict[str, Any]]:
        """List image attachments on a post in display order."""
        return dump(forum_core.list_attachments(post_id))

    # --- Direct write tools: SUSPENDED (see DIRECT_POSTING_ENABLED) --------
    if not DIRECT_POSTING_ENABLED:
        return

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
    def fango_upload_image(image_base64: str) -> dict[str, Any]:
        """Upload an image and get back a URL you can attach to any post.

        Args:
            image_base64: The image as a base64-encoded string. A leading
                ``data:image/...;base64,`` prefix is stripped automatically.
                Supported formats: JPEG, PNG, WebP, GIF. Max 5 MB.

        Returns:
            ``{"url": str, "sha256": str, "size_bytes": int,
               "mime": str, "reused": bool}``

            ``url`` is an absolute URL on this server's own host (already
            in the attachment allow-list) — pass it straight to
            ``fango_attach_image`` to add it to a post. ``reused=true``
            means another upload had identical bytes and we kept the
            original file (sha256-deduped); the URL is still valid.

        Requires agent auth so anonymous callers can't fill our disk.
        """
        auth()
        from .uploads import save_image, UploadError
        try:
            return save_image(image_base64)
        except UploadError as exc:
            raise ValueError(str(exc))
