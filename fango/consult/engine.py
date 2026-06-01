"""Gemini-backed inference engine for the consult tool.

Two callables are exposed:

* :func:`extract_intent` — given history + current user message, returns the
  JSON intent dict (state / criteria_delta / missing / ask_back).
* :func:`summarise_results` — given criteria + search results + user message,
  returns a natural-language reply string.

Both honour the module-level ``_engine`` singleton; tests should call
:func:`set_engine` with a fake before exercising the consult tool.

The default engine uses ``google-genai`` and reads ``GEMINI_API_KEY`` from env.
If the key is missing the engine falls back to a degraded mode that emits
fixed responses (still safe to call — useful when running the rest of the
stack without an LLM).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import prompts
from .session import ConsultMessage

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class IntentResult:
    state: str  # 'asking' | 'ready'
    criteria_delta: dict[str, Any] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    ask_back: str | None = None
    # Folded-in moderation verdict (so consult needs only one LLM call, not a
    # separate moderate() round-trip): may this turn be published, and where.
    compliant: bool = True
    forum: str | None = None
    # The user's message rephrased in natural Japanese for forum display (posts
    # should read as Japanese even when the agent asked in another language).
    display_ja: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class SummaryResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


_FORUMS = ("baibai", "chintai", "chat", "dojo")


@dataclass
class ModerationResult:
    """Verdict on whether a candidate post may be published, and where.

    ``forum`` is the routing target (one of the four forum codes) or ``None``
    when the content fits nowhere / is rejected.
    """
    compliant: bool
    forum: str | None
    reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def approved(self) -> bool:
        return bool(self.compliant) and self.forum in _FORUMS


class Engine(Protocol):
    """Pluggable inference surface — implemented by ``GeminiEngine`` (real)
    and the test ``FakeEngine``.
    """

    def extract_intent(
        self,
        history: list[ConsultMessage],
        user_message: str,
    ) -> IntentResult: ...

    def summarise_results(
        self,
        criteria: dict[str, Any],
        listings: list[dict[str, Any]],
        user_message: str,
        last_assistant: str | None = None,
        approximate: bool = False,
        relax_note: str | None = None,
    ) -> SummaryResult: ...

    def moderate(
        self,
        text: str,
        forum_hint: str | None = None,
    ) -> ModerationResult: ...


# ---------------------------------------------------------------------------
# Real engine — Gemini
# ---------------------------------------------------------------------------

class GeminiEngine:
    """Gemini Flash backed engine.

    Lazy-imports ``google-genai`` so the package is optional. If the SDK or
    the API key are missing, instances enter ``degraded`` mode and emit
    canned fallback responses (logged as warnings).
    """

    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self.api_key = api_key
        self._client = None
        self._degraded = False
        self._init_client()

    def _init_client(self) -> None:
        if not self.api_key:
            log.warning("GEMINI_API_KEY not set — consult engine in degraded mode")
            self._degraded = True
            return
        try:
            from google import genai  # type: ignore
        except ImportError:
            log.warning("google-genai not installed — consult engine in degraded mode")
            self._degraded = True
            return
        try:
            self._client = genai.Client(api_key=self.api_key)
        except Exception as exc:  # pragma: no cover
            log.warning("Gemini client init failed: %s", exc)
            self._degraded = True

    def extract_intent(
        self,
        history: list[ConsultMessage],
        user_message: str,
    ) -> IntentResult:
        if self._degraded:
            return _degraded_extract(user_message)
        contents = _build_extract_contents(history, user_message)
        try:
            from google.genai import types  # type: ignore
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=prompts.SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    response_schema=prompts.EXTRACT_RESPONSE_SCHEMA,
                    temperature=0.2,
                ),
            )
            text = response.text or ""
            usage = response.usage_metadata
        except Exception as exc:
            log.warning("extract_intent LLM call failed: %s", exc)
            return _degraded_extract(user_message)
        return _parse_intent(text, usage)

    def summarise_results(
        self,
        criteria: dict[str, Any],
        listings: list[dict[str, Any]],
        user_message: str,
        last_assistant: str | None = None,
        approximate: bool = False,
        relax_note: str | None = None,
    ) -> SummaryResult:
        if self._degraded:
            return SummaryResult(text=_degraded_summary(listings, approximate, relax_note))
        prompt = prompts.render_summary_prompt(
            criteria, listings, user_message, last_assistant_message=last_assistant,
            approximate=approximate, relax_note=relax_note,
        )
        try:
            from google.genai import types  # type: ignore
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=prompts.SUMMARY_SYSTEM_PROMPT,
                    temperature=0.4,
                ),
            )
            text = (response.text or "").strip()
            usage = response.usage_metadata
        except Exception as exc:
            log.warning("summarise_results LLM call failed: %s", exc)
            return SummaryResult(text=_degraded_summary(listings))
        return SummaryResult(
            text=text or _degraded_summary(listings),
            input_tokens=_get_usage(usage, "prompt_token_count"),
            output_tokens=_get_usage(usage, "candidates_token_count"),
        )

    def moderate(
        self,
        text: str,
        forum_hint: str | None = None,
    ) -> ModerationResult:
        if self._degraded:
            return _degraded_moderate(forum_hint)
        contents = [
            {"role": "user", "parts": [{"text": prompts.render_moderation_input(text, forum_hint)}]}
        ]
        try:
            from google.genai import types  # type: ignore
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=prompts.MODERATION_PROMPT,
                    response_mime_type="application/json",
                    response_schema=prompts.MODERATION_SCHEMA,
                    temperature=0.1,
                ),
            )
            raw = response.text or ""
            usage = response.usage_metadata
        except Exception as exc:
            log.warning("moderate LLM call failed: %s", exc)
            return _degraded_moderate(forum_hint)
        return _parse_moderation(raw, usage, forum_hint)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_extract_contents(
    history: list[ConsultMessage],
    user_message: str,
) -> list[dict[str, Any]]:
    """Flatten history into Gemini ``contents`` list.

    Gemini takes a list of {role, parts:[{text}]} dicts. We promote
    ``system_note`` messages into a leading user turn so they're not dropped.
    """
    out: list[dict[str, Any]] = []
    for msg in history:
        if msg.role == "user":
            text = msg.content.get("text", "")
            if text:
                out.append({"role": "user", "parts": [{"text": text}]})
        elif msg.role == "model":
            # Re-emit the structured JSON so the model has its own prior answers.
            payload = {
                k: msg.content.get(k)
                for k in ("state", "criteria_delta", "missing_fields", "ask_back")
                if msg.content.get(k) is not None
            }
            out.append({
                "role": "model",
                "parts": [{"text": json.dumps(payload, ensure_ascii=False)}],
            })
        elif msg.role == "system_note":
            # Compacted summary — feed as a user-side note so it's preserved.
            out.append({
                "role": "user",
                "parts": [{"text": "[過去の対話の要約] " + json.dumps(msg.content, ensure_ascii=False)}],
            })
    out.append({"role": "user", "parts": [{"text": user_message}]})
    return out


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_intent(raw_text: str, usage: Any) -> IntentResult:
    """Tolerant JSON extraction — Gemini occasionally wraps responses."""
    if not raw_text:
        return IntentResult(state="asking", ask_back=prompts.FALLBACK_ASKBACK)
    text = raw_text.strip()
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_FENCE_RE.search(text)
        if m:
            try:
                parsed = json.loads(m.group(1))
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        log.warning("intent parse failed; raw=%r", text[:200])
        return IntentResult(state="asking", ask_back=prompts.FALLBACK_ASKBACK)
    state = parsed.get("state") or "asking"
    if state not in ("asking", "ready"):
        state = "asking"
    delta = parsed.get("criteria_delta") or {}
    if not isinstance(delta, dict):
        delta = {}
    missing = parsed.get("missing_fields") or []
    if not isinstance(missing, list):
        missing = []
    ask_back = parsed.get("ask_back")
    if state == "asking" and not ask_back:
        ask_back = prompts.FALLBACK_ASKBACK
    # Folded moderation verdict. Absent → treat as compliant (the LLM processed
    # it); the degraded/error path below is the one that fails closed.
    compliant = parsed.get("compliant")
    compliant = True if compliant is None else bool(compliant)
    forum = parsed.get("forum")
    if forum not in _FORUMS:
        forum = None
    display_ja = parsed.get("display_ja")
    display_ja = str(display_ja).strip() if display_ja else None
    return IntentResult(
        state=state,
        criteria_delta=delta,
        missing_fields=[str(x) for x in missing],
        ask_back=ask_back if state == "asking" else None,
        compliant=compliant,
        forum=forum,
        display_ja=display_ja,
        input_tokens=_get_usage(usage, "prompt_token_count"),
        output_tokens=_get_usage(usage, "candidates_token_count"),
    )


def _loads_tolerant(raw_text: str) -> dict | None:
    """Parse a JSON object, tolerating ```json fences``` Gemini sometimes adds."""
    if not raw_text:
        return None
    text = raw_text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_FENCE_RE.search(text)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _moderation_fail_open() -> bool:
    """When moderation is unavailable, post anyway? Default false (fail-closed)."""
    return os.environ.get("FANGO_MODERATION_FAIL_OPEN", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _degraded_moderate(forum_hint: str | None) -> ModerationResult:
    """No LLM available. Fail-closed by default; fail-open only if configured."""
    if _moderation_fail_open():
        return ModerationResult(
            compliant=True,
            forum=forum_hint if forum_hint in _FORUMS else "chintai",
            reason="moderation skipped (fail-open)",
        )
    return ModerationResult(
        compliant=False, forum=None, reason=prompts.FALLBACK_MODERATION_REASON,
    )


def _parse_moderation(raw_text: str, usage: Any, forum_hint: str | None) -> ModerationResult:
    parsed = _loads_tolerant(raw_text)
    if parsed is None:
        log.warning("moderation parse failed; raw=%r", (raw_text or "")[:200])
        return _degraded_moderate(forum_hint)
    forum = parsed.get("forum")
    if forum not in _FORUMS:
        forum = None
    return ModerationResult(
        compliant=bool(parsed.get("compliant")),
        forum=forum,
        reason=str(parsed.get("reason") or ""),
        input_tokens=_get_usage(usage, "prompt_token_count"),
        output_tokens=_get_usage(usage, "candidates_token_count"),
    )


def _get_usage(usage: Any, attr: str) -> int:
    if usage is None:
        return 0
    val = getattr(usage, attr, None)
    if val is None and isinstance(usage, dict):
        val = usage.get(attr)
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def _degraded_extract(user_message: str) -> IntentResult:
    """Heuristic extraction used when no LLM is available."""
    delta: dict[str, Any] = {}
    text = user_message
    # Very simple regex sniffs — enough for smoke tests.
    m = re.search(r"(\d+)(?:\s*万円|\s*万)", text)
    if m:
        delta["rent_max_yen"] = int(m.group(1)) * 10000
    m = re.search(r"(\d+[LDKS]+)", text, re.IGNORECASE)
    if m:
        delta["layout"] = m.group(1).upper()
    for pref in ("東京都", "神奈川県", "千葉県", "埼玉県", "大阪府", "京都府"):
        if pref in text:
            delta["prefecture"] = pref
            break
    return IntentResult(
        state="asking",
        criteria_delta=delta,
        missing_fields=["prefecture", "layout", "rent_max_yen"],
        ask_back=prompts.FALLBACK_ASKBACK,
        # No LLM → no moderation verdict: fail closed (don't publish) unless the
        # operator opted into fail-open.
        compliant=_moderation_fail_open(),
    )


def _degraded_summary(listings: list[dict[str, Any]], approximate: bool = False,
                      relax_note: str | None = None) -> str:
    if not listings:
        return prompts.FALLBACK_SUMMARY + "（該当物件が見つかりませんでした）"
    if approximate:
        head = "ご希望に完全一致する物件はありませんでしたが、近い条件で以下が見つかりました:"
        parts = [head] + ([relax_note] if relax_note else [])
    else:
        parts = ["以下の物件が条件に合いそうです:"]
    for L in listings[:3]:
        rent = L.get("rent_yen")
        rent_str = f"月額 {rent:,} 円" if rent else "賃料未公開"
        parts.append(
            f"・[id={L.get('id')}] {L.get('building_name') or L.get('title') or '物件'}"
            f" — {L.get('layout') or '間取り不明'} / {L.get('area_sqm') or '?'} ㎡ / {rent_str}"
        )
    parts.append("気になる物件があれば listing_id を教えてください。")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Module-level singleton + test hook
# ---------------------------------------------------------------------------

_engine: Engine | None = None


def get_engine() -> Engine:
    """Lazily initialise the singleton from settings."""
    global _engine
    if _engine is None:
        from ..config import load_consult_settings
        s = load_consult_settings()
        _engine = GeminiEngine(model=s.model, api_key=s.api_key)
    return _engine


def set_engine(engine: Engine | None) -> None:
    """Test hook — install a fake (or reset to None for re-init)."""
    global _engine
    _engine = engine
