"""Saved-search storage + match engine."""
from __future__ import annotations

from fango.listings import service as ls
from fango.listings.saved_search import (
    create_saved_search,
    delete_saved_search,
    fetch_new_matches,
    list_saved_searches,
    match_new_listings,
)
from fango.db import connect


def _make_listing(**kwargs):
    defaults = {
        "reins_id": kwargs.pop("reins_id", None),
        "building_name": "Building",
        "address": "東京都渋谷区",
        "prefecture": "東京都",
        "city": "渋谷区",
        "layout": "2LDK",
        "area_sqm": 50.0,
        "rent_yen": 200000,
        "price_man": 20,
        "walk_minutes": 7,
    }
    defaults.update(kwargs)
    return ls.insert_listing(defaults)


def test_save_and_list(tmp_db, agent_factory):
    agent, _ = agent_factory("Alice")
    sid = create_saved_search(agent.id, "Setagaya 2LDK", {"prefecture": "東京都", "layout": "2LDK"})
    assert isinstance(sid, int) and sid > 0
    listed = list_saved_searches(agent.id)
    assert len(listed) == 1
    assert listed[0]["id"] == sid
    assert listed[0]["criteria"]["layout"] == "2LDK"
    assert listed[0]["active"] is True


def test_delete_only_own(tmp_db, agent_factory):
    a1, _ = agent_factory("Alice")
    a2, _ = agent_factory("Bob")
    sid = create_saved_search(a1.id, "A search", {"prefecture": "東京都"})
    assert delete_saved_search(a2.id, sid) is False  # not owner
    assert delete_saved_search(a1.id, sid) is True


def test_match_pipeline_end_to_end(tmp_db, agent_factory):
    """Save a search → insert a matching listing → match → fetch → ack."""
    agent, _ = agent_factory("Alice")
    sid = create_saved_search(
        agent.id, "Cheap rentals in 渋谷",
        {"prefecture": "東京都", "city": "渋谷", "rent_max_yen": 150_000},
    )
    # Two listings: one matches, one doesn't (city mismatch).
    matching = _make_listing(reins_id="M1", city="渋谷区", rent_yen=120_000, price_man=12)
    other = _make_listing(reins_id="X1", city="港区", rent_yen=120_000, price_man=12)

    inserted = match_new_listings([matching.id, other.id])
    assert inserted == 1

    notified = fetch_new_matches(agent.id)
    assert len(notified) == 1
    assert notified[0]["saved_search_id"] == sid
    assert notified[0]["listing"]["id"] == matching.id

    # Mark-on-read: next fetch is empty.
    again = fetch_new_matches(agent.id)
    assert again == []


def test_match_dedup_when_called_twice(tmp_db, agent_factory):
    agent, _ = agent_factory("Alice")
    sid = create_saved_search(agent.id, "S", {"prefecture": "東京都"})
    listing = _make_listing(reins_id="D1")
    assert match_new_listings([listing.id]) == 1
    # Re-running with the same listing must not double-insert (UNIQUE constraint).
    assert match_new_listings([listing.id]) == 0


def test_inactive_search_not_matched(tmp_db, agent_factory):
    agent, _ = agent_factory("Alice")
    sid = create_saved_search(agent.id, "S", {"prefecture": "東京都"})
    conn = connect(tmp_db)
    try:
        conn.execute("UPDATE saved_searches SET active = 0 WHERE id = ?", (sid,))
    finally:
        conn.close()
    listing = _make_listing(reins_id="I1")
    assert match_new_listings([listing.id]) == 0
