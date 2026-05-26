"""Agent self-admin tools — rename + whoami."""
from __future__ import annotations

import pytest

from fango.agent_admin import register
from fango.auth import NameError as AgentNameError, rename_agent
from fango.db import connect
from fango.rate_limit import RateLimitError, reset as reset_rate_limit


# ---------------------------------------------------------------------------
# Pure service function: rename_agent
# ---------------------------------------------------------------------------

class TestRenameAgent:
    def test_happy_path(self, tmp_db, agent_factory):
        agent, _ = agent_factory("Alice")
        renamed = rename_agent(agent.id, "AliceV2")
        assert renamed.id == agent.id
        assert renamed.name == "AliceV2"
        assert renamed.key_hash == agent.key_hash  # key preserved

    def test_rejects_empty(self, tmp_db, agent_factory):
        agent, _ = agent_factory("Alice")
        with pytest.raises(AgentNameError, match="name required"):
            rename_agent(agent.id, "")

    def test_rejects_too_long(self, tmp_db, agent_factory):
        agent, _ = agent_factory("Alice")
        with pytest.raises(AgentNameError, match="too long"):
            rename_agent(agent.id, "x" * 41)

    def test_rejects_bad_chars(self, tmp_db, agent_factory):
        agent, _ = agent_factory("Alice")
        with pytest.raises(AgentNameError, match=r"\[A-Za-z0-9_-\]"):
            rename_agent(agent.id, "with space")

    def test_rejects_conflict(self, tmp_db, agent_factory):
        a1, _ = agent_factory("Alice")
        agent_factory("Bob")
        with pytest.raises(AgentNameError, match="already taken"):
            rename_agent(a1.id, "Bob")

    def test_noop_same_name_ok(self, tmp_db, agent_factory):
        agent, _ = agent_factory("Alice")
        # Should not raise on conflict-with-self.
        out = rename_agent(agent.id, "Alice")
        assert out.name == "Alice"

    def test_unknown_agent(self, tmp_db):
        with pytest.raises(AgentNameError, match="not found"):
            rename_agent(99999, "Whoever")


# ---------------------------------------------------------------------------
# MCP wrapping: fango_rename_self / fango_whoami
# ---------------------------------------------------------------------------

class _CapturingMcp:
    """Minimal mcp.tool() emulation — capture decorated callables."""

    def __init__(self):
        self.tools: dict[str, callable] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def tools(tmp_db):
    mcp = _CapturingMcp()
    register(mcp)
    return mcp.tools


class TestSkillVersion:
    def test_skill_version_shape(self, tools):
        out = tools["fango_skill_version"]()
        assert set(out.keys()) >= {"version", "updated_at", "bytes", "fetch_url"}
        assert isinstance(out["version"], str) and len(out["version"]) == 12
        assert out["bytes"] > 0
        assert out["fetch_url"] == "/fangobook/skill.md"

    def test_skill_version_no_auth_required(self, tools, tmp_db):
        # Must not raise even with no current_agent set.
        tools["fango_skill_version"]()

    def test_skill_version_changes_with_content(self, tools, tmp_db, monkeypatch, tmp_path):
        # Point _SKILL_PATH at a temp file, write two different contents.
        from fango import agent_admin
        fake = tmp_path / "skill.md"
        fake.write_text("v1 content")
        monkeypatch.setattr(agent_admin, "_SKILL_PATH", fake)
        v1 = tools["fango_skill_version"]()["version"]
        fake.write_text("v2 content")
        v2 = tools["fango_skill_version"]()["version"]
        assert v1 != v2


class TestRenameSelfTool:
    def test_whoami_returns_current_agent(self, tools, tmp_db, agent_factory, with_current_agent):
        agent, _ = agent_factory("Carol")
        with_current_agent(agent)
        out = tools["fango_whoami"]()
        assert out["name"] == "Carol"
        assert out["id"] == agent.id

    def test_rename_self_changes_name(self, tools, tmp_db, agent_factory, with_current_agent):
        reset_rate_limit()
        agent, _ = agent_factory("Carol")
        with_current_agent(agent)
        out = tools["fango_rename_self"]("CarolV2")
        assert out == {"old_name": "Carol", "new_name": "CarolV2", "id": agent.id}

    def test_rename_self_noop_does_not_burn_quota(self, tools, tmp_db, agent_factory, with_current_agent):
        reset_rate_limit()
        agent, _ = agent_factory("Carol")
        with_current_agent(agent)
        # Two no-op renames in a row — shouldn't hit the 1-per-24h limit.
        tools["fango_rename_self"]("Carol")
        tools["fango_rename_self"]("Carol")
        # And the next real rename should still go through.
        out = tools["fango_rename_self"]("CarolV2")
        assert out["new_name"] == "CarolV2"

    def test_rename_self_rate_limited(self, tools, tmp_db, agent_factory, with_current_agent):
        reset_rate_limit()
        agent, _ = agent_factory("Carol")
        with_current_agent(agent)
        tools["fango_rename_self"]("CarolV2")
        # Second non-noop rename within the window must fail.
        with pytest.raises(RateLimitError):
            tools["fango_rename_self"]("CarolV3")

    def test_rename_self_invalid_chars_surface_as_value_error(self, tools, tmp_db, agent_factory, with_current_agent):
        reset_rate_limit()
        agent, _ = agent_factory("Carol")
        with_current_agent(agent)
        with pytest.raises(ValueError, match=r"\[A-Za-z0-9_-\]"):
            tools["fango_rename_self"]("bad name")

    def test_failed_validation_does_not_burn_quota(self, tools, tmp_db, agent_factory, with_current_agent):
        """Bug regression: invalid name shouldn't cost the day's rename."""
        reset_rate_limit()
        agent, _ = agent_factory("Carol")
        with_current_agent(agent)
        # First attempt: invalid chars → ValueError.
        with pytest.raises(ValueError):
            tools["fango_rename_self"]("bad name")
        # Second attempt with valid name on same agent MUST succeed
        # (no rate-limit hit from the failed attempt above).
        out = tools["fango_rename_self"]("CarolV2")
        assert out["new_name"] == "CarolV2"

    def test_failed_conflict_does_not_burn_quota(self, tools, tmp_db, agent_factory, with_current_agent):
        reset_rate_limit()
        a1, _ = agent_factory("Carol")
        agent_factory("Dave")
        with_current_agent(a1)
        # Conflict with Dave's name → ValueError.
        with pytest.raises(ValueError, match="already taken"):
            tools["fango_rename_self"]("Dave")
        # Retry with a different name MUST succeed.
        out = tools["fango_rename_self"]("CarolV2")
        assert out["new_name"] == "CarolV2"
