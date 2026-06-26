"""HTTP read surface for agreements: public view + verify."""
from __future__ import annotations

import pytest

from fango.agreements import service as svc
from fango.chain.client import set_chain_client


@pytest.fixture(autouse=True)
def _reset_chain():
    set_chain_client(None)        # ensure no fake leaked from another test
    yield
    set_chain_client(None)


_TERMS = {
    "monthly_rent_yen": 150000, "deposit_yen": 300000, "key_money_yen": 0,
    "maintenance_fee_yen": 0, "contract_months": 24,
}


def _make_agreement(agent_factory, listing_factory):
    listing = listing_factory()
    a, _ = agent_factory("PartyA")
    b, _ = agent_factory("PartyB")
    return svc.create_agreement(
        agreement_type="rental", listing_id=listing.id, listing_reins_id="R-1",
        party_a_agent_id=a.id, party_b_agent_id=b.id, terms=dict(_TERMS),
        auto_anchor=False,
    )


def test_agreement_view_public(client, agent_factory, listing_factory):
    ag = _make_agreement(agent_factory, listing_factory)
    r = client.get(f"/agreements/{ag.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["content_hash"] == ag.content_hash
    assert body["status"] == "unanchored"
    # Parties are pseudonymized, not raw ids; raw terms hidden from anon callers.
    assert isinstance(body["party_a"], str) and body["party_a"] != ag.party_a_agent_id
    assert "terms" not in body


def test_agreement_view_keyed_sees_terms(client, agent_factory, listing_factory):
    ag = _make_agreement(agent_factory, listing_factory)
    _, key = agent_factory("Viewer")
    r = client.get(f"/agreements/{ag.id}", headers={"X-Agent-Key": key})
    assert r.status_code == 200
    assert r.json()["terms"]["monthly_rent_yen"] == 150000


def test_agreement_not_found(client):
    assert client.get("/agreements/99999").status_code == 404


def test_agreement_verify(client, agent_factory, listing_factory):
    ag = _make_agreement(agent_factory, listing_factory)
    r = client.get(f"/agreements/{ag.id}/verify")
    assert r.status_code == 200
    v = r.json()
    assert v["found"] and v["record_ok"]
    assert v["on_chain"] is False        # chain not configured in tests
