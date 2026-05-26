"""MCP tools for listing search & detail (取得系: read-only, no auth).

Mirrors ``fango/wiki/tools.py`` in style — small wrappers around
``fango/listings/service.py``. The agent-binding writes (saved search,
listing recommendations) live in :mod:`fango.listings.saved_search`.
"""
from __future__ import annotations

from typing import Any

from ..tool_helpers import dump
from . import service as svc


def _listing_brief(listing, conn=None) -> dict[str, Any]:
    """Compact representation used by listing_search() results."""
    images = svc.get_listing_images(listing.id, kind="raw", conn=conn)
    thumb = None
    if images:
        first = images[0]
        thumb = _img_url(listing.id, first["kind"], first["rel_path"])
    return {
        "id": listing.id,
        "reins_id": listing.reins_id,
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


def _img_url(listing_id: int, kind: str, rel_path: str) -> str:
    """Build the URL served by :func:`fango.http_app.serve_listing_image`.

    ``rel_path`` is a project-root-relative path; the last segment is the
    filename that the endpoint validates and returns.
    """
    filename = rel_path.rsplit("/", 1)[-1]
    return f"/listings/img/{listing_id}/{kind}/{filename}"


def register(mcp) -> None:

    @mcp.tool()
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
            {"total": int, "items": [<listing brief>...]}
        """
        crit = dict(criteria or {})
        total = svc.count_listings(criteria=crit)
        rows = svc.search_listings(
            criteria=crit, limit=limit, offset=offset, sort_by=sort_by,
        )
        return {
            "total": total,
            "items": [_listing_brief(r) for r in rows],
        }

    @mcp.tool()
    def fango_get_listing(listing_id: int) -> dict[str, Any] | None:
        """Fetch a listing's full detail (incl. transports, images, price history)."""
        bundle = svc.get_listing_with_relations(listing_id)
        if bundle is None:
            return None
        listing = bundle["listing"]
        # Hydrate image URLs.
        images_out = []
        for img in bundle["images"]:
            images_out.append({
                "kind": img["kind"],
                "label": img.get("label"),
                "sort_order": img["sort_order"],
                "url": _img_url(listing.id, img["kind"], img["rel_path"]),
            })
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
        """
        rows = svc.get_listing_images(listing_id, kind=kind)
        return [
            {
                "kind": r["kind"],
                "label": r.get("label"),
                "sort_order": r["sort_order"],
                "url": _img_url(listing_id, r["kind"], r["rel_path"]),
            }
            for r in rows
        ]
