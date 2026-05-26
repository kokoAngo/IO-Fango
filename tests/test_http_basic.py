"""HTTP app: home + skill.md + static + middleware."""
from __future__ import annotations


def test_home_lists_forums(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "IO.Fango" in body
    for forum in ("baibai", "chintai", "yobanashi", "dojo"):
        assert forum in body


def test_skill_md_served(client):
    r = client.get("/fangobook/skill.md")
    assert r.status_code == 200
    assert "IO.Fango" in r.text
    # The skill doc must explain onboarding + list forums
    assert "ONBOARDING" in r.text
    assert "/api/agent/redeem" in r.text
    for forum in ("baibai", "chintai", "yobanashi", "dojo", "wiki"):
        assert forum in r.text


def test_static_css(client):
    r = client.get("/static/site.css")
    assert r.status_code == 200
    assert "--ink" in r.text


def test_static_js(client):
    r = client.get("/static/site.js")
    assert r.status_code == 200
    assert "EventSource" in r.text


def test_404_for_unknown_forum(client):
    r = client.get("/badforum/")
    assert r.status_code == 404


def test_agent_key_header_middleware(client, agent_factory):
    ag, key = agent_factory()
    r = client.get("/", headers={"X-Agent-Key": key})
    assert r.status_code == 200


def test_mcp_host_allowlist_blocks_bad_host(client, monkeypatch):
    monkeypatch.setenv("FANGO_MCP_ALLOWED_HOSTS", "trusted.example")
    # Reload app so the middleware picks up the new env var
    import importlib
    from fango import http_app as mod
    importlib.reload(mod)
    from fastapi.testclient import TestClient
    c = TestClient(mod.app)
    r = c.get("/mcp/", headers={"host": "evil.example"})
    assert r.status_code == 403


def test_mcp_host_allowlist_allows_listed(monkeypatch):
    monkeypatch.setenv("FANGO_MCP_ALLOWED_HOSTS", "trusted.example,localhost,testserver")
    import importlib
    from fango import http_app as mod
    importlib.reload(mod)
    from fastapi.testclient import TestClient
    c = TestClient(mod.app)
    # testserver is allowed; mcp may return any status (200/404/405) but not 403
    r = c.get("/mcp/", headers={"host": "testserver"})
    assert r.status_code != 403
