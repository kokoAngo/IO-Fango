"""Consult → broker inquiry routing + poll/ack journal.

When a customer consult reaches a searchable state, the platform matches the
demand against broker-owned inventory and fans the inquiry out to the matching
brokers (:func:`match_brokers_for` + :func:`route_inquiry`). Brokers — who are
external and not synchronously online — drain new inquiries with
:func:`fetch_new_inquiries` (mark-on-read, mirror of saved_search) and reply
into the same forum thread. Mirrors :mod:`fango.listings.saved_search`.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any

from ..db import connect, transaction

log = logging.getLogger(__name__)

# Strip the Japanese admin suffix so 新宿区 (query) matches 新宿 (broker area).
_ADMIN_SUFFIX = re.compile(r"[都道府県区市町村]+$")


def _area_core(s: Any) -> str:
    return _ADMIN_SUFFIX.sub("", str(s or "").strip())


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_brokers_by_area(
    criteria: dict[str, Any],
    limit: int = 3,
    conn: sqlite3.Connection | None = None,
) -> list[int]:
    """Brokers whose declared service areas cover the query's ward/city.

    Area-based routing: a broker that covers 新宿 gets 新宿 inquiries even when its
    specific listings weren't in the shown results. Matches on the ward/city core
    (新宿区 → 新宿), ignoring the admin suffix, against each broker's ``areas``.
    Prefecture alone is too broad to route on, so it is not used.
    """
    cores = {_area_core(criteria.get(k)) for k in ("ward", "city")}
    cores = {c for c in cores if c}
    if not cores:
        return []
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        out: list[int] = []
        for r in conn.execute(
            "SELECT agent_id, areas_json FROM brokers WHERE active = 1"
        ).fetchall():
            try:
                areas = json.loads(r["areas_json"] or "[]")
            except (ValueError, TypeError):
                areas = []
            if cores & {_area_core(a) for a in areas}:
                out.append(r["agent_id"])
            if len(out) >= limit:
                break
        return out
    finally:
        if owns_conn:
            conn.close()

def match_brokers_for(
    criteria: dict[str, Any],
    limit: int = 3,
    conn: sqlite3.Connection | None = None,
) -> list[tuple[int, int]]:
    """Rank brokers by how many of their (advertisable) listings fit ``criteria``.

    Returns ``[(broker_agent_id, match_count), ...]`` best-first, capped at
    ``limit``. Reuses :func:`fango.listings.service._build_search_where` via the
    ``broker_owned_only`` predicate so it stays in lockstep with normal search
    semantics (including the advertising-compliance gate).
    """
    from ..listings.service import _build_search_where
    crit = dict(criteria or {})
    crit["broker_owned_only"] = True
    where_sql, params, join = _build_search_where(crit)
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        sql = f"""
            SELECT listings.broker_agent_id AS bid, COUNT(*) AS score
            FROM listings
            {join}
            {('WHERE ' + where_sql) if where_sql else ''}
            GROUP BY listings.broker_agent_id
            ORDER BY score DESC
            LIMIT ?
        """
        rows = conn.execute(sql, [*params, limit]).fetchall()
        return [(r["bid"], r["score"]) for r in rows if r["bid"] is not None]
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Storage + fan-out
# ---------------------------------------------------------------------------

def create_inquiry(
    customer_agent_id: int,
    criteria: dict[str, Any],
    *,
    consult_session_id: str | None = None,
    thread_id: int | None = None,
    forum: str | None = None,
    matched_listing_ids: list[int] | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO broker_inquiries
               (customer_agent_id, consult_session_id, thread_id, forum,
                criteria_json, matched_listing_ids_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                customer_agent_id,
                consult_session_id,
                thread_id,
                forum,
                json.dumps(criteria or {}, ensure_ascii=False),
                json.dumps(matched_listing_ids or [], ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)
    finally:
        if owns_conn:
            conn.close()


def route_inquiry(
    inquiry_id: int,
    broker_scores: list[tuple[int, int]],
    conn: sqlite3.Connection | None = None,
) -> int:
    """Fan an inquiry out to matched brokers. Idempotent per (inquiry, broker).

    Returns the number of new route rows inserted.
    """
    if not broker_scores:
        return 0
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    inserted = 0
    try:
        with transaction(conn):
            for broker_agent_id, score in broker_scores:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO broker_inquiry_routes
                       (inquiry_id, broker_agent_id, match_score) VALUES (?, ?, ?)""",
                    (inquiry_id, broker_agent_id, score),
                )
                if cur.rowcount > 0:
                    inserted += 1
        return inserted
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Broker-side polling
# ---------------------------------------------------------------------------

def _hydrate_inquiry_row(r: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
    from ..listings import service as svc
    from ..listings.tools import _listing_brief
    try:
        listing_ids = json.loads(r["matched_listing_ids_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        listing_ids = []
    matched = []
    for lid in listing_ids:
        listing = svc.get_listing(lid, conn=conn)
        if listing is not None:
            matched.append(_listing_brief(listing, conn=conn))
    try:
        crit = json.loads(r["criteria_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        crit = {}
    return {
        "inquiry_id": r["inquiry_id"],
        "thread_id": r["thread_id"],
        "forum": r["forum"],
        "criteria": crit,
        "matched_listings": matched,
        "match_score": r["match_score"] if "match_score" in r.keys() else None,
        "created_at": r["created_at"],
        "status": r["status"],
    }


def fetch_new_inquiries(
    broker_agent_id: int,
    limit: int = 50,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Drain un-notified inquiries routed to this broker and mark them notified.

    Fetch-and-ack: a successfully returned inquiry won't reappear. The customer
    identity is intentionally NOT exposed — the broker replies into the thread.
    """
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            """SELECT r.id AS route_id, r.match_score, i.id AS inquiry_id,
                      i.thread_id, i.forum, i.criteria_json,
                      i.matched_listing_ids_json, i.created_at, i.status
               FROM broker_inquiry_routes r
               JOIN broker_inquiries i ON i.id = r.inquiry_id
               WHERE r.broker_agent_id = ? AND r.notified_at IS NULL
               ORDER BY r.matched_at DESC
               LIMIT ?""",
            (broker_agent_id, limit),
        ).fetchall()
        out = [_hydrate_inquiry_row(r, conn) for r in rows]
        route_ids = [r["route_id"] for r in rows]
        if route_ids:
            placeholders = ",".join("?" * len(route_ids))
            with transaction(conn):
                conn.execute(
                    f"""UPDATE broker_inquiry_routes
                        SET notified_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                        WHERE id IN ({placeholders})""",
                    route_ids,
                )
        return out
    finally:
        if owns_conn:
            conn.close()


def list_inquiries(
    broker_agent_id: int,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Non-destructive listing of inquiries routed to this broker (no ack)."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        sql = (
            "SELECT r.match_score, i.id AS inquiry_id, i.thread_id, i.forum, "
            "i.criteria_json, i.matched_listing_ids_json, i.created_at, i.status "
            "FROM broker_inquiry_routes r "
            "JOIN broker_inquiries i ON i.id = r.inquiry_id "
            "WHERE r.broker_agent_id = ?"
        )
        params: list[Any] = [broker_agent_id]
        if status:
            sql += " AND i.status = ?"
            params.append(status)
        sql += " ORDER BY i.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        return [_hydrate_inquiry_row(r, conn) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def get_routed_inquiry(
    inquiry_id: int,
    broker_agent_id: int,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    """Return an inquiry ONLY if it was routed to this broker, else None.

    Used by ``broker_respond`` to enforce that a broker can only reply to its
    own routed inquiries.
    """
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            """SELECT i.id, i.thread_id, i.forum, i.criteria_json,
                      i.matched_listing_ids_json, i.status, i.customer_agent_id
               FROM broker_inquiries i
               JOIN broker_inquiry_routes r ON r.inquiry_id = i.id
               WHERE i.id = ? AND r.broker_agent_id = ?""",
            (inquiry_id, broker_agent_id),
        ).fetchone()
        if row is None:
            return None
        try:
            listing_ids = json.loads(row["matched_listing_ids_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            listing_ids = []
        return {
            "inquiry_id": row["id"],
            "thread_id": row["thread_id"],
            "forum": row["forum"],
            "matched_listing_ids": listing_ids,
            "status": row["status"],
            "customer_agent_id": row["customer_agent_id"],
        }
    finally:
        if owns_conn:
            conn.close()


def mark_responded(
    inquiry_id: int,
    broker_agent_id: int,
    conn: sqlite3.Connection | None = None,
) -> None:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        with transaction(conn):
            conn.execute(
                "UPDATE broker_inquiries SET status = 'responded', "
                "responded_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (inquiry_id,),
            )
            conn.execute(
                "UPDATE broker_inquiry_routes SET status = 'accepted' "
                "WHERE inquiry_id = ? AND broker_agent_id = ?",
                (inquiry_id, broker_agent_id),
            )
    finally:
        if owns_conn:
            conn.close()
