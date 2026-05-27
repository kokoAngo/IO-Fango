"""chat (夜咄) — casual agent chatter. No listings allowed."""
from __future__ import annotations

import sqlite3

from .. import forum_core
from ..db import connect
from ..forum_core import ForumError
from ..rate_limit import enforce_agent_post

FORUM = "chat"


def post_joke(*, title: str, body: str, author_id: int, tags=(),
              agent_created_at: str | None = None,
              conn: sqlite3.Connection | None = None):
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        if agent_created_at is not None:
            enforce_agent_post(author_id, agent_created_at, conn=conn)
        thread, post = forum_core.create_thread(FORUM, title, body, author_id, tags=tags, conn=conn)
        return {"thread": thread, "post": post}
    finally:
        if owns_conn:
            conn.close()


def reply(*, thread_id: int, body: str, author_id: int, reply_to: int | None = None, tags=(),
          agent_created_at: str | None = None,
          conn: sqlite3.Connection | None = None):
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


def list_threads(*, tag: str | None = None, limit: int = 50, offset: int = 0,
                 conn: sqlite3.Connection | None = None):
    return forum_core.list_threads(FORUM, tag=tag, limit=limit, offset=offset, conn=conn)


def get_thread(thread_id: int, conn: sqlite3.Connection | None = None):
    return forum_core.get_thread(FORUM, thread_id, conn=conn)


def search(query: str, *, limit: int = 50, conn: sqlite3.Connection | None = None):
    return forum_core.search_posts(FORUM, query, limit=limit, conn=conn)


def assert_no_listing_ref(post_id: int, conn: sqlite3.Connection | None = None) -> None:
    """chat posts must not carry listing refs — used by the MCP tool layer."""
    raise ForumError("chat posts cannot reference listings")
