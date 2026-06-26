"""Durable secp256k1 agent identities: server custody, self custody, signing."""
from __future__ import annotations

import pytest

from fango import identity as idn
from fango.auth import create_agent
from fango.db import connect

_HAVE = idn.libs_available()
pytestmark = pytest.mark.skipif(not _HAVE, reason="identity libs (eth-account) not installed")

SECRET = "unit-test-identity-secret"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("FANGO_IDENTITY_SECRET", SECRET)
    yield


def _agent(tmp_db, name="Broker"):
    conn = connect(tmp_db)
    try:
        a, _ = create_agent(name, vendor="test", conn=conn)
        conn.commit()
    finally:
        conn.close()
    return a


def test_server_custody_available(tmp_db):
    assert idn.server_custody_available() is True


def test_provision_creates_address(tmp_db):
    a = _agent(tmp_db)
    res = idn.provision_server_identity(a.id)
    assert res["custody"] == "server"
    assert res["address"].startswith("0x") and len(res["address"]) == 42
    # stored + retrievable
    got = idn.get_identity(a.id)
    assert got["address"] == res["address"]


def test_provision_idempotent(tmp_db):
    a = _agent(tmp_db)
    r1 = idn.provision_server_identity(a.id)
    r2 = idn.provision_server_identity(a.id)
    assert r1["address"] == r2["address"]


def test_custodial_sign_recovers_to_address(tmp_db):
    from eth_account import Account
    from eth_account.messages import encode_defunct
    a = _agent(tmp_db)
    res = idn.provision_server_identity(a.id)
    msg = "I agree: rent 150000 yen"
    sig = idn.sign_message(a.id, msg)
    assert sig
    recovered = Account.recover_message(
        encode_defunct(text=msg),
        signature=bytes.fromhex(sig[2:] if sig.startswith("0x") else sig),
    )
    assert recovered == res["address"]            # non-repudiation primitive works


def test_self_custody_binds_external_pubkey(tmp_db):
    from eth_account import Account
    from eth_keys import keys
    a = _agent(tmp_db, "BrokerSelf")
    own = Account.create()
    pubkey = keys.PrivateKey(bytes(own.key)).public_key.to_hex()
    res = idn.register_self_identity(a.id, pubkey)
    assert res["custody"] == "self"
    assert res["address"] == own.address          # derived address matches eth_account
    # we hold no private key for self-custody, so we can't sign for them
    assert idn.sign_message(a.id, "x") is None
    conn = connect(tmp_db)
    try:
        assert conn.execute("SELECT privkey_enc FROM agents WHERE id=?", (a.id,)).fetchone()["privkey_enc"] is None
    finally:
        conn.close()


def test_self_custody_address_collision_rejected(tmp_db):
    from eth_account import Account
    from eth_keys import keys
    a = _agent(tmp_db, "A1")
    b = _agent(tmp_db, "B1")
    own = Account.create()
    pubkey = keys.PrivateKey(bytes(own.key)).public_key.to_hex()
    idn.register_self_identity(a.id, pubkey)
    with pytest.raises(idn.IdentityError):
        idn.register_self_identity(b.id, pubkey)   # same address, different agent


def test_invalid_pubkey_rejected(tmp_db):
    a = _agent(tmp_db)
    with pytest.raises(idn.IdentityError):
        idn.register_self_identity(a.id, "0xnothex")


def test_degrade_without_secret(tmp_db, monkeypatch):
    monkeypatch.delenv("FANGO_IDENTITY_SECRET", raising=False)
    assert idn.server_custody_available() is False
    a = _agent(tmp_db)
    assert idn.provision_server_identity(a.id) is None   # no-op, no crash
    assert idn.get_identity(a.id) is None


def test_redeem_provisions_identity(tmp_db):
    from fango.claims import create_claim, redeem_claim
    claim = create_claim(name="redeemed-agent", vendor="test")
    agent, key = redeem_claim(claim.code)
    ident = idn.get_identity(agent.id)
    assert ident is not None and ident["custody"] == "server"


def test_http_self_custody_endpoint(client, agent_factory):
    from eth_account import Account
    from eth_keys import keys
    agent, key = agent_factory("HttpBroker")
    own = Account.create()
    pubkey = keys.PrivateKey(bytes(own.key)).public_key.to_hex()
    r = client.post("/api/agent/identity", json={"pubkey": pubkey},
                    headers={"X-Agent-Key": key})
    assert r.status_code == 200, r.text
    assert r.json()["address"] == own.address
    assert r.json()["custody"] == "self"


def test_http_identity_requires_auth(client):
    r = client.post("/api/agent/identity", json={"pubkey": "0x04abc"})
    assert r.status_code == 401
