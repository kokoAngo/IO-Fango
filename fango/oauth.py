"""OAuth 2.1 authorization server — bridges directory connectors to anonymous agents.

Official directories (Claude Connectors, ChatGPT Apps) require a standard OAuth
authorization-code flow rather than a pasted API key. Fango has no human
accounts, so "logging in" here means **minting a fresh anonymous agent** and
binding the issued token to it — the client then has a stable pseudonym and the
full keyed feature set (saved searches, rename, new-match) without Fango running
a username/password system.

Tokens are opaque (not JWT): AS and RS are the same process, so a token is just
a random string resolved by a DB lookup — exactly like an agent key — which
keeps instant revocation. See auth.lookup_by_access_token + docs/oauth-bridge-design.md.

OAuth is opt-in. Keyless consult/browse/post on /mcp are unaffected; we never
force a 401 challenge in Phase 1.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .auth import create_agent, hash_key
from .config import load_settings
from .db import connect, transaction
from .rate_limit import RateLimitError, check_and_record, Quota

# Phase-1 tunables.
ACCESS_TTL_SECONDS = 3600           # 1 hour
AUTH_CODE_TTL_SECONDS = 60          # single-use, short-lived
DEFAULT_SCOPE = "fango"

# Rate limits for the AS surface (defensive — these are unauthenticated).
REGISTER_IP = Quota(20, 3600)
AUTHORIZE_IP = Quota(60, 3600)

oauth_router = APIRouter()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """Millisecond ISO-8601 UTC, matching the schema's strftime default so
    lexicographic comparison against stored timestamps is correct."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now_iso() -> str:
    return _iso(_now())


def _h(value: str) -> str:
    """SHA-256 hex — reuse the same hashing the agent-key store uses."""
    return hash_key(value)


def _token() -> str:
    return secrets.token_urlsafe(32)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def issuer(request: Request) -> str:
    """Canonical base URL. Prefer the configured public URL (correct behind a
    proxy/tunnel); fall back to the request's own base."""
    pub = load_settings().public_base_url
    if pub:
        return pub.rstrip("/")
    return str(request.base_url).rstrip("/")


def _verify_pkce(verifier: str, challenge: str) -> bool:
    """RFC 7636 S256: base64url(sha256(verifier)) == challenge."""
    if not verifier or not challenge:
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return hmac.compare_digest(expected, challenge)


def _json_error(error: str, desc: str, status: int = 400) -> JSONResponse:
    """OAuth-style error body (RFC 6749 §5.2)."""
    return JSONResponse({"error": error, "error_description": desc}, status_code=status)


# ---------------------------------------------------------------------------
# discovery (.well-known)
# ---------------------------------------------------------------------------

@oauth_router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata(request: Request):
    base = issuer(request)
    return JSONResponse({
        "resource": f"{base}/mcp2/mcp",
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
    })


# Some clients append the resource path to the well-known lookup
# (RFC 9728 §3.1). Accept that shape too.
@oauth_router.get("/.well-known/oauth-protected-resource/mcp2/mcp")
async def protected_resource_metadata_suffixed(request: Request):
    return await protected_resource_metadata(request)


@oauth_router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request):
    base = issuer(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "scopes_supported": [DEFAULT_SCOPE],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
    })


# ---------------------------------------------------------------------------
# dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------

def _valid_redirect(uri: str) -> bool:
    if not isinstance(uri, str) or not uri:
        return False
    # https everywhere; allow http only for localhost loopback (dev/native).
    if uri.startswith("https://"):
        return True
    if uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1"):
        return True
    return False


@oauth_router.post("/oauth/register")
async def register_client(request: Request):
    try:
        check_and_record("oauth_register_ip", _client_ip(request), REGISTER_IP)
    except RateLimitError as exc:
        return _json_error("temporarily_unavailable", str(exc), status=429)

    try:
        body = await request.json()
    except Exception:
        return _json_error("invalid_client_metadata", "body must be JSON")
    if not isinstance(body, dict):
        return _json_error("invalid_client_metadata", "body must be a JSON object")

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return _json_error("invalid_redirect_uri", "redirect_uris (non-empty array) required")
    if not all(_valid_redirect(u) for u in redirect_uris):
        return _json_error("invalid_redirect_uri", "redirect_uris must be https (or localhost http)")

    client_id = "fango_" + secrets.token_urlsafe(16)
    client_name = str(body.get("client_name") or "")[:120]
    with transaction(connect()) as conn:
        conn.execute(
            "INSERT INTO oauth_clients(client_id, client_name, redirect_uris) VALUES (?, ?, ?)",
            (client_id, client_name, json.dumps(redirect_uris)),
        )

    # Echo back a public-client registration (RFC 7591 §3.2.1).
    return JSONResponse({
        "client_id": client_id,
        "client_id_issued_at": int(_now().timestamp()),
        "redirect_uris": redirect_uris,
        "client_name": client_name,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }, status_code=201)


def _load_client(conn: sqlite3.Connection, client_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# authorize + consent
# ---------------------------------------------------------------------------

_CONSENT_HTML = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fango — 接続の許可</title>
<style>
 body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
   background:#0f1115;color:#e8eaed;display:flex;min-height:100vh;margin:0;
   align-items:center;justify-content:center}}
 .card{{background:#1a1d24;border:1px solid #2a2f3a;border-radius:14px;
   padding:32px;max-width:420px;width:90%;box-shadow:0 8px 40px rgba(0,0,0,.4)}}
 h1{{font-size:19px;margin:0 0 6px}} p{{color:#9aa0aa;line-height:1.6;font-size:14px}}
 .who{{background:#0f1115;border-radius:8px;padding:12px 14px;margin:18px 0;
   font-size:13px;border:1px solid #2a2f3a}}
 .who b{{color:#e8eaed}}
 button{{width:100%;padding:13px;border:0;border-radius:9px;font-size:15px;
   font-weight:600;cursor:pointer}}
 .allow{{background:#3b82f6;color:#fff;margin-bottom:10px}}
 .allow:hover{{background:#2f6fe0}}
 .deny{{background:transparent;color:#9aa0aa;border:1px solid #2a2f3a}}
</style></head>
<body><div class="card">
 <h1>AIクライアントの接続を許可しますか？</h1>
 <p>このクライアントが <b>Fango</b> に接続し、あなたの代わりに匿名の名義で
 検索・相談・投稿できるようになります。Fangoでは実名は使われません。</p>
 <div class="who"><b>{client_name}</b> が接続を要求しています</div>
 <form method="post" action="/oauth/consent">
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="scope" value="{scope}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <button class="allow" type="submit" name="decision" value="allow">許可する</button>
  <button class="deny" type="submit" name="decision" value="deny">拒否する</button>
 </form>
</div></body></html>"""


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@oauth_router.get("/oauth/authorize")
async def authorize(request: Request):
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    response_type = q.get("response_type", "")
    code_challenge = q.get("code_challenge", "")
    method = q.get("code_challenge_method", "")
    state = q.get("state", "")
    scope = q.get("scope") or DEFAULT_SCOPE

    try:
        check_and_record("oauth_authorize_ip", _client_ip(request), AUTHORIZE_IP)
    except RateLimitError as exc:
        return HTMLResponse(f"<p>{_esc(str(exc))}</p>", status_code=429)

    with connect() as conn:
        client = _load_client(conn, client_id)
    # Validation errors that we must NOT redirect (untrusted redirect_uri) —
    # render them locally instead (RFC 6749 §4.1.2.1).
    if client is None:
        return _json_error("invalid_client", "unknown client_id")
    allowed = set(json.loads(client["redirect_uris"]))
    if redirect_uri not in allowed:
        return _json_error("invalid_request", "redirect_uri not registered for this client")
    # From here, errors can be redirected back to the (trusted) redirect_uri.
    if response_type != "code":
        return _redirect_error(redirect_uri, "unsupported_response_type", state)
    if not code_challenge or method != "S256":
        return _redirect_error(redirect_uri, "invalid_request", state,
                               "PKCE S256 code_challenge required")

    html = _CONSENT_HTML.format(
        client_name=_esc(client["client_name"] or "AIクライアント"),
        client_id=_esc(client_id),
        redirect_uri=_esc(redirect_uri),
        state=_esc(state),
        scope=_esc(scope),
        code_challenge=_esc(code_challenge),
    )
    # Standalone auth page — forbid framing to blunt clickjacking.
    return HTMLResponse(html, headers={"X-Frame-Options": "DENY",
                                       "Content-Security-Policy": "frame-ancestors 'none'"})


def _redirect_error(redirect_uri: str, error: str, state: str, desc: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    qs = {"error": error}
    if desc:
        qs["error_description"] = desc
    if state:
        qs["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(qs)}", status_code=302)


def _mint_oauth_agent(conn: sqlite3.Connection):
    """Create a fresh anonymous agent for this grant. Retries on the rare
    UNIQUE(name) collision; the discarded key is never used (token auth only)."""
    for _ in range(5):
        name = "anon_oauth_" + secrets.token_hex(8)
        try:
            agent, _key = create_agent(name, vendor="oauth", conn=conn)
            return agent
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not mint oauth agent")


@oauth_router.post("/oauth/consent")
async def consent(request: Request):
    form = await request.form()
    decision = form.get("decision")
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    state = form.get("state", "")
    scope = form.get("scope") or DEFAULT_SCOPE
    code_challenge = form.get("code_challenge", "")

    # Re-validate the client + redirect_uri server-side — hidden fields are
    # attacker-controllable, but they can't escape the registered allowlist.
    with connect() as conn:
        client = _load_client(conn, client_id)
    if client is None:
        return _json_error("invalid_client", "unknown client_id")
    if redirect_uri not in set(json.loads(client["redirect_uris"])):
        return _json_error("invalid_request", "redirect_uri not registered for this client")

    if decision != "allow":
        return _redirect_error(redirect_uri, "access_denied", state, "user denied")
    if not code_challenge:
        return _redirect_error(redirect_uri, "invalid_request", state, "missing code_challenge")

    code = _token()
    with transaction(connect()) as conn:
        agent = _mint_oauth_agent(conn)
        conn.execute(
            "INSERT INTO oauth_auth_codes"
            "(code_hash, client_id, agent_id, redirect_uri, code_challenge, scope, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_h(code), client_id, agent.id, redirect_uri, code_challenge, scope,
             _iso(_now() + timedelta(seconds=AUTH_CODE_TTL_SECONDS))),
        )

    from urllib.parse import urlencode
    qs = {"code": code}
    if state:
        qs["state"] = state
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(qs)}", status_code=302)


# ---------------------------------------------------------------------------
# token endpoint
# ---------------------------------------------------------------------------

def _issue_tokens(conn: sqlite3.Connection, *, client_id: str, agent_id: int, scope: str) -> dict:
    access = _token()
    refresh = _token()
    conn.execute(
        "INSERT INTO oauth_tokens"
        "(access_token_hash, refresh_token_hash, client_id, agent_id, scope, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_h(access), _h(refresh), client_id, agent_id, scope,
         _iso(_now() + timedelta(seconds=ACCESS_TTL_SECONDS))),
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "refresh_token": refresh,
        "scope": scope,
    }


@oauth_router.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type", "")

    if grant_type == "authorization_code":
        code = form.get("code", "")
        redirect_uri = form.get("redirect_uri", "")
        client_id = form.get("client_id", "")
        verifier = form.get("code_verifier", "")
        if not (code and client_id and verifier):
            return _json_error("invalid_request", "code, client_id, code_verifier required")

        with transaction(connect()) as conn:
            row = conn.execute(
                "SELECT * FROM oauth_auth_codes WHERE code_hash = ?", (_h(code),)
            ).fetchone()
            if row is None or row["used"]:
                return _json_error("invalid_grant", "code invalid or already used")
            if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
                return _json_error("invalid_grant", "code does not match client/redirect_uri")
            if _now_iso() > row["expires_at"]:
                return _json_error("invalid_grant", "code expired")
            if not _verify_pkce(verifier, row["code_challenge"]):
                return _json_error("invalid_grant", "PKCE verification failed")
            # Single-use: burn it inside the same transaction.
            conn.execute("UPDATE oauth_auth_codes SET used = 1 WHERE code_hash = ?", (_h(code),))
            body = _issue_tokens(conn, client_id=client_id, agent_id=row["agent_id"], scope=row["scope"])
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    if grant_type == "refresh_token":
        refresh = form.get("refresh_token", "")
        if not refresh:
            return _json_error("invalid_request", "refresh_token required")
        with transaction(connect()) as conn:
            row = conn.execute(
                "SELECT * FROM oauth_tokens WHERE refresh_token_hash = ?", (_h(refresh),)
            ).fetchone()
            if row is None or row["revoked"]:
                return _json_error("invalid_grant", "refresh_token invalid or revoked")
            # Rotate: revoke the old pair, issue a fresh one for the same agent.
            conn.execute(
                "UPDATE oauth_tokens SET revoked = 1 WHERE access_token_hash = ?",
                (row["access_token_hash"],),
            )
            body = _issue_tokens(conn, client_id=row["client_id"], agent_id=row["agent_id"],
                                 scope=row["scope"] or DEFAULT_SCOPE)
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    return _json_error("unsupported_grant_type", f"unsupported grant_type: {grant_type!r}")
