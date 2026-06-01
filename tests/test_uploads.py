"""Image upload — base64 in, absolute URL out, dedup, validation."""
from __future__ import annotations

import base64
import io

import pytest

from fango import uploads


def _png_bytes(color=(255, 0, 0), size=(4, 4)) -> bytes:
    from PIL import Image
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes() -> bytes:
    from PIL import Image
    im = Image.new("RGB", (4, 4), (0, 200, 0))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _gif_bytes() -> bytes:
    from PIL import Image
    im = Image.new("P", (4, 4))
    buf = io.BytesIO()
    im.save(buf, format="GIF")
    return buf.getvalue()


def _webp_bytes() -> bytes:
    from PIL import Image
    im = Image.new("RGB", (4, 4), (0, 0, 255))
    buf = io.BytesIO()
    im.save(buf, format="WEBP")
    return buf.getvalue()


@pytest.fixture
def isolated_uploads(tmp_path, monkeypatch):
    """Point UPLOADS_DIR at a fresh temp dir per test."""
    d = tmp_path / "uploads"
    monkeypatch.setattr(uploads, "UPLOADS_DIR", d)
    return d


class TestSaveImage:
    def test_png_happy_path(self, isolated_uploads):
        b64 = base64.b64encode(_png_bytes()).decode()
        out = uploads.save_image(b64)
        assert out["mime"] == "image/png"
        assert out["url"].endswith(f"/uploads/{out['sha256']}.png")
        assert out["reused"] is False
        assert (isolated_uploads / f"{out['sha256']}.png").is_file()

    def test_jpeg_happy_path(self, isolated_uploads):
        b64 = base64.b64encode(_jpeg_bytes()).decode()
        out = uploads.save_image(b64)
        assert out["mime"] == "image/jpeg"
        assert out["url"].endswith(".jpg")

    def test_webp_happy_path(self, isolated_uploads):
        b64 = base64.b64encode(_webp_bytes()).decode()
        out = uploads.save_image(b64)
        assert out["mime"] == "image/webp"
        assert out["url"].endswith(".webp")

    def test_gif_happy_path(self, isolated_uploads):
        b64 = base64.b64encode(_gif_bytes()).decode()
        out = uploads.save_image(b64)
        assert out["mime"] == "image/gif"
        assert out["url"].endswith(".gif")

    def test_data_url_prefix_stripped(self, isolated_uploads):
        raw = base64.b64encode(_png_bytes()).decode()
        out = uploads.save_image(f"data:image/png;base64,{raw}")
        assert out["mime"] == "image/png"

    def test_dedup_on_identical_bytes(self, isolated_uploads):
        b64 = base64.b64encode(_png_bytes()).decode()
        a = uploads.save_image(b64)
        b = uploads.save_image(b64)
        assert a["sha256"] == b["sha256"]
        assert a["url"] == b["url"]
        assert b["reused"] is True

    def test_absolute_url_when_base_set(self, isolated_uploads, monkeypatch):
        monkeypatch.setenv("FANGO_PUBLIC_BASE_URL", "https://example.test")
        out = uploads.save_image(base64.b64encode(_png_bytes()).decode())
        assert out["url"].startswith("https://example.test/uploads/")


class TestValidation:
    def test_empty_string_rejected(self, isolated_uploads):
        with pytest.raises(uploads.UploadError, match="non-empty"):
            uploads.save_image("")

    def test_bad_base64_rejected(self, isolated_uploads):
        # b64decode is permissive (silently drops junk) — but the result
        # may decode to garbage that fails the magic-byte check.
        with pytest.raises(uploads.UploadError):
            uploads.save_image("not really base64 *** garbage")

    def test_decoded_empty_rejected(self, isolated_uploads):
        # Valid base64 of empty bytes.
        with pytest.raises(uploads.UploadError, match="empty"):
            uploads.save_image(base64.b64encode(b"").decode())

    def test_unknown_format_rejected(self, isolated_uploads):
        # 13 bytes of random non-image content — clears the size floor,
        # falls past every magic-byte branch.
        with pytest.raises(uploads.UploadError, match="unrecognised"):
            uploads.save_image(base64.b64encode(b"hello world\x00\x01").decode())

    def test_too_large_rejected(self, isolated_uploads, monkeypatch):
        monkeypatch.setattr(uploads, "MAX_SIZE_BYTES", 100)
        big = _png_bytes(size=(200, 200))   # > 100 bytes
        assert len(big) > 100
        with pytest.raises(uploads.UploadError, match="bytes"):
            uploads.save_image(base64.b64encode(big).decode())

    def test_svg_rejected(self, isolated_uploads):
        """SVG can carry script — must be refused even though it's an
        'image' format in some catalogues."""
        svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"></svg>'
        with pytest.raises(uploads.UploadError, match="unrecognised"):
            uploads.save_image(base64.b64encode(svg).decode())


@pytest.mark.skip(
    reason="fango_upload_image MCP tool suspended "
           "(forum_post_tools.DIRECT_POSTING_ENABLED). save_image logic stays "
           "covered by TestServeEndpoint + the service tests."
)
class TestMcpTool:
    def test_requires_auth(self, isolated_uploads, tmp_db):
        from fango.auth import AuthError
        from fango.forum_post_tools import register
        captured = {}
        class _M:
            def tool(self_):
                def deco(fn):
                    captured[fn.__name__] = fn
                    return fn
                return deco
        register(_M())
        with pytest.raises(AuthError):
            captured["fango_upload_image"](base64.b64encode(_png_bytes()).decode())

    def test_returns_url(self, isolated_uploads, tmp_db, agent_and_key, with_current_agent):
        from fango.forum_post_tools import register
        captured = {}
        class _M:
            def tool(self_):
                def deco(fn):
                    captured[fn.__name__] = fn
                    return fn
                return deco
        register(_M())
        agent, _ = agent_and_key
        with_current_agent(agent)
        out = captured["fango_upload_image"](base64.b64encode(_png_bytes()).decode())
        assert out["url"].startswith("/uploads/") or "://" in out["url"]
        assert out["mime"] == "image/png"

    def test_bad_data_raises_value_error(self, isolated_uploads, tmp_db, agent_and_key, with_current_agent):
        from fango.forum_post_tools import register
        captured = {}
        class _M:
            def tool(self_):
                def deco(fn):
                    captured[fn.__name__] = fn
                    return fn
                return deco
        register(_M())
        agent, _ = agent_and_key
        with_current_agent(agent)
        with pytest.raises(ValueError):
            captured["fango_upload_image"]("")


class TestServeEndpoint:
    def test_serves_uploaded_file(self, client, tmp_db, agent_factory, monkeypatch, tmp_path):
        """Upload via the service, then fetch via HTTP."""
        # Redirect uploads to a path inside the repo root so the path
        # check inside the route accepts it. (We can't easily monkeypatch
        # the route's import — but we can write into the real path.)
        from fango import uploads as up
        from fango import http_app as ha
        real_dir = ha.REPO_ROOT_FS / "data" / "uploads"
        monkeypatch.setattr(up, "UPLOADS_DIR", real_dir)
        out = up.save_image(base64.b64encode(_png_bytes()).decode())
        sha_filename = f"{out['sha256']}.png"
        try:
            r = client.get(f"/uploads/{sha_filename}")
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("image/")
            assert len(r.content) > 0
        finally:
            (real_dir / sha_filename).unlink(missing_ok=True)

    def test_bad_filename_404(self, client):
        r = client.get("/uploads/notasha.png")
        assert r.status_code == 404

    def test_unknown_extension_404(self, client):
        r = client.get("/uploads/" + "a" * 64 + ".exe")
        assert r.status_code == 404

    def test_traversal_blocked(self, client):
        r = client.get("/uploads/../../etc/passwd")
        assert r.status_code in (400, 404)
