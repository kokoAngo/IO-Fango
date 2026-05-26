"""Agent authentication: key hashing + 3-tier resolution.

Resolution priority (highest first):
  1. ``current_agent`` ContextVar (set by HTTP middleware / test harness)
  2. ``FANGO_AGENT_KEY`` env var
  3. ``X-Agent-Key`` HTTP header (passed in explicitly)

Plaintext keys are never stored — only SHA-256.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from contextvars import ContextVar
from typing import Optional

from .config import load_settings
from .db import connect
from .models import Agent

# ContextVar so HTTP middleware / tests can scope an agent for the call.
current_agent_var: ContextVar[Optional[Agent]] = ContextVar("current_agent", default=None)

KEY_BYTES = 32  # 256-bit


class AuthError(Exception):
    """Raised when an agent cannot be authenticated."""


def generate_key() -> str:
    """Return a fresh url-safe agent key (plaintext, returned once to caller)."""
    return secrets.token_urlsafe(KEY_BYTES)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_agent(name: str, vendor: str | None = None, conn: sqlite3.Connection | None = None) -> tuple[Agent, str]:
    """Create a new agent record, returning (Agent, plaintext_key)."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        key = generate_key()
        kh = hash_key(key)
        cur = conn.execute(
            "INSERT INTO agents(name, key_hash, vendor) VALUES (?, ?, ?)",
            (name, kh, vendor),
        )
        agent_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        return Agent.from_row(row), key
    finally:
        if owns_conn:
            conn.close()


def lookup_by_key(key: str, conn: sqlite3.Connection | None = None) -> Agent | None:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM agents WHERE key_hash = ? AND active = 1",
            (hash_key(key),),
        ).fetchone()
        return Agent.from_row(row) if row else None
    finally:
        if owns_conn:
            conn.close()


def revoke_agent(agent_id: int, conn: sqlite3.Connection | None = None) -> None:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        conn.execute("UPDATE agents SET active = 0 WHERE id = ?", (agent_id,))
    finally:
        if owns_conn:
            conn.close()


import re as _re

# Same shape as the onboarding form pattern (claim.html).
_AGENT_NAME_RE = _re.compile(r"^[A-Za-z0-9_\-]+$")


class NameError(Exception):
    """Raised on invalid or conflicting agent name."""


def validate_rename(
    agent_id: int,
    new_name: str,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Pre-flight rename check. Returns the normalised name on success.

    Raises NameError without mutating anything. Callers should run this
    before consuming any quota so a failed validation doesn't burn the
    day's rename allowance.
    """
    new_name = (new_name or "").strip()
    if not new_name:
        raise NameError("name required")
    if len(new_name) > 40:
        raise NameError("name too long (max 40 chars)")
    if not _AGENT_NAME_RE.match(new_name):
        raise NameError("name must match [A-Za-z0-9_-]+")
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        existing = conn.execute(
            "SELECT name FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if existing is None:
            raise NameError(f"agent {agent_id} not found")
        if existing["name"] == new_name:
            # Caller will short-circuit before touching quota.
            return new_name
        clash = conn.execute(
            "SELECT id FROM agents WHERE name = ? AND id != ?",
            (new_name, agent_id),
        ).fetchone()
        if clash is not None:
            raise NameError(f"name {new_name!r} already taken")
        return new_name
    finally:
        if owns_conn:
            conn.close()


def rename_agent(agent_id: int, new_name: str, conn: sqlite3.Connection | None = None) -> Agent:
    """Rename an existing agent. Preserves id, key_hash, vendor, created_at.

    Performs the same validation as :func:`validate_rename`. For paths that
    want to validate before consuming a quota, call ``validate_rename`` first
    and only ``rename_agent`` once validation has succeeded.
    """
    new_name = validate_rename(agent_id, new_name, conn=conn)
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        existing = conn.execute(
            "SELECT * FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if existing["name"] == new_name:
            return Agent.from_row(existing)
        conn.execute("UPDATE agents SET name = ? WHERE id = ?", (new_name, agent_id))
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return Agent.from_row(row)
    finally:
        if owns_conn:
            conn.close()


def resolve_agent(header_key: str | None = None, conn: sqlite3.Connection | None = None) -> Agent | None:
    """Resolve the current agent using the 3-tier priority chain."""
    # 1. ContextVar
    ctx_agent = current_agent_var.get()
    if ctx_agent is not None:
        # Re-load to honour soft-revoke.
        owns_conn = conn is None
        if conn is None:
            conn = connect()
        try:
            row = conn.execute(
                "SELECT * FROM agents WHERE id = ? AND active = 1", (ctx_agent.id,)
            ).fetchone()
            return Agent.from_row(row) if row else None
        finally:
            if owns_conn:
                conn.close()

    # 2. env var
    settings = load_settings()
    if settings.agent_key:
        agent = lookup_by_key(settings.agent_key, conn=conn)
        if agent is not None:
            return agent

    # 3. header
    if header_key:
        return lookup_by_key(header_key, conn=conn)

    return None


def require_agent(header_key: str | None = None, conn: sqlite3.Connection | None = None) -> Agent:
    agent = resolve_agent(header_key=header_key, conn=conn)
    if agent is None:
        raise AuthError("agent key required (set FANGO_AGENT_KEY or send X-Agent-Key header)")
    return agent


def set_current_agent(agent: Agent | None):
    """Set the ContextVar; returns the token for later reset()."""
    return current_agent_var.set(agent)
