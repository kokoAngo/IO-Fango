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
    """FastAPI app with a fresh DB."""
    # Importing here so monkeypatched env var is picked up.
    import importlib
    from fango import http_app as mod
    importlib.reload(mod)
    return mod.app


@pytest.fixture
def client(http_app):
    from fastapi.testclient import TestClient
    return TestClient(http_app)


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
