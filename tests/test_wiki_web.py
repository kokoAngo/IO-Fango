"""wiki web SSR."""
from __future__ import annotations


def test_wiki_index_empty(client):
    r = client.get("/wiki/")
    assert r.status_code == 200
    assert "横断検索" in r.text


def test_wiki_lookup_query(client, agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    fb.create_thread(title="t", body="湘南エリアの相場", author_id=ag.id)
    r = client.get("/wiki/?q=湘南")
    assert r.status_code == 200
    assert "湘南" in r.text


def test_wiki_no_thread_view(client):
    # /wiki/t/... is not a route (wiki has no threads of its own)
    r = client.get("/wiki/t/1")
    assert r.status_code == 404


def test_wiki_aggregates_multi_forum(client, agent_factory):
    from fango.baibai import service as fb
    from fango.chat import service as yo
    ag, _ = agent_factory()
    fb.create_thread(title="t", body="自由が丘の話", author_id=ag.id)
    yo.post_joke(title="t", body="自由が丘での雑談", author_id=ag.id)
    r = client.get("/wiki/?q=自由が丘")
    assert r.status_code == 200
    assert "自由が丘" in r.text


def test_wiki_section_listings(client, listing_factory):
    listing_factory(building_name="代々木プラザ", address="渋谷区代々木1", station="代々木")
    r = client.get("/wiki/?q=代々木")
    assert r.status_code == 200
    assert "代々木プラザ" in r.text


def test_wiki_catalog_link_in_home(client):
    r = client.get("/")
    assert "wiki" in r.text
