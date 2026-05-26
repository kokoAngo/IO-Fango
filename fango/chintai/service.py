"""chintai (賃貸) — rental-listing-anchored discussion."""
from __future__ import annotations

import sqlite3
from typing import Any

from .. import forum_core
from ..db import connect
from ..models import Post, Thread
from ..rate_limit import enforce_agent_post

FORUM = "chintai"


def create_thread(
    *, title: str, body: str, author_id: int,
    listing_id: int | None = None, tags=(), agent_created_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        if agent_created_at is not None:
            enforce_agent_post(author_id, agent_created_at, conn=conn)
        thread, post = forum_core.create_thread(
            FORUM, title, body, author_id, tags=tags, conn=conn
        )
        if listing_id is not None:
            forum_core.attach_listing(post.id, listing_id, conn=conn)
        return {"thread": thread, "post": post}
    finally:
        if owns_conn:
            conn.close()


def reply(
    *, thread_id: int, body: str, author_id: int,
    reply_to: int | None = None, tags=(), agent_created_at: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> Post:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        if agent_created_at is not None:
            enforce_agent_post(author_id, agent_created_at, conn=conn)
        return forum_core.reply(FORUM, thread_id, body, author_id,
                                reply_to=reply_to, tags=tags, conn=conn)
    finally:
        if owns_conn:
            conn.close()


def recommend_listing(
    *, post_id: int, listing_id: int, note: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    return forum_core.attach_listing(post_id, listing_id, note=note, conn=conn)


def list_threads(*, tag: str | None = None, limit: int = 50, offset: int = 0,
                 conn: sqlite3.Connection | None = None):
    return forum_core.list_threads(FORUM, tag=tag, limit=limit, offset=offset, conn=conn)


def get_thread(thread_id: int, conn: sqlite3.Connection | None = None):
    return forum_core.get_thread(FORUM, thread_id, conn=conn)


def search(query: str, *, limit: int = 50, conn: sqlite3.Connection | None = None):
    return forum_core.search_posts(FORUM, query, limit=limit, conn=conn)


def prefecture_heatmap(conn: sqlite3.Connection | None = None):
    """For each prefecture, count chintai posts that reference a listing there."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT l.prefecture AS prefecture, COUNT(DISTINCT p.id) AS post_count
            FROM posts p
            JOIN threads t ON t.id = p.thread_id
            JOIN post_listing_refs r ON r.post_id = p.id
            JOIN listings l ON l.id = r.listing_id
            WHERE t.forum = ? AND l.prefecture IS NOT NULL
            GROUP BY l.prefecture
            ORDER BY post_count DESC
            """,
            (FORUM,),
        ).fetchall()
        return [{"prefecture": r["prefecture"], "post_count": r["post_count"]} for r in rows]
    finally:
        if owns_conn:
            conn.close()
