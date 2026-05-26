"""CLI scripts: smoke tests."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout, redirect_stderr


def test_issue_agent_key_smoke(tmp_db, capsys):
    from scripts.issue_agent_key import main
    rc = main(["scripts-test-agent", "--vendor", "test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "scripts-test-agent" in out
    key_line = out.splitlines()[-1]
    assert len(key_line) > 30


def test_issue_agent_key_duplicate(tmp_db, capsys):
    from scripts.issue_agent_key import main
    assert main(["dup"]) == 0
    assert main(["dup"]) == 2


def test_build_prefecture_borders_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.build_prefecture_borders.SEED_DIR", tmp_path)
    from scripts import build_prefecture_borders
    rc = build_prefecture_borders.main()
    assert rc == 0
    out = tmp_path / "prefecture_borders.json"
    data = json.loads(out.read_text())
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 47


def test_render_pdf_assets_no_raw_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.render_pdf_assets.RAW_DIR", tmp_path / "nonexistent")
    monkeypatch.setattr("scripts.render_pdf_assets.SEED_DIR", tmp_path)
    from scripts import render_pdf_assets
    rc = render_pdf_assets.main()
    assert rc == 0
    out = tmp_path / "listing-rain.json"
    assert out.exists()
    assert json.loads(out.read_text()) == []


def test_ingest_notion_unconfigured(tmp_db, monkeypatch, capsys):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_LISTINGS_DATABASE_ID", raising=False)
    from scripts import ingest_notion
    rc = ingest_notion.main([])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Notion not configured" in err
