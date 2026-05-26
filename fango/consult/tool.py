"""``fango_consult`` MCP tool — server-side natural-language advisor.

This wires together :mod:`fango.consult.session` (state) and
:mod:`fango.consult.engine` (LLM) and exposes the dialogue as a single tool
the caller invokes once per turn.
"""
from __future__ import annotations

import logging
from typing import Any

from ..auth import current_agent_var
from ..config import load_consult_settings
from ..db import connect
from ..listings import service as ls
from ..listings.tools import _listing_brief
from . import engine as _engine
from . import session as _ss

log = logging.getLogger(__name__)


def register(mcp) -> None:

    @mcp.tool()
    def fango_consult(
        message: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Natural-language house-hunting advisor.

        Send the owner's request in any language. FANGO will (a) ask back when
        more conditions are needed, (b) once enough conditions are known,
        execute an internal listing search and return both a natural-language
        recommendation and a structured ``results`` list.

        Pass the ``session_id`` from a previous response back in to continue
        the same conversation.

        Args:
            message: The current user turn (free-form Japanese or English).
            session_id: Continue an existing dialogue. Omit on the first call.

        Returns:
            dict with keys ``session_id``, ``reply``, ``state``
            ('asking' | 'ready' | 'done'), ``criteria_extracted``,
            ``results`` ({items, total}), ``suggested_next_tools``, ``turn``,
            ``usage`` ({input_tokens, output_tokens}).
        """
        return _run_turn(message=message, session_id=session_id)


# ---------------------------------------------------------------------------
# Core handler — separated from the decorator for testability.
# ---------------------------------------------------------------------------

def _run_turn(message: str, session_id: str | None) -> dict[str, Any]:
    settings = load_consult_settings()
    agent = current_agent_var.get()  # optional; consult does not require auth
    agent_id = agent.id if agent else None

    conn = connect()
    try:
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
            )

        # Merge delta into the running criteria.
        merged = dict(sess.last_criteria)
        merged.update({k: v for k, v in intent.criteria_delta.items() if v not in (None, "")})

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
            briefs = [_listing_brief(r, conn=conn) for r in rows]
            total = ls.count_listings(criteria=merged, conn=conn)
            last_assistant = _last_assistant_text(history_for_llm)
            try:
                summary = engine.summarise_results(
                    merged, briefs, message, last_assistant=last_assistant,
                )
                reply = summary.text
                usage_in += summary.input_tokens
                usage_out += summary.output_tokens
            except Exception as exc:
                log.warning("engine.summarise_results raised: %s", exc)
                from . import prompts as _prompts
                reply = _prompts.FALLBACK_SUMMARY
            results_payload = {"total": total, "items": briefs}
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

        return _envelope(
            sess,
            reply=reply, state=state,
            criteria=merged,
            results=results_payload,
            suggested=_suggest_next(state, results_payload, agent_id is not None),
            usage_in=usage_in, usage_out=usage_out,
            turn_override=sess.total_turns + 1,
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
    }
