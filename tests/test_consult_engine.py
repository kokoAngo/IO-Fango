"""Consult tool — mock the engine and exercise the dialogue state machine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from fango.consult import engine as _engine
from fango.consult import session as _ss
from fango.consult.tool import _run_turn
from fango.listings import service as ls


# ---------------------------------------------------------------------------
# Fake engine — fully scripted, tracks call counts.
# ---------------------------------------------------------------------------

@dataclass
class FakeIntent:
    state: str
    criteria_delta: dict = field(default_factory=dict)
    missing_fields: list = field(default_factory=list)
    ask_back: str | None = None
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class FakeSummary:
    text: str
    input_tokens: int = 80
    output_tokens: int = 40


class FakeEngine:
    def __init__(self, intents: list[FakeIntent], summary_text: str = "おすすめ物件です。"):
        self.intents = list(intents)
        self.summary_text = summary_text
        self.extract_calls = 0
        self.summary_calls = 0
        self.last_history_len = 0
        self.fail_extract = False

    def extract_intent(self, history, user_message):
        self.extract_calls += 1
        self.last_history_len = len(history)
        if self.fail_extract:
            raise RuntimeError("forced failure")
        if not self.intents:
            return FakeIntent(state="asking", ask_back="more info please")
        return self.intents.pop(0)

    def summarise_results(self, criteria, listings, user_message, last_assistant=None):
        self.summary_calls += 1
        return FakeSummary(text=self.summary_text)


@pytest.fixture
def install_engine(monkeypatch):
    """Yield a setter; cleans up after each test."""
    def _set(engine):
        _engine.set_engine(engine)
    yield _set
    _engine.set_engine(None)


# ---------------------------------------------------------------------------
# Test scenarios
# ---------------------------------------------------------------------------

def test_new_session_asking_state(tmp_db, install_engine):
    fake = FakeEngine([FakeIntent(state="asking", ask_back="駅は?")])
    install_engine(fake)
    out = _run_turn("家探したい", session_id=None)
    assert out["state"] == "asking"
    assert out["reply"] == "駅は?"
    assert out["session_id"].startswith("cs_")
    assert out["results"] is None
    assert fake.extract_calls == 1
    assert fake.summary_calls == 0


def test_multi_turn_continues_session(tmp_db, install_engine):
    fake = FakeEngine([
        FakeIntent(state="asking", criteria_delta={"prefecture": "東京都"}, ask_back="間取りは?"),
        FakeIntent(state="asking", criteria_delta={"layout": "1LDK"}, ask_back="予算は?"),
    ])
    install_engine(fake)
    a = _run_turn("東京で部屋を探したい", session_id=None)
    b = _run_turn("1LDKがいい", session_id=a["session_id"])
    assert a["session_id"] == b["session_id"]
    assert b["state"] == "asking"
    # Criteria should accumulate across turns.
    assert b["criteria_extracted"]["prefecture"] == "東京都"
    assert b["criteria_extracted"]["layout"] == "1LDK"
    assert fake.extract_calls == 2


def test_ready_state_runs_search_and_calls_summary(tmp_db, install_engine):
    # Insert via service so rent_yen is populated (listing_factory lacks it).
    ls.insert_listing({
        "reins_id": "R1",
        "building_name": "Test 1LDK",
        "address": "東京都渋谷区",
        "prefecture": "東京都",
        "layout": "1LDK",
        "price_man": 10,
        "rent_yen": 100_000,
    })
    fake = FakeEngine([
        FakeIntent(
            state="ready",
            criteria_delta={"prefecture": "東京都", "layout": "1LDK", "rent_max_yen": 200_000},
        )
    ], summary_text="代々木上原の1LDKをおすすめします。")
    install_engine(fake)
    out = _run_turn("ピアノ弾ける1LDK 20万以下 東京", session_id=None)
    assert out["state"] == "ready"
    assert out["reply"] == "代々木上原の1LDKをおすすめします。"
    assert out["results"] is not None
    assert out["results"]["total"] >= 1
    assert fake.summary_calls == 1


def test_max_turns_caps_session(tmp_db, install_engine, monkeypatch):
    # Force max_turns=2 so the third call should immediately return state=done.
    monkeypatch.setenv("FANGO_CONSULT_MAX_TURNS", "2")
    fake = FakeEngine([
        FakeIntent(state="asking", ask_back="?1"),
        FakeIntent(state="asking", ask_back="?2"),
    ])
    install_engine(fake)
    r1 = _run_turn("hi", None)
    r2 = _run_turn("more", r1["session_id"])
    r3 = _run_turn("again", r1["session_id"])
    assert r3["state"] == "done"
    # extract should not have been called for the capped turn.
    assert fake.extract_calls == 2


def test_expired_session_creates_new(tmp_db, install_engine):
    fake = FakeEngine([
        FakeIntent(state="asking", ask_back="?1"),
        FakeIntent(state="asking", ask_back="?2"),
    ])
    install_engine(fake)
    r1 = _run_turn("hi", None)
    # Force expiry by clamping expires_at to the past.
    from fango.db import connect
    conn = connect(tmp_db)
    try:
        conn.execute(
            "UPDATE consult_sessions SET expires_at = '2000-01-01T00:00:00.000Z' WHERE id = ?",
            (r1["session_id"],),
        )
    finally:
        conn.close()
    r2 = _run_turn("more", r1["session_id"])
    assert r2["session_id"] != r1["session_id"]


def test_history_compaction_kicks_in(tmp_db, install_engine, monkeypatch):
    monkeypatch.setenv("FANGO_CONSULT_HISTORY_COMPRESS_AFTER", "2")
    monkeypatch.setenv("FANGO_CONSULT_MAX_TURNS", "10")
    fake = FakeEngine([
        FakeIntent(state="asking", ask_back="q1"),
        FakeIntent(state="asking", ask_back="q2"),
        FakeIntent(state="asking", ask_back="q3"),
        FakeIntent(state="asking", ask_back="q4"),
    ])
    install_engine(fake)
    sid = None
    for msg in ["a", "b", "c", "d"]:
        out = _run_turn(msg, sid)
        sid = out["session_id"]
    msgs = _ss.get_messages(sid)
    assert any(m.role == "system_note" for m in msgs), (
        "compaction should have emitted a system_note row"
    )


def test_engine_failure_falls_back(tmp_db, install_engine):
    """If the engine raises, the tool must still return a usable envelope."""
    fake = FakeEngine([])
    fake.fail_extract = True
    install_engine(fake)
    out = _run_turn("hi", None)
    assert out["state"] == "asking"
    # Reply is the FALLBACK_ASKBACK constant.
    from fango.consult.prompts import FALLBACK_ASKBACK
    assert out["reply"] == FALLBACK_ASKBACK
    assert out["results"] is None
