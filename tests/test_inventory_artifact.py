"""export_inventory → import_inventory round trip.

The app server cannot reach the upstream database or its photo bucket, so the
artifact is the only thing that crosses. What matters is that it crosses
*completely* (listings, transports, photo bytes) and *narrowly* (nothing that
belongs to the receiving environment gets clobbered).
"""
from __future__ import annotations

from datetime import datetime, timezone

import gzip
import io
import json

import pytest

from fango import db as fango_db
from fango import uploads
from fango.listings import service as ls


def _jpeg(color=(10, 20, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


@pytest.fixture
def artifact_env(tmp_path, tmp_db, monkeypatch):
    """Repo-root and uploads dir relocated into tmp for both scripts."""
    import scripts.export_inventory as ex
    import scripts.import_inventory as im

    root = tmp_path / "root"
    up = root / "data" / "uploads"
    up.mkdir(parents=True)
    monkeypatch.setattr(ex, "REPO_ROOT", root)
    monkeypatch.setattr(im, "REPO_ROOT", root)
    monkeypatch.setattr(im, "UPLOADS_DIR", up)
    monkeypatch.setattr(uploads, "UPLOADS_DIR", up)
    return {"root": root, "uploads": up, "out": tmp_path / "artifact", "tmp": tmp_path}


def _now_iso() -> str:
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _make_source_listing(env, reins_id="100000000001", with_photo=True):
    listing = ls.insert_listing({
        "reins_id": reins_id, "building_name": "テスト物件", "prefecture": "東京都",
        "city": "港区", "ward": "港区", "layout": "2LDK", "price_man": 8000,
        "transaction_type": "sale", "ad_status": "広告可", "tenancy_status": "-",
        # source='pg' rows are subject to the visibility window, so a fixture
        # without a posting date would be invisible on both sides of the trip.
        "source": "pg", "posted_at": _now_iso(),
    })
    ls.replace_transports(listing.id, [
        {"line": "日比谷線", "station": "六本木", "walk_minutes": 3, "sort_order": 0},
    ])
    if with_photo:
        blob = _jpeg()
        name = __import__("hashlib").sha256(blob).hexdigest() + ".jpg"
        (env["uploads"] / name).write_bytes(blob)
        ls.replace_images(listing.id, [{
            "kind": "raw", "rel_path": f"data/uploads/{name}",
            "label": "外観", "sort_order": 0,
        }])
    return listing


def _export(env, *extra):
    from scripts.export_inventory import main
    assert main(["--out", str(env["out"]), *extra]) == 0
    return json.loads((env["out"] / "manifest.json").read_text(encoding="utf-8"))


def _import_into_fresh_db(env, monkeypatch, *extra):
    """Point the process at an empty database, then import."""
    from scripts.import_inventory import main
    monkeypatch.setenv("FANGO_DB_PATH", str(env["tmp"] / "receiver.db"))
    fango_db.reset_bootstrap_cache()
    return main(["--in", str(env["out"]), *extra])


def test_round_trip_carries_listing_transports_and_photo_bytes(artifact_env, monkeypatch):
    env = artifact_env
    _make_source_listing(env)
    manifest = _export(env)
    assert manifest["listings"] == 1
    assert manifest["photo_files"] == 1
    assert manifest["missing_photo_files"] == 0

    # The photo bytes really travel — the far side has no access to the bucket.
    assert len(list((env["out"] / "photos").iterdir())) == 1
    # Wipe the sender's uploads so a pass can only come from the artifact.
    for f in env["uploads"].iterdir():
        f.unlink()

    assert _import_into_fresh_db(env, monkeypatch) == 0

    got = ls.search_listings(criteria={"prefecture": "東京都"}, limit=5)
    assert len(got) == 1
    assert got[0].reins_id == "100000000001"
    assert got[0].building_name == "テスト物件"
    assert ls.get_listing_transports(got[0].id)[0]["station"] == "六本木"
    imgs = ls.get_listing_images(got[0].id)
    assert len(imgs) == 1
    assert imgs[0]["label"] == "外観"
    # rel_path must point at a file that exists here, not at the sender's disk.
    assert (env["root"] / imgs[0]["rel_path"]).is_file()


def test_import_is_idempotent(artifact_env, monkeypatch):
    env = artifact_env
    _make_source_listing(env)
    _export(env)
    _import_into_fresh_db(env, monkeypatch)
    from scripts.import_inventory import main
    assert main(["--in", str(env["out"])]) == 0
    assert ls.count_listings(criteria={"include_non_advertisable": True}) == 1
    assert len(ls.get_listing_images(
        ls.search_listings(criteria={"prefecture": "東京都"}, limit=1)[0].id)) == 1


def test_export_skips_rows_the_sync_does_not_own(artifact_env):
    """A broker's own listing lives only on the server that created it; the
    artifact must not carry it back and forth between environments."""
    env = artifact_env
    _make_source_listing(env)
    ls.insert_listing({
        "reins_id": "BROKER-1", "building_name": "自社物件", "prefecture": "東京都",
        "city": "港区", "rent_yen": 90_000, "transaction_type": "rent",
        "ad_status": "可",      # source left NULL
    })
    _export(env)
    with gzip.open(env["out"] / "listings.ndjson.gz", "rt", encoding="utf-8") as fh:
        keys = [json.loads(l)["listing"]["reins_id"] for l in fh if l.strip()]
    assert keys == ["100000000001"]


def test_export_omits_environment_local_columns(artifact_env):
    """`id` and `broker_agent_id` are meaningless in the other database — the
    receiving side assigns brokers itself, and an import must not clobber it."""
    from scripts.export_inventory import EXPORT_COLUMNS
    assert "id" not in EXPORT_COLUMNS
    assert "broker_agent_id" not in EXPORT_COLUMNS

    env = artifact_env
    _make_source_listing(env)
    _export(env)
    with gzip.open(env["out"] / "listings.ndjson.gz", "rt", encoding="utf-8") as fh:
        rec = json.loads(fh.readline())
    assert "broker_agent_id" not in rec["listing"]


def test_import_preserves_a_local_broker_assignment(artifact_env, monkeypatch):
    env = artifact_env
    _make_source_listing(env)
    _export(env)
    _import_into_fresh_db(env, monkeypatch)

    listing = ls.search_listings(criteria={"prefecture": "東京都"}, limit=1)[0]
    # The agent must be created in the RECEIVING database — agent ids are
    # per-environment, which is the whole reason broker_agent_id never travels.
    from fango.auth import create_agent
    from fango.db import connect
    with connect() as conn:
        broker, _ = create_agent("a-broker", vendor="test", conn=conn)
        conn.execute("UPDATE listings SET broker_agent_id = ? WHERE id = ?",
                     (broker.id, listing.id))

    from scripts.import_inventory import main
    assert main(["--in", str(env["out"])]) == 0
    with connect() as conn:
        owner = conn.execute("SELECT broker_agent_id FROM listings WHERE id = ?",
                             (listing.id,)).fetchone()[0]
    assert owner == broker.id


def test_import_refuses_an_unknown_artifact_version(artifact_env, monkeypatch, capsys):
    env = artifact_env
    _make_source_listing(env)
    _export(env)
    m = json.loads((env["out"] / "manifest.json").read_text(encoding="utf-8"))
    m["artifact_version"] = 999
    (env["out"] / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    assert _import_into_fresh_db(env, monkeypatch) == 2
    assert "refusing" in capsys.readouterr().err


def test_export_rejects_a_local_time_since(artifact_env, capsys):
    """updated_at is a UTC '...Z' string compared as a string; a local-time
    --since matches nothing, which is indistinguishable from 'no changes'."""
    from scripts.export_inventory import main
    env = artifact_env
    assert main(["--out", str(env["out"]), "--since", "2026-09-04T17:00:00"]) == 2
    assert "must be UTC" in capsys.readouterr().err


def test_incremental_export_uses_the_previous_watermark(artifact_env):
    env = artifact_env
    _make_source_listing(env, "100000000001")
    first = _export(env)
    assert first["listings"] == 1
    # Nothing changed since: feeding the watermark back yields only the rows
    # at or after it (the same row, at the boundary) — never a silent zero.
    again = _export(env, "--since", first["watermark"])
    assert again["listings"] == 1

    _make_source_listing(env, "100000000002")
    third = _export(env, "--since", first["watermark"])
    assert third["listings"] == 2


def test_metadata_only_export_links_photos_the_receiver_already_has(artifact_env, monkeypatch, capsys):
    """Photos are content-addressed, so an artifact need not re-ship bytes the
    receiver already stores — that is what makes incremental exports cheap."""
    env = artifact_env
    _make_source_listing(env)
    m = _export(env, "--no-photos")
    assert m["photos_included"] is False
    assert _import_into_fresh_db(env, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "imported: 1" in out
    assert "photos linked: 1" in out


def test_metadata_only_export_drops_photos_the_receiver_lacks(artifact_env, monkeypatch, capsys):
    env = artifact_env
    _make_source_listing(env)
    _export(env, "--no-photos")
    # Simulate the real split: the receiver has never seen these bytes.
    for f in env["uploads"].iterdir():
        f.unlink()
    assert _import_into_fresh_db(env, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "imported: 1" in out
    # The reference is dropped rather than recorded as a path that 404s.
    assert "missing from artifact: 1" in out
    listing = ls.search_listings(criteria={"prefecture": "東京都"}, limit=1)[0]
    assert ls.get_listing_images(listing.id) == []
