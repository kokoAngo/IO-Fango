"""Shared thread / post helpers used by all forum services."""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from . import FORUMS
from .db import connect, transaction
from .events import Event, publish
from .models import Post, Thread


class ForumError(Exception):
    """Raised for invalid forum / thread / post operations."""


def _ensure_forum(forum: str) -> None:
    if forum not in FORUMS:
        raise ForumError(f"unknown forum: {forum}")


def create_thread(
    forum: str,
    title: str,
    body: str,
    author_id: int,
    *,
    locked: bool = False,
    tags: Iterable[str] = (),
    conn: sqlite3.Connection | None = None,
) -> tuple[Thread, Post]:
    _ensure_forum(forum)
    title = title.strip()
    body = body.strip()
    if not title:
        raise ForumError("title required")
    if not body:
        raise ForumError("body required")

    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO threads(forum, title, author_id, locked) VALUES (?, ?, ?, ?)",
                (forum, title, author_id, 1 if locked else 0),
            )
            thread_id = cur.lastrowid
            cur = conn.execute(
                "INSERT INTO posts(thread_id, author_id, body) VALUES (?, ?, ?)",
                (thread_id, author_id, body),
            )
            post_id = cur.lastrowid
            for t in _clean_tags(tags):
                conn.execute(
                    "INSERT OR IGNORE INTO post_tags(post_id, tag) VALUES (?, ?)",
                    (post_id, t),
                )
        thread = _load_thread(conn, thread_id)
        post = _load_post(conn, post_id)
        publish(Event(
            type="new_thread",
            forum=forum,
            thread_id=thread.id,
            post_id=post.id,
            payload={"title": thread.title, "author_id": author_id},
        ))
        return thread, post
    finally:
        if owns_conn:
            conn.close()


def reply(
    forum: str,
    thread_id: int,
    body: str,
    author_id: int,
    *,
    reply_to: int | None = None,
    tags: Iterable[str] = (),
    conn: sqlite3.Connection | None = None,
) -> Post:
    _ensure_forum(forum)
    body = body.strip()
    if not body:
        raise ForumError("body required")

    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        with transaction(conn):
            thread = conn.execute(
                "SELECT * FROM threads WHERE id = ? AND forum = ?", (thread_id, forum)
            ).fetchone()
            if thread is None:
                raise ForumError(f"thread {thread_id} not in forum {forum}")
            if thread["locked"]:
                raise ForumError(f"thread {thread_id} is locked")
            if reply_to is not None:
                rt = conn.execute(
                    "SELECT id FROM posts WHERE id = ? AND thread_id = ?",
                    (reply_to, thread_id),
                ).fetchone()
                if rt is None:
                    raise ForumError(f"reply_to post {reply_to} not in thread {thread_id}")
            cur = conn.execute(
                "INSERT INTO posts(thread_id, author_id, reply_to, body) VALUES (?, ?, ?, ?)",
                (thread_id, author_id, reply_to, body),
            )
            post_id = cur.lastrowid
            conn.execute(
                "UPDATE threads SET last_activity_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (thread_id,),
            )
            for t in _clean_tags(tags):
                conn.execute(
                    "INSERT OR IGNORE INTO post_tags(post_id, tag) VALUES (?, ?)",
                    (post_id, t),
                )
        post = _load_post(conn, post_id)
        publish(Event(
            type="new_post",
            forum=forum,
            thread_id=thread_id,
            post_id=post.id,
            payload={"author_id": author_id, "reply_to": reply_to},
        ))
        return post
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Image attachments
# ---------------------------------------------------------------------------

# Default URL allow-list. The MCP tool refuses attachment URLs whose host
# doesn't end in one of these suffixes. Override at startup with the env
# var ``FANGO_ATTACHMENT_HOSTS=fango.io.ngrok.app,i.imgur.com,...``.
_DEFAULT_ATTACHMENT_HOSTS: tuple[str, ...] = (
    "fango.io.ngrok.app",
    "ngrok.app",
    "ngrok-free.app",
    "trycloudflare.com",
    "serveousercontent.com",
    "localhost",
    "127.0.0.1",
    "i.imgur.com",
    "imgur.com",
    "pbs.twimg.com",
)

MAX_ATTACHMENTS_PER_POST = 10
MAX_ATTACHMENT_URL_LEN = 2048


def _attachment_hosts() -> tuple[str, ...]:
    import os
    raw = os.environ.get("FANGO_ATTACHMENT_HOSTS")
    if not raw:
        return _DEFAULT_ATTACHMENT_HOSTS
    return tuple(h.strip() for h in raw.split(",") if h.strip())


def _validate_attachment_url(url: str) -> str:
    """Normalised URL, or raises ForumError. Rejects non-https, oversize,
    or hosts outside the allow-list."""
    import re as _re
    from urllib.parse import urlparse

    url = (url or "").strip()
    if not url:
        raise ForumError("attachment URL is empty")
    if len(url) > MAX_ATTACHMENT_URL_LEN:
        raise ForumError(f"attachment URL longer than {MAX_ATTACHMENT_URL_LEN} chars")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ForumError("attachment URL must use http or https")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ForumError("non-localhost attachment URLs must use https")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ForumError("attachment URL must include a host")
    allowed = _attachment_hosts()
    if not any(host == h or host.endswith("." + h) for h in allowed):
        raise ForumError(
            f"attachment host {host!r} not allowed; "
            f"hosts allowed: {', '.join(allowed)}"
        )
    return url


def attach_image(
    post_id: int,
    url: str,
    label: str | None = None,
    sort_order: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Attach one image URL to ``post_id``. Returns the new attachment id
    (or the existing id if the URL was already attached). Enforces the
    per-post cap and the URL allow-list."""
    url = _validate_attachment_url(url)
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        p = conn.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone()
        if p is None:
            raise ForumError(f"post {post_id} not found")
        existing = conn.execute(
            "SELECT COUNT(*) AS n FROM post_attachments WHERE post_id = ?", (post_id,),
        ).fetchone()["n"]
        if existing >= MAX_ATTACHMENTS_PER_POST:
            raise ForumError(
                f"post already has {existing} attachments (max {MAX_ATTACHMENTS_PER_POST})"
            )
        if sort_order is None:
            sort_order = existing
        cur = conn.execute(
            """INSERT OR IGNORE INTO post_attachments(post_id, url, label, sort_order)
               VALUES (?, ?, ?, ?)""",
            (post_id, url, label, int(sort_order)),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT id FROM post_attachments WHERE post_id = ? AND url = ?",
                (post_id, url),
            ).fetchone()
            return row["id"] if row else 0
        return cur.lastrowid
    finally:
        if owns_conn:
            conn.close()


def list_attachments(
    post_id: int, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            """SELECT id, url, label, sort_order
               FROM post_attachments WHERE post_id = ?
               ORDER BY sort_order, id""",
            (post_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def attach_link_preview(
    post_id: int,
    url: str,
    *,
    image_url: str | None = None,
    title: str | None = None,
    description: str | None = None,
    source: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Attach an unfurled external link (OGP preview) to ``post_id``. ``url`` is
    the source page (e.g. a SUUMO/HOMES listing); ``image_url`` is a self-hosted
    copy of og:image. Idempotent per (post, url). Returns the row id."""
    if not (url or "").strip():
        raise ForumError("link preview url is empty")
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        p = conn.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone()
        if p is None:
            raise ForumError(f"post {post_id} not found")
        cur = conn.execute(
            """INSERT OR IGNORE INTO post_link_previews(
                   post_id, url, image_url, title, description, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (post_id, url.strip(), image_url, title, description, source),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT id FROM post_link_previews WHERE post_id = ? AND url = ?",
                (post_id, url.strip()),
            ).fetchone()
            return row["id"] if row else 0
        return cur.lastrowid
    finally:
        if owns_conn:
            conn.close()


def list_link_previews(
    post_id: int, conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            """SELECT id, url, image_url, title, description, source
               FROM post_link_previews WHERE post_id = ?
               ORDER BY id""",
            (post_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def attach_listing(post_id: int, listing_id: int, note: str | None = None,
                   conn: sqlite3.Connection | None = None) -> int:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        l = conn.execute("SELECT id FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if l is None:
            raise ForumError(f"listing {listing_id} not found")
        cur = conn.execute(
            "INSERT OR IGNORE INTO post_listing_refs(post_id, listing_id, note) VALUES (?, ?, ?)",
            (post_id, listing_id, note),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT id FROM post_listing_refs WHERE post_id = ? AND listing_id = ?",
                (post_id, listing_id),
            ).fetchone()
            return row["id"] if row else 0
        return cur.lastrowid
    finally:
        if owns_conn:
            conn.close()


def toggle_like(post_id: int, agent_id: int, conn: sqlite3.Connection | None = None) -> tuple[bool, int]:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        with transaction(conn):
            existing = conn.execute(
                "SELECT 1 FROM post_likes WHERE post_id = ? AND agent_id = ?",
                (post_id, agent_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "DELETE FROM post_likes WHERE post_id = ? AND agent_id = ?",
                    (post_id, agent_id),
                )
                liked = False
            else:
                conn.execute(
                    "INSERT INTO post_likes(post_id, agent_id) VALUES (?, ?)",
                    (post_id, agent_id),
                )
                liked = True
            count_row = conn.execute(
                "SELECT COUNT(*) AS n FROM post_likes WHERE post_id = ?", (post_id,)
            ).fetchone()
            thread_row = conn.execute(
                "SELECT t.id, t.forum FROM posts p JOIN threads t ON t.id = p.thread_id WHERE p.id = ?",
                (post_id,),
            ).fetchone()
        publish(Event(
            type="like_change",
            forum=thread_row["forum"] if thread_row else None,
            thread_id=thread_row["id"] if thread_row else None,
            post_id=post_id,
            payload={"liked": liked, "like_count": count_row["n"], "agent_id": agent_id},
        ))
        return liked, count_row["n"]
    finally:
        if owns_conn:
            conn.close()


def list_threads(
    forum: str,
    *,
    tag: str | None = None,
    limit: int = 50,
    offset: int = 0,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    _ensure_forum(forum)
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        params: list[Any] = [forum]
        join = ""
        where = "t.forum = ?"
        if tag:
            join = """JOIN posts p ON p.thread_id = t.id
                      JOIN post_tags pt ON pt.post_id = p.id"""
            where += " AND pt.tag = ?"
            params.append(tag)
        params.extend([limit, offset])
        sql = f"""
            SELECT DISTINCT t.*,
                   (SELECT COUNT(*) FROM posts WHERE thread_id = t.id) AS post_count
            FROM threads t
            {join}
            WHERE {where}
            ORDER BY t.last_activity_at DESC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "thread": Thread.from_row(r),
                "post_count": r["post_count"],
            }
            for r in rows
        ]
    finally:
        if owns_conn:
            conn.close()


def get_thread(
    forum: str,
    thread_id: int,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    _ensure_forum(forum)
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        t_row = conn.execute(
            "SELECT * FROM threads WHERE id = ? AND forum = ?", (thread_id, forum)
        ).fetchone()
        if t_row is None:
            return None
        post_rows = conn.execute(
            "SELECT * FROM posts WHERE thread_id = ? ORDER BY created_at, id",
            (thread_id,),
        ).fetchall()
        posts: list[Post] = []
        for r in post_rows:
            post = Post.from_row(r)
            post.tags = [
                row["tag"] for row in conn.execute(
                    "SELECT tag FROM post_tags WHERE post_id = ?", (post.id,)
                )
            ]
            post.listing_refs = [
                row["listing_id"] for row in conn.execute(
                    "SELECT listing_id FROM post_listing_refs WHERE post_id = ?", (post.id,)
                )
            ]
            post.attachments = [
                {"url": row["url"], "label": row["label"], "sort_order": row["sort_order"]}
                for row in conn.execute(
                    "SELECT url, label, sort_order FROM post_attachments "
                    "WHERE post_id = ? ORDER BY sort_order, id", (post.id,),
                )
            ]
            post.link_previews = [
                dict(row) for row in conn.execute(
                    "SELECT url, image_url, title, description, source "
                    "FROM post_link_previews WHERE post_id = ? ORDER BY id", (post.id,),
                )
            ]
            lc = conn.execute(
                "SELECT COUNT(*) AS n FROM post_likes WHERE post_id = ?", (post.id,)
            ).fetchone()
            post.like_count = lc["n"]
            posts.append(post)
        return {"thread": Thread.from_row(t_row), "posts": posts}
    finally:
        if owns_conn:
            conn.close()


def search_posts(
    forum: str,
    query: str,
    *,
    limit: int = 50,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    _ensure_forum(forum)
    q = (query or "").strip()
    if not q:
        return []
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        if len(q) < 3:
            # FTS5 trigram requires ≥3 codepoints; fall back to LIKE.
            rows = conn.execute(
                """
                SELECT p.*, t.title AS thread_title, t.forum AS forum
                FROM posts p
                JOIN threads t ON t.id = p.thread_id
                WHERE p.body LIKE ? AND t.forum = ?
                ORDER BY p.created_at DESC
                LIMIT ?
                """,
                (f"%{q}%", forum, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT p.*, t.title AS thread_title, t.forum AS forum
                FROM posts_fts f
                JOIN posts p ON p.id = f.rowid
                JOIN threads t ON t.id = p.thread_id
                WHERE posts_fts MATCH ? AND t.forum = ?
                ORDER BY p.created_at DESC
                LIMIT ?
                """,
                (q, forum, limit),
            ).fetchall()
        return [
            {"post": Post.from_row(r), "thread_title": r["thread_title"], "forum": r["forum"]}
            for r in rows
        ]
    finally:
        if owns_conn:
            conn.close()


def _load_thread(conn: sqlite3.Connection, thread_id: int) -> Thread:
    row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return Thread.from_row(row)


def _load_post(conn: sqlite3.Connection, post_id: int) -> Post:
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    return Post.from_row(row)


def _clean_tags(tags: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in tags or ():
        t = (t or "").strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out
