"""Engine moderation: degraded-mode fail-closed/open + verdict helpers."""
from __future__ import annotations

from fango.consult.engine import GeminiEngine, ModerationResult


def test_moderation_result_approved():
    assert ModerationResult(compliant=True, forum="chintai").approved is True
    assert ModerationResult(compliant=True, forum=None).approved is False
    assert ModerationResult(compliant=False, forum="chintai").approved is False
    assert ModerationResult(compliant=True, forum="nope").approved is False


def test_degraded_engine_fails_closed(monkeypatch):
    monkeypatch.delenv("FANGO_MODERATION_FAIL_OPEN", raising=False)
    eng = GeminiEngine(model="x", api_key=None)  # no key → degraded
    res = eng.moderate("東京で2LDKを探しています", forum_hint="chintai")
    assert res.compliant is False
    assert res.approved is False
    assert res.reason  # carries an explanation back to the agent


def test_degraded_engine_fail_open_escape_hatch(monkeypatch):
    monkeypatch.setenv("FANGO_MODERATION_FAIL_OPEN", "1")
    eng = GeminiEngine(model="x", api_key=None)
    res = eng.moderate("東京で2LDKを探しています", forum_hint="baibai")
    assert res.compliant is True
    assert res.forum == "baibai"
    assert res.approved is True
