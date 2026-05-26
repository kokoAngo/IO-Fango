"""Structured listing search/detail tools (取得系)."""
from __future__ import annotations

import pytest

from fango.listings import service as ls
from fango.listings.tools import _listing_brief
from fango.db import connect


@pytest.fixture
def seeded_listings(tmp_db):
    """Insert a small variety of listings to exercise the filters."""
    rows = []
    rows.append(ls.insert_listing({
        "reins_id": "T1",
        "building_name": "代々木上原ハイツ",
        "title": "代々木上原ハイツ 502",
        "address": "東京都渋谷区上原",
        "prefecture": "東京都",
        "city": "渋谷区",
        "station": "代々木上原",
        "station_line": "千代田線",
        "walk_minutes": 5,
        "layout": "2LDK",
        "area_sqm": 60.0,
        "rent_yen": 350000,
        "price_man": 35,
        "built_year": 2015,
        "url": "https://example.com/1",
    }))
    rows.append(ls.insert_listing({
        "reins_id": "T2",
        "building_name": "三軒茶屋アパートメント",
        "title": "三軒茶屋アパートメント 305",
        "address": "東京都世田谷区三軒茶屋",
        "prefecture": "東京都",
        "city": "世田谷区",
        "station": "三軒茶屋",
        "station_line": "田園都市線",
        "walk_minutes": 8,
        "layout": "1K",
        "area_sqm": 28.0,
        "rent_yen": 90000,
        "price_man": 9,
        "built_year": 2010,
    }))
    rows.append(ls.insert_listing({
        "reins_id": "T3",
        "building_name": "板橋ハウス",
        "title": "板橋ハウス 101",
        "address": "東京都板橋区蓮根",
        "prefecture": "東京都",
        "city": "板橋区",
        "station": "西台",
        "station_line": "都営三田線",
        "walk_minutes": 12,
        "layout": "1LDK",
        "area_sqm": 40.0,
        "rent_yen": 130000,
        "price_man": 13,
        "built_year": 2008,
    }))
    return rows


class TestSearchFilters:
    def test_filter_by_layout_prefix(self, seeded_listings):
        # layout="1L" should match "1LDK" only (prefix).
        out = ls.search_listings(layout="1L")
        layouts = {r.layout for r in out}
        assert layouts == {"1LDK"}

    def test_filter_by_city_like(self, seeded_listings):
        out = ls.search_listings(city="世田谷")
        assert len(out) == 1
        assert out[0].city == "世田谷区"

    def test_filter_rent_max(self, seeded_listings):
        out = ls.search_listings(rent_max_yen=100_000)
        rents = [r.extra.get("rent_yen") for r in out]
        assert all(r <= 100_000 for r in rents)
        assert 90000 in rents

    def test_filter_walk_minutes_max(self, seeded_listings):
        out = ls.search_listings(walk_minutes_max=5)
        # Only the 5-min walk listing should match.
        assert len(out) == 1
        assert out[0].walk_minutes == 5

    def test_only_listing_ids_pool(self, seeded_listings):
        target_id = seeded_listings[1].id
        out = ls.search_listings(only_listing_ids=[target_id])
        assert len(out) == 1
        assert out[0].id == target_id


class TestSort:
    def test_price_asc(self, seeded_listings):
        out = ls.search_listings(sort_by="price_asc")
        prices = [r.price_man for r in out]
        assert prices == sorted(prices)

    def test_rent_desc(self, seeded_listings):
        out = ls.search_listings(sort_by="rent_desc")
        rents = [r.extra.get("rent_yen") for r in out]
        assert rents == sorted(rents, reverse=True)


class TestPagination:
    def test_limit_and_offset(self, seeded_listings):
        first_page = ls.search_listings(limit=2, offset=0, sort_by="price_asc")
        second_page = ls.search_listings(limit=2, offset=2, sort_by="price_asc")
        assert len(first_page) == 2
        assert len(second_page) == 1
        # No overlap.
        assert {r.id for r in first_page} & {r.id for r in second_page} == set()


class TestCount:
    def test_count_with_filter(self, seeded_listings):
        total = ls.count_listings(criteria={"prefecture": "東京都"})
        assert total == 3
        narrow = ls.count_listings(criteria={"rent_max_yen": 100_000})
        assert narrow == 1


class TestThumbnail:
    def test_brief_thumbnail_when_image_present(self, seeded_listings):
        target = seeded_listings[0]
        ls.replace_images(target.id, [
            {"kind": "raw", "rel_path": f".Spotlight-V100/{target.reins_id}/reins_1.jpg",
             "label": "raw_1", "sort_order": 0},
        ])
        brief = _listing_brief(target)
        assert brief["thumbnail_url"] == f"/listings/img/{target.id}/raw/reins_1.jpg"

    def test_brief_no_thumbnail_without_images(self, seeded_listings):
        brief = _listing_brief(seeded_listings[0])
        assert brief["thumbnail_url"] is None


class TestRelations:
    def test_get_listing_with_relations(self, seeded_listings):
        target = seeded_listings[0]
        ls.replace_transports(target.id, [
            {"line": "千代田線", "station": "代々木上原", "walk_minutes": 5},
            {"line": "千代田線", "station": "代々木公園", "walk_minutes": 9},
        ])
        ls.replace_images(target.id, [
            {"kind": "raw", "rel_path": ".Spotlight-V100/T1/reins_1.jpg", "label": "raw_1"},
            {"kind": "shuhen", "rel_path": ".Spotlight-V100/T1/shuhen/shuhen_1_コンビニ.jpg",
             "label": "コンビニ"},
        ])
        bundle = ls.get_listing_with_relations(target.id)
        assert bundle is not None
        assert bundle["listing"].id == target.id
        assert len(bundle["transports"]) == 2
        assert len(bundle["images"]) == 2
        assert any(i["kind"] == "shuhen" for i in bundle["images"])
