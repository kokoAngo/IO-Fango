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
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import forum_core
from ..db import connect, transaction
from . import session as _ss

log = logging.getLogger(__name__)

# How many result listings to attach to a 'ready' turn's reply.
_MAX_ATTACHED = 3

# Background HOMES enrichment runs on a SINGLE-worker pool so a burst of consults
# can't spawn a thread-per-post storm (each would drive a headless browser). The
# real memory guard is the browser-concurrency cap in external_lookup; this just
# keeps us from queuing unbounded background work. Submissions past the queue cap
# are dropped (best-effort feature — a missed link card is acceptable).
_ENRICH_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="enrich")
_ENRICH_MAX_QUEUED = 16
_enrich_lock = threading.Lock()
_enrich_pending = 0


def _spawn_enrich(post_id: int, items: list[dict]) -> None:
    """Fire-and-forget: fetch up to ``_MAX_ATTACHED`` listings' HOMES links +
    photos in a background daemon thread (one shared browser session) and attach
    them to ``post_id``. A no-op unless external lookup is enabled. Runs off the
    request path because the browser lookups are slow; by the time they attach
    the post is committed. Best-effort."""
    from ..config import load_settings
    if not load_settings().external_lookup_enabled:
        return
    data = [{"id": it.get("id"), "building_name": it.get("building_name")}
            for it in items if it.get("id")]
    if not data:
        return

    global _enrich_pending
    with _enrich_lock:
        if _enrich_pending >= _ENRICH_MAX_QUEUED:
            log.debug("enrich queue full (%d); dropping post %s", _enrich_pending, post_id)
            return
        _enrich_pending += 1

    def _run():
        global _enrich_pending
        try:
            from ..listings import enrich as _enrich
            _enrich.enrich_post_with_listings(post_id, data)  # own DB connection
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("background enrich failed (post %s): %s", post_id, exc)
        finally:
            with _enrich_lock:
                _enrich_pending -= 1

    _ENRICH_POOL.submit(_run)

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
    # The publish runs in one BEGIN IMMEDIATE transaction, so every write must
    # share a single connection — own one when the caller didn't pass theirs.
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        from ..auth import get_or_create_anon_agent_for_ip, get_or_create_system_agent
        from .. import moderation

        # Single moderation gate. The question reuses the consult's folded verdict
        # (no extra LLM call); the FANGO answer is our own templated text, so it's
        # scrub-only.
        q = moderation.screen(user_message, verdict=(compliant, forum_class))
        if not q.approved:
            return {
                "posted": False,
                "forum": None,
                "thread_id": session.log_thread_id,
                "reason": q.reason,
            }
        user_message = q.text
        reply = moderation.screen(reply or "", use_llm=False).text

        # Stable posting identity: keyed agent id, or one anon agent per IP.
        post_author_id = (
            keyed_agent_id if keyed_agent_id is not None
            else get_or_create_anon_agent_for_ip(ip, conn=conn).id
        )

        # Serialize the whole publish (thread decision → question post → pin →
        # answer) under one BEGIN IMMEDIATE. Two concurrent turns from the same
        # caller would otherwise both read "no warm thread" and each fork a
        # duplicate; the write lock makes the second block until the first's
        # _set_active_thread commits, so it sees the warm thread. Also makes the
        # Q&A atomic — a mid-publish failure rolls back rather than orphaning a
        # half-posted thread. transaction() is re-entrant, so create_thread/reply
        # piggyback on this one.
        with transaction(conn):
            # Decide which thread this turn belongs to.
            if session.log_thread_id and session.log_forum:
                prev_area = (session.log_area_key or "").strip()
                # Re-route every continuation turn: a rental session that switches
                # to a sale query (or vice-versa) must move to the right board, not
                # stay pinned to session.log_forum. Only re-route on an actual
                # search signal so a criteria-less follow-up doesn't bounce boards.
                new_forum = (
                    _route_forum(criteria, forum_class)
                    if _has_search_signal(criteria) else session.log_forum
                )
                area_changed = bool(area_key and prev_area and area_key != prev_area)
                forum_changed = new_forum != session.log_forum
                if area_changed or forum_changed:
                    # Mid-session area switch (杉並区 → 中野区) or board switch
                    # (賃貸 → 売買): fork rather than mixing them into one thread.
                    # Join this caller's warm thread for the new forum+area if any,
                    # else open a fresh one.
                    forum = new_forum
                    thread_id = _active_thread_for(post_author_id, forum, area_key, conn)
                    is_new_thread = thread_id is None
                else:
                    # Same area and same board → keep appending here.
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

            # Pin this session to the thread (+ the area it settled on) and remember
            # it as the caller's active thread for this forum+area.
            _ss.set_log_thread(session.id, forum, thread_id, area_key=area_key, conn=conn)
            session.log_forum = forum
            session.log_thread_id = thread_id
            if area_key:
                session.log_area_key = area_key
            _persist_post_agent(session.id, post_author_id, conn=conn)
            _set_active_thread(post_author_id, forum, area_key, thread_id, conn=conn)
        # End of the race-critical section. The question post + grouping are now
        # committed; the answer commits separately so its live-feed event isn't
        # published before the data it points at is visible to other connections.

        # FANGO's answer — authored by the system narrator (FANGO案内).
        system = get_or_create_system_agent(conn=conn)
        a_body = _render_answer(reply, state, criteria, results, forum)
        a_post = forum_core.reply(
            forum, thread_id, a_body, system.id, tags=["consult", "fango"], conn=conn,
        )

        if results and results.get("items"):
            imageless = []
            for item in results["items"][:_MAX_ATTACHED]:
                lid = item.get("id")
                if lid is None:
                    continue
                try:
                    forum_core.attach_listing(a_post.id, int(lid), conn=conn)
                except forum_core.ForumError:
                    continue
                if not item.get("thumbnail_url"):
                    imageless.append(item)
            # Image-less listings → fetch their HOMES links + photos in the
            # BACKGROUND (one shared browser session) so the consult reply
            # isn't blocked. The cards appear on the thread a few seconds later.
            if imageless:
                _spawn_enrich(a_post.id, imageless)

        return {
            "posted": True,
            "forum": forum,
            "thread_id": thread_id,
            "post_agent_id": post_author_id,
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
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Broker auto-routing
# ---------------------------------------------------------------------------

# Cap the number of brokers a single inquiry fans out to.
_MAX_ROUTED_BROKERS = 3


def route_to_brokers(
    *,
    thread_id: int,
    forum: str,
    criteria: dict[str, Any],
    customer_post_agent_id: int,
    consult_session_id: str | None = None,
    result_listing_ids: list[int] | None = None,
    conn=None,
) -> dict[str, Any]:
    """Match a ready consult against broker inventory and route an inquiry.

    Brokers are external and asynchronous, so this never waits on them: it just
    records an inquiry pointing at the existing consult thread and fans it out to
    the brokers whose inventory fits. They poll ``broker_get_new_inquiries`` and
    reply into the same thread. Pure SQL over the already-extracted criteria — no
    LLM call. Best-effort: never raises (mirrors :func:`record_turn`). Returns
    ``{"routed": N, "broker_count": N, "inquiry_id": id|None}``.
    """
    null = {"routed": 0, "broker_count": 0, "inquiry_id": None}
    try:
        # Only real-estate boards with an actual search signal can be matched
        # against inventory; chat/dojo turns have nothing to route.
        if forum not in _SEARCH_FORUMS or not _has_search_signal(criteria):
            return null

        from ..brokers import inquiries as _inq

        owns_conn = conn is None
        if conn is None:
            conn = connect()
        try:
            # Prefer the brokers who own the listings ACTUALLY SHOWN to the
            # customer: the customer-facing search may have relaxed the criteria
            # (dropped a bad keyword, widened budget), so re-matching brokers on
            # the raw criteria would miss them. Fall back to a criteria-based
            # inventory match only when none of the shown listings are broker-owned.
            owned: dict[int, list[int]] = {}
            ids = [int(i) for i in (result_listing_ids or []) if i is not None]
            if ids:
                ph = ",".join("?" * len(ids))
                for r in conn.execute(
                    f"SELECT id, broker_agent_id FROM listings "
                    f"WHERE id IN ({ph}) AND broker_agent_id IS NOT NULL", ids
                ).fetchall():
                    owned.setdefault(r["broker_agent_id"], []).append(r["id"])
            if not owned:
                for bid, _score in _inq.match_brokers_for(
                    criteria, limit=_MAX_ROUTED_BROKERS, conn=conn
                ):
                    owned.setdefault(bid, [])
            if not owned:
                return null

            # Top brokers by how many of the shown listings they own. One inquiry
            # per broker, carrying that broker's own relevant listings.
            ranked = sorted(owned.items(), key=lambda kv: -len(kv[1]))[:_MAX_ROUTED_BROKERS]
            routed_total = 0
            first_inquiry = None
            for bid, listing_ids in ranked:
                inquiry_id = _inq.create_inquiry(
                    customer_post_agent_id, criteria,
                    consult_session_id=consult_session_id, thread_id=thread_id,
                    forum=forum, matched_listing_ids=listing_ids, conn=conn,
                )
                routed_total += _inq.route_inquiry(
                    inquiry_id, [(bid, len(listing_ids))], conn=conn
                )
                first_inquiry = first_inquiry or inquiry_id

            # A short system note so the customer (and forum readers) know the
            # inquiry was handed to brokers and an async reply is coming.
            try:
                from ..auth import get_or_create_system_agent
                from .. import moderation
                system = get_or_create_system_agent(conn=conn)
                note = (f"この条件に合う在庫を持つ仲介 {len(ranked)} 社にお繋ぎしました。"
                        "担当エージェントの返信をお待ちください。")
                note = moderation.screen(note, use_llm=False).text
                forum_core.reply(forum, thread_id, note, system.id,
                                 tags=["consult", "routed"], conn=conn)
            except Exception:
                pass

            return {
                "routed": routed_total,
                "broker_count": len(ranked),
                "inquiry_id": first_inquiry,
            }
        finally:
            if owns_conn:
                conn.close()
    except Exception as exc:  # pragma: no cover - best effort
        log.warning("broker routing failed for thread %s: %s", thread_id, exc)
        return null


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

        # Moderation runs below via moderation.screen on the question text: PII
        # always, LLM only when there's a free-text keyword (structured-only
        # searches are inherently on-topic, so they skip the LLM — keeping the
        # broadcast cheap). A caller that already has a compliance verdict (e.g.
        # the REST GET path) passes it so we don't spend a second Gemini call.
        has_keyword = bool(crit.get("keyword"))

        if keyed_agent_id:
            author_id = keyed_agent_id
        else:
            author_id = get_or_create_anon_agent_for_ip(ip, conn=conn).id

        forum = _route(crit)
        from .. import moderation

        # Question post (asker pseudonym) — screened (PII + LLM-on-keyword).
        q_text = (question or "").strip() or f"{_criteria_line(crit)} の物件を探しています。"
        _verdict = (compliant, forum) if compliant is not None else None
        scr = moderation.screen(q_text, forum_hint=forum, verdict=_verdict, use_llm=has_keyword)
        if not scr.approved:
            return {"posted": False, "forum": None, "thread_id": None, "reason": scr.reason}
        q_body = scr.text
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
        a_body = moderation.screen(_render_answer(reply, "ready", crit, results, forum), use_llm=False).text
        system = get_or_create_system_agent(conn=conn)
        a_post = forum_core.reply(
            forum, _thread.id, a_body, system.id, tags=["search", "fango"], conn=conn,
        )
        imageless = []
        for item in (items or [])[:_MAX_ATTACHED]:
            lid = item.get("id")
            if lid is None:
                continue
            try:
                forum_core.attach_listing(a_post.id, int(lid), conn=conn)
            except forum_core.ForumError:
                continue
            if not item.get("thumbnail_url"):
                imageless.append(item)
        if imageless:
            _spawn_enrich(a_post.id, imageless)

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
        return _route(criteria, moderation_forum)
    if moderation_forum in ("baibai", "chintai", "chat", "dojo"):
        return moderation_forum
    return _route(criteria, moderation_forum)


def _route(criteria: dict[str, Any], moderation_forum: str | None = None) -> str:
    """Pick 売買(baibai) vs 賃貸(chintai) from the extracted criteria.

    An explicit listing_type decides; otherwise whichever budget is present
    (rent vs sale are mutually exclusive — see the merge in consult.tool). If
    both somehow co-exist, defer to the moderator's per-turn classification
    rather than letting either mode unconditionally win; chintai is the neutral
    default so a thread always has a home.
    """
    crit = criteria or {}
    lt = str(crit.get("listing_type") or crit.get("transaction_type") or "").lower()
    if lt in ("sale", "buy", "baibai", "売買"):
        return "baibai"
    if lt in ("rent", "rental", "chintai", "賃貸"):
        return "chintai"
    has_rent = any(crit.get(k) for k in _RENTAL_KEYS)   # truthy → 0 isn't a signal
    has_sale = any(crit.get(k) for k in _SALE_KEYS)
    if has_sale and not has_rent:
        return "baibai"
    if has_rent and not has_sale:
        return "chintai"
    # both present (ambiguous) or neither → trust the moderator, else default.
    return moderation_forum if moderation_forum in ("baibai", "chintai") else "chintai"


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
