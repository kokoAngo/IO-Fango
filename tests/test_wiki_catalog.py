"""Wiki catalog: cross-forum counters used by the homepage + dashboards."""
from __future__ import annotations


def test_catalog_initial_zero(tmp_db):
    from fango.wiki.service import catalog
    c = catalog()
    assert c == {
        "thread_counts": {"baibai": 0, "chintai": 0, "chat": 0, "dojo": 0},
        "listing_count": 0,
        "active_agents": 0,
    }


def test_catalog_after_activity(agent_factory, listing_factory):
    from fango.baibai import service as fb
    from fango.chintai import service as ct
    from fango.chat import service as yo
    from fango.dojo import service as dj
    from fango.wiki.service import catalog
    ag, _ = agent_factory()
    fb.create_thread(title="t", body="b", author_id=ag.id)
    ct.create_thread(title="t", body="b", author_id=ag.id)
    yo.post_joke(title="t", body="b", author_id=ag.id)
    yo.post_joke(title="t", body="b", author_id=ag.id)
    dj.post_thread(title="t", body="b", author_id=ag.id)
    listing_factory()
    listing_factory()
    c = catalog()
    assert c["thread_counts"]["baibai"] == 1
    assert c["thread_counts"]["chintai"] == 1
    assert c["thread_counts"]["chat"] == 2
    assert c["thread_counts"]["dojo"] == 1
    assert c["listing_count"] == 2
    assert c["active_agents"] == 1


def test_catalog_excludes_inactive_agents(agent_factory):
    from fango.auth import revoke_agent
    from fango.wiki.service import catalog
    a1, _ = agent_factory("a1")
    a2, _ = agent_factory("a2")
    revoke_agent(a2.id)
    c = catalog()
    assert c["active_agents"] == 1


def test_catalog_endpoint_exposed_via_wiki_lookup(agent_factory, listing_factory):
    from fango.wiki.service import catalog, lookup
    listing_factory(building_name="A")
    out = lookup("A")
    assert isinstance(out, dict)
    c = catalog()
    assert c["listing_count"] >= 1


def test_home_page_uses_catalog(client, listing_factory):
    listing_factory()
    r = client.get("/")
    assert r.status_code == 200
    assert "登録物件数" in r.text
