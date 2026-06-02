"""fango_search_listings also broadcasts the query as an anonymous forum post."""
from __future__ import annotations

import pytest

from fango import forum_core
from fango.consult import autopost
from fango.listings import service as ls

from tests.test_consult_engine import FakeEngine, FakeIntent, install_engine  # noqa: F401


def _listing():
    ls.insert_listing({
        "reins_id": "S1", "building_name": "代々木テラス", "prefecture": "東京都",
        "city": "渋谷区", "station": "代々木", "layout": "1LDK", "rent_yen": 150_000,
    })


def test_structured_search_posts_qa_to_chintai(tmp_db):
    _listing()
    from fango.auth import get_or_create_anon_agent_for_ip
    crit = {"prefecture": "東京都", "layout": "1LDK", "rent_max_yen": 200_000}
    r = autopost.record_search(criteria=crit, total=1, items=[{"id": 1}],
                               keyed_agent_id=None, ip="203.0.113.7")
    assert r["posted"] is True and r["forum"] == "chintai"
    posts = forum_core.get_thread(r["forum"], r["thread_id"])["posts"]
    # Renders as a Q&A pair: question post + FANGO answer post.
    assert len(posts) == 2
    q_post, a_post = posts[0], posts[1]
    # Question is authored by the anonymous asker; answer by the FANGO narrator.
    anon = get_or_create_anon_agent_for_ip("203.0.113.7")
    assert q_post.author_id == anon.id
    assert "1LDK" in q_post.body
    # The listing is attached to the answer (FANGO's reply), not the question.
    assert a_post.listing_refs


def test_sale_criteria_routes_to_baibai(tmp_db):
    r = autopost.record_search(criteria={"prefecture": "東京都", "price_max_man": 8000},
                               total=0, items=[], keyed_agent_id=None, ip="1.2.3.4")
    assert r["posted"] is True and r["forum"] == "baibai"


def test_duplicate_search_is_skipped(tmp_db):
    crit = {"prefecture": "東京都", "layout": "1LDK"}
    a = autopost.record_search(criteria=crit, total=0, items=[], keyed_agent_id=None, ip="9.9.9.9")
    b = autopost.record_search(criteria=crit, total=0, items=[], keyed_agent_id=None, ip="9.9.9.9")
    assert a["posted"] is True
    assert b["posted"] is False and b["reason"] == "duplicate search"


def test_internal_and_empty_searches_skipped(tmp_db):
    assert autopost.record_search(criteria={"only_listing_ids": [1]}, total=1,
                                  items=[], keyed_agent_id=None, ip="1.1.1.1")["posted"] is False
    assert autopost.record_search(criteria={}, total=0, items=[],
                                  keyed_agent_id=None, ip="1.1.1.1")["posted"] is False


def test_hourly_cap(tmp_db):
    # Varied structured criteria (no keyword → no LLM call) so only the hourly
    # cap fires, not dedup. Keyless caller → a per-IP anon agent is minted.
    posted = 0
    for i in range(autopost._SEARCH_CAP_PER_HOUR + 3):
        r = autopost.record_search(criteria={"prefecture": "東京都", "walk_minutes_max": i + 1},
                                   total=0, items=[], keyed_agent_id=None, ip="8.8.8.8")
        if r["posted"]:
            posted += 1
    assert posted == autopost._SEARCH_CAP_PER_HOUR


def test_keyword_is_moderated(tmp_db, install_engine):
    from fango.consult.engine import ModerationResult
    install_engine(FakeEngine([FakeIntent(state="asking")],
                              moderation=ModerationResult(compliant=False, forum=None,
                                                          reason="spam")))
    r = autopost.record_search(criteria={"keyword": "buy cheap pills"}, total=0, items=[],
                               keyed_agent_id=None, ip="5.5.5.5")
    assert r["posted"] is False and "spam" in r["reason"]
