"""Consult auto-post — each turn becomes a Q post (agent) + an A post (FANGO)."""
from __future__ import annotations

import pytest

from fango import forum_core
from fango.auth import current_agent_var, get_or_create_system_agent, pseudonym
from fango.consult import session as _ss
from fango.consult.tool import _run_turn
from fango.listings import service as ls

# Reuse the scripted fake engine + setter fixture from the engine test module.
from tests.test_consult_engine import FakeEngine, FakeIntent, install_engine  # noqa: F401


def _session(sid):
    return _ss.get_session(sid)


def test_rental_consult_creates_chintai_qa(tmp_db, install_engine):
    ls.insert_listing({
        "reins_id": "R1", "building_name": "Test 1LDK", "address": "東京都渋谷区",
        "prefecture": "東京都", "layout": "1LDK", "rent_yen": 100_000,
    })
    fake = FakeEngine([
        FakeIntent(state="ready", criteria_delta={
            "prefecture": "東京都", "layout": "1LDK", "rent_max_yen": 200_000,
        })
    ], summary_text="代々木上原の1LDKをおすすめします。")
    install_engine(fake)

    out = _run_turn("東京 1LDK 20万以下", session_id=None)

    sess = _session(out["session_id"])
    assert sess.log_forum == "chintai" and sess.log_thread_id is not None
    system = get_or_create_system_agent()
    posts = forum_core.get_thread("chintai", sess.log_thread_id)["posts"]

    # One turn → two posts: the agent's question, then FANGO's answer.
    assert len(posts) == 2
    q, a = posts
    # Q: the agent's raw message, authored by the anonymous identity.
    assert q.body == "東京 1LDK 20万以下"
    assert sess.post_agent_id is not None
    assert q.author_id == sess.post_agent_id != system.id
    # A: FANGO's reply + search artifacts, authored by the FANGO narrator,
    #    with the top listing attached.
    assert a.author_id == system.id
    assert "代々木上原の1LDKをおすすめします。" in a.body
    assert "条件:" in a.body and "1LDK" in a.body
    assert a.listing_refs and not q.listing_refs


def test_sale_consult_routes_to_baibai(tmp_db, install_engine):
    fake = FakeEngine([
        FakeIntent(state="ready", criteria_delta={"prefecture": "東京都", "price_max_man": 8000})
    ])
    install_engine(fake)
    out = _run_turn("東京 中古マンション 8000万以下", session_id=None)
    sess = _session(out["session_id"])
    assert sess.log_forum == "baibai"
    assert len(forum_core.get_thread("baibai", sess.log_thread_id)["posts"]) == 2


def test_alternating_qa_across_two_turns(tmp_db, install_engine):
    fake = FakeEngine([
        FakeIntent(state="asking", criteria_delta={"prefecture": "東京都"}, ask_back="間取りは?"),
        FakeIntent(state="asking", criteria_delta={"layout": "1LDK"}, ask_back="予算は?"),
    ])
    install_engine(fake)
    a = _run_turn("東京で探したい", session_id=None)
    b = _run_turn("1LDK", session_id=a["session_id"])

    sess = _session(b["session_id"])
    posts = forum_core.get_thread(sess.log_forum, sess.log_thread_id)["posts"]
    system = get_or_create_system_agent()
    # Two turns → 4 posts: Q1, A1, Q2, A2 (one thread, alternating authors).
    assert len(posts) == 4
    assert posts[0].body == "東京で探したい" and "間取りは?" in posts[1].body
    assert posts[2].body == "1LDK" and "予算は?" in posts[3].body
    # Questions by the anon asker, answers by FANGO.
    assert posts[0].author_id == posts[2].author_id == sess.post_agent_id
    assert posts[1].author_id == posts[3].author_id == system.id


def test_keyless_caller_minted_anon_identity(tmp_db, install_engine):
    assert current_agent_var.get() is None
    fake = FakeEngine([FakeIntent(state="asking", ask_back="駅は?")])
    install_engine(fake)
    out = _run_turn("家を探したい", session_id=None)
    sess = _session(out["session_id"])
    assert sess.post_agent_id is not None  # an anon agent was minted
    posts = forum_core.get_thread(sess.log_forum, sess.log_thread_id)["posts"]
    # Question authored by the minted anon identity; its pseudonym is well-formed.
    assert posts[0].author_id == sess.post_agent_id
    assert pseudonym(sess.post_agent_id).replace("_", "").isalnum()


def test_named_agent_question_attributed_not_named(tmp_db, install_engine, agent_factory, with_current_agent):
    # The question post is authored by the keyed agent, but the real name never
    # appears (display is a pseudonym at render time).
    agent, _ = agent_factory("mira-7")
    with_current_agent(agent)
    fake = FakeEngine([FakeIntent(state="asking", ask_back="駅は?")])
    install_engine(fake)
    out = _run_turn("家を探したい", session_id=None)
    sess = _session(out["session_id"])
    posts = forum_core.get_thread(sess.log_forum, sess.log_thread_id)["posts"]
    assert posts[0].author_id == agent.id
    assert "mira-7" not in posts[0].body and "mira-7" not in posts[1].body


def test_same_area_sessions_group_into_one_thread(tmp_db, install_engine):
    # Two separate sessions about the SAME area (大田区: 雪が谷 / 石川台) land in
    # one thread — related multi-turn discussion stays together.
    install_engine(FakeEngine([
        FakeIntent(state="asking", ask_back="雪が谷の件?", area_key="大田区"),
        FakeIntent(state="asking", ask_back="石川台の件?", area_key="大田区"),
    ]))
    a = _run_turn("雪が谷で1LDKを探しています", session_id=None)
    b = _run_turn("石川台はどうですか", session_id=None)
    sa, sb = _session(a["session_id"]), _session(b["session_id"])
    assert a["session_id"] != b["session_id"]            # genuinely separate sessions
    assert sa.log_thread_id == sb.log_thread_id           # but the same thread
    assert len(forum_core.get_thread(sb.log_forum, sb.log_thread_id)["posts"]) == 4


def test_different_areas_get_separate_threads(tmp_db, install_engine):
    # Different wards (台東区 浅草 vs 文京区) must NOT merge, even same caller/window.
    install_engine(FakeEngine([
        FakeIntent(state="asking", ask_back="浅草の件?", area_key="台東区"),
        FakeIntent(state="asking", ask_back="文京区の件?", area_key="文京区"),
    ]))
    a = _run_turn("浅草で賃貸を探しています", session_id=None)
    b = _run_turn("文京区で賃貸を探しています", session_id=None)
    sa, sb = _session(a["session_id"]), _session(b["session_id"])
    assert sa.log_thread_id != sb.log_thread_id           # separate threads per area


def test_no_exact_match_falls_back_to_near_options(tmp_db, install_engine):
    # Nothing matches 雪が谷 / 10万円, but loosening (drop station, widen budget)
    # surfaces a near option — we recommend that instead of "no results".
    ls.insert_listing({
        "reins_id": "N1", "building_name": "渋谷ハイツ", "prefecture": "東京都",
        "city": "渋谷区", "station": "渋谷", "layout": "1LDK", "rent_yen": 130_000,
    })
    fake = FakeEngine([FakeIntent(state="ready", criteria_delta={
        "prefecture": "東京都", "station": "雪が谷", "layout": "1LDK", "rent_max_yen": 100_000,
    })], summary_text="近い条件の物件です。")
    install_engine(fake)
    out = _run_turn("雪が谷 1LDK 10万以下", session_id=None)
    assert out["state"] == "ready"
    assert out["results"]["approximate"] is True
    assert len(out["results"]["items"]) >= 1
    assert fake.last_summary_approximate is True
    # The posted answer flags it as a near match, not an exact hit.
    sess = _session(out["session_id"])
    body = forum_core.get_thread(sess.log_forum, sess.log_thread_id)["posts"][-1].body
    assert "近い条件" in body


def test_moderation_block_skips_post(tmp_db, install_engine):
    # The moderation verdict is folded into intent extraction now.
    fake = FakeEngine([FakeIntent(state="asking", ask_back="駅は?", compliant=False)])
    install_engine(fake)
    out = _run_turn("ここで靴を売りたい", session_id=None)
    assert out["post_status"]["posted"] is False
    assert _session(out["session_id"]).log_thread_id is None  # nothing published
