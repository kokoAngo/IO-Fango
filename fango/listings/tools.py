"""MCP tools for listing search & detail (取得系: read-only, no auth).

Mirrors ``fango/wiki/tools.py`` in style — small wrappers around
``fango/listings/service.py``. The agent-binding writes (saved search,
listing recommendations) live in :mod:`fango.listings.saved_search`.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Any

from ..auth import current_agent_var
from ..config import REPO_ROOT, load_settings
from ..rate_limit import AGENT_READ, check_and_record
from ..tool_helpers import dump
from . import service as svc


def _enforce_read_quota() -> None:
    """Per-key 200/h cap on listing reads. No-op for unauthenticated callers
    (the ScraperGuardMiddleware handles those by IP at the HTTP layer)."""
    agent = current_agent_var.get()
    if agent is None:
        return
    check_and_record("agent_read", str(agent.id), AGENT_READ)


@lru_cache(maxsize=4096)
def _content_hash(rel_path: str) -> str | None:
    """SHA-256 of an image file's bytes."""
    try:
        return hashlib.sha256((REPO_ROOT / rel_path).read_bytes()).hexdigest()
    except OSError:
        return None


# Hamming distance ≤ this is considered "same photo" (different framing /
# angle of the same scene typically lands ≤ 8 on a 64-bit dHash).
_PHASH_THRESHOLD = 8


@lru_cache(maxsize=4096)
def _phash(rel_path: str) -> int | None:
    """dHash (difference hash) of an image — 64-bit perceptual fingerprint.

    Images that look the same to a human land at Hamming distance ≤ 8 from
    each other; unrelated photos sit at 25+. Resilient to recompression,
    minor crops, brightness shifts.
    """
    try:
        from PIL import Image
        with Image.open(REPO_ROOT / rel_path) as im:
            small = im.convert("L").resize((9, 8), Image.LANCZOS)
            pixels = list(small.getdata())
    except (OSError, ValueError, ImportError):
        return None
    bits = 0
    for row in range(8):
        base = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _dedup_by_content(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop images that look identical (bytes OR perceptual).

    Two passes:
    1. byte-level (SHA-256) — fast, catches exact dupes (e.g. the same
       photo re-uploaded under multiple filenames).
    2. perceptual (dHash + Hamming) — catches the same room shot from a
       slightly different angle / recompressed thumbnail / minor crop.

    Stable: preserves the first occurrence in input order, so callers can
    control priority by ordering (raw first, then shuhen).
    """
    # Pass 1 — byte identical
    seen_bytes: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        h = _content_hash(r["rel_path"])
        if h is None:
            out.append(r)
            continue
        if h in seen_bytes:
            continue
        seen_bytes.add(h)
        out.append(r)

    # Pass 2 — perceptual
    kept_phashes: list[int] = []
    final: list[dict[str, Any]] = []
    for r in out:
        ph = _phash(r["rel_path"])
        if ph is None:
            final.append(r)
            continue
        if any(_hamming(ph, prev) <= _PHASH_THRESHOLD for prev in kept_phashes):
            continue
        kept_phashes.append(ph)
        final.append(r)
    return final


def _listing_brief(listing, conn=None) -> dict[str, Any]:
    """Compact representation used by listing_search() results."""
    images = svc.get_listing_images(listing.id, kind="raw", conn=conn)
    thumb = None
    if images:
        first = images[0]
        thumb = _img_url(listing.id, first["kind"], first["sort_order"])
    return {
        "id": listing.id,
        "external_id": listing.reins_id,
        "title": listing.title,
        "building_name": listing.building_name,
        "address": listing.address,
        "prefecture": listing.prefecture,
        "city": listing.city,
        "station": listing.station,
        "station_line": listing.station_line,
        "walk_minutes": listing.walk_minutes,
        "layout": listing.layout,
        "area_sqm": listing.area_sqm,
        "price_man": listing.price_man,
        "rent_yen": listing.extra.get("rent_yen") if isinstance(listing.extra, dict) else None,
        "built_year": listing.built_year,
        "listing_type": listing.extra.get("listing_type") if isinstance(listing.extra, dict) else None,
        "thumbnail_url": thumb,
    }


def _img_url(listing_id: int, kind: str, sort_order: int) -> str:
    """Build the URL served by :func:`fango.http_app.serve_listing_image`.

    The URL carries only the 1-based ``seq`` (= ``sort_order + 1``), not
    the on-disk filename — that way the upstream naming convention (which
    can carry source-system identifiers) never reaches a caller.

    Returns an absolute URL when ``FANGO_PUBLIC_BASE_URL`` is configured;
    falls back to a project-relative path so dev / loopback setups work.
    """
    seq = int(sort_order) + 1
    path = f"/listings/img/{listing_id}/{kind}/{seq}.jpg"
    base = load_settings().public_base_url
    return f"{base}{path}" if base else path


# Temporarily suspended to steer agents toward `fango_consult`, so the forum
# fills with Q&A conversations rather than one-shot search broadcasts. The
# consult tool searches via the service layer (`ls.search_listings`), so it is
# unaffected. Flip to True to restore the agent-facing structured-search tool.
SEARCH_LISTINGS_ENABLED = False


def register(mcp) -> None:

    def fango_search_listings(
        criteria: dict[str, Any] | None = None,
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "newest",
    ) -> dict[str, Any]:
        """Search listings by structured criteria.

        Args:
            criteria: Filter dict. Recognised keys:
                price_min_man, price_max_man, rent_min_yen, rent_max_yen,
                prefecture, city, ward, station, layout (prefix match, e.g. "1L"),
                area_min_sqm, area_max_sqm, walk_minutes_max, built_year_min,
                keyword (FTS over building_name / address / station).
            limit: Page size (default 20).
            offset: Pagination offset.
            sort_by: One of newest, oldest, price_asc, price_desc, rent_asc,
                rent_desc, area_desc, walk_asc. Default: newest.

        Returns:
            {"total": int, "items": [<listing brief>...],
             "post_status": {"posted": bool, "forum": str|null,
                             "thread_id": int|null, "reason": str}}

            ``post_status`` reports whether this query was broadcast to the
            forum. ``posted=false`` reasons include ``"duplicate search"`` (same
            query within ~30 min), ``"hourly cap"``, ``"pagination (offset>0)"``,
            ``"no search signal"``, or a moderation reason for a rejected keyword.
        """
        _enforce_read_quota()
        crit = dict(criteria or {})
        total = svc.count_listings(criteria=crit)
        rows = svc.search_listings(
            criteria=crit, limit=limit, offset=offset, sort_by=sort_by,
        )
        result = {
            "total": total,
            "items": [_listing_brief(r) for r in rows],
        }
        # Broadcast the query to the forum (anonymous, deduped, best-effort).
        # First page only — paginating through the same search shouldn't repost.
        # The verdict is surfaced as `post_status` so the caller can see whether
        # (and why not) the query was published.
        if offset == 0:
            try:
                from ..auth import client_ip_var, current_agent_var
                from ..consult import autopost
                ag = current_agent_var.get()
                result["post_status"] = autopost.record_search(
                    criteria=crit, total=total, items=result["items"],
                    keyed_agent_id=ag.id if ag else None,
                    ip=client_ip_var.get(),
                )
            except Exception:  # pragma: no cover - never fail the search
                pass
        else:
            result["post_status"] = {"posted": False, "forum": None,
                                     "thread_id": None, "reason": "pagination (offset>0)"}
        return result

    # SUSPENDED: see SEARCH_LISTINGS_ENABLED above. Defined but not registered.
    if SEARCH_LISTINGS_ENABLED:
        mcp.tool()(fango_search_listings)

    @mcp.tool()
    def fango_get_listing(listing_id: int) -> dict[str, Any] | None:
        """Fetch a listing's full detail (transports, images, price history).

        Images are filtered to the user-facing kinds — ``raw`` (interior /
        exterior photos) and ``shuhen`` (neighbourhood / POI). The
        ``processed`` kind (ML classifier crops; visually duplicates ``raw``)
        is intentionally omitted here; query ``fango_get_listing_images``
        with ``kind="processed"`` if you really need them.
        """
        _enforce_read_quota()
        bundle = svc.get_listing_with_relations(listing_id)
        if bundle is None:
            return None
        listing = bundle["listing"]
        # Drop the ML-internal 'processed' variants AND any byte-identical
        # duplicates (upstream feeds sometimes ship the same exterior
        # photo under multiple filenames; owners don't want dupes).
        visible = [img for img in bundle["images"] if img["kind"] != "processed"]
        deduped = _dedup_by_content(visible)
        images_out = [
            {
                "kind": img["kind"],
                "label": img.get("label"),
                "sort_order": img["sort_order"],
                "url": _img_url(listing.id, img["kind"], img["sort_order"]),
            }
            for img in deduped
        ]
        return {
            "listing": dump(listing),
            "transports": bundle["transports"],
            "images": images_out,
            "price_history": bundle["price_history"],
        }

    @mcp.tool()
    def fango_get_listing_images(
        listing_id: int,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get image URLs for a listing.

        Args:
            listing_id: Target listing id.
            kind: Optional filter — 'raw' | 'processed' | 'shuhen'.
                  If omitted, returns user-facing photos only (``raw`` +
                  ``shuhen``); ``processed`` is excluded by default since
                  it visually duplicates ``raw``. Pass ``kind="processed"``
                  explicitly to inspect them.
        """
        _enforce_read_quota()
        if kind is None:
            rows = svc.get_listing_images(listing_id)
            rows = [r for r in rows if r["kind"] != "processed"]
        else:
            rows = svc.get_listing_images(listing_id, kind=kind)
        # Same byte-level dedup as fango_get_listing — drop disk-identical
        # duplicates so owners don't see the same photo twice.
        rows = _dedup_by_content(rows)
        return [
            {
                "kind": r["kind"],
                "label": r.get("label"),
                "sort_order": r["sort_order"],
                "url": _img_url(listing_id, r["kind"], r["sort_order"]),
            }
            for r in rows
        ]
