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


# ---------------------------------------------------------------------------
# The shared publish gate: fango.moderation.screen
# ---------------------------------------------------------------------------

import pytest
from fango import moderation
from fango.consult.engine import set_engine


class _FakeEng:
    """Minimal engine exposing only moderate() — what screen() needs."""
    def __init__(self, compliant=True, forum="chintai", reason="", boom=False):
        self.compliant, self.forum, self.reason, self.boom = compliant, forum, reason, boom
        self.calls = 0

    def moderate(self, text, forum_hint=None):
        self.calls += 1
        if self.boom:
            raise RuntimeError("engine down")
        return ModerationResult(compliant=self.compliant, forum=self.forum, reason=self.reason)


@pytest.fixture
def fake_engine():
    eng = _FakeEng()
    set_engine(eng)
    try:
        yield eng
    finally:
        set_engine(None)


def test_screen_blocks_contact_pii():
    r = moderation.screen("連絡は taro@example.com まで", use_llm=False)
    assert r.approved is False and "個人情報" in r.reason


def test_screen_llm_approves(fake_engine):
    r = moderation.screen("新宿の2LDKを探しています")
    assert r.approved is True
    assert fake_engine.calls == 1


def test_screen_verdict_passthrough_skips_llm(fake_engine):
    r = moderation.screen("新宿の物件", verdict=(True, "chintai"))
    assert r.approved is True and r.forum == "chintai"
    assert fake_engine.calls == 0   # reused folded verdict, no LLM call


def test_screen_verdict_reject():
    assert moderation.screen("...", verdict=(False, None)).approved is False


def test_screen_use_llm_false_skips_engine(fake_engine):
    r = moderation.screen("システムからのお知らせ", use_llm=False)
    assert r.approved is True and fake_engine.calls == 0


def test_screen_llm_rejects_noncompliant():
    set_engine(_FakeEng(compliant=False, forum=None, reason="ガイドライン違反"))
    try:
        r = moderation.screen("（違反内容）")
        assert r.approved is False and r.reason == "ガイドライン違反"
    finally:
        set_engine(None)


def test_screen_engine_failure_fails_closed():
    set_engine(_FakeEng(boom=True))
    try:
        assert moderation.screen("text").approved is False  # held back, not published
    finally:
        set_engine(None)
