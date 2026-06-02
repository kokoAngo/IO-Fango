"""Listings CRUD + search."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from ..db import connect
from ..models import Listing

LISTING_COLUMNS = (
    "reins_id", "title", "building_name", "building_name_kana", "address",
    "prefecture", "city", "ward", "station", "station_line", "walk_minutes",
    "layout", "area_sqm", "balcony_sqm", "price_man", "price_per_sqm_man",
    "rent_yen", "deposit_text", "key_money_text", "tenancy_status",
    "maintenance_fee_yen", "repair_fee_yen", "built_year", "built_month",
    "structure", "floor", "total_floors", "direction", "parking",
    "pet_allowed", "renovation", "listing_type", "transaction_type",
    "url", "agent_company", "ad_status", "raw_json", "last_seen_at",
)

# Whitelisted sort options for search_listings().
_SORT_BY_SQL: dict[str, str] = {
    "newest":     "listings.updated_at DESC",
    "oldest":     "listings.updated_at ASC",
    "price_asc":  "listings.price_man ASC NULLS LAST",
    "price_desc": "listings.price_man DESC NULLS LAST",
    "rent_asc":   "listings.rent_yen ASC NULLS LAST",
    "rent_desc":  "listings.rent_yen DESC NULLS LAST",
    "area_desc":  "listings.area_sqm DESC NULLS LAST",
    "walk_asc":   "listings.walk_minutes ASC NULLS LAST",
}


def insert_listing(payload: dict[str, Any], conn: sqlite3.Connection | None = None) -> Listing:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        cols = [c for c in LISTING_COLUMNS if c in payload]
        vals = [payload[c] for c in cols]
        if not cols:
            raise ValueError("listing payload empty")
        placeholders = ",".join("?" * len(cols))
        cur = conn.execute(
            f"INSERT INTO listings({','.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        listing_id = cur.lastrowid
        if payload.get("price_man") is not None:
            conn.execute(
                "INSERT INTO price_history(listing_id, price_man) VALUES (?, ?)",
                (listing_id, payload["price_man"]),
            )
        _refresh_building_stats(conn, payload.get("building_name"))
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        return Listing.from_row(row)
    finally:
        if owns_conn:
            conn.close()


def upsert_listing(payload: dict[str, Any], conn: sqlite3.Connection | None = None) -> Listing:
    """Insert or update by reins_id. Records price change in price_history."""
    if "reins_id" not in payload or not payload["reins_id"]:
        return insert_listing(payload, conn=conn)
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        existing = conn.execute(
            "SELECT * FROM listings WHERE reins_id = ?", (payload["reins_id"],)
        ).fetchone()
        if existing is None:
            return insert_listing(payload, conn=conn)
        cols = [c for c in LISTING_COLUMNS if c in payload]
        if cols:
            assigns = ",".join(f"{c} = ?" for c in cols)
            conn.execute(
                f"UPDATE listings SET {assigns}, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                [payload[c] for c in cols] + [existing["id"]],
            )
        new_price = payload.get("price_man")
        if new_price is not None and new_price != existing["price_man"]:
            conn.execute(
                "INSERT INTO price_history(listing_id, price_man) VALUES (?, ?)",
                (existing["id"], new_price),
            )
        _refresh_building_stats(conn, payload.get("building_name") or existing["building_name"])
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (existing["id"],)).fetchone()
        return Listing.from_row(row)
    finally:
        if owns_conn:
            conn.close()


def get_listing(listing_id: int, conn: sqlite3.Connection | None = None) -> Listing | None:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        return Listing.from_row(row) if row else None
    finally:
        if owns_conn:
            conn.close()


def get_with_history(listing_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if row is None:
            return None
        history = conn.execute(
            "SELECT price_man, observed_at FROM price_history WHERE listing_id = ? ORDER BY observed_at",
            (listing_id,),
        ).fetchall()
        return {
            "listing": Listing.from_row(row),
            "price_history": [dict(h) for h in history],
        }
    finally:
        if owns_conn:
            conn.close()


def _build_search_where(criteria: dict[str, Any]) -> tuple[str, list[Any], str]:
    """Translate a criteria dict into (WHERE clause sans 'WHERE', params, join clause).

    Accepted keys mirror the public search_listings() signature; unknown keys are
    silently ignored so caller-supplied criteria dicts can be looser than the
    server schema.
    """
    where: list[str] = []
    params: list[Any] = []
    join = ""

    keyword = criteria.get("keyword")
    if keyword:
        kw = str(keyword).strip()
        if len(kw) < 3:
            like = f"%{kw}%"
            where.append(
                "(listings.building_name LIKE ? OR listings.address LIKE ? OR listings.station LIKE ?)"
            )
            params.extend([like, like, like])
        elif kw:
            # Quote each whitespace token so FTS5 treats it as a literal phrase —
            # otherwise stray operators ("/", "-", ":", "AND"…) raise
            # "fts5: syntax error". Double-quotes inside a token are escaped by
            # doubling. Falls back to LIKE if nothing usable survives.
            import re as _re
            terms = [t for t in _re.split(r"\s+", kw) if t]
            if terms:
                join = "JOIN listings_fts f ON f.rowid = listings.id"
                where.append("listings_fts MATCH ?")
                params.append(" ".join('"' + t.replace('"', '""') + '"' for t in terms))
            else:
                like = f"%{kw}%"
                where.append(
                    "(listings.building_name LIKE ? OR listings.address LIKE ? OR listings.station LIKE ?)"
                )
                params.extend([like, like, like])

    def _str_like(field: str, val: Any) -> None:
        s = str(val).strip()
        if not s:
            return
        where.append(f"listings.{field} LIKE ?")
        params.append(f"%{s}%")

    def _str_eq(field: str, val: Any) -> None:
        s = str(val).strip()
        if not s:
            return
        where.append(f"listings.{field} = ?")
        params.append(s)

    if criteria.get("prefecture"):
        _str_eq("prefecture", criteria["prefecture"])
    if criteria.get("city"):
        _str_like("city", criteria["city"])
    if criteria.get("ward"):
        _str_like("ward", criteria["ward"])
    if criteria.get("station"):
        _str_like("station", criteria["station"])
    if criteria.get("layout"):
        # Layout is treated as prefix (e.g. "1L" matches "1LDK"/"1LDK+S").
        where.append("listings.layout LIKE ?")
        params.append(f"{str(criteria['layout']).strip()}%")

    if criteria.get("price_min_man") is not None:
        where.append("listings.price_man >= ?")
        params.append(criteria["price_min_man"])
    if criteria.get("price_max_man") is not None:
        where.append("listings.price_man <= ?")
        params.append(criteria["price_max_man"])
    if criteria.get("rent_max_yen") is not None:
        where.append("listings.rent_yen <= ?")
        params.append(criteria["rent_max_yen"])
    if criteria.get("rent_min_yen") is not None:
        where.append("listings.rent_yen >= ?")
        params.append(criteria["rent_min_yen"])

    if criteria.get("area_min_sqm") is not None:
        where.append("listings.area_sqm >= ?")
        params.append(criteria["area_min_sqm"])
    if criteria.get("area_max_sqm") is not None:
        where.append("listings.area_sqm <= ?")
        params.append(criteria["area_max_sqm"])
    if criteria.get("walk_minutes_max") is not None:
        where.append("listings.walk_minutes <= ?")
        params.append(criteria["walk_minutes_max"])

    if criteria.get("built_year_min") is not None:
        where.append("listings.built_year >= ?")
        params.append(criteria["built_year_min"])

    # Legacy aliases (old code: min_price/max_price).
    if criteria.get("min_price") is not None:
        where.append("listings.price_man >= ?")
        params.append(criteria["min_price"])
    if criteria.get("max_price") is not None:
        where.append("listings.price_man <= ?")
        params.append(criteria["max_price"])

    # Restrict the candidate pool (used by saved-search matching).
    only_ids = criteria.get("only_listing_ids")
    if only_ids:
        placeholders = ",".join("?" * len(only_ids))
        where.append(f"listings.id IN ({placeholders})")
        params.extend(only_ids)

    where_sql = " AND ".join(where) if where else ""
    return where_sql, params, join


def search_listings(
    *,
    keyword: str | None = None,
    prefecture: str | None = None,
    city: str | None = None,
    ward: str | None = None,
    station: str | None = None,
    layout: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    price_min_man: int | None = None,
    price_max_man: int | None = None,
    rent_max_yen: int | None = None,
    rent_min_yen: int | None = None,
    area_min_sqm: float | None = None,
    area_max_sqm: float | None = None,
    walk_minutes_max: int | None = None,
    built_year_min: int | None = None,
    only_listing_ids: list[int] | None = None,
    criteria: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "newest",
    conn: sqlite3.Connection | None = None,
) -> list[Listing]:
    """Search listings. Accepts kwargs OR a single ``criteria`` dict (merged)."""
    crit: dict[str, Any] = dict(criteria or {})
    # kwargs override / fill in crit
    for k, v in {
        "keyword": keyword, "prefecture": prefecture, "city": city, "ward": ward,
        "station": station, "layout": layout, "min_price": min_price, "max_price": max_price,
        "price_min_man": price_min_man, "price_max_man": price_max_man,
        "rent_max_yen": rent_max_yen, "rent_min_yen": rent_min_yen,
        "area_min_sqm": area_min_sqm, "area_max_sqm": area_max_sqm,
        "walk_minutes_max": walk_minutes_max, "built_year_min": built_year_min,
        "only_listing_ids": only_listing_ids,
    }.items():
        if v is not None:
            crit[k] = v

    order_sql = _SORT_BY_SQL.get(sort_by) or _SORT_BY_SQL["newest"]
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        where_sql, params, join = _build_search_where(crit)
        params.extend([limit, offset])
        sql = f"""
            SELECT listings.* FROM listings
            {join}
            {('WHERE ' + where_sql) if where_sql else ''}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, params).fetchall()
        return [Listing.from_row(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def relaxed_search(
    criteria: dict[str, Any] | None,
    limit: int = 10,
    conn: sqlite3.Connection | None = None,
) -> tuple[list[Listing], str | None]:
    """Fallback when the exact criteria match nothing: progressively loosen the
    constraints until something turns up, so we can offer *near* options instead
    of "no results".

    Returns ``(rows, note)`` — ``note`` is a short Japanese description of what
    was relaxed (for the reply), or ``([], None)`` if even the widest search is
    empty.
    """
    base = {k: v for k, v in (criteria or {}).items() if v not in (None, "")}
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        for crit, note in _relaxation_steps(base):
            rows = search_listings(criteria=crit, limit=limit, sort_by="newest", conn=conn)
            if rows:
                return rows, note
        return [], None
    finally:
        if owns_conn:
            conn.close()


def _widen_budget(crit: dict[str, Any], factor: float) -> None:
    for k in ("rent_max_yen", "price_max_man"):
        if crit.get(k):
            try:
                crit[k] = int(round(int(crit[k]) * factor))
            except (TypeError, ValueError):
                pass


def _relaxation_steps(base: dict[str, Any]):
    """Yield (criteria, note) increasingly loose, from the original criteria."""
    # 1: drop soft constraints, widen budget ~20%.
    c1 = dict(base)
    for k in ("walk_minutes_max", "built_year_min", "area_min_sqm", "area_max_sqm"):
        c1.pop(k, None)
    _widen_budget(c1, 1.2)
    yield c1, "駅徒歩・築年・面積の条件を緩め、ご予算を少し広げて探しました。"
    # 2: also drop layout + keyword, widen budget to ~40% of original.
    c2 = dict(c1)
    c2.pop("layout", None)
    c2.pop("keyword", None)
    _widen_budget(c2, 1.4 / 1.2)
    yield c2, "間取りの条件も外し、ご予算を広げて探しました。"
    # 3: drop station, keep city/ward.
    c3 = dict(c2)
    c3.pop("station", None)
    yield c3, "駅の指定を外し、市区町村の範囲で近い物件を探しました。"
    # 4: widen the area to the whole prefecture.
    c4 = dict(c3)
    c4.pop("city", None)
    c4.pop("ward", None)
    yield c4, "エリアを都道府県全体に広げて近い物件を探しました。"


def count_listings(
    *,
    criteria: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Total count for a criteria dict (used for paginated UIs and MCP totals)."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        where_sql, params, join = _build_search_where(dict(criteria or {}))
        sql = f"""
            SELECT COUNT(*) AS n FROM listings
            {join}
            {('WHERE ' + where_sql) if where_sql else ''}
        """
        row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0
    finally:
        if owns_conn:
            conn.close()


def add_note(listing_id: int, agent_id: int, note: str, conn: sqlite3.Connection | None = None) -> int:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO listing_notes(listing_id, agent_id, note) VALUES (?, ?, ?)",
            (listing_id, agent_id, note),
        )
        return cur.lastrowid
    finally:
        if owns_conn:
            conn.close()


def _refresh_building_stats(conn: sqlite3.Connection, building_name: str | None) -> None:
    if not building_name:
        return
    row = conn.execute(
        "SELECT COUNT(*) AS n, AVG(price_man) AS avg FROM listings WHERE building_name = ?",
        (building_name,),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO building_stats(building_name, listing_count, avg_price_man, updated_at)
        VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ON CONFLICT(building_name) DO UPDATE SET
            listing_count = excluded.listing_count,
            avg_price_man = excluded.avg_price_man,
            updated_at = excluded.updated_at
        """,
        (building_name, row["n"], row["avg"]),
    )


def bulk_import(records: Iterable[dict[str, Any]], conn: sqlite3.Connection | None = None) -> int:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        n = 0
        for rec in records:
            upsert_listing(rec, conn=conn)
            n += 1
        return n
    finally:
        if owns_conn:
            conn.close()


def replace_images(
    listing_id: int,
    images: list[dict[str, Any]],
    conn: sqlite3.Connection | None = None,
) -> int:
    """Drop existing image rows for a listing and insert the given set.

    Each image dict expects keys: kind ('raw'|'processed'|'shuhen'), rel_path,
    optional label, optional sort_order.
    """
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        conn.execute("DELETE FROM listing_images WHERE listing_id = ?", (listing_id,))
        for img in images:
            conn.execute(
                """INSERT INTO listing_images(listing_id, kind, rel_path, label, sort_order)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    listing_id,
                    img["kind"],
                    img["rel_path"],
                    img.get("label"),
                    int(img.get("sort_order", 0)),
                ),
            )
        return len(images)
    finally:
        if owns_conn:
            conn.close()


def replace_transports(
    listing_id: int,
    transports: list[dict[str, Any]],
    conn: sqlite3.Connection | None = None,
) -> int:
    """Drop existing transport rows for a listing and insert the given set."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        conn.execute("DELETE FROM listing_transports WHERE listing_id = ?", (listing_id,))
        for i, t in enumerate(transports):
            conn.execute(
                """INSERT INTO listing_transports(listing_id, line, station, walk_minutes, sort_order)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    listing_id,
                    t.get("line"),
                    t.get("station"),
                    t.get("walk_minutes"),
                    int(t.get("sort_order", i)),
                ),
            )
        return len(transports)
    finally:
        if owns_conn:
            conn.close()


def get_listing_images(
    listing_id: int,
    kind: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        if kind:
            rows = conn.execute(
                """SELECT id, kind, rel_path, label, sort_order
                   FROM listing_images
                   WHERE listing_id = ? AND kind = ?
                   ORDER BY sort_order, id""",
                (listing_id, kind),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, kind, rel_path, label, sort_order
                   FROM listing_images
                   WHERE listing_id = ?
                   ORDER BY kind, sort_order, id""",
                (listing_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def get_listing_transports(
    listing_id: int,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            """SELECT line, station, walk_minutes, sort_order
               FROM listing_transports
               WHERE listing_id = ?
               ORDER BY sort_order, id""",
            (listing_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def get_listing_with_relations(
    listing_id: int,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Listing + transports[] + images[] + price_history[] in one bundle."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if row is None:
            return None
        listing = Listing.from_row(row)
        transports = get_listing_transports(listing_id, conn=conn)
        images = get_listing_images(listing_id, conn=conn)
        price_history_rows = conn.execute(
            "SELECT price_man, observed_at FROM price_history WHERE listing_id = ? ORDER BY observed_at",
            (listing_id,),
        ).fetchall()
        return {
            "listing": listing,
            "transports": transports,
            "images": images,
            "price_history": [dict(h) for h in price_history_rows],
        }
    finally:
        if owns_conn:
            conn.close()


def to_dict(listing: Listing) -> dict[str, Any]:
    d = {
        "id": listing.id,
        "external_id": listing.reins_id,
        "title": listing.title,
        "building_name": listing.building_name,
        "address": listing.address,
        "prefecture": listing.prefecture,
        "city": listing.city,
        "ward": listing.ward,
        "station": listing.station,
        "station_line": listing.station_line,
        "walk_minutes": listing.walk_minutes,
        "layout": listing.layout,
        "area_sqm": listing.area_sqm,
        "price_man": listing.price_man,
        "built_year": listing.built_year,
        "floor": listing.floor,
        "total_floors": listing.total_floors,
        "url": listing.url,
    }
    d.update({k: v for k, v in listing.extra.items() if v is not None})
    return d
