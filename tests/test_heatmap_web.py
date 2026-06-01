"""Prefecture heatmap page."""
from __future__ import annotations

import pytest

# 分布図(/heatmap)栏目は一時停止中。ルート/ナビに依存するテストはスキップする。
# 再開時(http_app.py / left_nav.html のコメントを外す時)にこのマーカーを削除。
_SUSPENDED = pytest.mark.skip(
    reason="分布図 栏目を一時停止中（http_app.py / left_nav.html のコメント参照）。再開時に解除。"
)


@_SUSPENDED
def test_heatmap_empty(client):
    r = client.get("/heatmap")
    assert r.status_code == 200
    assert "都道府県分布図" in r.text
    assert "47" in r.text  # subtitle mentions 47 prefectures


@_SUSPENDED
def test_heatmap_polygons_for_seeded_listings(client, listing_factory):
    listing_factory(prefecture="東京都", building_name="A")
    listing_factory(prefecture="東京都", building_name="B")
    listing_factory(prefecture="神奈川県", building_name="C")
    r = client.get("/heatmap")
    assert r.status_code == 200
    assert "東京都" in r.text
    assert "神奈川県" in r.text
    # Each prefecture has its own <path> with intensity-encoded fill
    assert 'class="pref"' in r.text
    assert 'data-pref="東京都"' in r.text


@_SUSPENDED
def test_heatmap_links_in_nav(client):
    r = client.get("/")
    assert "/heatmap" in r.text


@_SUSPENDED
def test_heatmap_svg_present(client, listing_factory):
    listing_factory(prefecture="北海道")
    r = client.get("/heatmap")
    assert "<svg" in r.text
    assert "class=\"heatmap-svg\"" in r.text
    # 47 prefectures all rendered as paths
    assert r.text.count('class="pref"') == 47


def test_heatmap_module_renders_47_prefectures():
    # 純モジュールテスト — ルート停止の影響を受けないので維持。
    from fango.jpmap import get_prefecture_paths
    paths = get_prefecture_paths()
    assert len(paths) == 47
    for pref in ("東京都", "北海道", "沖縄県", "京都府", "大阪府"):
        assert pref in paths
        assert paths[pref].startswith("M")
