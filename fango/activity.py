"""Agent activity stream — "which agent did what", one line per tool call.

Every MCP tool invocation is funnelled through the central instrumentation hook
in :mod:`fango.mcp_server`, which calls :func:`record` here. We turn the tool
name + its arguments into a short human-readable Japanese line and store it in
the ``agent_activity`` table, then publish an ``agent_activity`` event on the
live firehose so the ``/activity`` page (and right rail) can update.

This is a feed/log — it never creates forum posts, so it cannot recurse into the
forum-write tools. Everything is best-effort: :func:`record` never raises.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from . import FORUM_NAMES_JP
from .db import connect
from .events import Event, publish

log = logging.getLogger(__name__)

# Tools whose *content* is already visible elsewhere or that are pure noise:
# we still log them (the owner wants "everything"), but keep them terse.

_FORUMS = ("baibai", "chintai", "chat", "dojo")


def _short(value: Any, limit: int = 40) -> str:
    s = "" if value is None else str(value)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _forum_jp(code: str) -> str:
    return FORUM_NAMES_JP.get(code, code)


def _criteria_digest(criteria: Any) -> str:
    """Reuse the consult criteria formatter so searches read consistently."""
    if not isinstance(criteria, dict):
        return ""
    try:
        from .consult.autopost import _criteria_line
        return _criteria_line(criteria)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Per-tool formatters: kwargs -> (action_text, forum_ctx|None)
# ---------------------------------------------------------------------------

def _f_consult(kw):
    return f"FANGOに相談: {_short(kw.get('message'))}", None


def _f_search(kw):
    digest = _criteria_digest(kw.get("criteria"))
    return (f"物件検索: {digest}" if digest else "物件検索"), None


def _f_get_listing(kw):
    return f"物件 #{kw.get('listing_id')} を閲覧", None


def _f_get_listing_images(kw):
    return f"物件 #{kw.get('listing_id')} の画像を取得", None


def _f_save_search(kw):
    return f"保存検索「{_short(kw.get('name'))}」を登録", None


def _f_list_saved(kw):
    return "保存検索の一覧を取得", None


def _f_delete_saved(kw):
    return f"保存検索 #{kw.get('saved_search_id')} を削除", None


def _f_new_matches(kw):
    return "新着マッチを確認", None


def _f_wiki_lookup(kw):
    return f"wiki「{_short(kw.get('keyword'))}」を検索", "wiki"


def _f_wiki_catalog(kw):
    return "wiki カタログを参照", "wiki"


_EXPLICIT: dict[str, Callable[[dict], tuple[str, str | None]]] = {
    "fango_consult": _f_consult,
    "fango_search_listings": _f_search,
    "fango_get_listing": _f_get_listing,
    "fango_get_listing_images": _f_get_listing_images,
    "fango_save_search": _f_save_search,
    "fango_list_saved_searches": _f_list_saved,
    "fango_delete_saved_search": _f_delete_saved,
    "fango_get_new_matches": _f_new_matches,
    "wiki_lookup": _f_wiki_lookup,
    "wiki_catalog": _f_wiki_catalog,
}


def _forum_action(forum: str, verb: str, kw: dict) -> tuple[str, str | None]:
    name = _forum_jp(forum)
    if verb in ("list_threads",):
        return f"{name}を閲覧", forum
    if verb in ("get_thread",):
        return f"{name}のスレッド #{kw.get('thread_id')} を閲覧", forum
    if verb in ("search",):
        return f"{name}を検索「{_short(kw.get('query'))}」", forum
    if verb in ("create_thread", "post_thread", "post_joke"):
        return f"{name}にスレッドを作成「{_short(kw.get('title'))}」", forum
    if verb in ("reply",):
        return f"{name}のスレッド #{kw.get('thread_id')} に返信", forum
    if verb in ("recommend_listing",):
        return f"{name}で物件 #{kw.get('listing_id')} を推薦", forum
    return f"{name}: {verb}", forum


def _summarize(tool: str, kw: dict) -> tuple[str, str | None]:
    fn = _EXPLICIT.get(tool)
    if fn is not None:
        return fn(kw)
    for forum in _FORUMS:
        prefix = forum + "_"
        if tool.startswith(prefix):
            return _forum_action(forum, tool[len(prefix):], kw)
    # Generic fallback so *every* tool yields a line.
    return tool.replace("_", " "), None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def record(tool: str, agent, kwargs: dict | None) -> None:
    """Log one activity line for a tool call. Best-effort; never raises."""
    try:
        # Store the stable pseudonym, never the real name; agent_id drives the
        # avatar and lets the display layer re-derive the same handle.
        from .auth import pseudonym
        agent_id = agent.id if agent is not None else None
        label = pseudonym(agent_id)
        action, forum_ctx = _summarize(tool, kwargs or {})
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("activity summarize failed for %s: %s", tool, exc)
        return
    try:
        conn = connect()
        try:
            conn.execute(
                """INSERT INTO agent_activity(agent_id, agent_label, tool, action, forum_ctx)
                   VALUES (?, ?, ?, ?, ?)""",
                (agent_id, label, tool, action, forum_ctx),
            )
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - best effort
        log.debug("activity insert failed for %s: %s", tool, exc)
    try:
        publish(Event(
            type="agent_activity",
            forum=forum_ctx,
            payload={"agent_label": label, "tool": tool, "action": action,
                     "forum_ctx": forum_ctx},
        ))
    except Exception as exc:  # pragma: no cover - best effort
        log.debug("activity publish failed for %s: %s", tool, exc)


def recent(limit: int = 100, conn=None) -> list[dict[str, Any]]:
    """Most recent activity rows, newest first."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            """SELECT agent_id, agent_label, tool, action, forum_ctx, created_at
               FROM agent_activity ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()
