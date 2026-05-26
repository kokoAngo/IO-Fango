"""Saved-search subscriptions + match engine.

Each agent can persist a structured ``criteria`` dict (same shape as
``fango_search_listings``) under a name. When new listings are ingested the
caller fires :func:`match_new_listings` which intersects the new pool against
every active saved search and writes hits into ``saved_search_matches``.
Agents pull notifications via :func:`fango_get_new_matches`, which marks the
batch as ``notified``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from ..db import connect, transaction
from ..tool_helpers import auth, dump
from . import service as svc
from .tools import _listing_brief

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def create_saved_search(
    agent_id: int,
    name: str,
    criteria: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> int:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO saved_searches(agent_id, name, criteria_json)
               VALUES (?, ?, ?)""",
            (agent_id, name, json.dumps(criteria or {}, ensure_ascii=False)),
        )
        return int(cur.lastrowid)
    finally:
        if owns_conn:
            conn.close()


def list_saved_searches(
    agent_id: int,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            """SELECT id, name, criteria_json, active, created_at, last_run_at
               FROM saved_searches
               WHERE agent_id = ?
               ORDER BY created_at DESC""",
            (agent_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "criteria": json.loads(r["criteria_json"] or "{}"),
                "active": bool(r["active"]),
                "created_at": r["created_at"],
                "last_run_at": r["last_run_at"],
            }
            for r in rows
        ]
    finally:
        if owns_conn:
            conn.close()


def delete_saved_search(
    agent_id: int,
    saved_search_id: int,
    conn: sqlite3.Connection | None = None,
) -> bool:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        cur = conn.execute(
            "DELETE FROM saved_searches WHERE id = ? AND agent_id = ?",
            (saved_search_id, agent_id),
        )
        return cur.rowcount > 0
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Match engine
# ---------------------------------------------------------------------------

def match_new_listings(
    listing_ids: list[int],
    conn: sqlite3.Connection | None = None,
) -> int:
    """Score the given new listings against every active saved search.

    Hits are written to ``saved_search_matches`` (``INSERT OR IGNORE`` so a
    listing/search pair is recorded only once).  Returns the number of new
    rows inserted.
    """
    if not listing_ids:
        return 0
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    inserted = 0
    try:
        searches = conn.execute(
            """SELECT id, agent_id, name, criteria_json FROM saved_searches
               WHERE active = 1"""
        ).fetchall()
        for s in searches:
            try:
                crit = json.loads(s["criteria_json"] or "{}")
            except json.JSONDecodeError:
                log.warning("saved_search %d has invalid criteria_json; skipping", s["id"])
                continue
            crit = dict(crit)
            # Constrain the candidate pool to the new listings.
            crit["only_listing_ids"] = listing_ids
            matches = svc.search_listings(criteria=crit, limit=len(listing_ids), conn=conn)
            with transaction(conn):
                for listing in matches:
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO saved_search_matches
                           (saved_search_id, listing_id) VALUES (?, ?)""",
                        (s["id"], listing.id),
                    )
                    if cur.rowcount > 0:
                        inserted += 1
                conn.execute(
                    """UPDATE saved_searches
                       SET last_run_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                       WHERE id = ?""",
                    (s["id"],),
                )
        return inserted
    finally:
        if owns_conn:
            conn.close()


def fetch_new_matches(
    agent_id: int,
    limit: int = 50,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Return un-notified matches for the agent and mark them as notified.

    Mark-on-read is intentional: when an agent successfully receives the
    payload, the same listings won't appear again. For replay/inspection use
    the ``--no-mark`` shape isn't exposed yet — callers can query the table
    directly.
    """
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            """SELECT m.id AS match_id, m.saved_search_id, m.listing_id, m.matched_at,
                      s.name, s.criteria_json
               FROM saved_search_matches m
               JOIN saved_searches s ON s.id = m.saved_search_id
               WHERE s.agent_id = ? AND m.notified_at IS NULL
               ORDER BY m.matched_at DESC
               LIMIT ?""",
            (agent_id, limit),
        ).fetchall()
        out: list[dict[str, Any]] = []
        match_ids: list[int] = []
        for r in rows:
            listing = svc.get_listing(r["listing_id"], conn=conn)
            if listing is None:
                continue
            out.append({
                "saved_search_id": r["saved_search_id"],
                "name": r["name"],
                "matched_at": r["matched_at"],
                "listing": _listing_brief(listing, conn=conn),
            })
            match_ids.append(r["match_id"])
        if match_ids:
            placeholders = ",".join("?" * len(match_ids))
            with transaction(conn):
                conn.execute(
                    f"""UPDATE saved_search_matches
                        SET notified_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                        WHERE id IN ({placeholders})""",
                    match_ids,
                )
        return out
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# MCP registration (実行系 — needs agent auth)
# ---------------------------------------------------------------------------

def register(mcp) -> None:

    @mcp.tool()
    def fango_save_search(name: str, criteria: dict[str, Any]) -> dict[str, Any]:
        """Persist a search criteria dict for the current agent.

        Args:
            name: Human-readable label (e.g. "Setagaya 2LDK <300k").
            criteria: Same shape as ``fango_search_listings`` (price_min_man,
                rent_max_yen, prefecture, layout, etc.).
        """
        agent = auth()
        new_id = create_saved_search(agent.id, name, criteria or {})
        return {"id": new_id, "name": name}

    @mcp.tool()
    def fango_list_saved_searches() -> list[dict[str, Any]]:
        """List the current agent's saved searches."""
        agent = auth()
        return dump(list_saved_searches(agent.id))

    @mcp.tool()
    def fango_delete_saved_search(saved_search_id: int) -> dict[str, Any]:
        """Delete a saved search owned by the current agent."""
        agent = auth()
        ok = delete_saved_search(agent.id, saved_search_id)
        return {"ok": ok}

    @mcp.tool()
    def fango_get_new_matches(limit: int = 50) -> list[dict[str, Any]]:
        """Pull and acknowledge any unread matches for the current agent.

        Each call drains pending matches (marks them as notified) — agents
        should treat this as 'fetch & ack'.
        """
        agent = auth()
        return dump(fetch_new_matches(agent.id, limit=limit))
