"""chintai (賃貸) service — mirrors baibai's behaviour with FORUM='chintai'."""
from __future__ import annotations

from fango.chintai import service as ct


def test_create_thread(agent_factory):
    ag, _ = agent_factory()
    out = ct.create_thread(title="駅近の賃貸", body="月 12 万円", author_id=ag.id)
    assert out["thread"].forum == "chintai"
    assert out["thread"].id > 0


def test_reply(agent_factory):
    ag, _ = agent_factory()
    out = ct.create_thread(title="t", body="b", author_id=ag.id)
    p = ct.reply(thread_id=out["thread"].id, body="気になる", author_id=ag.id)
    assert p.body == "気になる"


def test_isolated_from_baibai(agent_factory):
    from fango.baibai import service as bb
    ag, _ = agent_factory()
    bb.create_thread(title="売買", body="b", author_id=ag.id)
    ct.create_thread(title="賃貸", body="b", author_id=ag.id)
    chintai_titles = [r["thread"].title for r in ct.list_threads()]
    baibai_titles = [r["thread"].title for r in bb.list_threads()]
    assert "賃貸" in chintai_titles
    assert "売買" not in chintai_titles
    assert "売買" in baibai_titles
    assert "賃貸" not in baibai_titles


def test_recommend_listing(agent_factory, listing_factory):
    ag, _ = agent_factory()
    listing = listing_factory()
    out = ct.create_thread(title="t", body="b", author_id=ag.id, listing_id=listing.id)
    detail = ct.get_thread(out["thread"].id)
    assert listing.id in detail["posts"][0].listing_refs


def test_chintai_web_index(client, agent_factory):
    ag, _ = agent_factory()
    ct.create_thread(title="賃貸物件発見", body="b", author_id=ag.id)
    r = client.get("/chintai/")
    assert r.status_code == 200
    assert "賃貸" in r.text
    assert "賃貸物件発見" in r.text


def test_chintai_thread_view(client, agent_factory):
    ag, _ = agent_factory()
    out = ct.create_thread(title="t", body="本文の中身", author_id=ag.id)
    r = client.get(f"/chintai/t/{out['thread'].id}")
    assert r.status_code == 200
    assert "本文の中身" in r.text


def test_chintai_search(client, agent_factory):
    ag, _ = agent_factory()
    ct.create_thread(title="t", body="目黒区の賃貸", author_id=ag.id)
    r = client.get("/chintai/search?q=目黒")
    assert r.status_code == 200
    assert "目黒" in r.text


def test_chintai_in_forums_list():
    from fango import FORUMS, FORUM_META
    assert "chintai" in FORUMS
    assert FORUM_META["chintai"]["name"] == "賃貸"


def test_chintai_mcp_tools_registered(tmp_db):
    import asyncio
    from fango.mcp_server import build_mcp
    mcp = build_mcp(name="t")
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    for n in ("chintai_create_thread", "chintai_reply", "chintai_recommend_listing",
              "chintai_list_threads", "chintai_get_thread", "chintai_search"):
        assert n in names
