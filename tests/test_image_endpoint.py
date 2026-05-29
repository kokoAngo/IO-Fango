"""Listing image endpoint — seq-based URL, traversal-proof, kind whitelist."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from fango.listings import service as ls


@pytest.fixture
def listing_with_image(tmp_db, listing_factory):
    """Create a listing + an on-disk image we can serve.

    The image lives under a ``.Spotlight-V100-test/`` subtree under the real
    project root, since the handler resolves rel_path against that root and
    forbids escape. The fixture rmtree-s the whole subtree on teardown.

    The file name on disk can be anything — the URL path no longer carries
    it; sort_order=0 maps to URL seq=1.
    """
    listing = listing_factory()
    from fango import http_app as ha
    repo_root = ha.REPO_ROOT_FS
    test_root = repo_root / ".Spotlight-V100-test"
    img_dir = test_root / "abc"
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = img_dir / "src_1.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0fakejpg")
    rel = str(img_path.relative_to(repo_root))
    ls.replace_images(listing.id, [{
        "kind": "raw", "rel_path": rel, "label": "raw_1", "sort_order": 0,
    }])
    try:
        yield listing
    finally:
        shutil.rmtree(test_root, ignore_errors=True)


def test_happy_path_serves_jpeg(client, listing_with_image):
    listing = listing_with_image
    resp = client.get(f"/listings/img/{listing.id}/raw/1.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")
    assert b"fakejpg" in resp.content


def test_invalid_kind_rejected(client, listing_with_image):
    listing = listing_with_image
    resp = client.get(f"/listings/img/{listing.id}/evil/1.jpg")
    assert resp.status_code == 400


def test_non_numeric_seq_rejected(client, listing_with_image):
    """The route accepts only ``{seq}.jpg`` with seq=int — anything else
    can't reach the handler, so traversal vectors of any shape just 404."""
    listing = listing_with_image
    resp = client.get(f"/listings/img/{listing.id}/raw/..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404, 422)


def test_seq_out_of_range_rejected(client, listing_with_image):
    listing = listing_with_image
    resp = client.get(f"/listings/img/{listing.id}/raw/99999.jpg")
    assert resp.status_code == 400


def test_unknown_seq_404(client, listing_with_image):
    """Seq within accepted range but no row in DB → 404."""
    listing = listing_with_image
    resp = client.get(f"/listings/img/{listing.id}/raw/9.jpg")
    assert resp.status_code == 404
