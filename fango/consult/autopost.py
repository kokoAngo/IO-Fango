"""Turn each ``fango_consult`` turn into a moderated, anonymous forum post.

Posting is no longer a separate, registration-gated action: an agent simply
talks to FANGO, and — once our LLM has cleared the content (legal/compliant and
on-topic) — the conversation is published. The consult *session* becomes one
forum thread; each *turn* becomes one reply, rendered as a 🧑 <pseudonym> /
🏠 FANGO dialogue card. Posts are attributed to the caller's **anonymous
identity** (a stable pseudonym), never their real name:
- keyed caller → their own agent id,
- keyless caller → a per-session minted anonymous agent
  (see :func:`fango.consult.session.get_or_create_post_agent_id`).

Routing picks the topically-correct board (売買/賃貸 by search criteria, else the
moderator's classification among the four forums).

Everything here is **best-effort**: :func:`record_turn` swallows all exceptions
so a posting/moderation failure can never break the consult reply. It returns a
small status dict the tool surfaces back to the agent.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import forum_core
from . import session as _ss

log = logging.getLogger(__name__)

# How many result listings to attach to a 'ready' turn's reply.
_MAX_ATTACHED = 3

# Forum routing.
_RENTAL_KEYS = ("rent_min_yen", "rent_max_yen")
_SALE_KEYS = ("price_min_man", "price_max_man", "price_man")
_SEARCH_KEYS = (
    "prefecture", "city", "ward", "station", "layout",
    "rent_min_yen", "rent_max_yen", "price_min_man", "price_max_man", "price_man",
    "area_min_sqm", "area_max_sqm", "walk_minutes_max", "built_year_min", "keyword",
)
_SEARCH_FORUMS = ("baibai", "chintai")

# A caller's consults group into one thread (per forum) while they keep talking;
# after this much idle time, the next consult opens a fresh thread.
_GROUP_IDLE_SECONDS = 2 * 3600


def record_turn(
    *,
    session: _ss.ConsultSession,
    user_message: str,
    reply: str,
    state: str,
    criteria: dict[str, Any],
    results: dict[str, Any] | None,
    keyed_agent_id: int | None,
    ip: str | None,
    compliant: bool,
    forum_class: str | None = None,
    area_key: str = "",
    conn=None,
) -> dict[str, Any]:
    """Publish this turn as a Q&A (question post + FANGO answer post). Never raises.

    The moderation verdict (``compliant`` + ``forum_class``) is folded into the
    intent-extraction call, so there is no extra LLM round-trip here. A
    non-compliant turn is held back. The caller is the keyed agent (``keyed_agent_id``)
    or a stable per-IP anonymous identity. Consecutive consults from the same
    caller+forum within ``_GROUP_IDLE_SECONDS`` continue the same thread; after a
    longer gap a new thread opens. Returns ``{"posted", "forum", "thread_id", "reason"}``.
    """
    try:
        from ..auth import get_or_create_anon_agent_for_ip, get_or_create_system_agent

        if not compliant:
            return {
                "posted": False,
                "forum": None,
                "thread_id": session.log_thread_id,
                "reason": "この内容は公開ガイドラインに合致しませんでした。",
            }

        # Deterministic PII safety net (defence-in-depth behind the LLM policy):
        # scrub names from everything we're about to publish, and hold the whole
        # turn if unambiguous contact info (email/phone) is present.
        from . import pii
        user_message, q_hits = pii.scrub_for_publish((user_message or "").strip())
        reply, _ = pii.scrub_for_publish(reply or "")
        if pii.BLOCKING & set(q_hits):
            return {
                "posted": False,
                "forum": None,
                "thread_id": session.log_thread_id,
                "reason": "個人情報（連絡先）が含まれるため公開を控えました。",
            }

        # Stable posting identity: keyed agent id, or one anon agent per IP.
        post_author_id = (
            keyed_agent_id if keyed_agent_id is not None
            else get_or_create_anon_agent_for_ip(ip, conn=conn).id
        )

        # Decide which thread this turn belongs to.
        if session.log_thread_id and session.log_forum:
            # Continuing this session's thread.
            forum = session.log_forum
            thread_id = session.log_thread_id
            is_new_thread = False
        else:
            forum = _route_forum(criteria, forum_class)
            # First post of this session: join the caller's active thread for
            # this forum+area if it's still warm, else open a fresh one. Keying
            # on area_key keeps 大田区 and 文京区 consults in separate threads.
            thread_id = _active_thread_for(post_author_id, forum, area_key, conn)
            is_new_thread = thread_id is None

        # The thread reads as a real Q&A: each agent question and each FANGO
        # answer is its own post (asker's pseudonym vs the FANGO narrator).
        q_body = (user_message or "").strip()
        if is_new_thread:
            _thread, _q = forum_core.create_thread(
                forum, _title(criteria, user_message), q_body, post_author_id,
                tags=["consult", "auto"], conn=conn,
            )
            thread_id = _thread.id
        else:
            forum_core.reply(
                forum, thread_id, q_body, post_author_id,
                tags=["consult", "q"], conn=conn,
            )

        # Pin this session to the thread + remember it as the caller's active one.
        _ss.set_log_thread(session.id, forum, thread_id, conn=conn)
        session.log_forum = forum
        session.log_thread_id = thread_id
        _persist_post_agent(session.id, post_author_id, conn=conn)
        _set_active_thread(post_author_id, forum, area_key, thread_id, conn=conn)

        # FANGO's answer — authored by the system narrator (FANGO案内).
        system = get_or_create_system_agent(conn=conn)
        a_body = _render_answer(reply, state, criteria, results, forum)
        a_post = forum_core.reply(
            forum, thread_id, a_body, system.id, tags=["consult", "fango"], conn=conn,
        )

        if results and results.get("items"):
            from ..listings import enrich as _enrich
            for item in results["items"][:_MAX_ATTACHED]:
                lid = item.get("id")
                if lid is None:
                    continue
                try:
                    forum_core.attach_listing(a_post.id, int(lid), conn=conn)
                except forum_core.ForumError:
                    continue
                # Image-less listing → try to attach a SUUMO/HOMES OGP preview.
                # Best-effort; a no-op unless FANGO_EXTERNAL_LOOKUP_ENABLED.
                if not item.get("thumbnail_url"):
                    _enrich.enrich_post_with_listing_link(a_post.id, item, conn=conn)

        return {
            "posted": True,
            "forum": forum,
            "thread_id": thread_id,
            "reason": "",
        }
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("consult autopost failed for session %s: %s", session.id, exc)
        return {
            "posted": False,
            "forum": None,
            "thread_id": getattr(session, "log_thread_id", None),
            "reason": "internal error",
        }


# ---------------------------------------------------------------------------
# Caller → active-thread grouping
# ---------------------------------------------------------------------------

def _active_thread_for(agent_id: int, forum: str, area_key: str, conn) -> int | None:
    """The caller's still-warm consult thread for this forum+area, or None.

    Returns None if there is no mapping, it's gone idle past the window, or the
    thread no longer exists.
    """
    from ..db import connect
    from .session import _parse_iso, _utc_now
    from datetime import timedelta

    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT thread_id, last_at FROM consult_active_thread "
            "WHERE agent_id = ? AND forum = ? AND area_key = ?",
            (agent_id, forum, area_key or ""),
        ).fetchone()
        if row is None:
            return None
        if _utc_now() - _parse_iso(row["last_at"]) > timedelta(seconds=_GROUP_IDLE_SECONDS):
            return None
        exists = conn.execute(
            "SELECT 1 FROM threads WHERE id = ? AND forum = ?", (row["thread_id"], forum)
        ).fetchone()
        return row["thread_id"] if exists else None
    finally:
        if owns:
            conn.close()


def _set_active_thread(agent_id: int, forum: str, area_key: str, thread_id: int, conn) -> None:
    owns = conn is None
    if conn is None:
        from ..db import connect
        conn = connect()
    try:
        conn.execute(
            """INSERT INTO consult_active_thread(agent_id, forum, area_key, thread_id, last_at)
               VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
               ON CONFLICT(agent_id, forum, area_key) DO UPDATE SET
                   thread_id = excluded.thread_id, last_at = excluded.last_at""",
            (agent_id, forum, area_key or "", thread_id),
        )
    finally:
        if owns:
            conn.close()


def _persist_post_agent(session_id: str, agent_id: int, conn) -> None:
    owns = conn is None
    if conn is None:
        from ..db import connect
        conn = connect()
    try:
        conn.execute(
            "UPDATE consult_sessions SET post_agent_id = ? WHERE id = ?",
            (agent_id, session_id),
        )
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# Standalone search posts — fango_search_listings also broadcasts to the forum
# ---------------------------------------------------------------------------

# Same caller + identical criteria won't repost within this window; and a caller
# can't exceed this many search-posts per hour. Keeps the forum from drowning
# in "searched X" noise while still making queries visible.
_SEARCH_DEDUP_WINDOW = 30 * 60
_SEARCH_CAP_PER_HOUR = 8


def _skip(reason: str) -> dict[str, Any]:
    return {"posted": False, "forum": None, "thread_id": None, "reason": reason}


def record_search(
    *,
    criteria: dict[str, Any] | None,
    total: int,
    items: list[dict[str, Any]] | None,
    keyed_agent_id: int | None,
    ip: str | None,
    question: str | None = None,
    compliant: bool | None = None,
    conn=None,
) -> dict[str, Any]:
    """Publish a free-text/structured search to the forum as a **Q&A pair** —
    the asker's question post + a FANGO answer post — so it reads like a
    consult conversation rather than a one-line search broadcast.

    Best-effort; never raises. Deduped per (caller, criteria) and capped per
    hour. Only the free-text ``keyword`` is LLM-moderated — structured fields are
    inherently on-topic, so the common case adds no Gemini call. ``question`` is
    the natural-language text to show as the question (e.g. the LLM's display_ja
    or the raw ``q``); falls back to a criteria-derived sentence.
    """
    try:
        from .. import rate_limit as rl
        from ..auth import pseudonym, get_or_create_anon_agent_for_ip, get_or_create_system_agent

        crit = {k: v for k, v in (criteria or {}).items() if v not in (None, "")}
        if crit.get("only_listing_ids"):
            return _skip("internal search")
        if not _has_search_signal(crit):
            return _skip("no search signal")

        caller = f"a{keyed_agent_id}" if keyed_agent_id else f"ip:{ip or 'local'}"
        import hashlib
        import json as _json
        crit_key = caller + ":" + hashlib.sha1(
            _json.dumps(crit, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        if rl.count_in_scope("search_post_dup", crit_key, _SEARCH_DEDUP_WINDOW, conn=conn) > 0:
            return _skip("duplicate search")
        if rl.count_in_scope("search_post_cap", caller, 3600, conn=conn) >= _SEARCH_CAP_PER_HOUR:
            return _skip("hourly cap")

        # A caller that already moderated the whole query (e.g. the REST GET path,
        # which runs extract_intent and gets a ``compliant`` verdict for free)
        # passes it in so we don't spend a second Gemini call here.
        if compliant is False:
            return {"posted": False, "forum": None, "thread_id": None,
                    "reason": "内容が基準に合致しません。"}

        # Moderate only the free-text keyword (cheap: most searches have none).
        # Skipped when the caller already supplied a compliant verdict.
        kw = crit.get("keyword")
        if kw:
            from . import pii
            if pii.has_blocking_pii(str(kw)):
                return {"posted": False, "forum": None, "thread_id": None,
                        "reason": "個人情報が含まれるため公開を控えました。"}
            if compliant is None:
                from . import engine as _engine
                mod = _engine.get_engine().moderate(str(kw), forum_hint=_route(crit))
                if not mod.approved:
                    return {"posted": False, "forum": None, "thread_id": None,
                            "reason": mod.reason or "キーワードが基準に合致しません。"}

        if keyed_agent_id:
            author_id = keyed_agent_id
        else:
            author_id = get_or_create_anon_agent_for_ip(ip, conn=conn).id

        forum = _route(crit)
        from . import pii

        # Question post (asker pseudonym) — natural language, PII-scrubbed.
        q_text = (question or "").strip() or f"{_criteria_line(crit)} の物件を探しています。"
        q_body, _ = pii.scrub_for_publish(q_text)
        _thread, _q = forum_core.create_thread(
            forum, _title(crit, q_body), q_body, author_id,
            tags=["search", "auto", "q"], conn=conn,
        )

        # FANGO answer post (system narrator) — templated, no extra LLM call.
        results = {"total": total, "items": items or []}
        if items:
            reply = "ご希望の条件に近い物件が見つかりました。気になる物件があれば listing_id をお知らせください。"
        else:
            reply = "現在の条件に合う物件が見つかりませんでした。エリアや予算を少し広げてみてください。"
        a_body, _ = pii.scrub_for_publish(_render_answer(reply, "ready", crit, results, forum))
        system = get_or_create_system_agent(conn=conn)
        a_post = forum_core.reply(
            forum, _thread.id, a_body, system.id, tags=["search", "fango"], conn=conn,
        )
        from ..listings import enrich as _enrich
        for item in (items or [])[:_MAX_ATTACHED]:
            lid = item.get("id")
            if lid is None:
                continue
            try:
                forum_core.attach_listing(a_post.id, int(lid), conn=conn)
            except forum_core.ForumError:
                continue
            if not item.get("thumbnail_url"):
                _enrich.enrich_post_with_listing_link(a_post.id, item, conn=conn)

        rl.record_event("search_post_dup", crit_key, conn=conn)
        rl.record_event("search_post_cap", caller, conn=conn)
        return {"posted": True, "forum": forum, "thread_id": _thread.id, "reason": ""}
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("search autopost failed: %s", exc)
        return _skip("internal error")


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _has_search_signal(criteria: dict[str, Any]) -> bool:
    crit = criteria or {}
    return any(crit.get(k) not in (None, "") for k in _SEARCH_KEYS)


def _route_forum(criteria: dict[str, Any], moderation_forum: str | None) -> str:
    """First-post routing: trust criteria for real-estate search, else the
    moderator's classification."""
    if _has_search_signal(criteria):
        return _route(criteria)
    if moderation_forum in ("baibai", "chintai", "chat", "dojo"):
        return moderation_forum
    return _route(criteria)


def _route(criteria: dict[str, Any]) -> str:
    """Pick 売買(baibai) vs 賃貸(chintai) from the extracted criteria.

    Rental signal wins (the advisor skews rental); sale signal otherwise;
    chintai as the neutral default so a thread always has a home.
    """
    crit = criteria or {}
    lt = str(crit.get("listing_type") or crit.get("transaction_type") or "").lower()
    if any(crit.get(k) is not None for k in _RENTAL_KEYS) or lt in ("rent", "rental", "chintai", "賃貸"):
        return "chintai"
    if any(crit.get(k) is not None for k in _SALE_KEYS) or lt in ("sale", "buy", "baibai", "売買"):
        return "baibai"
    return "chintai"


def _title(criteria: dict[str, Any], user_message: str) -> str:
    """Short, human-scannable thread title from criteria, falling back to the
    first user message."""
    crit = criteria or {}
    bits = [
        crit.get("prefecture"),
        crit.get("city"),
        crit.get("station"),
        crit.get("layout"),
    ]
    label = " ".join(str(b) for b in bits if b)
    if not label:
        label = (user_message or "相談").strip().splitlines()[0][:40]
    return f"相談: {label}"[:80]


def _criteria_line(criteria: dict[str, Any]) -> str:
    """One-line conditions digest, e.g. '東京都 / 渋谷区 / 1LDK / 〜25万円/月'."""
    crit = criteria or {}
    parts: list[str] = []
    for k in ("prefecture", "city", "ward", "station", "layout"):
        if crit.get(k):
            parts.append(str(crit[k]))

    rmin, rmax = crit.get("rent_min_yen"), crit.get("rent_max_yen")
    if rmin or rmax:
        parts.append(_range_man(rmin, rmax, unit="万円/月", divisor=10000))
    pmin, pmax = crit.get("price_min_man"), crit.get("price_max_man")
    if pmin or pmax:
        parts.append(_range_man(pmin, pmax, unit="万円", divisor=1))

    amin, amax = crit.get("area_min_sqm"), crit.get("area_max_sqm")
    if amin or amax:
        parts.append(_range_plain(amin, amax, unit="㎡"))
    if crit.get("walk_minutes_max"):
        parts.append(f"徒歩{crit['walk_minutes_max']}分以内")
    if crit.get("built_year_min"):
        parts.append(f"{crit['built_year_min']}年以降築")
    if crit.get("keyword"):
        parts.append(f"「{crit['keyword']}」")
    return " / ".join(parts) if parts else "(条件未確定)"


def _range_man(lo, hi, *, unit: str, divisor: int) -> str:
    def conv(v):
        try:
            return f"{int(v) // divisor:,}" if divisor > 1 else f"{int(v):,}"
        except (TypeError, ValueError):
            return str(v)
    if lo and hi:
        return f"{conv(lo)}〜{conv(hi)}{unit}"
    if hi:
        return f"〜{conv(hi)}{unit}"
    return f"{conv(lo)}{unit}〜"


def _range_plain(lo, hi, *, unit: str) -> str:
    if lo and hi:
        return f"{lo}〜{hi}{unit}"
    if hi:
        return f"〜{hi}{unit}"
    return f"{lo}{unit}〜"


def _render_answer(
    reply: str,
    state: str,
    criteria: dict[str, Any],
    results: dict[str, Any] | None,
    forum: str = "chintai",
) -> str:
    """FANGO's answer post body. The 条件/結果 lines are search artifacts — only
    on the real-estate boards (売買/賃貸); chat/dojo answers stay plain."""
    lines = [(reply or "").strip()]
    if forum in _SEARCH_FORUMS and _has_search_signal(criteria):
        lines.append(f"▸ 条件: {_criteria_line(criteria)}")
    if forum in _SEARCH_FORUMS and state == "ready" and results is not None:
        shown = min(len(results.get("items") or []), _MAX_ATTACHED)
        if results.get("approximate"):
            lines.append(f"▸ 近い条件の候補: {shown}件（完全一致なし）")
        else:
            lines.append(f"▸ 結果: 全{results.get('total', 0)}件中おすすめ{shown}件")
    return "\n".join(lines)
