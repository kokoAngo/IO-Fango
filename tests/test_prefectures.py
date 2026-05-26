"""47 都道府県 normalization + aggregation."""
from __future__ import annotations

from fango.prefectures import (
    PREFECTURES,
    aggregate_listing_heat,
    listings_per_prefecture,
    normalize,
)


def test_prefecture_count():
    assert len(PREFECTURES) == 47


def test_normalize_exact():
    assert normalize("東京都") == "東京都"
    assert normalize("北海道") == "北海道"


def test_normalize_stem():
    assert normalize("東京") == "東京都"
    assert normalize("大阪") == "大阪府"
    assert normalize("京都") == "京都府"
    assert normalize("青森") == "青森県"


def test_normalize_from_address_prefix():
    assert normalize("東京都港区六本木") == "東京都"
    assert normalize("北海道札幌市") == "北海道"
    assert normalize("青森県青森市") == "青森県"


def test_normalize_unknown():
    assert normalize("Atlantis") is None
    assert normalize("") is None
    assert normalize(None) is None


def test_listings_per_prefecture(listing_factory):
    listing_factory(prefecture="東京都")
    listing_factory(prefecture="東京都")
    listing_factory(prefecture="神奈川県")
    pp = listings_per_prefecture()
    assert pp["東京都"] == 2
    assert pp["神奈川県"] == 1


def test_aggregate_listing_heat(agent_factory, listing_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    t = listing_factory(prefecture="東京都")
    k = listing_factory(prefecture="神奈川県")
    fb.create_thread(title="t", body="b", author_id=ag.id, listing_id=t.id)
    fb.create_thread(title="t", body="b", author_id=ag.id, listing_id=t.id)
    fb.create_thread(title="t", body="b", author_id=ag.id, listing_id=k.id)
    heat = aggregate_listing_heat()
    by_pref = {h["prefecture"]: h["post_count"] for h in heat}
    assert by_pref["東京都"] == 2
    assert by_pref["神奈川県"] == 1
