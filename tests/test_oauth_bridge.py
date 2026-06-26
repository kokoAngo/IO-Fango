"""OAuth 2.1 bridge: discovery → DCR → authorize/consent → token → Bearer.

Verifies the full authorization-code + PKCE dance and that the issued bearer
token resolves, through AgentKeyMiddleware, to the anonymous agent minted at
consent time.
"""
from __future__ import annotations

import base64
import hashlib

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _pkce():
    verifier = base64.urlsafe_b64encode(b"x" * 48).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _register(client) -> str:
    r = client.post("/oauth/register", json={
        "client_name": "Test Connector",
        "redirect_uris": [REDIRECT],
    })
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _full_flow(client):
    """Run register → authorize → consent → token; return (token_json, client_id)."""
    client_id = _register(client)
    verifier, challenge = _pkce()

    r = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
    })
    assert r.status_code == 200
    assert "許可する" in r.text  # consent page rendered

    r = client.post("/oauth/consent", data={
        "decision": "allow", "client_id": client_id, "redirect_uri": REDIRECT,
        "state": "xyz", "scope": "fango", "code_challenge": challenge,
    }, follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(REDIRECT)
    assert "state=xyz" in loc
    from urllib.parse import urlparse, parse_qs
    code = parse_qs(urlparse(loc).query)["code"][0]

    r = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "client_id": client_id, "code_verifier": verifier,
    })
    assert r.status_code == 200, r.text
    return r.json(), client_id


# ---------------------------------------------------------------------------

def test_discovery_endpoints(client):
    r = client.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    assert body["resource"].endswith("/mcp2/mcp")
    assert body["authorization_servers"]

    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    meta = r.json()
    assert meta["authorization_endpoint"].endswith("/oauth/authorize")
    assert meta["token_endpoint"].endswith("/oauth/token")
    assert meta["registration_endpoint"].endswith("/oauth/register")
    assert "S256" in meta["code_challenge_methods_supported"]


def test_register_rejects_non_https_redirect(client):
    r = client.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_full_flow_issues_token(client):
    tok, _ = _full_flow(client)
    assert tok["token_type"] == "Bearer"
    assert tok["access_token"]
    assert tok["refresh_token"]
    assert tok["expires_in"] > 0


def test_token_resolves_to_minted_agent(client):
    tok, _ = _full_flow(client)
    from fango.auth import lookup_by_access_token
    agent = lookup_by_access_token(tok["access_token"])
    assert agent is not None
    assert agent.vendor == "oauth"
    assert agent.name.startswith("anon_oauth_")


def test_bearer_token_authenticates_request(client):
    """The bearer token must flow through AgentKeyMiddleware → current_agent."""
    tok, _ = _full_flow(client)
    # A normal SSR GET carrying the bearer should be accepted (200), exercising
    # the middleware's bearer branch end to end.
    r = client.get("/", headers={"Authorization": f"Bearer {tok['access_token']}"})
    assert r.status_code == 200


def test_authcode_is_single_use(client):
    client_id = _register(client)
    verifier, challenge = _pkce()
    client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    r = client.post("/oauth/consent", data={
        "decision": "allow", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge,
    }, follow_redirects=False)
    from urllib.parse import urlparse, parse_qs
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    exch = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier}
    assert client.post("/oauth/token", data=exch).status_code == 200
    # Second exchange of the same code must fail.
    r2 = client.post("/oauth/token", data=exch)
    assert r2.status_code == 400
    assert r2.json()["error"] == "invalid_grant"


def test_wrong_pkce_verifier_rejected(client):
    client_id = _register(client)
    _verifier, challenge = _pkce()
    client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    r = client.post("/oauth/consent", data={
        "decision": "allow", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge,
    }, follow_redirects=False)
    from urllib.parse import urlparse, parse_qs
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": "wrong-verifier",
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_refresh_token_rotates(client):
    tok, _ = _full_flow(client)
    r = client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
    })
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["access_token"] != tok["access_token"]
    # Old refresh token is now revoked (rotation).
    r2 = client.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
    })
    assert r2.status_code == 400


def test_unregistered_redirect_uri_rejected(client):
    client_id = _register(client)
    _verifier, challenge = _pkce()
    r = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id,
        "redirect_uri": "https://attacker.example/cb",
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"
