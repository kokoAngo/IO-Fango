"""Public advertising-compliance gate.

Only 広告可=可 (rental) / 取引状況=公開中 (sale) listings may appear on the public
surface — search results, the /listings/<id> page, and forum-post attachments.
Everything else (不可 / 確認待ち / 物件による / -- / unknown) is held back.
"""
from __future__ import annotations

import pytest

from fango import forum_core
from fango.listings import service as ls


def _ins(name: str, ad_status: str, **extra):
    payload = {
        "reins_id": name, "building_name": name, "prefecture": "東京都",
        "city": "港区", "layout": "1LDK", "rent_yen": 150_000, "ad_status": ad_status,
    }
    payload.update(extra)
    return ls.insert_listing(payload)


def test_is_advertisable_predicate():
    # Rental: only 可 is cleared.
    assert ls.is_advertisable(None, "可") is True
    assert ls.is_advertisable("rent", "可") is True
    for bad in ("不可（仲介）", "確認待ち", "物件による", "--", None):
        assert ls.is_advertisable(None, bad) is False
    # Sale: only 公開中 is cleared (and 可 does NOT clear a sale row).
    assert ls.is_advertisable("sale", "公開中") is True
    assert ls.is_advertisable("sale", "申込あり") is False
    assert ls.is_advertisable("sale", "可") is False


def test_search_and_count_hide_non_advertisable(tmp_db):
    _ins("OK", "可")
    _ins("NG", "不可（仲介）")
    _ins("PEND", "確認待ち")
    crit = {"prefecture": "東京都"}
    names = {l.building_name for l in ls.search_listings(criteria=crit, limit=50)}
    assert names == {"OK"}
    assert ls.count_listings(criteria=crit) == 1
    # Internal callers can opt out to see the full pool.
    full = {l.building_name for l in ls.search_listings(
        criteria={**crit, "include_non_advertisable": True}, limit=50)}
    assert full == {"OK", "NG", "PEND"}


def test_relaxed_search_inherits_the_gate(tmp_db):
    # A non-可 listing must not surface even via the near-match fallback.
    _ins("NG", "不可（仲介）", station="六本木")
    rows, _note = ls.relaxed_search({"prefecture": "東京都", "station": "存在しない駅",
                                     "rent_max_yen": 100_000}, limit=10)
    assert all(r.building_name != "NG" for r in rows)


def test_attach_listing_rejects_non_advertisable(tmp_db, agent_factory):
    ag, _ = agent_factory()
    ok = _ins("OK", "可")
    ng = _ins("NG", "不可（仲介）")
    _thread, post = forum_core.create_thread("baibai", "t", "b", ag.id)
    forum_core.attach_listing(post.id, ok.id)  # advertisable → fine
    with pytest.raises(forum_core.ForumError):
        forum_core.attach_listing(post.id, ng.id)


def test_detail_page_404s_non_advertisable(client, listing_factory):
    ok = listing_factory(building_name="OKTower", ad_status="可")
    ng = listing_factory(building_name="NGTower", ad_status="不可（仲介）")
    assert client.get(f"/listings/{ok.id}").status_code == 200
    assert client.get(f"/listings/{ng.id}").status_code == 404
