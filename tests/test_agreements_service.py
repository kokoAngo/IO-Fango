"""Agreement lifecycle: create → anchor → verify, with the in-memory fake chain."""
from __future__ import annotations

import pytest

from fango.agreements import service as svc
from fango.chain.client import set_chain_client
from fango.chain.fake import FakeChainClient
from fango.db import connect


@pytest.fixture
def fake_chain():
    client = FakeChainClient()
    set_chain_client(client)
    yield client
    set_chain_client(None)        # never leak across tests


def _parties(agent_factory):
    a, _ = agent_factory("PartyA")
    b, _ = agent_factory("PartyB")
    return a.id, b.id


_TERMS = {
    "monthly_rent_yen": 150000, "deposit_yen": 300000, "key_money_yen": 150000,
    "maintenance_fee_yen": 10000, "contract_months": 24,
}


def _create(tmp_db, agent_factory, listing_factory, **over):
    listing = listing_factory()
    a_id, b_id = _parties(agent_factory)
    kw = dict(
        agreement_type="rental", listing_id=listing.id, listing_reins_id="R-1",
        party_a_agent_id=a_id, party_b_agent_id=b_id, terms=dict(_TERMS),
        finalized_at="2026-01-02T03:04:05.678Z", auto_anchor=False,
    )
    kw.update(over)
    return svc.create_agreement(**kw)


def test_create_is_unanchored(tmp_db, agent_factory, listing_factory, fake_chain):
    ag = _create(tmp_db, agent_factory, listing_factory)
    assert ag.status == "unanchored"
    assert ag.content_hash.startswith("0x")
    assert ag.price_yen == 150000


def test_anchor_now_confirms(tmp_db, agent_factory, listing_factory, fake_chain):
    ag = _create(tmp_db, agent_factory, listing_factory)
    result = svc.anchor_now(ag.id)
    assert result["status"] == "confirmed"
    reloaded = svc.get_agreement(ag.id)
    assert reloaded.status == "anchored"
    conn = connect(tmp_db)
    try:
        anchor = svc.latest_anchor(ag.id, conn)
        assert anchor["status"] == "confirmed"
        assert anchor["tx_hash"].startswith("0x")
        assert anchor["block_number"] is not None
    finally:
        conn.close()


def test_create_is_idempotent(tmp_db, agent_factory, listing_factory, fake_chain):
    listing = listing_factory()
    a_id, b_id = _parties(agent_factory)
    common = dict(
        agreement_type="rental", listing_id=listing.id, listing_reins_id="R-1",
        party_a_agent_id=a_id, party_b_agent_id=b_id, terms=dict(_TERMS),
        finalized_at="2026-01-02T03:04:05.678Z", auto_anchor=False,
    )
    a1 = svc.create_agreement(**common)
    a2 = svc.create_agreement(**common)
    assert a1.id == a2.id
    conn = connect(tmp_db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM agreements").fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_verify_ok(tmp_db, agent_factory, listing_factory, fake_chain):
    ag = _create(tmp_db, agent_factory, listing_factory)
    svc.anchor_now(ag.id)
    v = svc.verify_agreement(ag.id)
    assert v["found"] and v["record_ok"] and v["on_chain"]
    assert v["tx_hash"].startswith("0x")


def test_verify_detects_tamper(tmp_db, agent_factory, listing_factory, fake_chain):
    ag = _create(tmp_db, agent_factory, listing_factory)
    conn = connect(tmp_db)
    try:
        # Corrupt the stored canonical record after the fact.
        conn.execute(
            "UPDATE agreements SET canonical_json = ? WHERE id = ?",
            ('{"tampered":true}', ag.id),
        )
        conn.commit()
    finally:
        conn.close()
    v = svc.verify_agreement(ag.id)
    assert v["record_ok"] is False


def test_anchor_failure_recorded(tmp_db, agent_factory, listing_factory):
    set_chain_client(FakeChainClient(fail=True))
    try:
        ag = _create(tmp_db, agent_factory, listing_factory)
        result = svc.anchor_now(ag.id)
        assert result["status"] == "failed"
        reloaded = svc.get_agreement(ag.id)
        assert reloaded.status == "failed"       # agreement still exists
        conn = connect(tmp_db)
        try:
            anchor = svc.latest_anchor(ag.id, conn)
            assert anchor["status"] == "failed" and anchor["error"]
        finally:
            conn.close()
    finally:
        set_chain_client(None)


def test_unconfigured_chain_skips(tmp_db, agent_factory, listing_factory):
    set_chain_client(FakeChainClient(configured=False))
    try:
        ag = _create(tmp_db, agent_factory, listing_factory)
        result = svc.anchor_now(ag.id)
        assert result["status"] == "skipped"
        assert svc.get_agreement(ag.id).status == "unanchored"   # no crash, stays off-chain
    finally:
        set_chain_client(None)
