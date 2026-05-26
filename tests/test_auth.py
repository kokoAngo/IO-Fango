"""Auth: key hashing, 3-tier resolution priority, soft-revoke."""
from __future__ import annotations

import pytest

from fango import auth
from fango.auth import (
    AuthError,
    create_agent,
    generate_key,
    hash_key,
    lookup_by_key,
    require_agent,
    resolve_agent,
    revoke_agent,
)
from fango.db import connect


def test_generate_key_is_unique():
    keys = {generate_key() for _ in range(50)}
    assert len(keys) == 50


def test_hash_key_deterministic():
    assert hash_key("abc") == hash_key("abc")
    assert hash_key("abc") != hash_key("abd")


def test_create_agent_returns_plaintext_once(tmp_db):
    conn = connect(tmp_db)
    try:
        agent, key = create_agent("alice", vendor="anthropic", conn=conn)
    finally:
        conn.close()
    assert agent.id > 0
    assert agent.name == "alice"
    assert agent.vendor == "anthropic"
    assert agent.active == 1
    assert agent.key_hash == hash_key(key)
    assert isinstance(key, str) and len(key) > 30


def test_lookup_by_key(agent_factory):
    ag, key = agent_factory("bob")
    found = lookup_by_key(key)
    assert found is not None
    assert found.id == ag.id


def test_lookup_unknown_key_returns_none(tmp_db):
    assert lookup_by_key("not-a-real-key") is None


def test_resolve_via_header(agent_factory):
    ag, key = agent_factory("carol")
    resolved = resolve_agent(header_key=key)
    assert resolved is not None
    assert resolved.id == ag.id


def test_resolve_via_env(agent_factory, monkeypatch):
    ag, key = agent_factory("dave")
    monkeypatch.setenv("FANGO_AGENT_KEY", key)
    resolved = resolve_agent(header_key=None)
    assert resolved is not None
    assert resolved.id == ag.id


def test_resolve_via_contextvar(agent_factory, with_current_agent):
    ag, _ = agent_factory("eve")
    with_current_agent(ag)
    resolved = resolve_agent(header_key="bogus")
    assert resolved is not None
    assert resolved.id == ag.id


def test_contextvar_beats_env_and_header(agent_factory, monkeypatch, with_current_agent):
    ctx_ag, _ = agent_factory("ctx-agent")
    env_ag, env_key = agent_factory("env-agent")
    hdr_ag, hdr_key = agent_factory("hdr-agent")
    monkeypatch.setenv("FANGO_AGENT_KEY", env_key)
    with_current_agent(ctx_ag)
    resolved = resolve_agent(header_key=hdr_key)
    assert resolved.id == ctx_ag.id


def test_env_beats_header(agent_factory, monkeypatch):
    env_ag, env_key = agent_factory("env-only")
    hdr_ag, hdr_key = agent_factory("hdr-only")
    monkeypatch.setenv("FANGO_AGENT_KEY", env_key)
    resolved = resolve_agent(header_key=hdr_key)
    assert resolved.id == env_ag.id


def test_resolve_none_when_no_source(tmp_db):
    assert resolve_agent() is None


def test_require_agent_raises_when_missing(tmp_db):
    with pytest.raises(AuthError):
        require_agent()


def test_revoked_agent_not_resolved(agent_factory):
    ag, key = agent_factory("revokeme")
    revoke_agent(ag.id)
    assert lookup_by_key(key) is None
    assert resolve_agent(header_key=key) is None


def test_revoked_agent_blocks_contextvar(agent_factory, with_current_agent):
    ag, _ = agent_factory("dropped")
    with_current_agent(ag)
    revoke_agent(ag.id)
    assert resolve_agent() is None
