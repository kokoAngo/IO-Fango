"""wiki — read-only cross-forum aggregator."""
from __future__ import annotations

import sqlite3
from typing import Any

from .. import FORUMS
from ..db import connect
from ..models import Listing, Post, Thread


def lookup(keyword: str, *, limit_per_section: int = 20,
           conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Return posts (across all 4 forums) + listings + co-occurring tags for ``keyword``."""
    q = (keyword or "").strip()
    if not q:
        return {"keyword": keyword, "posts": [], "listings": [], "tags": []}
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        short = len(q) < 3
        if short:
            post_rows = conn.execute(
                """
                SELECT p.*, t.forum AS forum, t.title AS thread_title
                FROM posts p
                JOIN threads t ON t.id = p.thread_id
                WHERE p.body LIKE ?
                ORDER BY p.created_at DESC
                LIMIT ?
                """,
                (f"%{q}%", limit_per_section),
            ).fetchall()
        else:
            post_rows = conn.execute(
                """
                SELECT p.*, t.forum AS forum, t.title AS thread_title
                FROM posts_fts f
                JOIN posts p ON p.id = f.rowid
                JOIN threads t ON t.id = p.thread_id
                WHERE posts_fts MATCH ?
                ORDER BY p.created_at DESC
                LIMIT ?
                """,
                (q, limit_per_section),
            ).fetchall()
        posts = [
            {"post": Post.from_row(r), "forum": r["forum"], "thread_title": r["thread_title"]}
            for r in post_rows
        ]

        if short:
            like = f"%{q}%"
            listing_rows = conn.execute(
                """
                SELECT * FROM listings
                WHERE building_name LIKE ? OR address LIKE ? OR station LIKE ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (like, like, like, limit_per_section),
            ).fetchall()
        else:
            listing_rows = conn.execute(
                """
                SELECT l.*
                FROM listings_fts f
                JOIN listings l ON l.id = f.rowid
                WHERE listings_fts MATCH ?
                ORDER BY l.updated_at DESC
                LIMIT ?
                """,
                (q, limit_per_section),
            ).fetchall()
        listings = [Listing.from_row(r) for r in listing_rows]

        # Co-occurring tags
        if short:
            tag_rows = conn.execute(
                """
                SELECT pt.tag AS tag, COUNT(*) AS n
                FROM posts p
                JOIN post_tags pt ON pt.post_id = p.id
                WHERE p.body LIKE ?
                GROUP BY pt.tag
                ORDER BY n DESC
                LIMIT ?
                """,
                (f"%{q}%", limit_per_section),
            ).fetchall()
        else:
            tag_rows = conn.execute(
                """
                SELECT pt.tag AS tag, COUNT(*) AS n
                FROM posts_fts f
                JOIN post_tags pt ON pt.post_id = f.rowid
                WHERE posts_fts MATCH ?
                GROUP BY pt.tag
                ORDER BY n DESC
                LIMIT ?
                """,
                (q, limit_per_section),
            ).fetchall()
        tags = [{"tag": r["tag"], "count": r["n"]} for r in tag_rows]

        return {"keyword": keyword, "posts": posts, "listings": listings, "tags": tags}
    finally:
        if owns_conn:
            conn.close()


def catalog(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """High-level numbers across the system."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        counts = {}
        for forum in FORUMS:
            r = conn.execute(
                "SELECT COUNT(*) AS n FROM threads WHERE forum = ?", (forum,)
            ).fetchone()
            counts[forum] = r["n"]
        listings_n = conn.execute("SELECT COUNT(*) AS n FROM listings").fetchone()["n"]
        agents_n = conn.execute("SELECT COUNT(*) AS n FROM agents WHERE active = 1").fetchone()["n"]
        mcp_calls_n = conn.execute(
            "SELECT COUNT(*) AS n FROM rate_limit_events WHERE scope = 'mcp_call'"
        ).fetchone()["n"]
        return {
            "thread_counts": counts,
            "listing_count": listings_n,
            "active_agents": agents_n,
            "mcp_call_count": mcp_calls_n,
        }
    finally:
        if owns_conn:
            conn.close()
