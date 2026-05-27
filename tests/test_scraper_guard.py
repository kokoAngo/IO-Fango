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
    """Without an X-Agent-Key, 30/h/IP cap on /listings/<id> applies."""
    from fango.rate_limit import reset as reset_rl
    reset_rl()
    listing_factory()
    ua = {"User-Agent": "Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/120.0"}
    # 30 successful requests, then 31st blocked.
    for _ in range(30):
        r = client.get("/listings/1", headers=ua)
        assert r.status_code == 200
    r = client.get("/listings/1", headers=ua)
    assert r.status_code == 429
    assert "rate limit" in r.text.lower()


def test_agent_key_bypasses_ip_limit(client, listing_factory, agent_factory):
    """Carrying a valid agent key skips the IP gate — agents are
    expected to call read endpoints many times per hour."""
    from fango.rate_limit import reset as reset_rl
    reset_rl()
    listing_factory()
    ag, key = agent_factory()
    ua = {"User-Agent": "Mozilla/5.0 (X11; Linux) Gecko/20100101 Firefox/120.0",
          "X-Agent-Key": key}
    # 60 requests, well past the 30/h cap, all succeed.
    for _ in range(60):
        r = client.get("/listings/1", headers=ua)
        assert r.status_code == 200, f"keyed call should not hit IP limit, got {r.status_code}"


def test_root_and_assets_unprotected(client):
    """Home / static assets / onboard etc. must stay open to anyone."""
    r = client.get("/", headers={"User-Agent": "curl/8.0"})
    # Not in protected prefixes — guard doesn't apply. Status will be a
    # 200 redirect-ish or HTML; the point is NOT a 403/429.
    assert r.status_code not in (403, 429)
