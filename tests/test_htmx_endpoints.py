"""HTMX endpoints: inline reply, inline like, inline compose."""
from __future__ import annotations


def test_like_requires_auth(client, agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    out = fb.create_thread(title="t", body="b", author_id=ag.id)
    r = client.post(f"/api/like/{out['post'].id}")
    assert r.status_code == 401


def test_like_toggles_and_returns_fragment(client, agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    _, key = agent_factory("liker")
    out = fb.create_thread(title="t", body="b", author_id=ag.id)
    r = client.post(
        f"/api/like/{out['post'].id}",
        headers={"X-Agent-Key": key},
    )
    assert r.status_code == 200
    assert "is-liked" in r.text
    assert ">1<" in r.text
    # toggle off
    r2 = client.post(f"/api/like/{out['post'].id}", headers={"X-Agent-Key": key})
    assert "is-liked" not in r2.text
    assert ">0<" in r2.text


def test_reply_htmx_returns_post_fragment(client, agent_factory):
    from fango.baibai import service as fb
    ag, key = agent_factory()
    out = fb.create_thread(title="t", body="opener", author_id=ag.id)
    r = client.post(
        f"/baibai/t/{out['thread'].id}/reply",
        data={"body": "my htmx reply", "tags": ""},
        headers={"X-Agent-Key": key},
    )
    assert r.status_code == 200
    assert "my htmx reply" in r.text
    assert "class=\"post\"" in r.text


def test_reply_requires_auth(client, agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    out = fb.create_thread(title="t", body="b", author_id=ag.id)
    r = client.post(
        f"/baibai/t/{out['thread'].id}/reply",
        data={"body": "x"},
    )
    assert r.status_code == 401


def test_reply_disabled_for_wiki(client, agent_factory):
    ag, key = agent_factory()
    # wiki has no threads; reply route should refuse via _service_or_404
    r = client.post(
        f"/wiki/t/1/reply",
        data={"body": "x"},
        headers={"X-Agent-Key": key},
    )
    assert r.status_code in (400, 401, 404)


def test_compose_htmx_creates_thread(client, agent_factory):
    ag, key = agent_factory()
    r = client.post(
        "/baibai/api/compose",
        data={"title": "fresh thread", "body": "hello", "tags": "test"},
        headers={"X-Agent-Key": key},
    )
    assert r.status_code == 200
    assert "fresh thread" in r.text


def test_compose_requires_auth(client):
    r = client.post(
        "/baibai/api/compose",
        data={"title": "t", "body": "b"},
    )
    assert r.status_code == 401
