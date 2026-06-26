"""Escrow lifecycle on the in-memory FakeChainClient: open/fund/settle/slash/expire."""
from __future__ import annotations

import pytest

from fango import identity as idn
from fango.agreements import service as agsvc
from fango.chain.client import set_chain_client
from fango.chain.fake import FakeChainClient
from fango.escrow import service as esvc

_HAVE = idn.libs_available()
pytestmark = pytest.mark.skipif(not _HAVE, reason="identity libs not installed")

ONE = 10 ** 18
DEP = 5 * ONE


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("FANGO_IDENTITY_SECRET", "escrow-test-secret")
    yield


@pytest.fixture
def fake():
    c = FakeChainClient()
    set_chain_client(c)
    yield c
    set_chain_client(None)


def _party(tmp_db, agent_factory, fake, name, mint=2 * DEP):
    a, _ = agent_factory(name)
    ident = idn.provision_server_identity(a.id)
    fake.mint(ident["address"], mint)
    return a, ident["address"]


def _agreement(agent_factory, listing_factory, a_id, b_id):
    listing = listing_factory()
    return agsvc.create_agreement(
        agreement_type="rental", listing_id=listing.id, listing_reins_id="R-1",
        party_a_agent_id=a_id, party_b_agent_id=b_id,
        terms={"monthly_rent_yen": 150000, "deposit_yen": 300000, "key_money_yen": 0,
               "maintenance_fee_yen": 0, "contract_months": 24}, auto_anchor=False)


def _open(tmp_db, agent_factory, listing_factory, fake, *, deposit=DEP, deadline_ts=None):
    a, addr_a = _party(tmp_db, agent_factory, fake, "PartyA")
    b, addr_b = _party(tmp_db, agent_factory, fake, "PartyB")
    ag = _agreement(agent_factory, listing_factory, a.id, b.id)
    esc = esvc.open_escrow(ag.id, deposit_a=deposit, deposit_b=deposit, deadline_ts=deadline_ts)
    return esc, a, b, addr_a, addr_b


def test_open_creates_escrow(tmp_db, agent_factory, listing_factory, fake):
    esc, *_ = _open(tmp_db, agent_factory, listing_factory, fake)
    assert esc.status == "open"
    assert esc.onchain_escrow_id is not None
    assert esc.content_hash.startswith("0x")
    assert esc.deposit_a == str(DEP)


def test_open_idempotent(tmp_db, agent_factory, listing_factory, fake):
    esc, a, b, *_ = _open(tmp_db, agent_factory, listing_factory, fake)
    again = esvc.open_escrow(esc.agreement_id, deposit_a=DEP, deposit_b=DEP)
    assert again.id == esc.id


def test_fund_both_to_funded(tmp_db, agent_factory, listing_factory, fake):
    esc, a, b, addr_a, addr_b = _open(tmp_db, agent_factory, listing_factory, fake)
    esvc.fund(esc.id, "a"); esvc.fund(esc.id, "b")
    assert esvc.get_escrow(esc.id).status == "funded"
    assert fake.balance_of(addr_a) == DEP and fake.balance_of(addr_b) == DEP  # 2*DEP - DEP
    assert fake.balance_of("0xESCROW") == 2 * DEP


def test_fund_idempotent(tmp_db, agent_factory, listing_factory, fake):
    esc, a, b, addr_a, _ = _open(tmp_db, agent_factory, listing_factory, fake)
    esvc.fund(esc.id, "a")
    r = esvc.fund(esc.id, "a")
    assert r["reason"] == "already funded"
    assert fake.balance_of(addr_a) == DEP        # only debited once


def test_settle_refunds_both(tmp_db, agent_factory, listing_factory, fake):
    esc, a, b, addr_a, addr_b = _open(tmp_db, agent_factory, listing_factory, fake)
    esvc.fund(esc.id, "a"); esvc.fund(esc.id, "b")
    res = esvc.settle(esc.id)
    assert res["status"] == "confirmed"
    e = esvc.get_escrow(esc.id)
    assert e.status == "settled" and e.outcome == "completed"
    assert fake.balance_of(addr_a) == 2 * DEP and fake.balance_of(addr_b) == 2 * DEP  # fully restored


def test_slash_loser_to_winner(tmp_db, agent_factory, listing_factory, fake):
    esc, a, b, addr_a, addr_b = _open(tmp_db, agent_factory, listing_factory, fake)
    esvc.fund(esc.id, "a"); esvc.fund(esc.id, "b")
    res = esvc.slash(esc.id, a.id)               # A reneges
    assert res["status"] == "confirmed"
    e = esvc.get_escrow(esc.id)
    assert e.status == "slashed" and e.outcome == f"slashed:{a.id}"
    assert fake.balance_of(addr_a) == DEP        # lost its stake
    assert fake.balance_of(addr_b) == 3 * DEP    # own stake back + A's stake


def test_settle_requires_funded(tmp_db, agent_factory, listing_factory, fake):
    esc, *_ = _open(tmp_db, agent_factory, listing_factory, fake)
    esvc.fund(esc.id, "a")                        # only one leg
    res = esvc.settle(esc.id)
    assert res["status"] == "failed"


def test_refund_expired(tmp_db, agent_factory, listing_factory, fake):
    fake.now_ts = 2_000_000_000
    esc, a, b, addr_a, _ = _open(tmp_db, agent_factory, listing_factory, fake,
                                 deadline_ts=1_000_000_000)   # already past
    esvc.fund(esc.id, "a")
    res = esvc.refund_expired(esc.id)
    assert res["status"] == "confirmed"
    assert esvc.get_escrow(esc.id).status == "expired"
    assert fake.balance_of(addr_a) == 2 * DEP    # refunded


def test_chain_unconfigured_offchain_only(tmp_db, agent_factory, listing_factory):
    set_chain_client(FakeChainClient(escrow_configured=False))
    try:
        # parties still need identities (open requires addresses)
        import os
        os.environ["FANGO_IDENTITY_SECRET"] = "escrow-test-secret"
        a, _ = agent_factory("A"); b, _ = agent_factory("B")
        idn.provision_server_identity(a.id); idn.provision_server_identity(b.id)
        ag = _agreement(agent_factory, listing_factory, a.id, b.id)
        esc = esvc.open_escrow(ag.id, deposit_a=DEP, deposit_b=DEP)
        assert esc.status == "open" and esc.onchain_escrow_id is None
        assert esvc.fund(esc.id, "a")["status"] == "skipped"
    finally:
        set_chain_client(None)


def test_big_deposit_overflows_int64_as_string(tmp_db, agent_factory, listing_factory, fake):
    big = 100 * ONE                               # >> int64 max (~9.2e18)
    a, addr_a = _party(tmp_db, agent_factory, fake, "BigA", mint=2 * big)
    b, addr_b = _party(tmp_db, agent_factory, fake, "BigB", mint=2 * big)
    ag = _agreement(agent_factory, listing_factory, a.id, b.id)
    esc = esvc.open_escrow(ag.id, deposit_a=big, deposit_b=big)
    assert esc.deposit_a == str(big)
    esvc.fund(esc.id, "a"); esvc.fund(esc.id, "b")
    assert esvc.get_escrow(esc.id).status == "funded"
    assert fake.balance_of("0xESCROW") == 2 * big
