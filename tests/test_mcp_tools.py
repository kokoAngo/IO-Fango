"""MCP tool integration: registration + auth-aware invocation."""
from __future__ import annotations

import pytest

from fango.auth import AuthError, current_agent_var

# wiki 栏目は人+agent ともに一時停止中。MCP ツール経由のテストはスキップする。
# 再開時(mcp_server.py の wiki_tools.register / skill.md のコメントを外す時)に解除。
_WIKI_SUSPENDED = pytest.mark.skip(
    reason="wiki 栏目を一時停止中（mcp_server.py / skill.md のコメント参照）。再開時に解除。"
)

# Direct posting MCP tools are suspended (forum_post_tools.DIRECT_POSTING_ENABLED);
# posting now flows through fango_consult. The service layer is still tested
# directly in test_{baibai,chintai,chat,dojo}_service.py.
_POST_SUSPENDED = pytest.mark.skip(
    reason="direct posting MCP tools suspended (DIRECT_POSTING_ENABLED). 再開時に解除。"
)


def _mcp(tmp_db):
    """Build a fresh FastMCP instance bound to the test DB."""
    from fango.mcp_server import build_mcp
    return build_mcp(name="test")


def _call(mcp, name, args=None):
    """Synchronously invoke a FastMCP tool by name and return Python value(s)."""
    import asyncio
    from mcp.types import TextContent
    res = asyncio.run(mcp.call_tool(name, args or {}))
    # FastMCP.call_tool returns (content_list, result_dict) in recent versions.
    if isinstance(res, tuple) and len(res) == 2:
        return res[1]
    return res


def test_mcp_registers_all_tools(tmp_db):
    import asyncio
    mcp = _mcp(tmp_db)
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    # Read tools stay; direct write tools are suspended (posting now flows
    # through fango_consult). wiki 栏目 also suspended.
    expected = {
        "baibai_list_threads", "baibai_get_thread", "baibai_search",
        "chintai_list_threads", "chintai_get_thread", "chintai_search",
        "chat_list_threads", "chat_get_thread", "chat_search",
        "dojo_list_threads", "dojo_get_thread", "dojo_search",
        "fango_consult", "fango_get_listing",
        # Structured search re-enabled as the engine surface for a caller's own
        # LLM (recommendation is the caller's job, not the server's).
        "fango_search_listings",
    }
    assert expected <= names
    suspended = {
        "baibai_create_thread", "baibai_reply", "baibai_recommend_listing",
        "chintai_create_thread", "chintai_reply", "chintai_recommend_listing",
        "chat_post_joke", "chat_reply", "dojo_post_thread", "dojo_reply",
        "fango_attach_image", "fango_upload_image",
        "wiki_lookup", "wiki_catalog",
    }
    assert not (suspended & names), suspended & names


def test_tool_requires_agent(tmp_db):
    mcp = _mcp(tmp_db)
    with pytest.raises(Exception):
        _call(mcp, "baibai_create_thread", {"title": "t", "body": "b"})


@_POST_SUSPENDED
def test_tool_with_contextvar(tmp_db, agent_factory, with_current_agent):
    ag, _ = agent_factory()
    with_current_agent(ag)
    mcp = _mcp(tmp_db)
    result = _call(mcp, "baibai_create_thread", {"title": "hi", "body": "body"})
    assert "thread" in result or "post" in result or "structuredContent" in result
    # extract dict
    payload = result.get("structuredContent") if isinstance(result, dict) and "structuredContent" in result else result
    if isinstance(payload, dict) and "thread" in payload:
        assert payload["thread"]["forum"] == "baibai"


@_POST_SUSPENDED
def test_tool_with_env_key(tmp_db, agent_factory, monkeypatch):
    ag, key = agent_factory()
    monkeypatch.setenv("FANGO_AGENT_KEY", key)
    mcp = _mcp(tmp_db)
    result = _call(mcp, "chat_post_joke", {"title": "haha", "body": "joke"})
    payload = result.get("structuredContent", result) if isinstance(result, dict) else result
    if isinstance(payload, dict) and "thread" in payload:
        assert payload["thread"]["forum"] == "chat"


@_WIKI_SUSPENDED
def test_wiki_lookup_no_auth_required(tmp_db, agent_factory, with_current_agent):
    ag, _ = agent_factory()
    with_current_agent(ag)
    mcp = _mcp(tmp_db)
    # Seed something
    _call(mcp, "baibai_create_thread", {"title": "t", "body": "六本木の話題"})
    # Drop the agent and confirm wiki still works
    current_agent_var.set(None)
    result = _call(mcp, "wiki_lookup", {"keyword": "六本木"})
    payload = result.get("structuredContent", result) if isinstance(result, dict) else result
    assert isinstance(payload, dict)
    assert "posts" in payload


@_POST_SUSPENDED
def test_recommend_listing_via_tool(tmp_db, agent_factory, listing_factory, with_current_agent):
    ag, _ = agent_factory()
    listing = listing_factory()
    with_current_agent(ag)
    mcp = _mcp(tmp_db)
    out = _call(mcp, "baibai_create_thread", {"title": "t", "body": "b"})
    payload = out.get("structuredContent", out) if isinstance(out, dict) else out
    post_id = payload["post"]["id"]
    rec = _call(mcp, "baibai_recommend_listing", {"post_id": post_id, "listing_id": listing.id})
    rec_payload = rec.get("structuredContent", rec) if isinstance(rec, dict) else rec
    assert rec_payload["ref_id"] > 0


@_POST_SUSPENDED
def test_dojo_post_thread_via_tool(tmp_db, agent_factory, with_current_agent):
    ag, _ = agent_factory()
    with_current_agent(ag)
    mcp = _mcp(tmp_db)
    out = _call(mcp, "dojo_post_thread", {"title": "稽古", "body": "本気の議論"})
    payload = out.get("structuredContent", out) if isinstance(out, dict) else out
    if isinstance(payload, dict) and "thread" in payload:
        assert payload["thread"]["forum"] == "dojo"


@_POST_SUSPENDED
def test_chintai_create_thread_via_tool(tmp_db, agent_factory, with_current_agent):
    ag, _ = agent_factory()
    with_current_agent(ag)
    mcp = _mcp(tmp_db)
    out = _call(mcp, "chintai_create_thread", {"title": "賃貸", "body": "b"})
    payload = out.get("structuredContent", out) if isinstance(out, dict) else out
    if isinstance(payload, dict) and "thread" in payload:
        assert payload["thread"]["forum"] == "chintai"


@_WIKI_SUSPENDED
def test_wiki_catalog_via_tool(tmp_db):
    mcp = _mcp(tmp_db)
    r = _call(mcp, "wiki_catalog", {})
    rp = r.get("structuredContent", r) if isinstance(r, dict) else r
    assert "thread_counts" in rp
    assert "listing_count" in rp
