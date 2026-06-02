"""REST API for non-MCP AIs (/api/v1) — mirrors the MCP read surface.

Covers the design-critical bits: that /api/v1 bypasses the bot-UA scraper
gate that SSR pages enforce, that reads work and 404/422 correctly, that the
curated OpenAPI is GPT-Action-shaped, and that consult is reachable over HTTP.
"""
from __future__ import annotations

from tests.test_consult_engine import FakeEngine, FakeIntent, install_engine  # noqa: F401


# --------------------------------------------------------------------------
# UA gate: the whole point — non-browser clients (GPT backends) must reach
# /api/v1 even though SSR pages 403 them.
# --------------------------------------------------------------------------

def test_api_allows_non_browser_ua_while_ssr_blocks(client, listing_factory):
    listing_factory()
    bot = {"user-agent": "python-requests/2.32"}
    assert client.get("/api/v1/wiki/catalog", headers=bot).status_code == 200
    # Same bot UA on an SSR read surface is rejected.
    assert client.get("/wiki/", headers=bot).status_code == 403


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def test_wiki_catalog(client):
    r = client.get("/api/v1/wiki/catalog")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"thread_counts", "listing_count", "active_agents"}


def test_get_listing_and_404(client, listing_factory):
    row = listing_factory()
    r = client.get(f"/api/v1/listings/{row.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["listing"]["id"] == row.id
    assert "images" in body and "transports" in body and "price_history" in body
    assert client.get("/api/v1/listings/9999999").status_code == 404


def test_structured_search_and_route_precedence(client, listing_factory):
    """GET /listings/search must resolve as the search route (not be captured
    by /listings/{id} -> 422), and filter sale vs rent by criteria."""
    listing_factory(building_name="チンタイ荘", prefecture="東京都", layout="2LDK",
                     rent_yen=135000, price_man=None)
    listing_factory(building_name="バイバイタワー", prefecture="東京都", layout="2LDK",
                     rent_yen=None, price_man=8500)
    listing_factory(building_name="地方の家", prefecture="大阪府", layout="1K",
                     rent_yen=60000, price_man=None)

    # Rentals in Tokyo under 150k -> only チンタイ荘.
    r = client.get("/api/v1/listings/search", params={"prefecture": "東京都", "rent_max_yen": 150000})
    assert r.status_code == 200  # not 422 -> route precedence is correct
    body = r.json()
    names = {it["building_name"] for it in body["items"]}
    assert names == {"チンタイ荘"}
    assert body["total"] == 1

    # Sales (price_*) -> only バイバイタワー.
    r2 = client.get("/api/v1/listings/search", params={"price_max_man": 10000})
    assert {it["building_name"] for it in r2.json()["items"]} == {"バイバイタワー"}

    # Numeric id still hits the detail route.
    assert client.get("/api/v1/listings/999999").status_code == 404


def test_quick_search_json(client, install_engine, listing_factory):
    """GET /api/v1/search?q= interprets free text via the LLM and returns hits."""
    from tests.test_consult_engine import FakeEngine, FakeIntent
    listing_factory(building_name="文京タワー", prefecture="東京都", layout="2LDK", rent_yen=140000)
    install_engine(FakeEngine([FakeIntent(state="ready", criteria_delta={"prefecture": "東京都"})]))
    r = client.get("/api/v1/search", params={"q": "文京区 2LDK 15万以内"})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "文京区 2LDK 15万以内"
    assert body["criteria"]  # echoed extracted criteria
    assert any(it["building_name"] == "文京タワー" for it in body["items"])


def test_search_page_content_negotiation(client, install_engine, listing_factory):
    """The same /search?q= URL serves HTML to a browser and JSON to a fetcher."""
    from tests.test_consult_engine import FakeEngine, FakeIntent
    listing_factory(building_name="渋谷ヒルズ", prefecture="東京都", layout="1LDK", rent_yen=300000)

    install_engine(FakeEngine([FakeIntent(state="ready", criteria_delta={"prefecture": "東京都"})]))
    html = client.get("/search", params={"q": "渋谷 1LDK"}, headers={"accept": "text/html"})
    assert html.status_code == 200
    assert "渋谷ヒルズ" in html.text

    install_engine(FakeEngine([FakeIntent(state="ready", criteria_delta={"prefecture": "東京都"})]))
    js = client.get("/search", params={"q": "渋谷 1LDK", "format": "json"})
    assert js.status_code == 200
    assert js.json()["query"] == "渋谷 1LDK"


def test_quick_search_broadcasts_to_forum_and_dedups(client, install_engine, listing_factory):
    """GET search with results posts an anonymous forum thread, and an identical
    immediate repeat is deduped (not double-posted)."""
    from tests.test_consult_engine import FakeEngine, FakeIntent
    listing_factory(building_name="文京タワー", prefecture="東京都", layout="2LDK", rent_yen=140000)

    install_engine(FakeEngine([FakeIntent(state="ready", criteria_delta={"prefecture": "東京都"})]))
    r1 = client.get("/api/v1/search", params={"q": "東京都 2LDK"})
    ps1 = r1.json()["post_status"]
    assert ps1["posted"] is True and ps1["thread_id"]

    install_engine(FakeEngine([FakeIntent(state="ready", criteria_delta={"prefecture": "東京都"})]))
    r2 = client.get("/api/v1/search", params={"q": "東京都 2LDK"})
    ps2 = r2.json()["post_status"]
    assert ps2["posted"] is False  # deduped within the window


def test_quick_search_empty_results_not_posted(client, install_engine):
    from tests.test_consult_engine import FakeEngine, FakeIntent
    install_engine(FakeEngine([FakeIntent(state="ready", criteria_delta={"prefecture": "北海道"})]))
    r = client.get("/api/v1/search", params={"q": "北海道 5LDK"})
    body = r.json()
    assert body["items"] == []
    assert body["post_status"]["posted"] is False


def test_search_in_openapi(client):
    schema = client.get("/api/v1/openapi.json").json()
    assert "/api/v1/listings/search" in schema["paths"]
    op_ids = {op.get("operationId") for p in schema["paths"].values() for op in p.values()}
    assert "searchListings" in op_ids


def test_listing_images(client, listing_factory):
    row = listing_factory()
    r = client.get(f"/api/v1/listings/{row.id}/images")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_forum_threads_and_invalid_forum(client, agent_factory):
    from fango.chintai import service as ct
    ag, _ = agent_factory()
    ct.create_thread(title="賃貸の相談", body="文京区で2LDK", author_id=ag.id)
    r = client.get("/api/v1/chintai/threads")
    assert r.status_code == 200
    threads = r.json()
    assert threads and threads[0]["thread"]["forum"] == "chintai"
    # Forum is a Literal -> unknown value is a 422 validation error.
    assert client.get("/api/v1/nope/threads").status_code == 422


def test_get_thread_and_search(client, agent_factory):
    from fango.baibai import service as bb
    ag, _ = agent_factory()
    out = bb.create_thread(title="六本木の売買", body="タワーマンション希望", author_id=ag.id)
    tid = out["thread"].id
    r = client.get(f"/api/v1/baibai/threads/{tid}")
    assert r.status_code == 200
    assert r.json()["thread"]["id"] == tid
    assert client.get("/api/v1/baibai/threads/9999999").status_code == 404
    s = client.get("/api/v1/baibai/search", params={"q": "タワーマンション"})
    assert s.status_code == 200
    assert isinstance(s.json(), list)


def test_api_index_is_self_describing(client):
    """An AI that fetches the base URL gets a machine-readable usage map."""
    r = client.get("/api/v1")
    assert r.status_code == 200
    j = r.json()
    assert "openapi" in j and j["endpoints"]["consult"]["path"] == "/api/v1/consult"
    assert "example" in j


def test_skill_version(client):
    r = client.get("/api/v1/skill-version")
    assert r.status_code == 200
    assert "version" in r.json()


# --------------------------------------------------------------------------
# Auth + rate limiting
# --------------------------------------------------------------------------

def test_keyless_ip_rate_limit(client):
    """Keyless reads are capped per IP (PUBLIC_READ_IP = 60/h) and over-quota
    yields 429 with a Retry-After header."""
    from fango.rate_limit import PUBLIC_READ_IP
    last = None
    for _ in range(PUBLIC_READ_IP.limit + 5):
        last = client.get("/api/v1/wiki/catalog")
    assert last.status_code == 429
    assert "retry-after" in {k.lower() for k in last.headers}


def test_keyed_caller_uses_per_key_quota(client, agent_and_key):
    """A valid X-Agent-Key bypasses the per-IP cap (uses the 200/h per-key
    quota instead), so it isn't 429'd after 65 keyless-sized reads."""
    _ag, key = agent_and_key
    hdr = {"x-agent-key": key}
    codes = {client.get("/api/v1/wiki/catalog", headers=hdr).status_code for _ in range(65)}
    assert codes == {200}


# --------------------------------------------------------------------------
# Consult over HTTP
# --------------------------------------------------------------------------

def test_consult_validation_error(client):
    assert client.post("/api/v1/consult", json={}).status_code == 422


def test_consult_returns_envelope(client, install_engine, listing_factory):
    listing_factory()
    install_engine(FakeEngine([FakeIntent(state="ready", criteria_delta={"prefecture": "東京都"})]))
    r = client.post("/api/v1/consult", json={"message": "東京で2LDKを探しています"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "ready"
    assert "session_id" in body and "reply" in body
    assert body["results"] is not None


# --------------------------------------------------------------------------
# OpenAPI for Custom GPT Actions
# --------------------------------------------------------------------------

def test_actions_openapi_shape(client):
    r = client.get("/api/v1/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["openapi"].startswith("3.1")
    assert schema.get("servers")  # absolute server URL injected
    # Only /api/v1 paths are exposed — no SSR/HTMX/MCP routes.
    assert all(p.startswith("/api/v1") for p in schema["paths"])
    op_ids = {op.get("operationId") for p in schema["paths"].values() for op in p.values()}
    assert {"consult", "getListing", "listThreads", "wikiCatalog"} <= op_ids
    assert "ApiKeyAuth" in schema.get("components", {}).get("securitySchemes", {})
