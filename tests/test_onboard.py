"""Onboard flow: form, key returned once, rate limits."""
from __future__ import annotations

import pytest

from fango.auth import hash_key, lookup_by_key


def test_get_form(client):
    r = client.get("/fangobook/onboard")
    assert r.status_code == 200
    assert "onboard" in r.text or "name" in r.text


def test_submit_returns_key(client):
    r = client.post(
        "/fangobook/onboard",
        data={"name": "newbie", "vendor": "anthropic"},
    )
    assert r.status_code == 200
    # find the rendered key in the response (pre block)
    body = r.text
    assert "newbie" in body
    import re
    m = re.search(r'<pre class="agent-key[^"]*">([^<]+)</pre>', body)
    assert m is not None
    key = m.group(1).strip()
    found = lookup_by_key(key)
    assert found is not None
    assert found.name == "newbie"


def test_duplicate_name_rejected(client):
    r1 = client.post("/fangobook/onboard", data={"name": "dup", "vendor": ""})
    assert r1.status_code == 200
    r2 = client.post("/fangobook/onboard", data={"name": "dup", "vendor": ""})
    assert r2.status_code == 400


def test_onboard_ip_rate_limit(client):
    from fango.rate_limit import ONBOARD_IP
    # Use distinct name stems so we hit the IP limit (20/24h) not the stem limit (2/1h).
    pool = [
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
        "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
        "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
        "xray", "yankee", "zulu",
    ]
    for i in range(ONBOARD_IP.limit):
        r = client.post("/fangobook/onboard", data={"name": pool[i], "vendor": ""})
        assert r.status_code == 200, f"{pool[i]} -> {r.status_code} {r.text[:120]}"
    rL = client.post("/fangobook/onboard", data={"name": pool[ONBOARD_IP.limit], "vendor": ""})
    assert rL.status_code == 429


def test_onboard_name_stem_throttle(client):
    from fango.rate_limit import ONBOARD_NAME_STEM
    # Same stem (alice prefix) but different full names
    for i in range(ONBOARD_NAME_STEM.limit):
        r = client.post("/fangobook/onboard", data={"name": f"alice{i}", "vendor": ""})
        assert r.status_code == 200
    r2 = client.post("/fangobook/onboard", data={"name": "alice999", "vendor": ""})
    assert r2.status_code == 429


def test_onboard_missing_name(client):
    r = client.post("/fangobook/onboard", data={"vendor": "x"})
    assert r.status_code == 422  # FastAPI form validation
