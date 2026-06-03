"""HOMES external-link lookup + link-preview attach/render/surfacing.

The browser lookup (Playwright → HOMES) is stubbed, so these tests never touch
the network. One live test against real HOMES is included but skipped unless
FANGO_LIVE_HOMES=1.
"""
from __future__ import annotations

import os

import pytest

from fango import forum_core
from fango.listings import enrich, external_lookup

# What a successful browser lookup returns.
_FOUND = {
    "url": "https://www.homes.co.jp/chintai/room/abc123def456/",
    "source": "homes",
    "image": "https://image1.homes.jp/smallimg/x.jpg",
    "title": "GENOVIA南麻布green veil[1K/13.3万円]",
}


# --------------------------------------------------------------------------
# external_lookup: pure helpers (no browser)
# --------------------------------------------------------------------------
class TestExternalLookup:
    def test_source_of_url(self):
        assert external_lookup.source_of_url("https://www.homes.co.jp/chintai/room/x/") == "homes"
        assert external_lookup.source_of_url("https://suumo.jp/chintai/bc_1/") == "suumo"
        assert external_lookup.source_of_url("https://example.com/x") is None

    def test_source_url_shortcut_skips_browser(self, monkeypatch):
        # If we already have a HOMES detail URL, no browser is launched.
        monkeypatch.setattr(external_lookup, "_run_isolated",
                            lambda *a, **k: pytest.fail("browser should not run"))
        out = external_lookup.find_listing("X", source_url="https://www.homes.co.jp/chintai/room/z/")
        assert out["url"] == "https://www.homes.co.jp/chintai/room/z/"
        assert out["source"] == "homes"

    def test_norm_folds_fullwidth(self):
        # 'Fiore' (ascii) and 'Ｆｉｏｒｅ' (full-width) compare equal.
        assert external_lookup._norm("Fiore南麻布") == external_lookup._norm("Ｆｉｏｒｅ南麻布　")


# --------------------------------------------------------------------------
# forum_core: attach_link_preview round-trip + get_thread exposure
# --------------------------------------------------------------------------
@pytest.fixture
def a_post(tmp_db, agent_factory):
    from fango.baibai import service as bb
    ag, _ = agent_factory()
    out = bb.create_thread(title="t", body="b", author_id=ag.id, agent_created_at=ag.created_at)
    return out["thread"].id, out["post"].id


class TestAttachLinkPreview:
    def test_round_trip_and_idempotent(self, a_post):
        _, post_id = a_post
        pid = forum_core.attach_link_preview(
            post_id, "https://www.homes.co.jp/x/",
            image_url="https://image1.homes.jp/p.jpg", title="物件A", source="homes",
        )
        assert pid > 0
        again = forum_core.attach_link_preview(post_id, "https://www.homes.co.jp/x/")
        assert again == pid  # idempotent per (post, url)
        rows = forum_core.list_link_previews(post_id)
        assert len(rows) == 1
        assert rows[0]["image_url"] == "https://image1.homes.jp/p.jpg"
        assert rows[0]["source"] == "homes"

    def test_get_thread_exposes_link_previews(self, a_post):
        from fango.baibai import service as bb
        thread_id, post_id = a_post
        forum_core.attach_link_preview(post_id, "https://www.homes.co.jp/r/",
                                       image_url="https://image1.homes.jp/x.jpg", title="T", source="homes")
        data = bb.get_thread(thread_id)
        first = data["posts"][0]
        assert getattr(first, "link_previews", None)
        assert first.link_previews[0]["url"] == "https://www.homes.co.jp/r/"


# --------------------------------------------------------------------------
# enrich: orchestration (kill-switch, browser-lookup stub, cache, attach)
# --------------------------------------------------------------------------
def _stub_find(monkeypatch, *, found, calls=None):
    def fake(name, **kw):
        if calls is not None:
            calls.append(name)
        return found
    monkeypatch.setattr(external_lookup, "find_listing", fake)


class TestEnrich:
    def test_disabled_is_noop(self, a_post, monkeypatch):
        _, post_id = a_post
        monkeypatch.delenv("FANGO_EXTERNAL_LOOKUP_ENABLED", raising=False)
        _stub_find(monkeypatch, found=_FOUND)  # would succeed, but kill-switch wins
        assert enrich.enrich_post_with_listing_link(post_id, {"id": 999, "building_name": "X"}) is None
        assert forum_core.list_link_previews(post_id) == []

    def test_attaches_and_caches(self, a_post, listing_factory, monkeypatch):
        _, post_id = a_post
        monkeypatch.setenv("FANGO_EXTERNAL_LOOKUP_ENABLED", "1")
        listing = listing_factory(building_name="GENOVIA南麻布", url=None)
        calls: list = []
        _stub_find(monkeypatch, found=_FOUND, calls=calls)

        out = enrich.enrich_post_with_listing_link(post_id, {"id": listing.id, "building_name": "GENOVIA南麻布"})
        assert out["attached"] is True
        previews = forum_core.list_link_previews(post_id)
        assert previews[0]["url"] == _FOUND["url"]
        assert previews[0]["image_url"] == _FOUND["image"]   # HOMES image hotlinked
        assert previews[0]["source"] == "homes"
        assert len(calls) == 1

        # Second call, same listing → cache hit, no second browser lookup.
        out2 = enrich.enrich_post_with_listing_link(post_id, {"id": listing.id})
        assert out2["cached"] is True
        assert len(calls) == 1

    def test_negative_result_cached(self, a_post, listing_factory, monkeypatch):
        _, post_id = a_post
        monkeypatch.setenv("FANGO_EXTERNAL_LOOKUP_ENABLED", "1")
        listing = listing_factory(building_name="ナイショ", url=None)
        calls: list = []
        _stub_find(monkeypatch, found=None, calls=calls)
        out = enrich.enrich_post_with_listing_link(post_id, {"id": listing.id, "building_name": "ナイショ"})
        assert out["attached"] is False
        assert forum_core.list_link_previews(post_id) == []
        enrich.enrich_post_with_listing_link(post_id, {"id": listing.id, "building_name": "ナイショ"})
        assert len(calls) == 1  # negative cached → no re-lookup


class TestBlockFallback:
    """When HOMES blocks the browser, recover the URL via DuckDuckGo."""

    _CHALLENGE = ('<html><head><title>Human Verification</title>'
                  '<script src="https://x.token.awswaf.com/challenge.js"></script>'
                  '<script>window.gokuProps={};</script></head></html>')
    _NORMAL = '<html><head><title>【ホームズ】賃貸</title></head><body>…results…</body></html>'

    _DDG = (
        '<div class="result"><a class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.homes.co.jp%2Fchintai%2Froom%2Fabc123%2F&rut=z">'
        'GENOVIA南麻布 - ホームズ</a></div>'
        '<div class="result"><a class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fsuumo.jp%2Fchintai%2Fjnc_1%2F">other</a></div>'
    )

    def test_is_block_markup(self):
        assert external_lookup._is_block_markup("Human Verification", "") is True
        assert external_lookup._is_block_markup("", self._CHALLENGE) is True
        assert external_lookup._is_block_markup("【ホームズ】賃貸", self._NORMAL) is False

    def test_ddg_parse_extracts_homes_link(self):
        urls = external_lookup._ddg_parse(self._DDG)
        assert urls[0] == "https://www.homes.co.jp/chintai/room/abc123/"  # decoded, HOMES, first
        assert all("suumo" not in u for u in urls)

    def test_ddg_parse_none(self):
        assert external_lookup._ddg_parse("<div>no links here</div>") == []

    def test_find_listings_falls_back_on_block(self, monkeypatch):
        # Browser reports the name blocked; DDG recovers the URL.
        monkeypatch.setattr(external_lookup, "_run_isolated",
                            lambda fn, *a, **k: {"results": {}, "blocked": ["B"]})
        monkeypatch.setattr(external_lookup, "_ddg_find_homes",
                            lambda name: {"url": "https://www.homes.co.jp/chintai/room/z/",
                                          "source": "homes", "image": None, "title": None})
        out = external_lookup.find_listings(["B"])
        assert out["B"]["url"] == "https://www.homes.co.jp/chintai/room/z/"
        assert out["B"]["image"] is None

    def test_no_fallback_when_browser_succeeds(self, monkeypatch):
        found = {"url": "https://www.homes.co.jp/chintai/room/ok/", "source": "homes",
                 "image": "https://image1.homes.jp/a.jpg", "title": "T"}
        monkeypatch.setattr(external_lookup, "_run_isolated",
                            lambda fn, *a, **k: {"results": {"B": found}, "blocked": []})
        monkeypatch.setattr(external_lookup, "_ddg_find_homes",
                            lambda name: pytest.fail("DDG fallback must not run when HOMES succeeded"))
        out = external_lookup.find_listings(["B"])
        assert out["B"] == found


class TestEnrichBatch:
    def test_enriches_up_to_three_in_one_session(self, a_post, listing_factory, monkeypatch):
        _, post_id = a_post
        monkeypatch.setenv("FANGO_EXTERNAL_LOOKUP_ENABLED", "1")
        a = listing_factory(building_name="GENOVIA南麻布", url=None)
        b = listing_factory(building_name="コンフォリア下北沢", url=None)
        c = listing_factory(building_name="存在しないビル", url=None)  # HOMES miss
        calls: list = []

        def fake_find_listings(names):
            calls.append(list(names))
            return {
                "GENOVIA南麻布": {"url": "https://www.homes.co.jp/chintai/room/aaa/",
                                  "source": "homes", "image": "https://image1.homes.jp/a.jpg", "title": "A"},
                "コンフォリア下北沢": {"url": "https://www.homes.co.jp/chintai/room/bbb/",
                                      "source": "homes", "image": "https://image1.homes.jp/b.jpg", "title": "B"},
                "存在しないビル": None,
            }
        monkeypatch.setattr(external_lookup, "find_listings", fake_find_listings)

        enrich.enrich_post_with_listings(post_id, [
            {"id": a.id, "building_name": "GENOVIA南麻布"},
            {"id": b.id, "building_name": "コンフォリア下北沢"},
            {"id": c.id, "building_name": "存在しないビル"},
        ])
        urls = {p["url"] for p in forum_core.list_link_previews(post_id)}
        assert urls == {"https://www.homes.co.jp/chintai/room/aaa/",
                        "https://www.homes.co.jp/chintai/room/bbb/"}  # 2 hits, miss skipped
        assert len(calls) == 1                       # ONE batch browser session
        assert set(calls[0]) == {"GENOVIA南麻布", "コンフォリア下北沢", "存在しないビル"}

    def test_uses_cache_and_only_looks_up_uncached(self, a_post, listing_factory, monkeypatch):
        _, post_id = a_post
        monkeypatch.setenv("FANGO_EXTERNAL_LOOKUP_ENABLED", "1")
        cached = listing_factory(building_name="Cached", url=None)
        fresh = listing_factory(building_name="Fresh", url=None)
        # Pre-warm one via the single resolver.
        _stub_find(monkeypatch, found=_FOUND)
        enrich.resolve_external_link(cached.id, "Cached")

        calls: list = []
        def fake_find_listings(names):
            calls.append(list(names))
            return {"Fresh": {"url": "https://www.homes.co.jp/chintai/room/fff/", "source": "homes",
                              "image": None, "title": "F"}}
        monkeypatch.setattr(external_lookup, "find_listings", fake_find_listings)

        enrich.enrich_post_with_listings(post_id, [
            {"id": cached.id, "building_name": "Cached"},
            {"id": fresh.id, "building_name": "Fresh"},
        ])
        # Cached one attached from cache; only the uncached name hit the browser.
        assert calls == [["Fresh"]]
        urls = {p["url"] for p in forum_core.list_link_previews(post_id)}
        assert _FOUND["url"] in urls and "https://www.homes.co.jp/chintai/room/fff/" in urls


# --------------------------------------------------------------------------
# The discovered link is surfaced to the agent (consult results + get_listing)
# --------------------------------------------------------------------------
class TestSurfacing:
    def test_resolve_disabled_returns_none(self, tmp_db, listing_factory, monkeypatch):
        monkeypatch.delenv("FANGO_EXTERNAL_LOOKUP_ENABLED", raising=False)
        listing = listing_factory(building_name="X")
        assert enrich.resolve_external_link(listing.id, "X") is None

    def test_resolve_returns_link(self, tmp_db, listing_factory, monkeypatch):
        monkeypatch.setenv("FANGO_EXTERNAL_LOOKUP_ENABLED", "1")
        _stub_find(monkeypatch, found=_FOUND)
        listing = listing_factory(building_name="GENOVIA南麻布", url=None)
        link = enrich.resolve_external_link(listing.id, "GENOVIA南麻布")
        assert link["url"] == _FOUND["url"]
        assert link["source"] == "homes"
        assert link["image_url"] == _FOUND["image"]
        assert link["title"] == _FOUND["title"]

    def test_get_listing_payload_includes_cached_external_url(self, tmp_db, listing_factory, monkeypatch):
        from fango.listings.tools import get_listing_payload
        monkeypatch.setenv("FANGO_EXTERNAL_LOOKUP_ENABLED", "1")
        _stub_find(monkeypatch, found=_FOUND)
        listing = listing_factory(building_name="GENOVIA南麻布", url=None)  # no images
        # get_listing is cache-only (no inline browser). Warm the cache first.
        enrich.resolve_external_link(listing.id, "GENOVIA南麻布")
        payload = get_listing_payload(listing.id)
        assert payload["external_url"] == _FOUND["url"]
        assert payload["external_source"] == "homes"

    def test_get_listing_payload_no_lookup_when_cold(self, tmp_db, listing_factory, monkeypatch):
        from fango.listings.tools import get_listing_payload
        monkeypatch.setenv("FANGO_EXTERNAL_LOOKUP_ENABLED", "1")
        # find_listing must NOT be called from the read path (cache-only).
        monkeypatch.setattr(external_lookup, "find_listing",
                            lambda *a, **k: pytest.fail("get_listing must stay cache-only"))
        listing = listing_factory(building_name="未キャッシュ", url=None)
        payload = get_listing_payload(listing.id)
        assert "external_url" not in payload


# --------------------------------------------------------------------------
# SSR: the link-preview card renders on the thread page
# --------------------------------------------------------------------------
class TestRender:
    def test_thread_page_shows_card(self, client, tmp_db, agent_factory):
        from fango.baibai import service as bb
        ag, _ = agent_factory()
        out = bb.create_thread(title="t", body="b", author_id=ag.id, agent_created_at=ag.created_at)
        thread_id, post_id = out["thread"].id, out["post"].id
        forum_core.attach_link_preview(
            post_id, _FOUND["url"], image_url=_FOUND["image"],
            title=_FOUND["title"], source="homes",
        )
        r = client.get(f"/baibai/t/{thread_id}")
        assert r.status_code == 200
        assert "link-preview-card" in r.text
        assert _FOUND["image"] in r.text
        assert "GENOVIA南麻布" in r.text


# --------------------------------------------------------------------------
# Live HOMES (real browser + WAF). Opt-in only.
# --------------------------------------------------------------------------
@pytest.mark.skipif(os.environ.get("FANGO_LIVE_HOMES") != "1",
                    reason="set FANGO_LIVE_HOMES=1 to hit real HOMES via a browser")
def test_live_homes_lookup():
    found = external_lookup.find_listing("GENOVIA南麻布")
    assert found and "homes.co.jp/chintai/room/" in found["url"]
