"""PII safety net: the deterministic scrubber + autopost integration.

Guards against the real incident where "Chris Dai氏向けの…" reached a public
forum post. Names must be stripped, contact info must block the whole post,
and legitimate search conditions (areas, budget, layout) must survive.
"""
from __future__ import annotations

from fango.consult import pii


# --------------------------------------------------------------------------
# Scrubber unit tests
# --------------------------------------------------------------------------

def test_latin_name_with_honorific_is_removed():
    text = "Chris Dai氏向けの東京都内での賃貸物件を探しています。"
    out, hits = pii.scrub_for_publish(text)
    assert "Chris" not in out and "Dai" not in out
    assert "name" in hits
    # Search condition survives.
    assert "東京都内での賃貸物件を探しています" in out
    assert out.startswith("オーナー向けの")


def test_bare_latin_name_without_honorific_removed():
    # The exact hole the incident slipped through: a Latin name with NO title
    # and NO honorific, in various contexts.
    for text in (
        "Chris Dai向けの東京都内の賃貸物件",
        "Chris Daiのための物件を探しています",
        "Find a rental in Tokyo for Chris Dai",
        "Chris Dai is looking for a 2LDK",
    ):
        out, hits = pii.scrub_for_publish(text)
        assert "Chris" not in out and "Dai" not in out, text
        assert "name" in hits, text


def test_latin_place_and_building_names_survive():
    # Geography / building vocabulary must NOT be generalised to オーナー.
    for text in ("Tokyo Tower の近く", "Shibuya Station 徒歩5分", "Park Hills という物件"):
        out, hits = pii.scrub_for_publish(text)
        assert "オーナー" not in out, text
        assert "name" not in hits, text


def test_western_title_name_removed():
    out, hits = pii.scrub_for_publish("Mr. John Smith のための物件")
    assert "John" not in out and "Smith" not in out
    assert "name" in hits


def test_japanese_name_with_honorific_removed():
    out, hits = pii.scrub_for_publish("田中さんのご希望に合う2LDK")
    assert "田中" not in out
    assert "name" in hits
    assert "2LDK" in out


def test_polite_nouns_not_treated_as_names():
    # 皆さん / お客様 are politeness, not personal names — must NOT be scrubbed.
    for phrase in ("皆さんで住める広い家", "お客様を招きやすい間取り"):
        out, hits = pii.scrub_for_publish(phrase)
        assert out == phrase
        assert "name" not in hits


def test_email_is_blocking():
    out, hits = pii.scrub_for_publish("連絡は taro@example.com まで")
    assert "@example.com" not in out
    assert "email" in hits
    assert pii.BLOCKING & set(hits)
    assert pii.has_blocking_pii("taro@example.com")


def test_phone_is_blocking():
    for num in ("090-1234-5678", "03-1234-5678", "+81 90-1234-5678", "09012345678"):
        assert pii.has_blocking_pii(f"電話は{num}です"), num


def test_prices_and_areas_are_not_phones():
    # Plain figures (rent, area, walk minutes, year) must not trip the phone rule.
    safe = "東京都 文京区 2LDK 50㎡以上 駅徒歩10分 家賃150000円 築2015年"
    out, hits = pii.scrub_for_publish(safe)
    assert out == safe
    assert not (pii.BLOCKING & set(hits))


def test_clean_text_unchanged():
    text = "渋谷・代官山エリアで1LDK〜3LDK、50㎡以上、駅徒歩10分以内、家賃50万円以内。"
    out, hits = pii.scrub_for_publish(text)
    assert out == text
    assert hits == []


# --------------------------------------------------------------------------
# Autopost integration
# --------------------------------------------------------------------------

def test_autopost_scrubs_name_before_publishing(tmp_db, monkeypatch):
    """A turn whose display text still carries a name gets scrubbed, not leaked."""
    from fango.consult import autopost
    from fango.consult import session as ss
    from fango.forum_core import get_thread

    sess = ss.create_session(None, ttl_seconds=3600)
    res = autopost.record_turn(
        session=sess,
        user_message="Chris Dai氏向けの東京都内での賃貸物件を探しています。",
        reply="Chris Dai様のご希望に合う物件を提案します。",
        state="ready",
        criteria={"prefecture": "東京都", "layout": "2LDK"},
        results=None,
        keyed_agent_id=None,
        ip="203.0.113.9",
        compliant=True,
        forum_class="chintai",
    )
    assert res["posted"] is True
    data = get_thread(res["forum"], res["thread_id"])
    bodies = " ".join(p.body for p in data["posts"]) + " " + data["thread"].title
    assert "Chris" not in bodies and "Dai" not in bodies
    assert "オーナー" in bodies  # name replaced, condition kept


def test_autopost_holds_when_contact_info_present(tmp_db):
    from fango.consult import autopost
    from fango.consult import session as ss

    sess = ss.create_session(None, ttl_seconds=3600)
    res = autopost.record_turn(
        session=sess,
        user_message="家を探してください。連絡は taro@example.com / 090-1234-5678",
        reply="承知しました。",
        state="asking",
        criteria={},
        results=None,
        keyed_agent_id=None,
        ip="203.0.113.10",
        compliant=True,
        forum_class="chintai",
    )
    assert res["posted"] is False
    assert "個人情報" in res["reason"]


def test_pii_policy_is_in_prompts():
    from fango.consult import prompts
    assert "個人情報" in prompts.SYSTEM_PROMPT
    assert "個人情報" in prompts.MODERATION_PROMPT
    assert prompts.PII_POLICY  # loaded from the md file
