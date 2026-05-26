"""baibai service: thread/reply/recommend/list/get/search + heatmap."""
from __future__ import annotations

import pytest

from fango.baibai import service as fb
from fango.forum_core import ForumError
from fango.prefectures import aggregate_listing_heat


def test_create_thread(agent_factory):
    ag, _ = agent_factory()
    out = fb.create_thread(title="hello", body="body", author_id=ag.id)
    assert out["thread"].id > 0
    assert out["thread"].forum == "baibai"
    assert out["post"].body == "body"


def test_create_thread_with_listing(agent_factory, listing_factory):
    ag, _ = agent_factory()
    listing = listing_factory()
    out = fb.create_thread(
        title="great spot", body="have a look", author_id=ag.id, listing_id=listing.id,
    )
    detail = fb.get_thread(out["thread"].id)
    assert listing.id in detail["posts"][0].listing_refs


def test_reply(agent_factory):
    ag, _ = agent_factory()
    out = fb.create_thread(title="t", body="b", author_id=ag.id)
    p = fb.reply(thread_id=out["thread"].id, body="reply!", author_id=ag.id)
    assert p.id > 0
    assert p.body == "reply!"


def test_reply_to_specific_post(agent_factory):
    ag, _ = agent_factory()
    out = fb.create_thread(title="t", body="root", author_id=ag.id)
    root = out["post"]
    nested = fb.reply(
        thread_id=out["thread"].id, body="branch", author_id=ag.id, reply_to=root.id,
    )
    assert nested.reply_to == root.id


def test_reply_to_bad_target_raises(agent_factory):
    ag, _ = agent_factory()
    out = fb.create_thread(title="t", body="b", author_id=ag.id)
    with pytest.raises(ForumError):
        fb.reply(thread_id=out["thread"].id, body="x", author_id=ag.id, reply_to=999)


def test_empty_title_or_body_raises(agent_factory):
    ag, _ = agent_factory()
    with pytest.raises(ForumError):
        fb.create_thread(title="", body="b", author_id=ag.id)
    with pytest.raises(ForumError):
        fb.create_thread(title="t", body=" ", author_id=ag.id)


def test_recommend_listing(agent_factory, listing_factory):
    ag, _ = agent_factory()
    listing = listing_factory()
    out = fb.create_thread(title="t", body="b", author_id=ag.id)
    rid = fb.recommend_listing(post_id=out["post"].id, listing_id=listing.id, note="nice view")
    assert rid > 0


def test_recommend_listing_unknown_raises(agent_factory):
    ag, _ = agent_factory()
    out = fb.create_thread(title="t", body="b", author_id=ag.id)
    with pytest.raises(ForumError):
        fb.recommend_listing(post_id=out["post"].id, listing_id=99999)


def test_list_threads_order_by_activity(agent_factory):
    ag, _ = agent_factory()
    a = fb.create_thread(title="A", body="b", author_id=ag.id)
    b = fb.create_thread(title="B", body="b", author_id=ag.id)
    fb.reply(thread_id=a["thread"].id, body="bump", author_id=ag.id)
    listing = fb.list_threads()
    assert listing[0]["thread"].id == a["thread"].id
    assert listing[1]["thread"].id == b["thread"].id


def test_list_threads_filter_by_tag(agent_factory):
    ag, _ = agent_factory()
    fb.create_thread(title="A", body="b", author_id=ag.id, tags=("market",))
    fb.create_thread(title="B", body="b", author_id=ag.id, tags=("legal",))
    rows = fb.list_threads(tag="market")
    assert len(rows) == 1
    assert rows[0]["thread"].title == "A"


def test_search_posts(agent_factory):
    ag, _ = agent_factory()
    fb.create_thread(title="t", body="六本木の物件をどう思う", author_id=ag.id)
    fb.create_thread(title="t2", body="渋谷スクランブル", author_id=ag.id)
    results = fb.search("六本木")
    assert len(results) == 1


def test_search_empty_returns_empty(agent_factory):
    ag, _ = agent_factory()
    fb.create_thread(title="t", body="content", author_id=ag.id)
    assert fb.search("") == []
    assert fb.search("   ") == []


def test_prefecture_heatmap(agent_factory, listing_factory):
    ag, _ = agent_factory()
    tokyo = listing_factory(prefecture="東京都", building_name="T1")
    kanagawa = listing_factory(prefecture="神奈川県", building_name="K1")
    tokyo2 = listing_factory(prefecture="東京都", building_name="T2")

    a = fb.create_thread(title="a", body="x", author_id=ag.id, listing_id=tokyo.id)
    b = fb.create_thread(title="b", body="x", author_id=ag.id, listing_id=kanagawa.id)
    c = fb.create_thread(title="c", body="x", author_id=ag.id, listing_id=tokyo2.id)

    heat = fb.prefecture_heatmap()
    by_pref = {h["prefecture"]: h["post_count"] for h in heat}
    assert by_pref["東京都"] == 2
    assert by_pref["神奈川県"] == 1


def test_get_thread_includes_likes(agent_factory):
    ag, _ = agent_factory()
    ag2, _ = agent_factory("liker")
    out = fb.create_thread(title="t", body="b", author_id=ag.id)
    from fango.forum_core import toggle_like
    toggle_like(out["post"].id, ag2.id)
    detail = fb.get_thread(out["thread"].id)
    assert detail["posts"][0].like_count == 1


def test_get_unknown_thread_returns_none(tmp_db):
    assert fb.get_thread(9999) is None


def test_unknown_forum_raises(agent_factory):
    from fango.forum_core import create_thread as cc
    ag, _ = agent_factory()
    with pytest.raises(ForumError):
        cc("nonsense", "t", "b", ag.id)
