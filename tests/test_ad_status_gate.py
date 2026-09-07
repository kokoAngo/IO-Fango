"""Public advertising-compliance gate.

A listing reaches the public surface — search results, the /listings/<id>
page, forum-post attachments — only when BOTH hold:

  * it is cleared for advertising: rental 広告可=可, sale 広告転載可否 ∈
    (広告可, 広告可(但し要連絡)). 不可 / 確認待ち / 物件による / 一部可 / -- / unknown
    are all held back.
  * it is still on the market: sale rows that are 成約 or 申込あり are out,
    rental rows that are 成約済 are out.
  * it is still inside its visibility window: synced (source='pg') rows expire
    RENTAL_VISIBLE_DAYS / SALE_VISIBLE_DAYS after their upstream posting date.
    Locally-created rows have no upstream clock and never expire.
"""
from __future__ import annotations

import pytest

from datetime import datetime, timedelta, timezone

from fango import forum_core
from fango.listings import service as ls


def _ago(days: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _ins(name: str, ad_status: str | None, **extra):
    payload = {
        "reins_id": name, "building_name": name, "prefecture": "東京都",
        "city": "港区", "layout": "1LDK", "rent_yen": 150_000, "ad_status": ad_status,
    }
    payload.update(extra)
    return ls.insert_listing(payload)


def test_is_advertisable_predicate():
    # A locally-created row (source NULL) has no upstream clock: the window
    # does not apply, so these cases isolate the two status axes.
    def adv(tt, ad, ten):
        return ls.is_advertisable(tt, ad, ten, None, None)

    # Rental: only 可 is cleared.
    assert adv(None, "可", None) is True
    assert adv("rent", "可", None) is True
    for bad in ("不可（仲介）", "確認待ち", "物件による", "--", None):
        assert adv(None, bad, None) is False

    # Sale: 広告転載可否 clears it, and 「但し要連絡」 counts (the broker wants a
    # call before the ad runs, not no ad at all).
    assert adv("sale", "広告可", "-") is True
    assert adv("sale", "広告可(但し要連絡)", "公開中") is True
    # 一部可 is media-scoped (チラシ・新聞広告), not a web clearance.
    assert adv("sale", "一部可(インターネット)", "公開中") is False
    assert adv("sale", "不可", "公開中") is False
    assert adv("sale", None, "公開中") is False
    # The rental vocabulary does not clear a sale row and vice versa.
    assert adv("sale", "可", "公開中") is False
    assert adv("rent", "広告可", None) is False

    # On-market axis, sale: only 成約 / 申込あり take a cleared row off.
    assert adv("sale", "広告可", "成約") is False
    assert adv("sale", "広告可", "申込あり") is False
    assert adv("sale", "広告可", None) is True

    # On-market axis, rental: 成約済 takes a cleared row off. This is the axis
    # that used to be sale-only, which left already-let flats advertised.
    assert adv("rent", "可", "成約済") is False
    assert adv(None, "可", "成約済") is False
    assert adv("rent", "可", None) is True


def test_visibility_window_applies_to_synced_rows_only():
    # Synced rental: inside the window shows, past it does not.
    assert ls.is_advertisable("rent", "可", None, _ago(1), "pg") is True
    assert ls.is_advertisable("rent", "可", None,
                              _ago(ls.RENTAL_VISIBLE_DAYS - 0.5), "pg") is True
    assert ls.is_advertisable("rent", "可", None,
                              _ago(ls.RENTAL_VISIBLE_DAYS + 1), "pg") is False

    # Sale gets the longer window, so the same age that expires a rental keeps
    # a sale row visible.
    assert ls.is_advertisable("sale", "広告可", "-",
                              _ago(ls.RENTAL_VISIBLE_DAYS + 1), "pg") is True
    assert ls.is_advertisable("sale", "広告可", "-",
                              _ago(ls.SALE_VISIBLE_DAYS + 1), "pg") is False

    # A synced row with no posting date fails CLOSED — unknown age is exactly
    # what the window exists to catch.
    assert ls.is_advertisable("rent", "可", None, None, "pg") is False

    # A broker's own listing has no upstream clock and must not expire, however
    # old it is: expiring it would delete that broker's shopfront.
    assert ls.is_advertisable("rent", "可", None, None, None) is True
    assert ls.is_advertisable("rent", "可", None, _ago(9999), None) is True


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


def test_detail_page_applies_the_window_to_synced_rows(client, listing_factory):
    """The detail page reads the gate inputs off Listing.extra, so this is also
    the check that `posted_at` / `source` actually travel on the model. When
    they did not, every synced listing 404'd — the whole ingested inventory."""
    fresh = listing_factory(building_name="FreshTower", ad_status="可",
                            posted_at=_ago(1), source="pg")
    expired = listing_factory(building_name="ExpiredTower", ad_status="可",
                              posted_at=_ago(ls.RENTAL_VISIBLE_DAYS + 2), source="pg")
    undated = listing_factory(building_name="UndatedTower", ad_status="可",
                              posted_at=None, source="pg")
    assert client.get(f"/listings/{fresh.id}").status_code == 200
    assert client.get(f"/listings/{expired.id}").status_code == 404
    assert client.get(f"/listings/{undated.id}").status_code == 404


def test_feed_gate_sql_mirrors_the_predicate(tmp_db):
    """http_app._visible_listing_sql() is a second expression of the same gate.

    It is interpolated (not bound) into larger queries, so it is generated
    from the service constants — this test is the check that the generated
    SQL and is_advertisable() actually agree, row for row. The cases cover all
    three axes, including rows on both sides of the visibility window.
    """
    from fango.db import connect
    from fango.http_app import _visible_listing_sql

    fresh, stale = _ago(1), _ago(ls.RENTAL_VISIBLE_DAYS + 2)
    old_sale = _ago(ls.SALE_VISIBLE_DAYS + 2)
    cases = [
        # (transaction_type, ad_status, tenancy_status, posted_at, source)
        ("sale", "広告可", "-", fresh, "pg"),
        ("sale", "広告可(但し要連絡)", "公開中", fresh, "pg"),
        ("sale", "広告可", "成約", fresh, "pg"),
        ("sale", "広告可", "申込あり", fresh, "pg"),
        ("sale", "不可", "公開中", fresh, "pg"),
        ("sale", "一部可(インターネット)", "公開中", fresh, "pg"),
        ("sale", None, "公開中", fresh, "pg"),
        ("sale", "可", "公開中", fresh, "pg"),
        ("rent", "可", None, fresh, "pg"),
        ("rent", "確認待ち", None, fresh, "pg"),
        ("rent", "広告可", None, fresh, "pg"),
        (None, "可", None, fresh, "pg"),
        # the window
        ("rent", "可", None, stale, "pg"),          # expired rental
        ("sale", "広告可", "-", stale, "pg"),        # still inside the sale window
        ("sale", "広告可", "-", old_sale, "pg"),     # past the sale window
        ("rent", "可", None, None, "pg"),           # synced, unknown age → closed
        ("rent", "可", None, None, None),           # local row → never expires
        ("rent", "可", None, stale, None),          # local row, old → still shows
        # rental on-market axis
        ("rent", "可", "成約済", fresh, "pg"),
        (None, "可", "成約済", fresh, None),
    ]
    ids = {}
    for i, (tt, ad, ten, posted, src) in enumerate(cases):
        row = _ins(f"case{i}", ad, transaction_type=tt, tenancy_status=ten,
                   posted_at=posted, source=src)
        ids[row.id] = (tt, ad, ten, posted, src)

    conn = connect()
    try:
        visible = {
            r[0] for r in conn.execute(
                f"SELECT l.id FROM listings l WHERE {_visible_listing_sql()}"
            ).fetchall()
        }
    finally:
        conn.close()

    for lid, case in ids.items():
        assert (lid in visible) is ls.is_advertisable(*case), case


def test_search_sql_mirrors_the_predicate_on_the_window(tmp_db):
    """The search WHERE clause is the third expression of the gate."""
    fresh = _ins("FRESH", "可", posted_at=_ago(1), source="pg")
    stale = _ins("STALE", "可", posted_at=_ago(ls.RENTAL_VISIBLE_DAYS + 2), source="pg")
    undated = _ins("UNDATED", "可", posted_at=None, source="pg")
    local = _ins("LOCAL", "可", posted_at=None, source=None)

    names = {l.building_name for l in ls.search_listings(
        criteria={"prefecture": "東京都"}, limit=50)}
    assert names == {"FRESH", "LOCAL"}
    assert ls.is_listing_advertisable(fresh.id) is True
    assert ls.is_listing_advertisable(stale.id) is False
    assert ls.is_listing_advertisable(undated.id) is False
    assert ls.is_listing_advertisable(local.id) is True

    # Internal callers can still reach the full pool.
    everything = {l.building_name for l in ls.search_listings(
        criteria={"prefecture": "東京都", "include_stale": True,
                  "include_non_advertisable": True}, limit=50)}
    assert everything == {"FRESH", "STALE", "UNDATED", "LOCAL"}


def test_contracted_rental_is_not_advertised(tmp_db):
    """The regression that motivated the rental on-market axis: reconcile marks
    a let flat 成約済 on a row we already hold, and it stayed searchable."""
    _ins("LET", "可", transaction_type="rent", tenancy_status="成約済",
         posted_at=_ago(1), source="pg")
    _ins("FREE", "可", transaction_type="rent", tenancy_status=None,
         posted_at=_ago(1), source="pg")
    names = {l.building_name for l in ls.search_listings(
        criteria={"prefecture": "東京都"}, limit=50)}
    assert names == {"FREE"}
