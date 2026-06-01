"""Agent activity stream — formatters, recording, and the /activity page."""
from __future__ import annotations

import pytest

from fango import activity


# ---------------------------------------------------------------------------
# Formatter unit tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,kwargs,expect_sub,expect_forum", [
    ("wiki_lookup", {"keyword": "賃貸"}, "wiki「賃貸」を検索", "wiki"),
    ("wiki_catalog", {}, "wiki カタログを参照", "wiki"),
    ("fango_get_listing", {"listing_id": 42}, "物件 #42 を閲覧", None),
    ("baibai_list_threads", {}, "を閲覧", "baibai"),
    ("chintai_get_thread", {"thread_id": 7}, "スレッド #7 を閲覧", "chintai"),
    ("dojo_search", {"query": "AI"}, "を検索", "dojo"),
    ("fango_save_search", {"name": "渋谷2LDK"}, "保存検索「渋谷2LDK」を登録", None),
    ("some_unknown_tool", {}, "some unknown tool", None),
])
def test_summarize(tool, kwargs, expect_sub, expect_forum):
    action, forum_ctx = activity._summarize(tool, kwargs)
    assert expect_sub in action
    assert forum_ctx == expect_forum


def test_search_uses_criteria_digest():
    action, _ = activity._summarize(
        "fango_search_listings",
        {"criteria": {"prefecture": "東京都", "layout": "1LDK", "rent_max_yen": 250_000}},
    )
    assert action.startswith("物件検索:")
    assert "東京都" in action and "1LDK" in action


# ---------------------------------------------------------------------------
# Recording + retrieval
# ---------------------------------------------------------------------------

def test_record_inserts_row_anonymous(tmp_db):
    activity.record("wiki_lookup", None, {"keyword": "中古"})
    rows = activity.recent()
    assert len(rows) == 1
    r = rows[0]
    assert r["agent_id"] is None
    # Unauthenticated caller → guest label (no stable identity to pseudonymise).
    assert r["agent_label"] == "ゲスト"
    assert r["tool"] == "wiki_lookup"
    assert r["action"] == "wiki「中古」を検索"
    assert r["forum_ctx"] == "wiki"


def test_record_named_agent_is_pseudonymised(tmp_db, agent_factory):
    from fango.auth import pseudonym
    agent, _ = agent_factory("mira-7")
    activity.record("fango_search_listings", agent, {"criteria": {"prefecture": "東京都"}})
    rows = activity.recent()
    # agent_id is kept (drives the stable avatar); the name is a stable pseudonym,
    # never the real name.
    assert rows[0]["agent_id"] == agent.id
    assert rows[0]["agent_label"] == pseudonym(agent.id)
    assert "mira-7" not in rows[0]["agent_label"]
    # pseudonym is [A-Za-z0-9_].
    assert rows[0]["agent_label"].replace("_", "").isalnum()


def test_record_never_raises_on_bad_input(tmp_db):
    # Bad kwargs / weird tool must not bubble up.
    activity.record("fango_search_listings", None, None)
    activity.record("baibai_reply", None, {"thread_id": None})
    assert len(activity.recent()) == 2


def test_mcp_server_hook_records(tmp_db):
    """The central instrumentation helper inserts an activity row."""
    from fango import mcp_server
    mcp_server._record_activity("wiki_catalog", {})
    rows = activity.recent()
    assert any(r["tool"] == "wiki_catalog" for r in rows)


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

# The standalone エージェント実況 page is retired (per-forum live feeds replace
# it). The activity module/table stay dormant — the formatter/record unit tests
# above still exercise them — but the /activity route is gone.
_ACTIVITY_RETIRED = pytest.mark.skip(
    reason="/activity stream retired in favour of per-forum live feeds. 再開時に解除。"
)


@_ACTIVITY_RETIRED
def test_activity_page_renders(client, tmp_db):
    activity.record("wiki_lookup", None, {"keyword": "渋谷"})
    resp = client.get("/activity")
    assert resp.status_code == 200
    assert "エージェント実況" in resp.text
    assert "wiki「渋谷」を検索" in resp.text


@_ACTIVITY_RETIRED
def test_activity_empty_state(client):
    resp = client.get("/activity")
    assert resp.status_code == 200
    assert "まだ動きはありません" in resp.text


def test_activity_route_removed(client):
    # The standalone stream is gone; the route 404s now.
    assert client.get("/activity").status_code == 404
