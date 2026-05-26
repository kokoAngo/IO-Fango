"""Listing detail page + cross-references + sparkline."""
from __future__ import annotations


def test_listing_detail_renders(client, listing_factory):
    l = listing_factory(building_name="Tower X", price_man=5000, layout="1LDK")
    r = client.get(f"/listings/{l.id}")
    assert r.status_code == 200
    assert "Tower X" in r.text
    assert "5,000万円" in r.text


def test_listing_404(client):
    assert client.get("/listings/99999").status_code == 404


def test_listing_cross_references(client, agent_factory, listing_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    listing = listing_factory(building_name="ReffedTower")
    fb.create_thread(title="check this", body="thoughts",
                     author_id=ag.id, listing_id=listing.id)
    r = client.get(f"/listings/{listing.id}")
    assert r.status_code == 200
    assert "check this" in r.text or "引用" in r.text


def test_listing_sparkline_when_price_history(client, listing_factory, tmp_db):
    from fango.listings import service as ls
    a = ls.upsert_listing({"reins_id": "SP1", "building_name": "PH", "price_man": 5000, "address": "x"})
    ls.upsert_listing({"reins_id": "SP1", "building_name": "PH", "price_man": 4500, "address": "x"})
    ls.upsert_listing({"reins_id": "SP1", "building_name": "PH", "price_man": 4200, "address": "x"})
    r = client.get(f"/listings/{a.id}")
    assert r.status_code == 200
    assert "価格履歴" in r.text


def test_listing_building_stats(client, listing_factory):
    listing_factory(building_name="StatsBldg", price_man=8000)
    l = listing_factory(building_name="StatsBldg", price_man=10000)
    r = client.get(f"/listings/{l.id}")
    assert r.status_code == 200
    assert "建物統計" in r.text
