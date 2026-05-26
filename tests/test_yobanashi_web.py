"""yobanashi web SSR."""
from __future__ import annotations


def test_index(client, agent_factory):
    from fango.yobanashi import service as yo
    ag, _ = agent_factory()
    yo.post_joke(title="yokai joke", body="b", author_id=ag.id)
    r = client.get("/yobanashi/")
    assert r.status_code == 200
    assert "yokai joke" in r.text


def test_thread_view(client, agent_factory):
    from fango.yobanashi import service as yo
    ag, _ = agent_factory()
    out = yo.post_joke(title="t", body="dadjoke", author_id=ag.id)
    r = client.get(f"/yobanashi/t/{out['thread'].id}")
    assert r.status_code == 200
    assert "dadjoke" in r.text


def test_search(client, agent_factory):
    from fango.yobanashi import service as yo
    ag, _ = agent_factory()
    yo.post_joke(title="t", body="奇遇な事", author_id=ag.id)
    r = client.get("/yobanashi/search?q=奇遇")
    assert r.status_code == 200
    assert "奇遇" in r.text


def test_tag(client, agent_factory):
    from fango.yobanashi import service as yo
    ag, _ = agent_factory()
    yo.post_joke(title="alpha", body="b", author_id=ag.id, tags=("oops",))
    r = client.get("/yobanashi/tag/oops")
    assert r.status_code == 200
    assert "alpha" in r.text


def test_thread_404(client):
    assert client.get("/yobanashi/t/12345").status_code == 404


def test_empty_index(client):
    r = client.get("/yobanashi/")
    assert r.status_code == 200
