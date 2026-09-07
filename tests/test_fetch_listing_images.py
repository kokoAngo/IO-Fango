"""scripts/fetch_listing_images.py — the per-photo fetch/persist step.

The upstream bucket and Postgres are both LAN-only, so this covers the piece
that has real logic: turning one manifest entry into a listing_images row (or
into a clean rejection).
"""
from __future__ import annotations

import io

import pytest

from fango import uploads
from fango.listings.objectstore import ObjectStoreError


def _jpeg_bytes(color=(0, 200, 0)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


class _Store:
    """Serves canned bytes per key; raises for keys it doesn't know."""

    def __init__(self, blobs):
        self.blobs = blobs
        self.asked = []

    def get(self, key, **kw):
        self.asked.append(key)
        if key not in self.blobs:
            raise ObjectStoreError(f"{key}: HTTP 404", status=404)
        return self.blobs[key]


@pytest.fixture
def uploads_dir(tmp_path, monkeypatch):
    d = tmp_path / "uploads"
    monkeypatch.setattr(uploads, "UPLOADS_DIR", d)
    import scripts.fetch_listing_images as f
    monkeypatch.setattr(f, "UPLOADS_DIR", d)
    monkeypatch.setattr(f, "REPO_ROOT", tmp_path)
    return d


def test_save_photo_writes_a_repo_relative_path(uploads_dir):
    import scripts.fetch_listing_images as f
    data = _jpeg_bytes()
    store = _Store({"details/1/images/a.jpg": data})
    rec, err = f.save_photo(store, {"storage_key": "details/1/images/a.jpg", "label": "洋室"})

    assert err is None
    assert rec["kind"] == "raw"
    assert rec["label"] == "洋室"
    # rel_path must be repo-relative: the image endpoint resolves it against
    # the repo root and refuses anything that escapes.
    assert rec["rel_path"].startswith("uploads/")
    assert not rec["rel_path"].startswith("/")
    assert (uploads_dir.parent / rec["rel_path"]).is_file()
    assert rec["reused"] is False
    assert rec["size_bytes"] == len(data)


def test_save_photo_dedups_identical_bytes(uploads_dir):
    import scripts.fetch_listing_images as f
    data = _jpeg_bytes()
    store = _Store({"k1": data, "k2": data})   # same photo, two upstream keys
    first, _ = f.save_photo(store, {"storage_key": "k1"})
    second, _ = f.save_photo(store, {"storage_key": "k2"})
    assert first["rel_path"] == second["rel_path"]      # content-addressed
    assert second["reused"] is True
    assert len(list(uploads_dir.iterdir())) == 1


def test_save_photo_reports_a_fetch_failure_without_raising(uploads_dir):
    import scripts.fetch_listing_images as f
    rec, err = f.save_photo(_Store({}), {"storage_key": "missing.jpg"})
    assert rec is None and "404" in err


def test_save_photo_rejects_non_image_bytes(uploads_dir):
    """Upstream 物件画像 rows occasionally point at a PDF; a magic-byte sniff
    keeps it out rather than serving a broken <img>."""
    import scripts.fetch_listing_images as f
    store = _Store({"doc.pdf": b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nrest of a pdf"})
    rec, err = f.save_photo(store, {"storage_key": "doc.pdf"})
    assert rec is None
    assert "unrecognised image format" in err
    assert not uploads_dir.exists() or not list(uploads_dir.iterdir())


def test_default_per_listing_cap_is_small():
    """Only the first raw photo renders anywhere today; the cap keeps a full
    sweep from pulling ~2 GB of bytes nothing displays."""
    import scripts.fetch_listing_images as f
    assert 1 <= f.DEFAULT_PER_LISTING <= 6
