"""Spotlight adapter — parses real REINS JSON and pairs the image folder."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fango.db import connect, transaction
from fango.listings import service as ls
from fango.listings.ingestion.spotlight import (
    SpotlightListingAdapter,
    SpotlightRecord,
    _compose_layout,
    _parse_built,
    _parse_rent_yen,
    _parse_area,
    _parse_yen,
    _parse_transports,
)


# ---------------------------------------------------------------------------
# Pure-function parser tests
# ---------------------------------------------------------------------------

class TestParseRent:
    def test_basic(self):
        assert _parse_rent_yen("7.85万円") == (78500, 7.85)

    def test_integer(self):
        assert _parse_rent_yen("10万円") == (100000, 10.0)

    def test_full_width_digits(self):
        # NFKC folds the full-width digits before regex.
        assert _parse_rent_yen("１０万円") == (100000, 10.0)

    def test_missing(self):
        assert _parse_rent_yen(None) == (None, None)
        assert _parse_rent_yen("なし") == (None, None)


class TestParseArea:
    def test_with_unit(self):
        assert _parse_area("26.08㎡") == 26.08

    def test_with_alt_unit(self):
        assert _parse_area("70m2") == 70.0


class TestParseYen:
    def test_basic(self):
        assert _parse_yen("8,500円") == 8500

    def test_no_comma(self):
        assert _parse_yen("33000円") == 33000

    def test_none_string(self):
        assert _parse_yen("なし") is None
        assert _parse_yen(None) is None


class TestParseBuilt:
    def test_heisei(self):
        assert _parse_built("2014年（平成26年） 8月") == (2014, 8)

    def test_reiwa_with_space(self):
        assert _parse_built("2019年（令和 1年）11月") == (2019, 11)

    def test_missing(self):
        assert _parse_built(None) == (None, None)
        assert _parse_built("不明") == (None, None)


class TestComposeLayout:
    def test_single_k(self):
        assert _compose_layout("Ｋ", "1室") == "1K"

    def test_ldk(self):
        assert _compose_layout("ＬＤＫ", "2室") == "2LDK"

    def test_full_width_rooms(self):
        assert _compose_layout("ＤＫ", "１室") == "1DK"


class TestParseTransports:
    def test_basic(self):
        raw = [
            {"沿線": "都営三田線", "駅": "西台", "徒歩": "5分"},
            {"沿線": "都営三田線", "駅": "蓮根", "徒歩": "9分"},
        ]
        result = _parse_transports(raw)
        assert len(result) == 2
        assert result[0] == {"line": "都営三田線", "station": "西台", "walk_minutes": 5, "sort_order": 0}
        assert result[1]["walk_minutes"] == 9

    def test_empty(self):
        assert _parse_transports(None) == []
        assert _parse_transports([]) == []
        assert _parse_transports([{}]) == []


# ---------------------------------------------------------------------------
# Adapter end-to-end against a real sample folder
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_REINS_ID = "100138924518"
SAMPLE_DATED = "20260427-093209_100138924518"
SPOTLIGHT_ROOT = REPO_ROOT / ".Spotlight-V100"


@pytest.fixture
def sample_root(tmp_path):
    """Copy the real sample folders into a temp Spotlight root."""
    if not SPOTLIGHT_ROOT.exists():
        pytest.skip(".Spotlight-V100 not present in repo")
    src_dated = SPOTLIGHT_ROOT / SAMPLE_DATED
    src_images = SPOTLIGHT_ROOT / SAMPLE_REINS_ID
    if not src_dated.exists():
        pytest.skip(f"missing sample folder {src_dated}")
    dest = tmp_path / ".Spotlight-V100"
    dest.mkdir()
    # Copy dated folder (just the JSON files we care about).
    dated_dst = dest / SAMPLE_DATED
    dated_dst.mkdir()
    for name in ("reins-data.json", "run.json"):
        src_file = src_dated / name
        if src_file.exists():
            (dated_dst / name).write_bytes(src_file.read_bytes())
    # Copy image folder, if present.
    if src_images.exists():
        images_dst = dest / SAMPLE_REINS_ID
        images_dst.mkdir()
        for f in src_images.iterdir():
            if f.is_file() and not f.name.startswith("._"):
                (images_dst / f.name).write_bytes(f.read_bytes())
    return dest


def test_adapter_yields_record(sample_root, tmp_db):
    adapter = SpotlightListingAdapter(root=sample_root)
    records = list(adapter.iter_records())
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, SpotlightRecord)
    p = rec.payload
    assert p["reins_id"] == SAMPLE_REINS_ID
    assert p["rent_yen"] == 78500
    assert p["layout"] == "1K"
    assert p["prefecture"] == "東京都"
    assert p["city"] == "板橋区"
    assert p["built_year"] == 2014
    assert p["built_month"] == 8
    assert p["area_sqm"] == 26.08
    assert "板橋区" in p["address"]
    # Transports: 3 entries.
    assert len(rec.transports) == 3
    assert rec.transports[0]["station"] == "西台"
    assert rec.transports[0]["walk_minutes"] == 5


def test_full_pipeline_writes_listings_images_transports(sample_root, tmp_db):
    """Upsert + replace_images/transports → DB rows are persistent and shaped right."""
    adapter = SpotlightListingAdapter(root=sample_root)
    conn = connect(tmp_db)
    try:
        for rec in adapter.iter_records():
            with transaction(conn):
                listing = ls.upsert_listing(rec.payload, conn=conn)
                if rec.transports:
                    ls.replace_transports(listing.id, rec.transports, conn=conn)
                if rec.images:
                    ls.replace_images(listing.id, rec.images, conn=conn)
        listing_count = conn.execute("SELECT COUNT(*) AS n FROM listings").fetchone()["n"]
        transports = conn.execute(
            "SELECT COUNT(*) AS n FROM listing_transports"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert listing_count == 1
    assert transports == 3
