"""SUUMO/HOMES external-link lookup + OGP unfurl + link-preview rendering.

All network is stubbed — these tests never reach the internet.
"""
from __future__ import annotations

import base64

import pytest

from fango import forum_core, unfurl
from fango.listings import enrich, external_lookup

# A real 1x1 PNG so the magic-byte sniff in uploads accepts it.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# --------------------------------------------------------------------------
# unfurl: OGP parsing + SSRF guard (pure, no network)
# --------------------------------------------------------------------------
class TestUnfurl:
    def test_parse_ogp_prefers_og_over_title(self):
        html = (
            '<meta property="og:image" content="https://img.x/a.jpg">'
            '<meta property="og:title" content="グランド○○ 2LDK">'
            "<title>ignored</title>"
        )
        out = unfurl.parse_ogp(html, "https://www.homes.co.jp/p/")
        assert out["image"] == "https://img.x/a.jpg"
        assert out["title"] == "グランド○○ 2LDK"

    def test_parse_ogp_twitter_and_relative_image(self):
        html = (
            '<meta name="twitter:image" content="/img/b.jpg">'
            "<title>Fallback Title</title>"
        )
        out = unfurl.parse_ogp(html, "https://suumo.jp/chintai/x/")
        assert out["image"] == "https://suumo.jp/img/b.jpg"  # resolved
        assert out["title"] == "Fallback Title"              # <title> fallback

    def test_parse_ogp_no_image(self):
        out = unfurl.parse_ogp("<p>nothing</p>", "https://example.com/")
        assert out["image"] is None

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/x", "http://10.0.0.1/", "http://169.254.1.1/",
        "http://192.168.1.1/", "ftp://example.com/x", "https://",
    ])
    def test_ssrf_guard_rejects(self, url):
        assert unfurl._is_safe_url(url) is False


# --------------------------------------------------------------------------
# external_lookup: detail-link extraction + search (stubbed fetch_html)
# --------------------------------------------------------------------------
class TestExternalLookup:
    def test_extract_homes_relative(self):
        html = '<a href="/chintai/room/abc/">A</a><a href="https://www.homes.co.jp/mansion/b-1/">B</a>'
        url = external_lookup._extract_first_listing(html, "homes", "https://www.homes.co.jp/list/")
        assert url == "https://www.homes.co.jp/chintai/room/abc/"

    def test_extract_suumo_absolute(self):
        html = '<a href="https://suumo.jp/chintai/jnc_123/">x</a>'
        url = external_lookup._extract_first_listing(html, "suumo", "https://suumo.jp/s/")
        assert url == "https://suumo.jp/chintai/jnc_123/"

    def test_source_url_shortcut(self):
        out = external_lookup.find_external_url("X", source_url="https://suumo.jp/chintai/jnc_9/")
        assert out == {"url": "https://suumo.jp/chintai/jnc_9/", "source": "suumo"}

    def test_prefers_homes_over_suumo(self, monkeypatch):
        def fake_fetch(url):
            if "homes.co.jp" in url:
                return '物件グランド東京 <a href="/chintai/room/h1/">x</a>'
            return '物件グランド東京 <a href="https://suumo.jp/chintai/jnc_1/">y</a>'
        monkeypatch.setattr(unfurl, "fetch_html", fake_fetch)
        out = external_lookup.find_external_url("グランド東京")
        assert out["source"] == "homes"
        assert "homes.co.jp" in out["url"]

    def test_rejects_when_name_absent(self, monkeypatch):
        # Results page doesn't mention the building → don't trust the link.
        monkeypatch.setattr(unfurl, "fetch_html",
                            lambda url: '<a href="/chintai/room/z/">別物件</a>')
        assert external_lookup.find_external_url("実在しないビル名XYZ") is None


# --------------------------------------------------------------------------
# uploads.save_image_bytes (self-hosting path)
# --------------------------------------------------------------------------
class TestSaveImageBytes:
    def test_dedup(self, tmp_db, tmp_path, monkeypatch):
        from fango import uploads
        monkeypatch.setattr(uploads, "UPLOADS_DIR", tmp_path / "up")
        first = uploads.save_image_bytes(_PNG)
        assert first["url"].startswith("/uploads/") and first["url"].endswith(".png")
        assert first["reused"] is False
        second = uploads.save_image_bytes(_PNG)
        assert second["sha256"] == first["sha256"]
        assert second["reused"] is True


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
            image_url="/uploads/abc.png", title="物件A", source="homes",
        )
        assert pid > 0
        again = forum_core.attach_link_preview(post_id, "https://www.homes.co.jp/x/")
        assert again == pid  # idempotent per (post, url)
        rows = forum_core.list_link_previews(post_id)
        assert len(rows) == 1
        assert rows[0]["image_url"] == "/uploads/abc.png"
        assert rows[0]["source"] == "homes"

    def test_get_thread_exposes_link_previews(self, a_post):
        from fango.baibai import service as bb
        thread_id, post_id = a_post
        forum_core.attach_link_preview(post_id, "https://suumo.jp/chintai/jnc_1/",
                                       image_url="/uploads/x.png", title="T", source="suumo")
        data = bb.get_thread(thread_id)
        first = data["posts"][0]
        assert getattr(first, "link_previews", None)
        assert first.link_previews[0]["url"] == "https://suumo.jp/chintai/jnc_1/"


# --------------------------------------------------------------------------
# enrich: end-to-end orchestration (kill-switch, cache, attach)
# --------------------------------------------------------------------------
class TestEnrich:
    def _wire(self, monkeypatch, *, found, calls=None):
        def fake_find(name, **kw):
            if calls is not None:
                calls.append(name)
            return found
        monkeypatch.setattr(external_lookup, "find_external_url", fake_find)
        monkeypatch.setattr(unfurl, "fetch_ogp",
                            lambda url: {"image": "https://img/x.jpg", "title": "OGP T"})
        monkeypatch.setattr(enrich, "_self_host_image", lambda u: "/uploads/hosted.png")

    def test_disabled_is_noop(self, a_post, monkeypatch):
        _, post_id = a_post
        monkeypatch.delenv("FANGO_EXTERNAL_LOOKUP_ENABLED", raising=False)
        # Even if lookup would succeed, the kill-switch short-circuits.
        self._wire(monkeypatch, found={"url": "https://www.homes.co.jp/x/", "source": "homes"})
        assert enrich.enrich_post_with_listing_link(post_id, {"id": 999, "building_name": "X"}) is None
        assert forum_core.list_link_previews(post_id) == []

    def test_attaches_and_caches(self, a_post, listing_factory, monkeypatch):
        _, post_id = a_post
        monkeypatch.setenv("FANGO_EXTERNAL_LOOKUP_ENABLED", "1")
        listing = listing_factory(building_name="グランド東京", url=None)
        calls: list = []
        self._wire(monkeypatch, found={"url": "https://www.homes.co.jp/x/", "source": "homes"}, calls=calls)

        out = enrich.enrich_post_with_listing_link(post_id, {"id": listing.id, "building_name": "グランド東京"})
        assert out["attached"] is True
        previews = forum_core.list_link_previews(post_id)
        assert previews[0]["image_url"] == "/uploads/hosted.png"
        assert previews[0]["source"] == "homes"
        assert len(calls) == 1

        # Second call (different post, same listing) hits the cache — no re-lookup.
        from fango.baibai import service as bb
        ag2 = listing  # reuse db; make a second post
        out2 = enrich.enrich_post_with_listing_link(post_id, {"id": listing.id})
        assert out2["cached"] is True
        assert len(calls) == 1  # find_external_url NOT called again

    def test_negative_result_cached(self, a_post, listing_factory, monkeypatch):
        _, post_id = a_post
        monkeypatch.setenv("FANGO_EXTERNAL_LOOKUP_ENABLED", "1")
        listing = listing_factory(building_name="ナイショ", url=None)
        calls: list = []
        self._wire(monkeypatch, found=None, calls=calls)
        out = enrich.enrich_post_with_listing_link(post_id, {"id": listing.id, "building_name": "ナイショ"})
        assert out["attached"] is False
        assert forum_core.list_link_previews(post_id) == []
        # Negative cached → no second lookup.
        enrich.enrich_post_with_listing_link(post_id, {"id": listing.id, "building_name": "ナイショ"})
        assert len(calls) == 1


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
            post_id, "https://www.homes.co.jp/x/",
            image_url="/uploads/hosted.png", title="グランド東京 2LDK", source="homes",
        )
        r = client.get(f"/baibai/t/{thread_id}")
        assert r.status_code == 200
        assert "link-preview-card" in r.text
        assert "/uploads/hosted.png" in r.text
        assert "グランド東京 2LDK" in r.text
