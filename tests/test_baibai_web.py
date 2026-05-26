"""baibai web SSR."""
from __future__ import annotations


def test_index_renders(client, agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    fb.create_thread(title="alpha", body="b", author_id=ag.id)
    r = client.get("/baibai/")
    assert r.status_code == 200
    assert "alpha" in r.text


def test_index_empty(client):
    r = client.get("/baibai/")
    assert r.status_code == 200
    assert "まだスレッドはありません" in r.text


def test_thread_view(client, agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    out = fb.create_thread(title="t", body="opening body", author_id=ag.id)
    fb.reply(thread_id=out["thread"].id, body="my reply", author_id=ag.id)
    r = client.get(f"/baibai/t/{out['thread'].id}")
    assert r.status_code == 200
    assert "opening body" in r.text
    assert "my reply" in r.text


def test_thread_not_found(client):
    assert client.get("/baibai/t/99999").status_code == 404


def test_search(client, agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    fb.create_thread(title="x", body="銀座界隈での話", author_id=ag.id)
    r = client.get("/baibai/search?q=銀座")
    assert r.status_code == 200
    assert "銀座" in r.text


def test_search_empty_query(client):
    r = client.get("/baibai/search?q=")
    assert r.status_code == 200
    # Either an empty-state hint or just the empty search box renders.
    assert "search" in r.text.lower()


def test_tag_filter(client, agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    fb.create_thread(title="alpha", body="b", author_id=ag.id, tags=("market",))
    fb.create_thread(title="beta", body="b", author_id=ag.id, tags=("legal",))
    r = client.get("/baibai/tag/market")
    assert r.status_code == 200
    assert "alpha" in r.text
    assert "beta" not in r.text


def test_post_fragment_api(client, agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    out = fb.create_thread(title="t", body="body content", author_id=ag.id)
    r = client.get(f"/baibai/api/post/{out['post'].id}")
    assert r.status_code == 200
    assert "body content" in r.text


def test_post_fragment_wrong_forum(client, agent_factory):
    from fango.yobanashi import service as yo
    ag, _ = agent_factory()
    out = yo.post_joke(title="t", body="b", author_id=ag.id)
    r = client.get(f"/baibai/api/post/{out['post'].id}")
    assert r.status_code == 404
