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


def test_listing_preview_omits_cross_references(client, agent_factory, listing_factory):
    """SSR preview must not leak post bodies that reference the listing.

    Cross-references are deep data — they come back through ``fango_get_listing``
    over MCP, not the public HTML.
    """
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    listing = listing_factory(building_name="ReffedTower")
    fb.create_thread(title="check this", body="thoughts",
                     author_id=ag.id, listing_id=listing.id)
    r = client.get(f"/listings/{listing.id}")
    assert r.status_code == 200
    assert "check this" not in r.text
    assert "引用" not in r.text


def test_listing_preview_omits_price_history_chart(client, tmp_db):
    """Price history is anti-scrape sensitive — no chart and no data points."""
    from fango.listings import service as ls
    a = ls.upsert_listing({"reins_id": "SP1", "building_name": "PH", "price_man": 5000, "address": "x"})
    ls.upsert_listing({"reins_id": "SP1", "building_name": "PH", "price_man": 4500, "address": "x"})
    ls.upsert_listing({"reins_id": "SP1", "building_name": "PH", "price_man": 4200, "address": "x"})
    r = client.get(f"/listings/{a.id}")
    assert r.status_code == 200
    assert "price-history-card" not in r.text
    assert "price-history-svg" not in r.text
    assert "%+.1f" not in r.text   # the sparkline-delta formatter token
    # The MCP-pointer callout IS present, naming the field as something
    # only MCP exposes — that's fine.
    assert "fango_get_listing" in r.text


def test_listing_preview_omits_reins_id(client, listing_factory):
    """REINS id is the actual source-system identifier — never expose it."""
    l = listing_factory(
        building_name="StatsBldg", price_man=10000, reins_id="REINS-SENSITIVE-001",
    )
    r = client.get(f"/listings/{l.id}")
    assert r.status_code == 200
    assert "REINS-SENSITIVE-001" not in r.text
    # And no building-stats card (avg_price_man / listing_count siblings).
    assert "building-stats" not in r.text
