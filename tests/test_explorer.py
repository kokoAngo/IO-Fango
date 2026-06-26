"""Ledger explorer pages: list, detail, search."""
from __future__ import annotations

import pytest

from fango.agreements import service as svc
from fango.chain.client import set_chain_client


@pytest.fixture(autouse=True)
def _reset_chain():
    set_chain_client(None)
    yield
    set_chain_client(None)


_TERMS = {
    "monthly_rent_yen": 150000, "deposit_yen": 300000, "key_money_yen": 0,
    "maintenance_fee_yen": 0, "contract_months": 24,
}


def _make(agent_factory, listing_factory):
    listing = listing_factory()
    a, _ = agent_factory("PartyA")
    b, _ = agent_factory("PartyB")
    return svc.create_agreement(
        agreement_type="rental", listing_id=listing.id, listing_reins_id="R-1",
        party_a_agent_id=a.id, party_b_agent_id=b.id, terms=dict(_TERMS),
        auto_anchor=False,
    )


def test_explorer_index_empty(client):
    r = client.get("/explorer")
    assert r.status_code == 200
    assert "台帳エクスプローラー" in r.text


def test_explorer_lists_agreement(client, agent_factory, listing_factory):
    ag = _make(agent_factory, listing_factory)
    r = client.get("/explorer")
    assert r.status_code == 200
    assert f"/explorer/{ag.id}" in r.text
    assert ag.content_hash[:10] in r.text          # short hash shown
    # Terms must NOT leak into the public list.
    assert "150000" not in r.text


def test_explorer_detail(client, agent_factory, listing_factory):
    ag = _make(agent_factory, listing_factory)
    r = client.get(f"/explorer/{ag.id}")
    assert r.status_code == 200
    assert ag.content_hash in r.text               # full hash on detail
    assert "レコード整合性 OK" in r.text            # verify section


def test_explorer_detail_404(client):
    r = client.get("/explorer/99999")
    assert r.status_code == 404


def test_explorer_search_by_id_redirects(client, agent_factory, listing_factory):
    ag = _make(agent_factory, listing_factory)
    r = client.get("/explorer", params={"q": str(ag.id)}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/explorer/{ag.id}"


def test_explorer_search_by_hash_redirects(client, agent_factory, listing_factory):
    ag = _make(agent_factory, listing_factory)
    r = client.get("/explorer", params={"q": ag.content_hash}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/explorer/{ag.id}"


def test_explorer_search_no_match(client):
    r = client.get("/explorer", params={"q": "0xdeadbeef"})
    assert r.status_code == 200
    assert "見つかりませんでした" in r.text


def test_explorer_detail_terms_gated(client, agent_factory, listing_factory):
    ag = _make(agent_factory, listing_factory)
    # Anonymous: terms hidden.
    assert "150000" not in client.get(f"/explorer/{ag.id}").text
    # Keyed: terms shown.
    _, key = agent_factory("Viewer")
    r = client.get(f"/explorer/{ag.id}", headers={"X-Agent-Key": key})
    assert "150000" in r.text
