"""Public advertising-compliance gate.

A listing reaches the public surface — search results, the /listings/<id>
page, forum-post attachments — only when BOTH hold:

  * it is cleared for advertising: rental 広告可=可, sale 広告転載可否 ∈
    (広告可, 広告可(但し要連絡)). 不可 / 確認待ち / 物件による / 一部可 / -- / unknown
    are all held back.
  * it is still on the market: sale rows that are 成約 or 申込あり are out.
"""
from __future__ import annotations

import pytest

from fango import forum_core
from fango.listings import service as ls


def _ins(name: str, ad_status: str | None, **extra):
    payload = {
        "reins_id": name, "building_name": name, "prefecture": "東京都",
        "city": "港区", "layout": "1LDK", "rent_yen": 150_000, "ad_status": ad_status,
    }
    payload.update(extra)
    return ls.insert_listing(payload)


def test_is_advertisable_predicate():
    # Rental: only 可 is cleared. tenancy_status is a sale-only axis.
    assert ls.is_advertisable(None, "可", None) is True
    assert ls.is_advertisable("rent", "可", None) is True
    for bad in ("不可（仲介）", "確認待ち", "物件による", "--", None):
        assert ls.is_advertisable(None, bad, None) is False

    # Sale: 広告転載可否 clears it, and 「但し要連絡」 counts (the broker wants a
    # call before the ad runs, not no ad at all).
    assert ls.is_advertisable("sale", "広告可", "-") is True
    assert ls.is_advertisable("sale", "広告可(但し要連絡)", "公開中") is True
    # 一部可 is media-scoped (チラシ・新聞広告), not a web clearance.
    assert ls.is_advertisable("sale", "一部可(インターネット)", "公開中") is False
    assert ls.is_advertisable("sale", "不可", "公開中") is False
    assert ls.is_advertisable("sale", None, "公開中") is False
    # The rental vocabulary does not clear a sale row and vice versa.
    assert ls.is_advertisable("sale", "可", "公開中") is False
    assert ls.is_advertisable("rent", "広告可", None) is False

    # On-market axis: only 成約 / 申込あり take a cleared sale row off.
    assert ls.is_advertisable("sale", "広告可", "成約") is False
    assert ls.is_advertisable("sale", "広告可", "申込あり") is False
    assert ls.is_advertisable("sale", "広告可", None) is True


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


def test_feed_gate_sql_mirrors_the_predicate(tmp_db):
    """http_app._VISIBLE_LISTING_SQL is a second expression of the same gate.

    It is interpolated (not bound) into larger queries, so it is generated
    from the service constants — this test is the check that the generated
    SQL and is_advertisable() actually agree, row for row.
    """
    from fango.db import connect
    from fango.http_app import _VISIBLE_LISTING_SQL

    cases = [
        ("sale", "広告可", "-"),
        ("sale", "広告可(但し要連絡)", "公開中"),
        ("sale", "広告可", "成約"),
        ("sale", "広告可", "申込あり"),
        ("sale", "不可", "公開中"),
        ("sale", "一部可(インターネット)", "公開中"),
        ("sale", None, "公開中"),
        ("sale", "可", "公開中"),
        ("rent", "可", None),
        ("rent", "確認待ち", None),
        ("rent", "広告可", None),
        (None, "可", None),
    ]
    ids = {}
    for i, (tt, ad, ten) in enumerate(cases):
        row = _ins(f"case{i}", ad, transaction_type=tt, tenancy_status=ten)
        ids[row.id] = (tt, ad, ten)

    conn = connect()
    try:
        visible = {
            r[0] for r in conn.execute(
                f"SELECT l.id FROM listings l WHERE {_VISIBLE_LISTING_SQL}"
            ).fetchall()
        }
    finally:
        conn.close()

    for lid, (tt, ad, ten) in ids.items():
        assert (lid in visible) is ls.is_advertisable(tt, ad, ten), (tt, ad, ten)
