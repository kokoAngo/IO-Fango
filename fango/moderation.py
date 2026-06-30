"""Single moderation gate for everything published to the public forum.

Two layers, in order:
1. Deterministic PII scrub (:mod:`fango.consult.pii`) — names removed in place;
   unambiguous contact info (email/phone) blocks the post.
2. LLM compliance verdict (:meth:`fango.consult.engine.Engine.moderate`) — legal /
   on-topic / no spam.

Reads and searches are NOT gated — only *publish* actions route through
:func:`screen`. Callers that already hold a folded compliance verdict (consult's
``extract_intent`` returns ``compliant``/``forum``) pass it via ``verdict=`` so we
don't spend a second LLM call. System-generated text (routing notes, deal
confirmations) passes ``use_llm=False`` (scrub only).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .consult import pii

_PII_BLOCKED = "個人情報（連絡先）が含まれるため公開を控えました。"
_NONCOMPLIANT = "公開ガイドラインに合致しませんでした。"


@dataclass
class ScreenResult:
    approved: bool
    text: str                 # PII-scrubbed; safe to publish when approved
    reason: str = ""          # why rejected (empty when approved)
    forum: str | None = None  # suggested board (advisory; None when rejected)


def screen(
    text: str,
    *,
    forum_hint: str | None = None,
    verdict: tuple[bool, str | None] | None = None,
    use_llm: bool = True,
    conn: sqlite3.Connection | None = None,
) -> ScreenResult:
    """Screen ``text`` for publication. Never raises.

    - ``verdict=(compliant, forum)``: reuse an already-computed verdict (consult)
      — no LLM call.
    - ``use_llm=True`` (default) and no verdict: run the LLM moderator.
    - ``use_llm=False``: scrub only (for server-generated system text).

    Gating is on the compliance boolean (not on whether a forum was chosen) so a
    reply into an existing thread isn't rejected merely for lacking a route.
    """
    scrubbed, hits = pii.scrub_for_publish((text or "").strip())
    if pii.BLOCKING & set(hits):
        return ScreenResult(False, scrubbed, _PII_BLOCKED, None)

    if verdict is not None:
        compliant, forum = verdict
        if not compliant:
            return ScreenResult(False, scrubbed, _NONCOMPLIANT, None)
        return ScreenResult(True, scrubbed, "", forum)

    if not use_llm:
        return ScreenResult(True, scrubbed, "", forum_hint)

    try:
        from .consult import engine as _engine
        mod = _engine.get_engine().moderate(scrubbed, forum_hint=forum_hint)
    except Exception:
        # Defensive: the engine already fails closed on its own; if the call
        # itself blows up, hold the post back rather than publish unscreened.
        return ScreenResult(False, scrubbed, _NONCOMPLIANT, None)
    if not mod.compliant:
        return ScreenResult(False, scrubbed, mod.reason or _NONCOMPLIANT, None)
    return ScreenResult(True, scrubbed, "", mod.forum or forum_hint)
