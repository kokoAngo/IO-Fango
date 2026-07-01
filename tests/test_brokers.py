"""Broker agents: onboarding, inventory scoping, inquiry routing, responding."""
from __future__ import annotations

import pytest

from fango.auth import AuthError
from fango.brokers import inquiries as inq
from fango.brokers import service as bsvc
from fango.listings import service as ls


def _rent_listing(broker_id, ward="新宿区", rent=150000, ad="可"):
    return {
        "building_name": f"テスト{ward}マンション",
        "prefecture": "東京都",
        "ward": ward,
        "layout": "2LDK",
        "rent_yen": rent,
        "ad_status": ad,
        "broker_agent_id": broker_id,
    }


def test_create_broker_marks_vendor_and_profile(tmp_db):
    agent, key = bsvc.create_broker("brokerA", "新宿不動産", areas=["新宿", "渋谷"])
    assert agent.vendor == "broker"
    assert key  # plaintext returned once
    assert bsvc.is_broker(agent.id)
    profile = bsvc.get_broker(agent.id)
    assert profile["company"] == "新宿不動産"
    assert profile["areas"] == ["新宿", "渋谷"]
    # A non-broker agent is not a broker.
    from fango.auth import create_agent
    other, _ = create_agent("normal", vendor="anon")
    assert not bsvc.is_broker(other.id)


def test_create_broker_requires_company(tmp_db):
    with pytest.raises(bsvc.BrokerError):
        bsvc.create_broker("b", "  ")


def test_inventory_is_scoped_to_owning_broker(tmp_db):
    a, _ = bsvc.create_broker("brokerA", "A社")
    b, _ = bsvc.create_broker("brokerB", "B社")
    la = bsvc.upsert_listing(a.id, _rent_listing(a.id))
    bsvc.upsert_listing(b.id, _rent_listing(b.id, ward="渋谷区"))

    a_rows = bsvc.list_listings(a.id)
    b_rows = bsvc.list_listings(b.id)
    assert {r.id for r in a_rows} == {la.id}
    assert la.id not in {r.id for r in b_rows}

    # Broker B cannot update broker A's listing.
    with pytest.raises(bsvc.BrokerError):
        bsvc.upsert_listing(b.id, {"rent_yen": 99999}, listing_id=la.id)

    # Broker A can update its own.
    updated = bsvc.upsert_listing(a.id, {"rent_yen": 140000}, listing_id=la.id)
    assert updated.extra.get("rent_yen") == 140000


def test_remove_listing_is_scoped(tmp_db):
    a, _ = bsvc.create_broker("brokerA", "A社")
    b, _ = bsvc.create_broker("brokerB", "B社")
    la = bsvc.upsert_listing(a.id, _rent_listing(a.id))
    # B can't remove A's listing.
    assert bsvc.remove_listing(b.id, la.id) is False
    # A can.
    assert bsvc.remove_listing(a.id, la.id) is True


def test_match_and_route_to_brokers(tmp_db):
    a, _ = bsvc.create_broker("brokerA", "A社")
    b, _ = bsvc.create_broker("brokerB", "B社")
    bsvc.upsert_listing(a.id, _rent_listing(a.id, ward="新宿区"))
    bsvc.upsert_listing(a.id, _rent_listing(a.id, ward="新宿区", rent=130000))
    bsvc.upsert_listing(b.id, _rent_listing(b.id, ward="渋谷区"))

    criteria = {"prefecture": "東京都", "ward": "新宿", "rent_max_yen": 160000}
    scores = inq.match_brokers_for(criteria)
    # Only broker A has 新宿 inventory; ranked first with 2 matches.
    assert scores and scores[0][0] == a.id
    assert scores[0][1] == 2
    assert b.id not in {bid for bid, _ in scores}


def test_inquiry_fetch_is_per_broker_and_acks(tmp_db):
    a, _ = bsvc.create_broker("brokerA", "A社")
    b, _ = bsvc.create_broker("brokerB", "B社")
    cust, _ = __import__("fango.auth", fromlist=["create_agent"]).create_agent("cust", vendor="anon")
    iid = inq.create_inquiry(cust.id, {"ward": "新宿"}, matched_listing_ids=[])
    inserted = inq.route_inquiry(iid, [(a.id, 2)])
    assert inserted == 1

    # Routing again is idempotent (no duplicate route rows).
    assert inq.route_inquiry(iid, [(a.id, 2)]) == 0

    # Broker B sees nothing; broker A sees the inquiry once, then it's acked.
    assert inq.fetch_new_inquiries(b.id) == []
    first = inq.fetch_new_inquiries(a.id)
    assert len(first) == 1 and first[0]["inquiry_id"] == iid
    assert inq.fetch_new_inquiries(a.id) == []  # fetch-and-ack


def test_get_routed_inquiry_enforces_ownership(tmp_db):
    a, _ = bsvc.create_broker("brokerA", "A社")
    b, _ = bsvc.create_broker("brokerB", "B社")
    cust, _ = __import__("fango.auth", fromlist=["create_agent"]).create_agent("cust", vendor="anon")
    iid = inq.create_inquiry(cust.id, {"ward": "新宿"})
    inq.route_inquiry(iid, [(a.id, 1)])
    assert inq.get_routed_inquiry(iid, a.id) is not None
    assert inq.get_routed_inquiry(iid, b.id) is None  # not routed to B


def test_route_to_brokers_uses_shown_listings(tmp_db):
    # The customer-facing search may relax criteria, so routing must follow the
    # listings actually shown — not a strict re-match that would find nothing.
    from fango import forum_core
    from fango.auth import create_agent
    from fango.consult import autopost
    a, _ = bsvc.create_broker("brokerA", "A社")
    listing = bsvc.upsert_listing(a.id, _rent_listing(a.id))
    cust, _ = create_agent("cust", vendor="anon")
    thread, _q = forum_core.create_thread("chintai", "相談", "新宿で部屋を探しています。", cust.id)

    # A criteria with a search signal but a keyword that matches nothing — a
    # strict broker re-match would return 0; the shown listing must still route.
    routing = autopost.route_to_brokers(
        thread_id=thread.id, forum="chintai",
        criteria={"prefecture": "東京都", "keyword": "no-such-building-xyz"},
        customer_post_agent_id=cust.id,
        result_listing_ids=[listing.id],
    )
    assert routing["broker_count"] == 1 and routing["routed"] == 1
    # The broker sees that exact listing on its inquiry.
    got = inq.fetch_new_inquiries(a.id)
    assert len(got) == 1
    assert listing.id in [m["id"] for m in got[0]["matched_listings"]]


def test_broker_respond_scrubs_pii_but_allows_quotes(tmp_db, with_current_agent):
    # Broker replies are a vetted commercial tier: legitimate quotes / 内見 offers
    # must go through (NOT the anti-solicitation LLM moderator), while contact-info
    # PII is still blocked. No engine is installed → if broker_respond called the
    # LLM moderator it would fail closed; a passing quote proves it doesn't.
    import asyncio
    from fango import forum_core
    from fango.auth import create_agent
    from fango.brokers import inquiries as inq
    from fango.mcp_server import build_mcp

    broker, _ = bsvc.create_broker("brokerA", "A社")
    listing = bsvc.upsert_listing(broker.id, _rent_listing(broker.id))
    cust, _ = create_agent("cust", vendor="anon")
    thread, _q = forum_core.create_thread("chintai", "相談", "新宿で部屋を探しています。", cust.id)
    iid = inq.create_inquiry(cust.id, {"ward": "新宿"}, thread_id=thread.id,
                             forum="chintai", matched_listing_ids=[listing.id])
    inq.route_inquiry(iid, [(broker.id, 1)])
    with_current_agent(broker)
    mcp = build_mcp()

    def _respond(msg):
        res = asyncio.run(mcp.call_tool("broker_respond", {"inquiry_id": iid, "message": msg}))
        return res[1] if isinstance(res, tuple) else res

    # A legitimate quote / viewing offer is published (no LLM anti-solicitation gate).
    ok = _respond("内見可能です。初期費用の目安もお出しできます。")
    assert ok["posted"] is True
    # Contact info (email) is still blocked by the PII layer.
    blocked = _respond("直接ご連絡ください broker@example.com")
    assert blocked["posted"] is False


def test_route_to_brokers_by_area(tmp_db):
    # A broker that declares it covers 新宿 gets a 新宿 inquiry even when none of
    # the shown listings are its own (area-based routing, union with inventory).
    from fango import forum_core
    from fango.auth import create_agent
    from fango.consult import autopost

    a, _ = bsvc.create_broker("brokerA", "新宿不動産", areas=["新宿", "渋谷"])
    a_listing = bsvc.upsert_listing(a.id, _rent_listing(a.id, ward="新宿区"))
    # A shown listing owned by NOBODY (house pool) — inventory routing alone
    # would find no broker.
    house = ls.insert_listing({"building_name": "家主直", "prefecture": "東京都",
                               "ward": "新宿区", "rent_yen": 150000, "ad_status": "可"})
    cust, _ = create_agent("cust", vendor="anon")
    thread, _q = forum_core.create_thread("chintai", "相談", "新宿で部屋を探しています。", cust.id)

    routing = autopost.route_to_brokers(
        thread_id=thread.id, forum="chintai",
        criteria={"prefecture": "東京都", "ward": "新宿区"},
        customer_post_agent_id=cust.id,
        result_listing_ids=[house.id],   # shown listing is NOT broker A's
    )
    assert routing["broker_count"] == 1 and routing["routed"] == 1
    # The area-matched broker's inquiry carries ITS OWN matching listing.
    got = inq.fetch_new_inquiries(a.id)
    assert len(got) == 1
    assert a_listing.id in [m["id"] for m in got[0]["matched_listings"]]


def test_broker_auth_rejects_non_broker(tmp_db, with_current_agent):
    from fango.auth import create_agent
    from fango.brokers.tools import broker_auth
    normal, _ = create_agent("normal", vendor="anon")
    with_current_agent(normal)
    with pytest.raises(AuthError):
        broker_auth()
    # A broker passes.
    broker_agent, _ = bsvc.create_broker("brokerA", "A社")
    with_current_agent(broker_agent)
    assert broker_auth().id == broker_agent.id


def test_agent_name_shows_broker_brand(tmp_db):
    import fango.template_filters as tf
    tf._broker_brand._cache = None  # reset TTL cache for isolation
    a, _ = bsvc.create_broker("brokerA", "新宿不動産")
    assert tf.agent_name(a.id) == "新宿不動産"
    # A normal agent stays pseudonymous.
    from fango.auth import create_agent, pseudonym
    normal, _ = create_agent("normal", vendor="anon")
    tf._broker_brand._cache = None
    assert tf.agent_name(normal.id) == pseudonym(normal.id)


# ---------------------------------------------------------------------------
# Self-serve onboarding (claim → redeem) for brokers
# ---------------------------------------------------------------------------

def test_broker_claim_captures_profile(tmp_db):
    from fango import claims
    c = claims.create_claim(name="brokerX", vendor="broker", company="港不動産",
                            areas=["港区", "中央区"], license_no="東京都知事(1)第9号")
    assert c.company == "港不動産"
    got = claims.get_claim(c.code)
    assert got.company == "港不動産" and got.license_no.endswith("第9号")


def test_broker_claim_requires_company(tmp_db):
    from fango import claims
    with pytest.raises(claims.ClaimError):
        claims.create_claim(name="b", vendor="broker", company="  ")


def test_broker_redeem_creates_broker_profile(tmp_db):
    from fango import claims
    c = claims.create_claim(name="brokerY", vendor="broker", company="渋谷商事",
                            areas=["渋谷区"])
    agent, key = claims.redeem_claim(c.code)
    assert agent.vendor == "broker" and key
    assert bsvc.is_broker(agent.id)
    assert bsvc.get_broker(agent.id)["company"] == "渋谷商事"


def test_normal_redeem_is_not_a_broker(tmp_db):
    from fango import claims
    c = claims.create_claim(name="plainAgent", vendor="anthropic")
    agent, _ = claims.redeem_claim(c.code)
    assert not bsvc.is_broker(agent.id)


def test_onboard_and_connect_broker_routes(client):
    # Onboarding form renders.
    r = client.get("/onboard/broker")
    assert r.status_code == 200 and "商号" in r.text
    # Connect guide renders and points at the broker skill + key auth.
    r = client.get("/connect/broker")
    assert r.status_code == 200
    assert "broker-skill.md" in r.text and "agent_key" in r.text
    # Submitting the broker form mints a code; the result page shows it.
    r = client.post("/onboard/broker", data={
        "name": "brokerHttp", "company": "新宿不動産",
        "areas": "新宿区, 渋谷区", "captcha": "on",
    })
    assert r.status_code == 200 and "コード" in r.text
