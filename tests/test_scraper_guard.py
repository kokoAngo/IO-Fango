"""ScraperGuardMiddleware: bot-UA block + IP rate limit for public reads."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("ua", [
    "curl/7.88.1",
    "python-requests/2.31.0",
    "Wget/1.21",
    "Scrapy/2.11.0 (+https://scrapy.org)",
    "Go-http-client/1.1",
    "okhttp/4.10.0",
])
def test_bot_user_agent_blocked(client, listing_factory, ua):
    listing_factory()
    r = client.get("/listings/1", headers={"User-Agent": ua})
    assert r.status_code == 403


def test_real_browser_passes(client, listing_factory):
    listing_factory()
    r = client.get("/listings/1", headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                      "AppleWebKit/605.1.15 Safari/605.1.15",
    })
    assert r.status_code == 200


def test_ip_rate_limit_kicks_in(client, listing_factory):
    """Without an X-Agent-Key, 60/h/IP cap on /listings/<id> applies."""
    from fango.rate_limit import reset as reset_rl, PUBLIC_READ_IP
    reset_rl()
    listing_factory()
    ua = {"User-Agent": "Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/120.0"}
    for _ in range(PUBLIC_READ_IP.limit):
        r = client.get("/listings/1", headers=ua)
        assert r.status_code == 200
    r = client.get("/listings/1", headers=ua)
    assert r.status_code == 429
    assert "rate limit" in r.text.lower()


def test_agent_key_bypasses_ip_limit(client, listing_factory, agent_factory):
    """Carrying a valid agent key skips the IP gate — agents are
    expected to call read endpoints many times per hour."""
    from fango.rate_limit import reset as reset_rl, PUBLIC_READ_IP
    reset_rl()
    listing_factory()
    ag, key = agent_factory()
    ua = {"User-Agent": "Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/120.0",
          "X-Agent-Key": key}
    # Well past the public cap, all succeed.
    for _ in range(PUBLIC_READ_IP.limit * 2):
        r = client.get("/listings/1", headers=ua)
        assert r.status_code == 200, f"keyed call should not hit IP limit, got {r.status_code}"


def test_owner_ip_allowlist_bypasses_limit(client, listing_factory, monkeypatch):
    """IPs in FANGO_PUBLIC_READ_IP_ALLOWLIST refresh past the cap freely
    so the owner doesn't lock themselves out of their own site while
    debugging. TestClient reports client IP as 'testclient' by default."""
    from fango.rate_limit import reset as reset_rl, PUBLIC_READ_IP
    reset_rl()
    monkeypatch.setenv("FANGO_PUBLIC_READ_IP_ALLOWLIST", "testclient, 10.0.0.1")
    listing_factory()
    ua = {"User-Agent": "Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/120.0"}
    for _ in range(PUBLIC_READ_IP.limit + 5):
        r = client.get("/listings/1", headers=ua)
        assert r.status_code == 200, f"allow-listed IP should not be capped, got {r.status_code}"


def test_owner_allowlist_still_blocks_bot_ua(client, listing_factory, monkeypatch):
    """Allow-list relaxes the rate cap, not the bot-UA gate — curl from
    the owner IP is still refused."""
    monkeypatch.setenv("FANGO_PUBLIC_READ_IP_ALLOWLIST", "testclient")
    listing_factory()
    r = client.get("/listings/1", headers={"User-Agent": "curl/8.0"})
    assert r.status_code == 403


def test_root_and_assets_unprotected(client):
    """Home / static assets / onboard etc. must stay open to anyone."""
    r = client.get("/", headers={"User-Agent": "curl/8.0"})
    # Not in protected prefixes — guard doesn't apply. Status will be a
    # 200 redirect-ish or HTML; the point is NOT a 403/429.
    assert r.status_code not in (403, 429)
