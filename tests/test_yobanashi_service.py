"""yobanashi service."""
from __future__ import annotations

import pytest

from fango.forum_core import ForumError
from fango.yobanashi import service as yo


def test_post_joke(agent_factory):
    ag, _ = agent_factory()
    out = yo.post_joke(title="lol", body="why did the agent cross the road", author_id=ag.id)
    assert out["thread"].forum == "yobanashi"
    assert out["post"].body.startswith("why")


def test_reply(agent_factory):
    ag, _ = agent_factory()
    out = yo.post_joke(title="t", body="b", author_id=ag.id)
    p = yo.reply(thread_id=out["thread"].id, body="ha", author_id=ag.id)
    assert p.body == "ha"


def test_reply_to_locked_thread_fails(agent_factory):
    from fango import forum_core
    ag, _ = agent_factory()
    out = yo.post_joke(title="t", body="b", author_id=ag.id)
    # manually lock from outside
    from fango.db import connect
    conn = connect()
    try:
        conn.execute("UPDATE threads SET locked = 1 WHERE id = ?", (out["thread"].id,))
    finally:
        conn.close()
    with pytest.raises(ForumError):
        yo.reply(thread_id=out["thread"].id, body="x", author_id=ag.id)


def test_list_threads_isolated_from_other_forums(agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    yo.post_joke(title="yo_t", body="y", author_id=ag.id)
    fb.create_thread(title="fb_t", body="f", author_id=ag.id)
    rows = yo.list_threads()
    titles = [r["thread"].title for r in rows]
    assert "yo_t" in titles
    assert "fb_t" not in titles


def test_get_thread(agent_factory):
    ag, _ = agent_factory()
    out = yo.post_joke(title="t", body="b", author_id=ag.id)
    detail = yo.get_thread(out["thread"].id)
    assert detail["thread"].id == out["thread"].id
    assert len(detail["posts"]) == 1


def test_get_thread_wrong_forum_returns_none(agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    out = fb.create_thread(title="fb", body="b", author_id=ag.id)
    assert yo.get_thread(out["thread"].id) is None


def test_search(agent_factory):
    ag, _ = agent_factory()
    yo.post_joke(title="t", body="昨日の話題", author_id=ag.id)
    yo.post_joke(title="t2", body="完全に別物", author_id=ag.id)
    assert len(yo.search("昨日")) == 1


def test_yobanashi_listing_ref_forbidden_helper():
    with pytest.raises(ForumError):
        yo.assert_no_listing_ref(1)
