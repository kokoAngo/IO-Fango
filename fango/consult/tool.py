"""``fango_consult`` MCP tool — server-side natural-language advisor.

This wires together :mod:`fango.consult.session` (state) and
:mod:`fango.consult.engine` (LLM) and exposes the dialogue as a single tool
the caller invokes once per turn.
"""
from __future__ import annotations

import functools
import logging
from typing import Any

import anyio

from ..auth import client_ip_var, current_agent_var
from ..config import load_consult_settings
from ..db import connect
from ..rate_limit import RateLimitError, enforce_consult_ip
from ..listings import service as ls
from ..listings.tools import _listing_brief
from . import autopost as _autopost
from . import engine as _engine
from . import session as _ss

log = logging.getLogger(__name__)

# Resolve external links for at most this many proposed listings per turn, so a
# single consult can't fan out into a pile of SUUMO/HOMES fetches.
_MAX_EXTERNAL_LINKS = 5


def _attach_external_links(briefs: list[dict], conn) -> None:
    """Add ``external_url`` / ``external_source`` (a public HOMES/SUUMO page the
    owner can rent through) to proposed listings. Best-effort; a no-op unless
    external lookup is enabled. Cache-first, so repeats are cheap."""
    try:
        from ..listings.enrich import resolve_external_link
    except Exception:  # pragma: no cover
        return
    for b in briefs[:_MAX_EXTERNAL_LINKS]:
        try:
            # Cache-only: never block the consult reply on a ~10s browser lookup.
            # The background post-enrichment populates the cache; subsequent
            # turns/queries then carry the link.
            link = resolve_external_link(b.get("id"), b.get("building_name"),
                                         conn=conn, allow_lookup=False)
        except Exception:  # pragma: no cover - best effort
            link = None
        if link and link.get("url"):
            b["external_url"] = link["url"]
            b["external_source"] = link.get("source")
            if link.get("note"):
                b["external_note"] = link["note"]


def _session_listing_links(sess: _ss.ConsultSession, conn) -> list[dict[str, Any]]:
    """Every HOMES link resolved so far for listings shown in THIS session, read
    from the cache. Link enrichment runs in the background (~10s after a turn),
    so a turn's own links usually aren't ready when it returns — but they land in
    the cache shortly after and surface here on the next turn. Returns the
    cumulative set, so the agent receives links across several turns even though
    none were ready on turn 1."""
    thread_id = getattr(sess, "log_thread_id", None)
    if not thread_id:
        return []
    try:
        from ..listings.enrich import resolve_external_link
        rows = conn.execute(
            """SELECT DISTINCT r.listing_id, l.building_name
               FROM post_listing_refs r
               JOIN posts p ON p.id = r.post_id
               JOIN listings l ON l.id = r.listing_id
               WHERE p.thread_id = ?""",
            (thread_id,),
        ).fetchall()
    except Exception:  # pragma: no cover - best effort
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            link = resolve_external_link(r["listing_id"], r["building_name"],
                                         conn=conn, allow_lookup=False)
        except Exception:  # pragma: no cover - best effort
            link = None
        if link and link.get("url"):
            out.append({
                "listing_id": r["listing_id"],
                "building_name": r["building_name"],
                "url": link["url"],
                "source": link.get("source"),
                "image": link.get("image_url"),
                "note": link.get("note"),
            })
    return out


def register(mcp) -> None:

    @mcp.tool()
    async def fango_consult(
        message: str,
        session_id: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Natural-language house-hunting advisor.

        Send the owner's request in any language. FANGO will (a) ask back when
        more conditions are needed, (b) once enough conditions are known,
        execute an internal listing search and return both a natural-language
        recommendation and a structured ``results`` list.

        Pass the ``session_id`` from a previous response back in to continue
        the same conversation.

        Pass ``category`` to declare the board/intent up front so FANGO never has
        to guess 買房 vs 租房: ``"sale"`` (売買), ``"rental"`` (賃貸), ``"chat"``
        (雑談), ``"dojo"`` (道場), or ``"auto"`` to let FANGO classify from the
        message (the default when omitted). When set to sale/rental it is
        authoritative — the listing search and the forum routing both honour it,
        so a buyer never gets 賃貸 results back.

        ``listing_links`` carries public "rent it here" HOMES URLs (with a photo)
        for the listings shown in this session. These are fetched in the
        background, so the field is usually EMPTY on the turn that first shows a
        listing and fills in on a later turn — call again (e.g. to refine or just
        to check) to collect them; you can hand these URLs to the owner.

        Args:
            message: The current user turn (free-form Japanese or English).
            session_id: Continue an existing dialogue. Omit on the first call.
            category: Optional board/intent tag — one of "sale", "rental",
                "chat", "dojo", "auto". Missing or "auto" → FANGO classifies
                from the message.

        Returns:
            dict with keys ``session_id``, ``reply``, ``state``
            ('asking' | 'ready' | 'done'), ``criteria_extracted``,
            ``results`` ({items, total}), ``suggested_next_tools``, ``turn``,
            ``usage`` ({input_tokens, output_tokens}), and ``listing_links``
            (list of {listing_id, building_name, url, source, image}).
        """
        # FastMCP runs a *sync* tool inline on the event loop, so the multi-second
        # Gemini calls + DB writes inside _run_turn would freeze every concurrent
        # SSR request (forum navigation hangs while an agent is consulting). Make
        # the tool async and push the blocking work to a worker thread; anyio
        # carries the request contextvars (agent key / client IP) into it.
        return await anyio.to_thread.run_sync(
            functools.partial(
                _run_turn, message=message, session_id=session_id, category=category,
            )
        )


# ---------------------------------------------------------------------------
# Core handler — separated from the decorator for testability.
# ---------------------------------------------------------------------------

# Caller-declared category → (canonical intent, forum slug). Lenient on aliases so
# an agent can send friendly words in any of the three languages. Anything not
# recognised (incl. "auto"/"either"/None) → (None, None) = fall back to inference.
_CATEGORY_FORUM = {"sale": "baibai", "rental": "chintai", "chat": "chat", "dojo": "dojo"}


def _normalize_category(category: str | None) -> str | None:
    s = str(category or "").strip().lower()
    if s in ("sale", "buy", "baibai", "売買", "买房", "购房", "购买", "卖房"):
        return "sale"
    if s in ("rental", "rent", "chintai", "賃貸", "租房", "租赁", "租"):
        return "rental"
    if s in ("chat", "夜咄", "雑談", "闲聊", "杂谈"):
        return "chat"
    if s in ("dojo", "道場", "道场"):
        return "dojo"
    return None  # auto / either / unknown / empty → let FANGO classify


def _run_turn(
    message: str, session_id: str | None, category: str | None = None,
) -> dict[str, Any]:
    settings = load_consult_settings()
    agent = current_agent_var.get()  # optional; consult does not require auth
    agent_id = agent.id if agent else None

    conn = connect()
    try:
        # Keyless callers can now post (anonymously, moderated), so throttle them
        # per source IP to stop an anonymous flood. Keyed agents bypass this.
        if agent is None:
            ip = client_ip_var.get()
            if ip:
                try:
                    enforce_consult_ip(ip, conn=conn)
                except RateLimitError:
                    return _rate_limited_envelope(session_id)

        sess = _resolve_session(conn, session_id, agent_id, settings.session_ttl_seconds)

        # Hard cap on turns — drain into 'done' state.
        if sess.total_turns >= settings.max_turns:
            _ss.update_session_state(sess.id, state="done", conn=conn)
            return _envelope(
                sess, reply=_done_reply(settings.max_turns),
                state="done", results=None, suggested=["fango_search_listings"],
                usage_in=0, usage_out=0,
            )

        # Turn index = current persisted turn count (user msg + model reply pair).
        turn_index = sess.total_turns
        _ss.append_message(sess.id, turn_index, "user", {"text": message}, conn=conn)

        history = _ss.get_messages(sess.id, conn=conn)
        # Drop the just-appended user message — the engine adds it back.
        if history and history[-1].role == "user":
            history_for_llm = history[:-1]
        else:
            history_for_llm = history

        engine = _engine.get_engine()
        try:
            intent = engine.extract_intent(history_for_llm, message)
        except Exception as exc:
            log.warning("engine.extract_intent raised: %s", exc)
            from . import prompts as _prompts
            intent = _engine.IntentResult(
                state="asking",
                criteria_delta={},
                missing_fields=[],
                ask_back=_prompts.FALLBACK_ASKBACK,
                # No verdict available → fail closed (don't publish this turn).
                compliant=False,
            )

        # Merge delta into the running criteria. Drop 0 too — the LLM sometimes
        # fills 0 for unspecified numeric limits, and walk_minutes_max=0 /
        # area_max_sqm=0 would otherwise exclude every listing.
        delta = {k: v for k, v in intent.criteria_delta.items() if v not in (None, "", 0)}
        merged = dict(sess.last_criteria)
        # Rent vs sale are mutually exclusive. When this turn signals one mode
        # (a 賃貸/売買 budget, listing_type, or the moderator's forum), clear the
        # OTHER mode's stale budget so a caller who switched 賃貸→売買 mid-session
        # doesn't keep a rent ceiling that misroutes the post (sale→賃貸) and
        # skews the search.
        _RENT_K = ("rent_min_yen", "rent_max_yen")
        _SALE_K = ("price_min_man", "price_max_man", "price_man")
        _dlt = str(delta.get("listing_type") or delta.get("transaction_type") or "").lower()
        sale_now = any(k in delta for k in _SALE_K) or _dlt in ("sale", "buy", "baibai", "売買") or intent.forum == "baibai"
        rent_now = any(k in delta for k in _RENT_K) or _dlt in ("rent", "rental", "chintai", "賃貸") or intent.forum == "chintai"
        if sale_now and not rent_now:
            for k in _RENT_K:
                merged.pop(k, None)
        elif rent_now and not sale_now:
            for k in _SALE_K:
                merged.pop(k, None)
        merged.update(delta)

        # Caller-declared category wins over the LLM's buy-vs-rent guess. For
        # sale/rental we pin transaction_type (drives both the listing search
        # filter and forum routing) and drop the opposite mode's stale budget so
        # it can't skew the search. chat/dojo only steer the board. 'auto'/None →
        # unchanged.
        cat_norm = _normalize_category(category)
        forum_override = _CATEGORY_FORUM.get(cat_norm)
        if cat_norm in ("sale", "rental"):
            merged["transaction_type"] = cat_norm
            for k in (_SALE_K if cat_norm == "rental" else _RENT_K):
                merged.pop(k, None)

        usage_in = intent.input_tokens
        usage_out = intent.output_tokens

        results_payload: dict[str, Any] | None = None
        state = intent.state
        reply: str

        if state == "ready":
            try:
                rows = ls.search_listings(criteria=merged, limit=10, sort_by="newest", conn=conn)
            except Exception as exc:  # pragma: no cover
                log.exception("search_listings failed: %s", exc)
                rows = []
            total = ls.count_listings(criteria=merged, conn=conn)
            # No exact match → offer near options by progressively loosening the
            # criteria, rather than replying "nothing found".
            approximate = False
            relax_note = None
            if not rows:
                try:
                    rows, relax_note = ls.relaxed_search(merged, limit=10, conn=conn)
                    approximate = bool(rows)
                except Exception as exc:  # pragma: no cover
                    log.warning("relaxed_search failed: %s", exc)
            briefs = [_listing_brief(r, conn=conn) for r in rows]
            _attach_external_links(briefs, conn)
            # Gemini no longer writes the recommendation — that's the caller's LLM
            # (it has `results`) or the broker agents (async, after routing). The
            # reply is a concise templated acknowledgement; brokers/the caller do
            # the actual recommending. Saves one Gemini call per ready turn.
            if briefs:
                reply = (
                    "ご希望に完全一致する物件はありませんでしたが、条件を少し広げて近い候補をお出ししました。"
                    "気になる物件があれば listing_id をお知らせください。"
                    if approximate else
                    "ご希望の条件に近い物件が見つかりました。気になる物件があれば listing_id をお知らせください。"
                )
            else:
                reply = "現在の条件に合う物件が見つかりませんでした。エリアや予算を少し広げてみてください。"
            results_payload = {"total": total, "items": briefs, "approximate": approximate}
            if relax_note:
                results_payload["relax_note"] = relax_note
        else:
            reply = intent.ask_back or "もう少し情報を教えてください。"

        # Persist the model reply (structured form so future turns can inspect it).
        _ss.append_message(
            sess.id, turn_index, "model",
            {
                "state": state,
                "criteria_delta": intent.criteria_delta,
                "missing_fields": intent.missing_fields,
                "ask_back": intent.ask_back if state == "asking" else None,
                "reply": reply,
            },
            conn=conn,
        )

        _ss.update_session_state(
            sess.id,
            state=state,
            last_criteria=merged,
            increment_turn=True,
            add_input_tokens=usage_in,
            add_output_tokens=usage_out,
            conn=conn,
        )

        # Compact older messages if we're past the threshold.
        if sess.total_turns + 1 > settings.history_compress_after:
            _ss.maybe_compact_history(
                sess.id,
                compact_after_turns=settings.history_compress_after,
                keep_recent_turns=3,
                conn=conn,
            )

        # Moderate this turn and, if cleared, publish it as a forum thread/reply
        # under the caller's anonymous identity (best-effort; never fails the
        # consult call). ``sess`` still holds the pre-turn log_thread_id, so the
        # first turn creates the thread and later turns append replies.
        post_status = _autopost.record_turn(
            session=sess,
            # Publish the question in Japanese (the agent may have asked in
            # another language); fall back to the raw message.
            user_message=intent.display_ja or message,
            reply=reply,
            state=state,
            criteria=merged,
            results=results_payload,
            keyed_agent_id=agent_id,
            ip=client_ip_var.get(),
            compliant=intent.compliant,
            forum_class=intent.forum,
            forum_override=forum_override,
            area_key=intent.area_key,
            conn=conn,
        )

        # If the turn published to a real-estate board, route the inquiry to
        # brokers whose inventory fits (async — they reply into the same thread).
        # Best-effort; never fails the consult call.
        broker_routing = None
        if (state == "ready" and post_status.get("posted")
                and post_status.get("thread_id") and post_status.get("post_agent_id")):
            shown_ids = [it.get("id") for it in (results_payload or {}).get("items", []) if it.get("id")]
            broker_routing = _autopost.route_to_brokers(
                thread_id=post_status["thread_id"],
                forum=post_status["forum"],
                criteria=merged,
                customer_post_agent_id=post_status["post_agent_id"],
                consult_session_id=sess.id,
                result_listing_ids=shown_ids,
                conn=conn,
            )
            if broker_routing and broker_routing.get("broker_count"):
                reply = (reply + f"\n\nご希望の条件に合う在庫を持つ仲介 "
                         f"{broker_routing['broker_count']} 社にお問い合わせを送りました。"
                         "少し時間をおいて、同じスレッドで担当エージェントの返信をご確認ください。")

        # HOMES "rent it here" links resolved so far for this session's listings
        # (accumulates across turns as background enrichment completes).
        listing_links = _session_listing_links(sess, conn)

        return _envelope(
            sess,
            reply=reply, state=state,
            criteria=merged,
            results=results_payload,
            suggested=_suggest_next(state, results_payload, agent_id is not None),
            usage_in=usage_in, usage_out=usage_out,
            turn_override=sess.total_turns + 1,
            post_status=post_status,
            listing_links=listing_links,
            broker_routing=broker_routing,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_session(
    conn,
    session_id: str | None,
    agent_id: int | None,
    ttl_seconds: int,
) -> _ss.ConsultSession:
    if session_id:
        existing = _ss.get_session(session_id, conn=conn)
        if existing is not None and not _ss.is_expired(existing):
            return existing
        # Expired or unknown — start fresh, but note this in the log.
        if existing is not None:
            log.info("consult session %s expired; starting new", session_id)
    return _ss.create_session(agent_id, ttl_seconds=ttl_seconds, conn=conn)


def _last_assistant_text(history: list[_ss.ConsultMessage]) -> str | None:
    for msg in reversed(history):
        if msg.role == "model":
            return msg.content.get("reply") or msg.content.get("ask_back")
    return None


def _suggest_next(state: str, results: dict | None, has_agent: bool) -> list[str]:
    if state == "done":
        return ["fango_search_listings"]
    if state == "asking":
        return ["fango_consult"]
    out = ["fango_get_listing", "fango_search_listings"]
    if has_agent and results and results.get("items"):
        out.append("fango_save_search")
    return out


def _rate_limited_envelope(session_id: str | None) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "reply": "リクエストが多すぎます。少し時間をおいてから、もう一度お試しください。",
        "state": "done",
        "criteria_extracted": {},
        "results": None,
        "suggested_next_tools": [],
        "turn": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "post_status": {"posted": False, "forum": None, "thread_id": None,
                        "reason": "rate limited"},
        "listing_links": [],
    }


def _done_reply(max_turns: int) -> str:
    return (
        f"このセッションは {max_turns} ターンの上限に達しました。"
        "新しい session_id でやり直すか、fango_search_listings で直接検索してください。"
    )


def _envelope(
    sess: _ss.ConsultSession,
    *,
    reply: str,
    state: str,
    results: dict[str, Any] | None,
    suggested: list[str],
    usage_in: int,
    usage_out: int,
    criteria: dict[str, Any] | None = None,
    turn_override: int | None = None,
    post_status: dict[str, Any] | None = None,
    listing_links: list[dict[str, Any]] | None = None,
    broker_routing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "session_id": sess.id,
        "reply": reply,
        "state": state,
        "criteria_extracted": criteria if criteria is not None else sess.last_criteria,
        "results": results,
        "suggested_next_tools": suggested,
        "turn": turn_override if turn_override is not None else sess.total_turns,
        "usage": {"input_tokens": usage_in, "output_tokens": usage_out},
        # Whether this turn was published to a forum, and why not if held back.
        "post_status": post_status or {"posted": False, "forum": None,
                                       "thread_id": None, "reason": ""},
        # Whether the inquiry was auto-routed to brokers (async; they reply in
        # the same thread). None when no routing happened this turn.
        "broker_routing": broker_routing,
        # HOMES "rent it here" links for this session's listings, resolved so far.
        # Background-enriched, so empty on turn 1 and fills in over later turns.
        "listing_links": listing_links or [],
    }
