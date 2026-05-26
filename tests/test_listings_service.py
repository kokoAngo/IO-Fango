"""Listings: CRUD, FTS, history, building stats, search filters."""
from __future__ import annotations

import pytest

from fango.db import connect
from fango.listings import service as ls


def test_insert_listing_basic(tmp_db):
    conn = connect(tmp_db)
    try:
        result = ls.insert_listing(
            {
                "building_name": "Tower A",
                "address": "東京都港区赤坂1-1",
                "prefecture": "東京都",
                "station": "赤坂",
                "price_man": 5000,
                "area_sqm": 50.0,
                "layout": "1LDK",
            },
            conn=conn,
        )
    finally:
        conn.close()
    assert result.id > 0
    assert result.building_name == "Tower A"
    assert result.price_man == 5000


def test_insert_records_price_history(tmp_db):
    conn = connect(tmp_db)
    try:
        l = ls.insert_listing({"building_name": "B", "price_man": 7000, "address": "x"}, conn=conn)
        with_hist = ls.get_with_history(l.id, conn=conn)
    finally:
        conn.close()
    assert with_hist is not None
    assert len(with_hist["price_history"]) == 1
    assert with_hist["price_history"][0]["price_man"] == 7000


def test_upsert_inserts_when_no_match(tmp_db):
    l = ls.upsert_listing({"reins_id": "X1", "building_name": "U1", "address": "a", "price_man": 100})
    assert l.id > 0
    assert l.reins_id == "X1"


def test_upsert_updates_existing_and_logs_price_change(tmp_db):
    a = ls.upsert_listing({"reins_id": "X2", "building_name": "U2", "price_man": 100, "address": "a"})
    b = ls.upsert_listing({"reins_id": "X2", "building_name": "U2", "price_man": 120, "address": "a"})
    assert a.id == b.id
    assert b.price_man == 120
    h = ls.get_with_history(a.id)
    assert h is not None
    assert [p["price_man"] for p in h["price_history"]] == [100, 120]


def test_upsert_skips_history_when_price_unchanged(tmp_db):
    a = ls.upsert_listing({"reins_id": "X3", "building_name": "U3", "price_man": 100, "address": "a"})
    ls.upsert_listing({"reins_id": "X3", "building_name": "U3 renamed", "price_man": 100, "address": "a"})
    h = ls.get_with_history(a.id)
    assert len(h["price_history"]) == 1


def test_get_unknown_listing_returns_none(tmp_db):
    assert ls.get_listing(9999) is None
    assert ls.get_with_history(9999) is None


def test_search_by_prefecture(listing_factory):
    listing_factory(prefecture="東京都", building_name="A")
    listing_factory(prefecture="神奈川県", building_name="B")
    results = ls.search_listings(prefecture="東京都")
    assert len(results) == 1
    assert results[0].building_name == "A"


def test_search_by_price_range(listing_factory):
    listing_factory(price_man=3000)
    listing_factory(price_man=8000)
    listing_factory(price_man=15000)
    results = ls.search_listings(min_price=5000, max_price=10000)
    assert len(results) == 1
    assert results[0].price_man == 8000


def test_search_by_layout(listing_factory):
    listing_factory(layout="1LDK")
    listing_factory(layout="2LDK")
    listing_factory(layout="2LDK")
    results = ls.search_listings(layout="2LDK")
    assert len(results) == 2


def test_search_fts_keyword_building(listing_factory):
    listing_factory(building_name="六本木ヒルズレジデンス", address="赤坂9-7-1", station="赤坂")
    listing_factory(building_name="渋谷ガーデンタワー", address="渋谷2-2", station="渋谷")
    results = ls.search_listings(keyword="六本木")
    assert len(results) == 1
    assert "六本木" in results[0].building_name


def test_search_fts_keyword_station(listing_factory):
    listing_factory(building_name="A棟", address="世田谷1", station="自由が丘")
    listing_factory(building_name="B棟", address="世田谷2", station="二子玉川")
    results = ls.search_listings(keyword="自由が丘")
    assert len(results) == 1


def test_search_combined_filters(listing_factory):
    listing_factory(prefecture="東京都", layout="1LDK", price_man=4000)
    listing_factory(prefecture="東京都", layout="2LDK", price_man=6000)
    listing_factory(prefecture="神奈川県", layout="1LDK", price_man=4000)
    results = ls.search_listings(prefecture="東京都", layout="1LDK", max_price=5000)
    assert len(results) == 1


def test_search_pagination(listing_factory):
    for i in range(5):
        listing_factory(building_name=f"P{i}")
    a = ls.search_listings(limit=2, offset=0)
    b = ls.search_listings(limit=2, offset=2)
    assert len(a) == 2 and len(b) == 2
    assert {x.id for x in a}.isdisjoint({x.id for x in b})


def test_building_stats_updates(listing_factory):
    listing_factory(building_name="Shared", price_man=1000)
    listing_factory(building_name="Shared", price_man=3000)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM building_stats WHERE building_name = ?", ("Shared",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["listing_count"] == 2
    assert row["avg_price_man"] == 2000


def test_add_note(listing_factory, agent_factory):
    listing = listing_factory()
    ag, _ = agent_factory()
    nid = ls.add_note(listing["id"] if isinstance(listing, dict) else listing.id, ag.id, "good light")
    assert nid > 0


def test_bulk_import(tmp_db):
    n = ls.bulk_import([
        {"reins_id": "B1", "building_name": "X", "price_man": 100, "address": "a"},
        {"reins_id": "B2", "building_name": "Y", "price_man": 200, "address": "b"},
    ])
    assert n == 2
    assert len(ls.search_listings()) == 2


def test_notion_adapter_unconfigured_yields_nothing(monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_LISTINGS_DATABASE_ID", raising=False)
    from fango.listings.ingestion.notion import NotionListingAdapter
    a = NotionListingAdapter()
    assert not a.is_configured()
    assert list(a.iter_listings()) == []
