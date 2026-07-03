"""Deal-formation bridge: broker proposes terms → customer accepts → anchored.

Uses FakeChainClient so the full create_agreement + anchor path runs without a
node. Mirrors the broker-test fixtures in test_brokers.py.
"""
from __future__ import annotations

import pytest

from fango.auth import create_agent
from fango.brokers import inquiries as inq
from fango.brokers import proposals
from fango.brokers import service as bsvc
from fango.chain.client import set_chain_client
from fango.chain.fake import FakeChainClient


@pytest.fixture
def fake_chain():
    set_chain_client(FakeChainClient())
    try:
        yield
    finally:
        set_chain_client(None)


def _rent_listing(broker_id, ward="新宿区", rent=150000, ad="可"):
    return {
        "building_name": f"テスト{ward}マンション",
        "reins_id": f"REINS-{ward}-{rent}",
        "prefecture": "東京都",
        "ward": ward,
        "layout": "2LDK",
        "rent_yen": rent,
        "ad_status": ad,
        "broker_agent_id": broker_id,
    }


_RENT_TERMS = {
    "monthly_rent_yen": 150000,
    "deposit_yen": 150000,
    "key_money_yen": 0,
    "maintenance_fee_yen": 10000,
    "contract_months": 24,
}


def _setup_inquiry(tmp_db):
    """Broker A with a listing, a customer, an inquiry routed to A. Returns ids."""
    broker, _ = bsvc.create_broker("brokerA", "新宿不動産")
    listing = bsvc.upsert_listing(broker.id, _rent_listing(broker.id))
    customer, _ = create_agent("anon_cust", vendor="anon")
    iid = inq.create_inquiry(customer.id, {"ward": "新宿"}, matched_listing_ids=[listing.id])
    inq.route_inquiry(iid, [(broker.id, 1)])
    return broker, listing, customer, iid


def test_create_proposal_validates_ownership_and_terms(tmp_db):
    broker, listing, customer, iid = _setup_inquiry(tmp_db)
    res = proposals.create_proposal(broker.id, iid, listing.id, "rental", _RENT_TERMS)
    assert res["proposal_id"] and res["customer_agent_id"] == customer.id

    # Bad terms (float money) rejected.
    with pytest.raises(proposals.ProposalError):
        proposals.create_proposal(broker.id, iid, listing.id, "rental",
                                  {**_RENT_TERMS, "monthly_rent_yen": 150000.5})

    # Another broker can't propose on this inquiry / listing.
    other, _ = bsvc.create_broker("brokerB", "渋谷不動産")
    with pytest.raises(proposals.ProposalError):
        proposals.create_proposal(other.id, iid, listing.id, "rental", _RENT_TERMS)


def test_list_for_customer_then_accept(tmp_db, fake_chain):
    # The customer-side discovery path the agent cluster uses: list pending
    # proposals → accept by id → agreement anchored → it drops off the list.
    broker, listing, customer, iid = _setup_inquiry(tmp_db)
    res = proposals.create_proposal(broker.id, iid, listing.id, "rental", _RENT_TERMS)

    pending = proposals.list_for_customer(customer.id)
    assert len(pending) == 1
    p = pending[0]
    assert p["proposal_id"] == res["proposal_id"]
    assert p["agreement_type"] == "rental"
    assert p["terms"]["monthly_rent_yen"] == _RENT_TERMS["monthly_rent_yen"]
    assert p["broker_company"] == "新宿不動産"

    out = proposals.accept_proposal(p["proposal_id"], customer.id)
    assert out["agreement"].status == "anchored"
    # Accepted → no longer pending; another customer sees nothing.
    assert proposals.list_for_customer(customer.id) == []
    other, _ = create_agent("other", vendor="anon")
    assert proposals.list_for_customer(other.id) == []


def test_derive_terms_matches_canonical_schema(tmp_db):
    # The autoresponder's auto-derived terms must satisfy create_proposal's schema.
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "broker_autoresponder",
        Path(__file__).resolve().parent.parent / "scripts" / "broker_autoresponder.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    broker, listing, customer, iid = _setup_inquiry(tmp_db)
    atype, terms = mod.derive_terms({"rent_yen": 150000})
    assert atype == "rental"
    # Feeds cleanly into a real proposal (would raise ProposalError on bad terms).
    res = proposals.create_proposal(broker.id, iid, listing.id, atype, terms)
    assert res["proposal_id"]
    # Sale derivation too.
    assert mod.derive_terms({"price_man": 5000})[0] == "sale"
    assert mod.derive_terms({})[0] is None


def test_accept_creates_and_anchors_agreement(tmp_db, fake_chain):
    broker, listing, customer, iid = _setup_inquiry(tmp_db)
    res = proposals.create_proposal(broker.id, iid, listing.id, "rental", _RENT_TERMS)
    out = proposals.accept_proposal(res["proposal_id"], customer.id)

    ag = out["agreement"]
    assert ag.party_a_agent_id == broker.id and ag.party_b_agent_id == customer.id
    assert ag.agreement_type == "rental"
    assert out["anchor"]["status"] == "confirmed"
    assert ag.status == "anchored"

    # Proposal + inquiry state updated.
    p = proposals.get_proposal(res["proposal_id"])
    assert p["status"] == "accepted" and p["agreement_id"] == ag.id
    # Inquiry closed.
    from fango.db import connect
    conn = connect()
    try:
        row = conn.execute("SELECT status FROM broker_inquiries WHERE id=?", (iid,)).fetchone()
        assert row["status"] == "closed"
    finally:
        conn.close()


def test_accept_offchain_when_chain_off(tmp_db):
    # Force the chain OFF deterministically (don't rely on ambient .env, which
    # may carry real chain config — config.py auto-loads it on import).
    set_chain_client(FakeChainClient(configured=False))
    try:
        _run_offchain_accept(tmp_db)
    finally:
        set_chain_client(None)


def _run_offchain_accept(tmp_db):
    broker, listing, customer, iid = _setup_inquiry(tmp_db)
    res = proposals.create_proposal(broker.id, iid, listing.id, "rental", _RENT_TERMS)
    out = proposals.accept_proposal(res["proposal_id"], customer.id)
    assert out["agreement"].status == "unanchored"
    assert out["anchor"]["status"] == "skipped"


def test_accept_rejects_wrong_customer(tmp_db, fake_chain):
    broker, listing, customer, iid = _setup_inquiry(tmp_db)
    res = proposals.create_proposal(broker.id, iid, listing.id, "rental", _RENT_TERMS)
    intruder, _ = create_agent("intruder", vendor="anon")
    with pytest.raises(proposals.ProposalError):
        proposals.accept_proposal(res["proposal_id"], intruder.id)


def test_accept_twice_is_rejected(tmp_db, fake_chain):
    broker, listing, customer, iid = _setup_inquiry(tmp_db)
    res = proposals.create_proposal(broker.id, iid, listing.id, "rental", _RENT_TERMS)
    proposals.accept_proposal(res["proposal_id"], customer.id)
    with pytest.raises(proposals.ProposalError):
        proposals.accept_proposal(res["proposal_id"], customer.id)
