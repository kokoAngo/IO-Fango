"""Human-mediated claim → agent redeem handshake."""
from __future__ import annotations

import time

import pytest

from fango.claims import (
    ClaimError, CODE_TTL_SECONDS, create_claim, generate_code,
    get_claim, normalize_code, redeem_claim,
)
from fango.db import connect


# ─── unit: code generation / normalization ─────────────────────────────────

def test_generate_code_format():
    code = generate_code()
    assert len(code) == 14  # 12 chars + 2 dashes
    assert code[4] == "-" and code[9] == "-"
    bare = code.replace("-", "")
    assert all(c in "23456789ABCDEFGHJKLMNPQRSTUVWXYZ" for c in bare)


def test_generate_code_unique():
    seen = {generate_code() for _ in range(100)}
    assert len(seen) == 100


def test_normalize_code():
    assert normalize_code("ab-cd-ef") == "AB-CD-EF"
    assert normalize_code("  abcd  efgh  ijkl") == "ABCDEFGHJKL"
    assert normalize_code("") == ""


# ─── service: create_claim ──────────────────────────────────────────────────

def test_create_claim_returns_record(tmp_db):
    c = create_claim(name="alice", vendor="anthropic", ip="1.1.1.1")
    assert c.name == "alice"
    assert c.vendor == "anthropic"
    assert c.redeemed_at is None
    assert c.agent_id is None


def test_create_claim_empty_name(tmp_db):
    with pytest.raises(ClaimError):
        create_claim(name="", vendor=None)


def test_create_claim_name_too_long(tmp_db):
    with pytest.raises(ClaimError):
        create_claim(name="x" * 60, vendor=None)


def test_get_claim(tmp_db):
    c = create_claim(name="bob", vendor=None)
    fetched = get_claim(c.code)
    assert fetched is not None
    assert fetched.name == "bob"


def test_get_unknown_claim(tmp_db):
    assert get_claim("FAKE-CODE-AB12") is None


# ─── service: redeem_claim ──────────────────────────────────────────────────

def test_redeem_happy_path(tmp_db):
    c = create_claim(name="carol", vendor="anthropic")
    agent, key = redeem_claim(c.code)
    assert agent.name == "carol"
    assert agent.vendor == "anthropic"
    assert isinstance(key, str) and len(key) > 30
    # claim is now marked redeemed
    after = get_claim(c.code)
    assert after.redeemed_at is not None
    assert after.agent_id == agent.id


def test_redeem_tolerates_lowercase_and_spaces(tmp_db):
    c = create_claim(name="dave", vendor=None)
    # Simulate user pasting code with extra whitespace + lowercase
    messy = "  " + c.code.lower() + " "
    agent, _ = redeem_claim(messy)
    assert agent.name == "dave"


def test_redeem_unknown_code(tmp_db):
    with pytest.raises(ClaimError):
        redeem_claim("XXXX-YYYY-ZZZZ")


def test_redeem_malformed_code(tmp_db):
    with pytest.raises(ClaimError):
        redeem_claim("nope")


def test_redeem_already_used(tmp_db):
    c = create_claim(name="eve", vendor=None)
    redeem_claim(c.code)
    with pytest.raises(ClaimError):
        redeem_claim(c.code)


def test_redeem_expired(tmp_db):
    c = create_claim(name="frank", vendor=None)
    # Push the created_at into the past so it appears expired.
    conn = connect()
    try:
        conn.execute(
            "UPDATE agent_claims SET created_at = ? WHERE code = ?",
            ("2020-01-01T00:00:00.000Z", c.code),
        )
    finally:
        conn.close()
    with pytest.raises(ClaimError):
        redeem_claim(c.code)


# ─── HTTP: /onboard/ form + /api/agent/redeem ──────────────────────────────

def test_claim_form_renders(client):
    r = client.get("/onboard/")
    assert r.status_code == 200
    assert "AI のためのコード発行" in r.text
    assert "私はロボットではありません" in r.text
    # Step progress dots
    for s in ("01", "02", "03"):
        assert s in r.text


def test_claim_submit_without_captcha(client):
    r = client.post("/onboard/", data={"name": "x", "vendor": "anthropic", "captcha": ""})
    assert r.status_code == 200
    assert "人間チェックボックス" in r.text


def test_claim_submit_happy_path(client):
    r = client.post("/onboard/", data={
        "name": "agt-from-web", "vendor": "anthropic", "captcha": "on",
    })
    assert r.status_code == 200
    assert "コード発行" in r.text
    # The code shows in the page; extract it
    import re
    m = re.search(r'id="claim-code">([A-Z0-9\-]+)<', r.text)
    assert m is not None
    code = m.group(1)
    # Now redeem via API
    r2 = client.post("/api/agent/redeem", json={"code": code})
    assert r2.status_code == 200
    data = r2.json()
    assert data["name"] == "agt-from-web"
    assert data["vendor"] == "anthropic"
    assert len(data["agent_key"]) > 30


def test_redeem_api_unknown_code(client):
    r = client.post("/api/agent/redeem", json={"code": "FAKE-CODE-ABCD"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_redeem_api_missing_code(client):
    r = client.post("/api/agent/redeem", json={})
    assert r.status_code == 400


def test_claim_ip_rate_limit(client):
    # 5 ok, 6th should fail
    for i in range(5):
        r = client.post("/onboard/", data={
            "name": f"agt-rate-{i}", "vendor": "", "captcha": "on",
        })
        assert r.status_code == 200, r.text[:200]
    r6 = client.post("/onboard/", data={
        "name": "agt-blocked", "vendor": "", "captcha": "on",
    })
    assert r6.status_code == 200
    assert "レート制限" in r6.text
