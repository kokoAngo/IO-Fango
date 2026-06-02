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
from .. import forum_core
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


def _listing_id_of(listing) -> int | None:
    if isinstance(listing, dict):
        return listing.get("id")
    return getattr(listing, "id", None)


def resolve_external_link(listing_id: int, building_name: str | None = None,
                          conn=None, *, allow_lookup: bool = True) -> dict | None:
    """The external HOMES link for a listing — for surfacing to the agent (so it
    can hand the customer a "rent it here" URL) and for attaching to a post.

    Cache-first. On a cache miss it performs a gated, rate-limited **browser**
    lookup (~10s) only when ``allow_lookup`` is True — callers on a latency-
    sensitive path (read tools, the consult reply) pass ``allow_lookup=False`` to
    stay cache-only; the background post-enrichment does the actual lookup.

    Returns ``{url, source, image_url, title, cached}`` or None (disabled /
    not found / negative cache / cache-miss when lookup disallowed). Never raises."""
    if not load_settings().external_lookup_enabled or not listing_id:
        return None
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        cached = _fresh_cache(conn, listing_id)
        if cached:
            if cached["status"] == "ok" and cached.get("url"):
                return {
                    "url": cached["url"], "source": cached.get("source"),
                    "image_url": cached.get("image_url"), "title": cached.get("title"),
                    "cached": True,
                }
            return None  # fresh negative cache → don't re-scrape
        if not allow_lookup:
            return None  # cache-only path: don't trigger a slow browser lookup

        from ..rate_limit import EXTERNAL_LOOKUP, RateLimitError, check_and_record
        try:
            check_and_record("external_lookup", "global", EXTERNAL_LOOKUP, conn=conn)
        except RateLimitError:
            return None

        row = conn.execute(
            "SELECT building_name, url FROM listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
        name = building_name or (row["building_name"] if row else None)
        source_url = row["url"] if row else None
        if not name:
            _write_cache(conn, listing_id, url=None, source=None, image_url=None,
                         title=None, status="none")
            return None

        # One browser session returns url + og:image + og:title together
        # (HOMES is WAF-walled, so we can't refetch with httpx). We hotlink the
        # HOMES image rather than self-host it.
        found = external_lookup.find_listing(name, source_url=source_url)
        if not found or not found.get("url"):
            _write_cache(conn, listing_id, url=None, source=None, image_url=None,
                         title=None, status="none")
            return None
        _write_cache(conn, listing_id, url=found["url"], source=found.get("source"),
                     image_url=found.get("image"), title=found.get("title"), status="ok")
        return {"url": found["url"], "source": found.get("source"),
                "image_url": found.get("image"), "title": found.get("title"),
                "cached": False}
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("external-link resolve failed (listing %s): %s", listing_id, exc)
        return None
    finally:
        if owns:
            conn.close()


def enrich_post_with_listing_link(post_id: int, listing, conn=None) -> dict | None:
    """Attach an OGP preview (SUUMO/HOMES link + self-hosted image) for
    ``listing`` to ``post_id``. ``listing`` may be a brief dict (with ``id`` /
    ``building_name``) or anything with an ``id``. Best-effort; returns a small
    status dict or None (disabled)."""
    if not load_settings().external_lookup_enabled:
        return None
    listing_id = _listing_id_of(listing)
    if not listing_id:
        return None
    brief_name = listing.get("building_name") if isinstance(listing, dict) else None
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        link = resolve_external_link(listing_id, brief_name, conn=conn)
        if not link:
            return {"attached": False, "reason": "no external link"}
        forum_core.attach_link_preview(
            post_id, link["url"], image_url=link.get("image_url"),
            title=link.get("title"), source=link.get("source"), conn=conn,
        )
        return {"attached": True, "url": link["url"],
                "image_url": link.get("image_url"), "cached": link.get("cached", False)}
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("listing-link enrich failed (post %s, listing %s): %s",
                    post_id, listing_id, exc)
        return None
    finally:
        if owns:
            conn.close()


def enrich_post_with_listings(post_id: int, listings, conn=None) -> None:
    """Attach HOMES previews for several image-less listings to one post,
    reusing a single browser session for all the uncached ones (open HOMES +
    clear the WAF once, then search each building). Cached buildings attach
    immediately without a browser. Best-effort; never raises."""
    if not load_settings().external_lookup_enabled:
        return
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        # Cache pass: attach cached hits now; queue uncached for one batch lookup.
        pending: list[tuple[int, str]] = []   # (listing_id, building_name)
        for listing in listings:
            lid = _listing_id_of(listing)
            if not lid:
                continue
            cached = _fresh_cache(conn, lid)
            if cached:
                if cached["status"] == "ok" and cached.get("url"):
                    forum_core.attach_link_preview(
                        post_id, cached["url"], image_url=cached.get("image_url"),
                        title=cached.get("title"), source=cached.get("source"), conn=conn)
                continue
            name = (listing.get("building_name") if isinstance(listing, dict) else None)
            if not name:
                row = conn.execute("SELECT building_name FROM listings WHERE id = ?", (lid,)).fetchone()
                name = row["building_name"] if row else None
            if not name:
                _write_cache(conn, lid, url=None, source=None, image_url=None, title=None, status="none")
                continue
            pending.append((lid, name))

        if not pending:
            return

        from ..rate_limit import EXTERNAL_LOOKUP, RateLimitError, check_and_record
        try:
            check_and_record("external_lookup", "global", EXTERNAL_LOOKUP, conn=conn)
        except RateLimitError:
            return

        results = external_lookup.find_listings([n for _, n in pending])  # one browser session
        for lid, name in pending:
            found = results.get(name)
            if found and found.get("url"):
                _write_cache(conn, lid, url=found["url"], source=found.get("source"),
                             image_url=found.get("image"), title=found.get("title"), status="ok")
                forum_core.attach_link_preview(
                    post_id, found["url"], image_url=found.get("image"),
                    title=found.get("title"), source=found.get("source"), conn=conn)
            else:
                _write_cache(conn, lid, url=None, source=None, image_url=None, title=None, status="none")
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("batch listing-link enrich failed (post %s): %s", post_id, exc)
    finally:
        if owns:
            conn.close()
