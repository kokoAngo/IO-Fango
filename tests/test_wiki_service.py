"""wiki: cross-forum lookup + catalog."""
from __future__ import annotations

from fango.baibai import service as fb
from fango.chintai import service as ct
from fango.dojo import service as dj
from fango.chat import service as yo
from fango.wiki import service as wk


def test_lookup_pulls_from_all_forums(agent_factory):
    ag, _ = agent_factory()
    fb.create_thread(title="t", body="銀座エリアの相場", author_id=ag.id)
    ct.create_thread(title="t", body="銀座の賃貸事情", author_id=ag.id)
    yo.post_joke(title="t", body="銀座で奇遇", author_id=ag.id)
    dj.post_thread(title="t", body="銀座スパー", author_id=ag.id)
    result = wk.lookup("銀座")
    forums = {p["forum"] for p in result["posts"]}
    assert forums == {"baibai", "chintai", "chat", "dojo"}


def test_lookup_includes_listings(agent_factory, listing_factory):
    listing_factory(building_name="銀座タワー", address="東京都中央区銀座", station="銀座")
    result = wk.lookup("銀座")
    assert len(result["listings"]) >= 1


def test_lookup_co_occurring_tags(agent_factory):
    ag, _ = agent_factory()
    fb.create_thread(title="t", body="新宿の話", author_id=ag.id, tags=("market", "rent"))
    fb.create_thread(title="t", body="新宿の駅近", author_id=ag.id, tags=("market",))
    result = wk.lookup("新宿")
    tags = {t["tag"]: t["count"] for t in result["tags"]}
    assert tags.get("market") == 2
    assert tags.get("rent") == 1


def test_lookup_empty_keyword(tmp_db):
    result = wk.lookup("")
    assert result["posts"] == []
    assert result["listings"] == []


def test_catalog_counts(agent_factory, listing_factory):
    ag, _ = agent_factory()
    fb.create_thread(title="t", body="b", author_id=ag.id)
    yo.post_joke(title="t", body="b", author_id=ag.id)
    listing_factory()
    result = wk.catalog()
    assert result["thread_counts"]["baibai"] == 1
    assert result["thread_counts"]["chat"] == 1
    assert result["listing_count"] == 1
    assert result["active_agents"] >= 1


def test_catalog_empty(tmp_db):
    result = wk.catalog()
    assert result["thread_counts"]["baibai"] == 0
    assert result["listing_count"] == 0
