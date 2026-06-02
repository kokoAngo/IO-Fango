"""Best-effort enrichment: give an image-less listing a photo by finding a
SUUMO/HOMES URL (HOMES preferred, then SUUMO — else nothing) and unfurling its
og:image onto a forum post as a preview card.

Never raises — a failure here must not break posting. Outbound work is gated by
the ``FANGO_EXTERNAL_LOOKUP_ENABLED`` kill-switch, an hourly rate cap, and a
per-listing cache (``listing_external_links``) that also remembers negatives so
we don't re-scrape a listing on every proposal.
"""
from __future__ import annotations

import logging

from . import external_lookup
from .. import forum_core, unfurl
from ..config import load_settings
from ..db import connect

log = logging.getLogger(__name__)

# Re-check cadence: keep a found link for a while; retry a "not found" sooner.
_CACHE_TTL_OK = "-14 days"
_CACHE_TTL_NONE = "-3 days"


def _fresh_cache(conn, listing_id: int) -> dict | None:
    row = conn.execute(
        """SELECT url, source, image_url, title, status
             FROM listing_external_links
            WHERE listing_id = ?
              AND ( (status = 'ok'  AND checked_at > datetime('now', ?))
                 OR (status != 'ok' AND checked_at > datetime('now', ?)) )""",
        (listing_id, _CACHE_TTL_OK, _CACHE_TTL_NONE),
    ).fetchone()
    return dict(row) if row else None


def _write_cache(conn, listing_id: int, *, url, source, image_url, title, status) -> None:
    conn.execute(
        """INSERT INTO listing_external_links(
               listing_id, url, source, image_url, title, status, checked_at)
           VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
           ON CONFLICT(listing_id) DO UPDATE SET
               url=excluded.url, source=excluded.source, image_url=excluded.image_url,
               title=excluded.title, status=excluded.status, checked_at=excluded.checked_at""",
        (listing_id, url, source, image_url, title, status),
    )


def _self_host_image(image_url: str) -> str | None:
    """Download an og:image and re-host it via the uploads pipeline. Returns a
    /uploads/<sha>.ext URL (allowlist-clean), or None on failure."""
    data = unfurl.fetch_image_bytes(image_url)
    if not data:
        return None
    try:
        from ..uploads import save_image_bytes
        return save_image_bytes(data)["url"]
    except Exception as exc:  # invalid/oversize image, etc.
        log.debug("self-host og:image failed for %s: %s", image_url, exc)
        return None


def enrich_post_with_listing_link(post_id: int, listing, conn=None) -> dict | None:
    """Attach an OGP preview (SUUMO/HOMES link + self-hosted image) for
    ``listing`` to ``post_id``. ``listing`` may be a brief dict (with ``id`` /
    ``building_name``) or anything with an ``id``. Best-effort; returns a small
    status dict or None."""
    settings = load_settings()
    if not settings.external_lookup_enabled:
        return None

    if isinstance(listing, dict):
        listing_id = listing.get("id")
        brief_name = listing.get("building_name")
    else:
        listing_id = getattr(listing, "id", None)
        brief_name = getattr(listing, "building_name", None)
    if not listing_id:
        return None

    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        cached = _fresh_cache(conn, listing_id)
        if cached:
            if cached["status"] == "ok" and cached.get("url"):
                forum_core.attach_link_preview(
                    post_id, cached["url"], image_url=cached.get("image_url"),
                    title=cached.get("title"), source=cached.get("source"), conn=conn,
                )
                return {"attached": True, "cached": True, "url": cached["url"]}
            return {"attached": False, "cached": True, "reason": "negative cache"}

        from ..rate_limit import EXTERNAL_LOOKUP, RateLimitError, check_and_record
        try:
            check_and_record("external_lookup", "global", EXTERNAL_LOOKUP, conn=conn)
        except RateLimitError:
            return {"attached": False, "reason": "rate limited"}

        row = conn.execute(
            "SELECT building_name, ward, city, url FROM listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
        building_name = brief_name or (row["building_name"] if row else None)
        ward = (row["ward"] or row["city"]) if row else None
        source_url = row["url"] if row else None
        if not building_name:
            _write_cache(conn, listing_id, url=None, source=None, image_url=None,
                         title=None, status="none")
            return {"attached": False, "reason": "no building name"}

        found = external_lookup.find_external_url(
            building_name, ward=ward, source_url=source_url,
        )
        if not found:
            _write_cache(conn, listing_id, url=None, source=None, image_url=None,
                         title=None, status="none")
            return {"attached": False, "reason": "no external url"}

        ogp = unfurl.fetch_ogp(found["url"])
        title = ogp.get("title") if ogp else None
        image_url = _self_host_image(ogp["image"]) if (ogp and ogp.get("image")) else None

        forum_core.attach_link_preview(
            post_id, found["url"], image_url=image_url, title=title,
            source=found["source"], conn=conn,
        )
        _write_cache(conn, listing_id, url=found["url"], source=found["source"],
                     image_url=image_url, title=title, status="ok")
        return {"attached": True, "url": found["url"], "image_url": image_url}
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("listing-link enrich failed (post %s, listing %s): %s",
                    post_id, listing_id, exc)
        return None
    finally:
        if owns:
            conn.close()
