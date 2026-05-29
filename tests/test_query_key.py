"""``?agent_key=...`` query-string fallback for MCP transport paths.

Some hosted agent platforms don't expose MCP-client headers to the
end-owner. For those, embedding the key in the MCP URL is the only
practical workaround. This middleware path must:

* accept ``?agent_key=<key>`` ONLY on /mcp/ and /mcp2/ paths
* never accept it on SSR pages (would leak to browser history / referrer)
* prefer ``X-Agent-Key`` header when both are present
"""
from __future__ import annotations


def _build_mcp_call_request(key_qs: str | None = None, key_header: str | None = None):
    """Return a (path, headers) tuple for a tools/call probe."""
    path = "/mcp2/mcp"
    if key_qs:
        path = f"{path}?agent_key={key_qs}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if key_header:
        headers["X-Agent-Key"] = key_header
    return path, headers


def _do_initialize(client, path, headers):
    """Mini handshake — returns the mcp-session-id."""
    init = client.post(
        path,
        headers=headers,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )
    return init.headers.get("mcp-session-id")


def test_query_key_authenticates_on_mcp_path(client, agent_factory):
    """``?agent_key=...`` on /mcp2/mcp should authenticate just like a header."""
    ag, key = agent_factory("QSAgent")
    path, headers = _build_mcp_call_request(key_qs=key)
    sid = _do_initialize(client, path, headers)
    assert sid, "init handshake should succeed even without a header key"

    # Call fango_whoami to confirm the agent was resolved.
    headers["mcp-session-id"] = sid
    client.post(path, headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    r = client.post(
        path, headers=headers,
        json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "fango_whoami", "arguments": {}},
        },
    )
    # Response is SSE — the event body contains a JSON-RPC envelope.
    assert "QSAgent" in r.text, f"expected whoami to return our agent name, got: {r.text[:300]}"


def test_query_key_ignored_on_ssr_path(client, agent_factory, listing_factory):
    """SSR /listings/... must NOT honour ``?agent_key=...`` — query keys would
    leak into browser history and referrer headers."""
    from fango.rate_limit import reset as reset_rl, PUBLIC_READ_IP
    reset_rl()
    ag, key = agent_factory("QSAgent2")
    listing_factory()
    # One past the public-read cap — would succeed only if the query key
    # leaked into SSR auth. We want the cap to fire instead.
    ua = {"User-Agent": "Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/120.0"}
    for _ in range(PUBLIC_READ_IP.limit):
        r = client.get(f"/listings/1?agent_key={key}", headers=ua)
        assert r.status_code == 200
    r = client.get(f"/listings/1?agent_key={key}", headers=ua)
    assert r.status_code == 429, "SSR must not accept query-string keys; IP cap should fire"


def test_header_wins_over_query_string(client, agent_factory):
    """If both are present, header takes precedence (real key in header,
    bogus key in query string → still authenticated as the header's agent)."""
    ag, key = agent_factory("HeaderAgent")
    path = f"/mcp2/mcp?agent_key=this-is-not-a-real-key"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Agent-Key": key,
    }
    sid = _do_initialize(client, path, headers)
    assert sid
    headers["mcp-session-id"] = sid
    client.post(path, headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    r = client.post(
        path, headers=headers,
        json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "fango_whoami", "arguments": {}},
        },
    )
    assert "HeaderAgent" in r.text


def test_invalid_query_key_is_unauthenticated(client):
    """Bad key in query string → treated as anonymous, not 4xx."""
    path = "/mcp2/mcp?agent_key=definitely-not-real"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    sid = _do_initialize(client, path, headers)
    # init still succeeds — auth is best-effort; tools that require auth
    # will raise AuthError later.
    assert sid
