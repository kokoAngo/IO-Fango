"""Mapping layer for the upstream inventory Postgres.

These tests are driver-free on purpose: the parsers and row→payload mapping
must be verifiable without psycopg or a live LAN database.
"""
from __future__ import annotations

import json

import pytest

from fango.listings.ingestion import postgres as pg


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def test_parse_man_to_yen():
    assert pg.parse_man_to_yen("10.3") == 103_000
    assert pg.parse_man_to_yen("21.4") == 214_000
    assert pg.parse_man_to_yen("7万円") == 70_000
    assert pg.parse_man_to_yen(None) is None
    assert pg.parse_man_to_yen("") is None
    assert pg.parse_man_to_yen("応相談") is None


def test_parse_area_rejects_noise():
    assert pg.parse_area("37.67") == pytest.approx(37.67)
    assert pg.parse_area("104.03㎡") == pytest.approx(104.03)
    assert pg.parse_area("0") is None          # 0 is upstream noise, not an area
    assert pg.parse_area("―") is None


def test_parse_built_ym_reads_seireki_year_not_yyyymm():
    """built_ym is 西暦年 + 和暦年 despite the name — see the docstring there.

    "198762" is 1987年（昭和62年）, not 1987-62. Reading the trailing pair as a
    month produces impossible months and throws away ~96% of the column.
    """
    assert pg.parse_built_ym("198762") == 1987      # 昭和62年
    assert pg.parse_built_ym("200719") == 2007      # 平成19年
    assert pg.parse_built_ym("201830") == 2018      # 平成30年
    assert pg.parse_built_ym("1998") == 1998
    assert pg.parse_built_ym(None) is None
    assert pg.parse_built_ym("") is None
    assert pg.parse_built_ym("築年不詳") is None


def test_parse_built_at_handles_era_years():
    assert pg.parse_built_at("2008年（平成20年） 1月") == (2008, 1)
    assert pg.parse_built_at("1999年（平成11年）11月") == (1999, 11)
    # Year only still pins built_year, which is what built_year_min filters on.
    assert pg.parse_built_at("1985年") == (1985, None)
    assert pg.parse_built_at("築年不詳") == (None, None)


def test_parse_floor():
    assert pg.parse_floor("56階") == 56
    assert pg.parse_floor("2") == 2
    assert pg.parse_floor("B1階") is None      # no basement representation in schema
    assert pg.parse_floor(None) is None


def test_parse_yen():
    assert pg.parse_yen("8,500円") == 8500
    assert pg.parse_yen("8500") == 8500
    assert pg.parse_yen("なし") is None
    assert pg.parse_yen("--") is None


def test_split_address():
    assert pg.split_address("東京都武蔵野市西久保3丁目1-11") == ("東京都", "武蔵野市", None)
    # A 23-区 address fills both city and ward so either search path hits.
    assert pg.split_address("東京都港区六本木6-12-1") == ("東京都", "港区", "港区")
    # 政令市: the 市 is the city, the inner 区 is the ward.
    assert pg.split_address("神奈川県横浜市西区みなとみらい3-3-3") == ("神奈川県", "横浜市", "西区")
    assert pg.split_address(None) == (None, None, None)
    assert pg.split_address("番地未定") == (None, None, None)


def test_split_line_station_folds_fullwidth_space():
    assert pg.split_line_station("中央線　三鷹") == ("中央線", "三鷹")
    assert pg.split_line_station("半蔵門線　清澄白河") == ("半蔵門線", "清澄白河")
    assert pg.split_line_station("三鷹駅") == (None, "三鷹")
    assert pg.split_line_station(None) == (None, None)


def test_parse_access_splits_legs():
    legs = pg.parse_access("大江戸線 勝どき駅 徒歩7分 / 有楽町線 月島駅 徒歩14分")
    assert legs == [
        {"line": "大江戸線", "station": "勝どき", "walk_minutes": 7, "sort_order": 0},
        {"line": "有楽町線", "station": "月島", "walk_minutes": 14, "sort_order": 1},
    ]
    assert pg.parse_access(None) == []


# ---------------------------------------------------------------------------
# Row → payload
# ---------------------------------------------------------------------------

RENTAL_ROW = {
    "reins_id": "100140604673",
    "building_name": "サン・ガーデン武蔵野",
    "address": "東京都武蔵野市西久保３丁目１－１１",
    "rent_man": "10.3",
    "area_sqm": "37.67",
    "layout": "1LDK",
    "built_ym": "199810",   # 1998年（平成10年）
    "floor": "2",
    "walk_min": "13",
    "line_station": "中央線　三鷹",
    "property_type": "アパート",
    "mgmt_company": "東急住宅リース（株）",
    "kanrihi": None,
    "shikikin": "10.3万円",
    "reikin": "10.3万円",
    "ad_ok": "可",
    "seiyaku": None,
}

SALE_ROW = {
    "bukken_no": "100140603651",
    "ward": "中央区",
    "property_type": "売マンション",
    "property_category": "中古マンション",
    "price_man": 24980,
    "address": "東京都中央区勝どき６丁目",
    "station": "大江戸線 勝どき駅",
    "access": "大江戸線 勝どき駅 徒歩7分 / 有楽町線 月島駅 徒歩14分",
    "layout": "3SLDK",
    "exclusive_area": "104.03㎡",
    "building_name": "ＴＨＥ ＴＯＫＹＯ ＴＯＷＥＲＳ",
    "floor": "56階",
    "built_at": "2008年（平成20年） 1月",
    "transaction_type": "売主",
    "transaction_status": "公開中",
    "ad_repost": "広告可（但し要連絡）",
    "broker": "（株）コスモスイニシア",
    "direction": "北西",
    "balcony_area": "9.43㎡",
}


def test_map_rental_row():
    rec = pg.map_rental_row(RENTAL_ROW)
    p = rec.payload
    assert p["reins_id"] == "100140604673"
    assert p["transaction_type"] == "rent"
    assert p["rent_yen"] == 103_000
    # price_man is the sale-price column; a rental must never populate it.
    assert "price_man" not in p
    assert p["prefecture"] == "東京都" and p["city"] == "武蔵野市"
    assert p["station"] == "三鷹" and p["station_line"] == "中央線"
    assert p["walk_minutes"] == 13
    assert p["built_year"] == 1998
    assert p["ad_status"] == "可"
    assert p["listing_type"] == "アパート"
    assert "tenancy_status" not in p        # not 成約
    assert rec.transports == [
        {"line": "中央線", "station": "三鷹", "walk_minutes": 13, "sort_order": 0}
    ]


def test_map_rental_marks_contracted_rows():
    row = {**RENTAL_ROW, "seiyaku": '["3801c197-4dad-8100-a941-c1fc87dcc5ec"]'}
    assert pg.map_rental_row(row).payload["tenancy_status"] == "成約済"
    # An empty Notion relation is not a 成約.
    assert "tenancy_status" not in pg.map_rental_row({**RENTAL_ROW, "seiyaku": "[]"}).payload


def test_map_rental_built_year_falls_back_to_built_ym():
    # No 築年月 text: the year still lands, the month is simply unknown.
    rec = pg.map_rental_row(RENTAL_ROW)
    assert rec.payload["built_year"] == 1998
    assert "built_month" not in rec.payload


def test_map_rental_prefers_full_chikunen_text_for_the_month():
    rec = pg.map_rental_row({**RENTAL_ROW, "built_at_text": "1987年（昭和62年） 2月"})
    assert rec.payload["built_year"] == 1987
    assert rec.payload["built_month"] == 2


def test_map_rental_survives_unparseable_built_columns():
    rec = pg.map_rental_row({**RENTAL_ROW, "built_ym": "", "built_at_text": None})
    assert "built_year" not in rec.payload
    assert rec.payload["reins_id"] == "100140604673"   # row still ingests


def test_map_sale_row():
    rec = pg.map_sale_row(SALE_ROW)
    p = rec.payload
    assert p["reins_id"] == "100140603651"
    assert p["transaction_type"] == "sale"
    assert p["price_man"] == 24980
    assert "rent_yen" not in p
    assert p["area_sqm"] == pytest.approx(104.03)
    assert p["balcony_sqm"] == pytest.approx(9.43)
    assert p["floor"] == 56
    assert p["built_year"] == 2008 and p["built_month"] == 1
    assert p["ward"] == "中央区"
    assert p["station"] == "勝どき" and p["walk_minutes"] == 7
    assert p["listing_type"] == "中古マンション"
    assert len(rec.transports) == 2


def test_map_sale_backfills_station_from_the_station_column():
    """~60% of sale rows put only the walk time in `access` and keep 沿線+駅 in
    the `station` column. Parsing `access` alone loses the station on those."""
    row = {**SALE_ROW, "station": "南北線 白金台", "access": "徒歩 9分"}
    p = pg.map_sale_row(row).payload
    assert p["station"] == "白金台"
    assert p["station_line"] == "南北線"
    assert p["walk_minutes"] == 9


def test_map_sale_station_column_only():
    row = {**SALE_ROW, "station": "山手線 渋谷駅", "access": None}
    p = pg.map_sale_row(row).payload
    assert (p["station"], p["station_line"]) == ("渋谷", "山手線")
    assert "walk_minutes" not in p


def test_map_sale_access_legs_win_over_the_station_column():
    p = pg.map_sale_row(SALE_ROW).payload
    assert p["station"] == "勝どき" and p["walk_minutes"] == 7


def test_sale_ad_gate_uses_ad_repost_not_transaction_status():
    """広告転載可否 is the advertising clearance; 取引状況 is on-market state.

    Mapping 取引状況 into ad_status (the pre-P3 behaviour) would clear rows the
    listing broker has explicitly refused advertising on.
    """
    rec = pg.map_sale_row(SALE_ROW)
    # NFKC folds the upstream full-width parens, so the stored value is the
    # half-width spelling — which is exactly what SALE_ADVERTISABLE holds.
    assert rec.payload["ad_status"] == "広告可(但し要連絡)"
    assert rec.payload["ad_status"] in pg.SALE_ADVERTISABLE
    assert rec.payload["tenancy_status"] == "公開中"

    refused = pg.map_sale_row({**SALE_ROW, "ad_repost": "不可"})
    assert refused.payload["ad_status"] == "不可"
    assert refused.payload["ad_status"] not in pg.SALE_ADVERTISABLE


def test_sale_transaction_type_does_not_inherit_torihiki_tairyo():
    """b91b3fa regression guard.

    Upstream's `transaction_type` is 取引態様 (売主/仲介). Letting it through
    would put 売主/仲介 in the canonical sale-vs-rent column, and the search
    gate (``transaction_type = 'sale'``) would drop every sale row.
    """
    for torihiki in ("売主", "仲介", "代理", "専任"):
        p = pg.map_sale_row({**SALE_ROW, "transaction_type": torihiki}).payload
        assert p["transaction_type"] == "sale"
        assert json.loads(p["raw_json"])["transaction_type"] == torihiki


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def test_map_images_holds_back_documents_and_people():
    imgs = [
        {"category": "物件画像", "storage_key": "details/1/images/a.jpg", "subject": "洋室"},
        # Internal broker documents — they carry the broker's name and phone.
        {"category": "販売図面", "storage_key": "details/1/b.jpg"},
        {"category": "概要書", "storage_key": "details/1/c.jpg"},
        # Upstream classifier verdicts.
        {"category": "物件画像", "storage_key": "details/1/images/d.jpg", "disqualified": True},
        {"category": "物件画像", "storage_key": "details/1/images/e.jpg", "has_person": True},
        {"category": "物件画像", "storage_key": None},
    ]
    out = pg.map_images(imgs)
    assert [i["storage_key"] for i in out] == ["details/1/images/a.jpg"]
    assert out[0]["label"] == "洋室"
    assert out[0]["sort_order"] == 0


def test_map_images_ignores_the_stale_url_column():
    """storage_url still points at a decommissioned MinIO (wrong host AND
    wrong bucket); storage_key is the durable identifier."""
    out = pg.map_images([{
        "category": "物件画像",
        "storage_key": "details/1/images/a.jpg",
        "storage_url": "http://localhost:9000/fango/details/1/images/a.jpg",
    }])
    assert out[0]["storage_key"] == "details/1/images/a.jpg"
    assert "rel_path" not in out[0]     # not a listing_images row yet
    assert "storage_url" not in out[0]


def test_map_sale_row_attaches_images():
    rec = pg.map_sale_row(SALE_ROW, [
        {"category": "物件画像", "storage_key": "details/1/images/a.jpg"},
        {"category": "販売図面", "storage_key": "details/1/b.jpg"},
    ])
    assert len(rec.images) == 1


# ---------------------------------------------------------------------------
# Adapter wiring (no driver / no DB)
# ---------------------------------------------------------------------------

def test_adapter_unconfigured_yields_nothing(monkeypatch):
    monkeypatch.delenv("FANGO_PG_DSN", raising=False)
    adapter = pg.PgListingAdapter()
    assert adapter.is_configured() is False
    assert list(adapter.iter_records()) == []


def test_rental_filters():
    a = pg.PgListingAdapter(dsn="postgresql://x")
    clauses, params = a._rental_filters()
    assert any("seiyaku" in c for c in clauses) and not params

    a = pg.PgListingAdapter(dsn="postgresql://x", advertisable_only=True)
    _, params = a._rental_filters()
    assert params["ad_ok"] == ["可"]

    a = pg.PgListingAdapter(dsn="postgresql://x", include_contracted=True)
    clauses, _ = a._rental_filters()
    assert not any("seiyaku" in c for c in clauses)


def test_sale_filters_use_ad_repost():
    a = pg.PgListingAdapter(dsn="postgresql://x", advertisable_only=True)
    clauses, params = a._sale_filters()
    assert any("ad_repost" in c for c in clauses)
    # The SQL filter matches the raw upstream spelling (full-width parens),
    # NOT the NFKC-folded one that gets stored in listings.ad_status.
    assert params["ad_repost"] == ["広告可", "広告可（但し要連絡）"]
    assert pg.SALE_ADVERTISABLE == ("広告可", "広告可(但し要連絡)")


def test_pagination_key_is_selected():
    """_iter_pages advances on rows[-1][key], so the key must be in the SELECT."""
    assert "id" in pg.RENTAL_COLUMNS
    assert "bukken_no" in pg.SALE_COLUMNS


# ---------------------------------------------------------------------------
# Skeleton rows
# ---------------------------------------------------------------------------

def test_is_ingestable_rejects_id_only_rows():
    """~8% of the rental table is an id with every descriptive column NULL —
    an upstream row whose detail fetch has not run yet."""
    skeleton = pg.map_rental_row({"reins_id": "100000000001"}).payload
    assert pg.is_ingestable(skeleton) is False
    assert pg.is_ingestable(pg.map_rental_row(RENTAL_ROW).payload) is True
    assert pg.is_ingestable(pg.map_sale_row(SALE_ROW).payload) is True
    # No id at all: an upsert would blind-insert and duplicate every run.
    assert pg.is_ingestable({"address": "東京都港区"}) is False


def test_image_columns_cover_the_mapper():
    """map_images reads these fields; if the SELECT list stops carrying one,
    every photo is silently filtered out instead of failing loudly."""
    for field in ("bukken_no", "storage_key", "category", "subject",
                  "disqualified", "has_person"):
        assert field in pg.IMAGE_COLUMNS
    # A row shaped exactly like the SELECT returns must survive the mapper.
    row = {c: None for c in pg.IMAGE_COLUMNS}
    row.update({"category": "物件画像", "storage_key": "details/1/images/a.jpg"})
    assert len(pg.map_images([row])) == 1
