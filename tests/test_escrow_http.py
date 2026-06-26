"""GET /escrows/{id} + escrow panel on the explorer detail."""
from __future__ import annotations

import pytest

from fango import identity as idn
from fango.agreements import service as agsvc
from fango.chain.client import set_chain_client
from fango.chain.fake import FakeChainClient

_HAVE = idn.libs_available()
pytestmark = pytest.mark.skipif(not _HAVE, reason="identity libs not installed")

ONE = 10 ** 18


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("FANGO_IDENTITY_SECRET", "escrow-http-secret")
    yield
    set_chain_client(None)


def _make_escrow(agent_factory, listing_factory):
    fake = FakeChainClient()
    set_chain_client(fake)
    a, _ = agent_factory("EA"); b, _ = agent_factory("EB")
    ia = idn.provision_server_identity(a.id); ib = idn.provision_server_identity(b.id)
    fake.mint(ia["address"], 10 * ONE); fake.mint(ib["address"], 10 * ONE)
    listing = listing_factory()
    ag = agsvc.create_agreement(
        agreement_type="rental", listing_id=listing.id, listing_reins_id="R-1",
        party_a_agent_id=a.id, party_b_agent_id=b.id,
        terms={"monthly_rent_yen": 150000, "deposit_yen": 300000, "key_money_yen": 0,
               "maintenance_fee_yen": 0, "contract_months": 24}, auto_anchor=False)
    from fango.escrow import service as esvc
    esc = esvc.open_escrow(ag.id, deposit_a=5 * ONE, deposit_b=5 * ONE)
    return esc, ag


def test_escrow_view_public(client, agent_factory, listing_factory):
    esc, ag = _make_escrow(agent_factory, listing_factory)
    r = client.get(f"/escrows/{esc.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "open"
    assert body["deposit_a_tokens"] == 5.0
    assert isinstance(body["party_a"], str)
    assert "party_a_address" not in body          # hidden from anon


def test_escrow_view_keyed_sees_address(client, agent_factory, listing_factory):
    esc, ag = _make_escrow(agent_factory, listing_factory)
    _, key = agent_factory("Viewer")
    r = client.get(f"/escrows/{esc.id}", headers={"X-Agent-Key": key})
    assert r.status_code == 200
    assert r.json()["party_a_address"].startswith("0x")


def test_escrow_not_found(client):
    assert client.get("/escrows/99999").status_code == 404


def test_explorer_detail_shows_escrow_panel(client, agent_factory, listing_factory):
    esc, ag = _make_escrow(agent_factory, listing_factory)
    r = client.get(f"/explorer/{ag.id}")
    assert r.status_code == 200
    assert "保証金" in r.text
