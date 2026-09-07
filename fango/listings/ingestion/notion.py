"""Notion → Listings adapter (stub).

Configure with ``NOTION_TOKEN`` and ``NOTION_LISTINGS_DATABASE_ID``.
If unconfigured, iter_listings() yields nothing — the rest of the system still works.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from ...config import load_settings
from .base import ListingAdapter

log = logging.getLogger(__name__)

# 「広告可」 values that are explicitly cleared for advertising. We now ingest
# ALL statuses (recorded as listing.ad_status); this set is kept for any later
# display-time filtering / ranking.
ADVERTISABLE = {"可", "おすすめ"}


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
                    record = self._page_to_record(page)
                    if record:
                        yield record
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")

    def _page_to_record(self, page: dict[str, Any]) -> dict[str, Any] | None:
        """Map a Notion page → a listings payload. Overridden per source."""
        return _page_to_record(page)


def _num(s: Any) -> float | None:
    """First numeric value in a string like '57.30', '7,000円', '17.8万円'."""
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(s).replace(",", ""))
    return float(m.group()) if m else None


def _intf(s: Any) -> int | None:
    n = _num(s)
    return int(round(n)) if n is not None else None


def _split_address(addr: str | None) -> tuple[str | None, str | None]:
    """'東京都武蔵野市境３丁目' → ('東京都', '武蔵野市')."""
    if not addr:
        return None, None
    m = re.match(r"\s*(.+?[都道府県])(.*)", addr)
    if not m:
        return None, None
    pref, rest = m.group(1), m.group(2)
    m2 = re.match(r"(.+?[市区町村])", rest)
    return pref, (m2.group(1) if m2 else None)


def _split_station(s: str | None) -> tuple[str | None, str | None]:
    """'中央線　武蔵境' → ('中央線', '武蔵境')."""
    if not s:
        return None, None
    parts = [p for p in re.split(r"[\s　/・]+", s.strip()) if p]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return (None, parts[0]) if parts else (None, None)


def _page_to_record(page: dict[str, Any]) -> dict[str, Any] | None:
    """Map a 「新着物件DB」 Notion page → a listings payload (rental data).

    Returns None for listings that are NOT cleared for advertising (広告可).
    """
    props = page.get("properties") or {}

    def t(name: str) -> str | None:
        prop = props.get(name)
        if not prop:
            return None
        for key in ("title", "rich_text"):
            arr = prop.get(key)
            if isinstance(arr, list) and arr:
                return "".join(p.get("plain_text", "") for p in arr).strip() or None
        if prop.get("type") == "select":
            sel = prop.get("select")
            return sel.get("name") if sel else None
        if prop.get("type") == "url":
            return prop.get("url")
        return None

    # Ingest all statuses; the source 「広告可」 verdict is recorded on the
    # listing (ad_status) so it can be filtered/ranked later without re-ingesting.
    ad_status = t("広告可")
    # Skip only genuinely empty rows (no address and no building name).
    if not t("所在地") and not t("建物名"):
        return None

    pref, city = _split_address(t("所在地"))
    line, station = _split_station(t("沿線駅"))
    rent_man = _num(t("賃料（万円）"))
    built = t("築年月") or ""
    built_year = int(built[:4]) if built[:4].isdigit() else None

    return {
        "reins_id": t("REINS_ID") or page["id"],
        "building_name": t("建物名"),
        "address": t("所在地"),
        "prefecture": pref,
        "city": city,
        "station": station,
        "station_line": line,
        "walk_minutes": _intf(t("徒歩(分)")),
        "layout": t("間取"),
        "area_sqm": _num(t("使用部分面積（m2）")),
        "rent_yen": int(round(rent_man * 10000)) if rent_man else None,
        "deposit_text": t("敷金"),
        "key_money_text": t("礼金"),
        "maintenance_fee_yen": _intf(t("共益費（円）")) or _intf(t("管理費（円）")),
        "floor": _intf(t("所在階")),
        "built_year": built_year,
        "structure": t("物件種目"),
        "agent_company": t("商号"),
        "ad_status": ad_status,
        "listing_type": "rent",
        "transaction_type": "rent",
        "url": None,
        "raw_json": json.dumps(page, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# Sale (売買) source — a separate Notion DB with its own columns. Its 取引状況
# select (公開中 / 申込あり / 一時停止 / -) is stored on ``ad_status`` so search
# can show ONLY 公開中 sale listings (see service._build_search_where).
# ---------------------------------------------------------------------------

# Phone numbers (often a personal mobile) are never ingested or stored.
_SALE_PII_PROP = "電話番号"


def _prop(props: dict, name: str):
    """Read a Notion property value: title/rich_text/select/url → str, number → float."""
    p = props.get(name)
    if not p:
        return None
    typ = p.get("type")
    if typ in ("title", "rich_text"):
        arr = p.get(typ) or []
        return "".join(x.get("plain_text", "") for x in arr).strip() or None
    if typ == "select":
        sel = p.get("select")
        return sel.get("name") if sel else None
    if typ == "number":
        return p.get("number")
    if typ == "url":
        return p.get("url")
    return None


def _page_to_sale_record(page: dict[str, Any]) -> dict[str, Any] | None:
    """Map a 売買 Notion page → a listings payload (transaction_type='sale').

    All statuses are ingested. 取引状況 lands on ``tenancy_status`` (on-market
    state), NOT on ``ad_status`` — ad_status means 広告転載可否, and this Notion
    database carries no such field. Sale rows from here therefore have unknown
    ad clearance and stay off the public surface until a source that knows
    (the upstream Postgres) fills it in. See service.is_advertisable."""
    props = page.get("properties") or {}

    def g(name: str):
        return _prop(props, name)

    building = g("建物名") or g("名称")
    # 名称 (the title) is often just the 物件番号 for rows that have no real
    # 建物名 — a building name is never all-digits, so drop those (else the
    # recommendation shows a bare number as the "name").
    if building and building.strip().isdigit():
        building = None
    address = g("所在地")
    if not address and not building:
        return None

    pref, city = _split_address(address)
    line, station = _split_station(g("沿線駅") or g("交通"))
    built = (g("築年月") or "")
    built_year = int(built[:4]) if built[:4].isdigit() else None
    area = _num(g("専有面積")) or _num(g("建物面積")) or _num(g("土地面積"))

    # Drop the phone number before persisting the raw page (PII hygiene).
    safe_page = dict(page)
    if isinstance(safe_page.get("properties"), dict):
        safe_props = dict(safe_page["properties"])
        safe_props.pop(_SALE_PII_PROP, None)
        safe_page["properties"] = safe_props

    return {
        "reins_id": f"sale:{g('物件番号') or page['id']}",
        "building_name": building,
        "address": address,
        "prefecture": pref,
        "city": city,
        "ward": g("区"),
        "station": station,
        "station_line": line,
        "layout": g("間取"),
        "area_sqm": area,
        "price_man": _intf(g("価格万円")),
        "floor": _intf(g("所在階")),
        "built_year": built_year,
        "structure": g("物件種目") or g("物件種別"),
        "agent_company": g("業者名"),
        "tenancy_status": g("取引状況"),       # 公開中 / 申込あり / 一時停止 / -
        "listing_type": "sale",
        "transaction_type": "sale",
        "url": None,
        "raw_json": json.dumps(safe_page, ensure_ascii=False),
    }


class NotionSaleListingAdapter(NotionListingAdapter):
    """Same Notion HTTP loop, pointed at the 売買 database with the sale mapper."""
    name = "notion_sale"

    def __init__(self, token: str | None = None, database_id: str | None = None):
        settings = load_settings()
        self.token = token or settings.notion_token
        self.database_id = database_id or settings.notion_sale_db

    def _page_to_record(self, page: dict[str, Any]) -> dict[str, Any] | None:
        return _page_to_sale_record(page)
