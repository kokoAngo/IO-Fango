"""47 都道府県 — canonical list, normalization, aggregation helpers."""
from __future__ import annotations

import sqlite3
from typing import Any

from .db import connect

PREFECTURES: tuple[str, ...] = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
)

# Short-stem → canonical full form. Forgives sloppy input like "東京" or "大阪".
def _stem(p: str) -> str:
    if p == "北海道":
        return "北海道"
    return p[:-1]  # strip 県/都/府

_STEMS: dict[str, str] = {_stem(p): p for p in PREFECTURES}


def normalize(name: str | None) -> str | None:
    if name is None:
        return None
    s = name.strip()
    if not s:
        return None
    if s in PREFECTURES:
        return s
    if s in _STEMS:
        return _STEMS[s]
    # If the address starts with a prefecture, peel it.
    for p in PREFECTURES:
        if s.startswith(p):
            return p
    # Try stem at start
    for stem, full in _STEMS.items():
        if stem and s.startswith(stem):
            return full
    return None


def aggregate_listing_heat(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """For each prefecture, count posts whose referenced listings live there."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT l.prefecture AS prefecture, COUNT(DISTINCT p.id) AS post_count
            FROM posts p
            JOIN post_listing_refs r ON r.post_id = p.id
            JOIN listings l ON l.id = r.listing_id
            WHERE l.prefecture IS NOT NULL
            GROUP BY l.prefecture
            ORDER BY post_count DESC
            """
        ).fetchall()
        return [{"prefecture": r["prefecture"], "post_count": r["post_count"]} for r in rows]
    finally:
        if owns_conn:
            conn.close()


def listings_per_prefecture(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            "SELECT prefecture, COUNT(*) AS n FROM listings WHERE prefecture IS NOT NULL GROUP BY prefecture"
        ).fetchall()
        return {r["prefecture"]: r["n"] for r in rows}
    finally:
        if owns_conn:
            conn.close()
