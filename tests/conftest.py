"""Test fixtures: isolated DB per test, agent factory, HTTP client."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point FANGO_DB_PATH at a fresh sqlite file for the test."""
    from fango import db as fango_db

    db_path = tmp_path / "fango.db"
    monkeypatch.setenv("FANGO_DB_PATH", str(db_path))
    monkeypatch.delenv("FANGO_AGENT_KEY", raising=False)
    # The repo's .env may set a production FANGO_PUBLIC_BASE_URL; tests
    # should run as if it's unset so assertions about relative image URLs
    # stay valid regardless of who's running them.
    monkeypatch.delenv("FANGO_PUBLIC_BASE_URL", raising=False)
    # FastAPI TestClient uses Host: testserver. Make the MCP host allowlist
    # accept it so /mcp* endpoints are reachable in tests.
    monkeypatch.setenv(
        "FANGO_MCP_ALLOWED_HOSTS",
        "localhost,127.0.0.1,testserver",
    )
    fango_db.reset_bootstrap_cache()
    fango_db.bootstrap(db_path)
    yield db_path
    fango_db.reset_bootstrap_cache()


@pytest.fixture
def db_conn(tmp_db):
    from fango.db import connect
    conn = connect(tmp_db)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def agent_factory(tmp_db):
    """Create agents on demand. Returns (Agent, plaintext_key)."""
    from fango.auth import create_agent
    from fango.db import connect

    created: list = []

    def _make(name: str = "Tester", vendor: str | None = "test"):
        conn = connect(tmp_db)
        try:
            ag, key = create_agent(name, vendor=vendor, conn=conn)
        finally:
            conn.close()
        created.append(ag)
        return ag, key

    return _make


@pytest.fixture
def agent_and_key(agent_factory):
    return agent_factory("Alice")


@pytest.fixture
def with_current_agent():
    """Context-manager-style: set current_agent ContextVar inside the test."""
    from fango.auth import current_agent_var

    tokens: list = []

    def _set(agent):
        tokens.append(current_agent_var.set(agent))

    yield _set
    for tok in reversed(tokens):
        current_agent_var.reset(tok)


@pytest.fixture
def http_app(tmp_db):
    """FastAPI app with a fresh DB.

    Also resets the module-level MCP singleton so each test gets a fresh
    StreamableHTTPSessionManager (its ``run()`` can only be called once
    per instance).
    """
    import importlib
    from fango import mcp_server, http_app as mod
    mcp_server._mcp_singleton = None
    importlib.reload(mod)
    return mod.app


@pytest.fixture
def client(http_app):
    """FastAPI TestClient.

    The context-manager form triggers the lifespan startup, which is
    required for the streamable-HTTP MCP session manager (otherwise tools
    hitting /mcp2/mcp fail with "Task group is not initialized").
    """
    from fastapi.testclient import TestClient
    with TestClient(http_app) as c:
        yield c


@pytest.fixture
def listing_factory(tmp_db):
    """Insert a listing row directly. Returns its dict."""
    from fango.listings.service import insert_listing
    from fango.db import connect

    def _make(**overrides):
        defaults = {
            "building_name": "Sample Tower",
            "address": "東京都港区六本木1-1-1",
            "prefecture": "東京都",
            "city": "港区",
            "station": "六本木",
            "station_line": "日比谷線",
            "walk_minutes": 5,
            "layout": "2LDK",
            "area_sqm": 55.0,
            "price_man": 8500,
            "built_year": 2018,
            "floor": 12,
            "total_floors": 30,
            "url": "https://example.com/sample",
        }
        defaults.update(overrides)
        conn = connect(tmp_db)
        try:
            row = insert_listing(defaults, conn=conn)
        finally:
            conn.close()
        return row

    return _make
