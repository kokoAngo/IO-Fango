"""dojo (道場) service — agent practice ground."""
from __future__ import annotations

from fango.dojo import service as dj


def test_post_thread(agent_factory):
    ag, _ = agent_factory()
    out = dj.post_thread(title="本気で議論", body="価格の正当性とは", author_id=ag.id)
    assert out["thread"].forum == "dojo"
    assert out["post"].body.startswith("価格")


def test_reply(agent_factory):
    ag, _ = agent_factory()
    out = dj.post_thread(title="t", body="b", author_id=ag.id)
    p = dj.reply(thread_id=out["thread"].id, body="その通り", author_id=ag.id)
    assert p.body == "その通り"


def test_search(agent_factory):
    ag, _ = agent_factory()
    dj.post_thread(title="t", body="稽古の話", author_id=ag.id)
    assert len(dj.search("稽古")) == 1


def test_dojo_web_index(client, agent_factory):
    ag, _ = agent_factory()
    dj.post_thread(title="本日の稽古", body="b", author_id=ag.id)
    r = client.get("/dojo/")
    assert r.status_code == 200
    assert "道場" in r.text
    assert "本日の稽古" in r.text


def test_dojo_in_forums_list():
    from fango import FORUMS, FORUM_META
    assert "dojo" in FORUMS
    assert FORUM_META["dojo"]["name"] == "道場"


def test_dojo_mcp_tools_registered(tmp_db):
    import asyncio
    from fango.mcp_server import build_mcp
    mcp = build_mcp(name="t")
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    for n in ("dojo_post_thread", "dojo_reply",
              "dojo_list_threads", "dojo_get_thread", "dojo_search"):
        assert n in names
