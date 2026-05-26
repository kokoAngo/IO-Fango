"""Notion → Listings adapter (stub).

Configure with ``NOTION_TOKEN`` and ``NOTION_LISTINGS_DATABASE_ID``.
If unconfigured, iter_listings() yields nothing — the rest of the system still works.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator

from ...config import load_settings
from .base import ListingAdapter

log = logging.getLogger(__name__)


class NotionListingAdapter(ListingAdapter):
    name = "notion"

    def __init__(self, token: str | None = None, database_id: str | None = None):
        settings = load_settings()
        self.token = token or settings.notion_token
        self.database_id = database_id or settings.notion_listings_db

    def is_configured(self) -> bool:
        return bool(self.token and self.database_id)

    def iter_listings(self) -> Iterator[dict[str, Any]]:
        if not self.is_configured():
            log.warning(
                "NotionListingAdapter not configured (NOTION_TOKEN / NOTION_LISTINGS_DATABASE_ID empty); skipping."
            )
            return iter(())
        return self._iter_pages()

    def _iter_pages(self) -> Iterator[dict[str, Any]]:
        try:
            import httpx
        except ImportError:  # pragma: no cover
            log.error("httpx not installed; cannot reach Notion")
            return iter(())

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        url = f"https://api.notion.com/v1/databases/{self.database_id}/query"
        cursor: str | None = None
        with httpx.Client(timeout=30.0) as client:
            while True:
                payload: dict[str, Any] = {"page_size": 100}
                if cursor:
                    payload["start_cursor"] = cursor
                resp = client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                for page in data.get("results", []):
                    record = _page_to_record(page)
                    if record:
                        yield record
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")


def _page_to_record(page: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort Notion page → listing payload mapping.

    The exact property names depend on the user's Notion database, so we only
    pull the common ones and let the rest live in raw_json.
    """
    props = page.get("properties") or {}

    def _plain(name: str) -> str | None:
        prop = props.get(name)
        if not prop:
            return None
        for key in ("title", "rich_text"):
            arr = prop.get(key)
            if isinstance(arr, list) and arr:
                return "".join(p.get("plain_text", "") for p in arr) or None
        if prop.get("type") == "url":
            return prop.get("url")
        if prop.get("type") == "select":
            sel = prop.get("select")
            return sel.get("name") if sel else None
        return None

    def _number(name: str) -> float | None:
        prop = props.get(name)
        if not prop:
            return None
        n = prop.get("number")
        return n

    import json as _json
    return {
        "reins_id": _plain("REINS_ID") or page["id"],
        "title": _plain("Title") or _plain("Name"),
        "building_name": _plain("Building"),
        "address": _plain("Address"),
        "prefecture": _plain("Prefecture"),
        "city": _plain("City"),
        "station": _plain("Station"),
        "layout": _plain("Layout"),
        "area_sqm": _number("Area"),
        "price_man": int(_number("Price") or 0) or None,
        "built_year": int(_number("Built") or 0) or None,
        "url": _plain("URL"),
        "raw_json": _json.dumps(page, ensure_ascii=False),
    }
