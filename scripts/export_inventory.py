#!/usr/bin/env python3
"""Package the synced inventory into an artifact the app server can import.

    python -m scripts.export_inventory --out /tmp/inv
    python -m scripts.export_inventory --out /tmp/inv --since 2026-09-04T00:00:00Z

The app server cannot reach the upstream database or its photo bucket — both
are LAN-only — so the sync runs here and ships its result. What travels is an
inventory artifact, never the SQLite file: the app server's own
``data/fango.db`` holds forum threads, agents, agreements and escrow rows that
are not ours to overwrite.

Artifact layout::

    <out>/manifest.json          counts, watermark, schema version
    <out>/listings.ndjson.gz     one JSON object per listing (+ transports, photos)
    <out>/photos/<sha256>.<ext>  only the photos the artifact references

Photos are content-addressed, so an incremental export carries only pictures
the receiving side has not seen, and re-sending one is a no-op there.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.db import bootstrap, connect
from fango.listings.ingestion.postgres import SOURCE_PG

# Bumped when the artifact layout changes in a way the importer must notice.
ARTIFACT_VERSION = 1

# Columns that travel. Deliberately not "everything": `id` is local to each
# database (the app server has its own autoincrement, and reins_id is the key
# both sides agree on), and `broker_agent_id` points at an agents row whose id
# differs per environment — the broker fleet is assigned on the receiving side.
EXPORT_COLUMNS = (
    "reins_id", "title", "building_name", "building_name_kana", "address",
    "prefecture", "city", "ward", "station", "station_line", "walk_minutes",
    "layout", "area_sqm", "balcony_sqm", "price_man", "price_per_sqm_man",
    "rent_yen", "deposit_text", "key_money_text", "tenancy_status",
    "maintenance_fee_yen", "repair_fee_yen", "built_year", "built_month",
    "structure", "floor", "total_floors", "direction", "parking",
    "pet_allowed", "renovation", "listing_type", "transaction_type",
    "url", "agent_company", "ad_status", "raw_json", "last_seen_at", "source",
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", required=True, type=Path, help="Artifact directory to write")
    p.add_argument("--since", default=None,
                   help="Only listings with updated_at >= this UTC timestamp. Must be "
                        "'YYYY-MM-DD' or end in 'Z' — feed back the previous run's "
                        "manifest 'watermark'.")
    p.add_argument("--no-photos", action="store_true",
                   help="Metadata only; skip copying photo files")
    args = p.parse_args(argv)

    # updated_at is stored as a UTC string ('...Z') and compared as a string.
    # A local-time value silently matches nothing, which looks exactly like
    # "no changes" — so refuse it rather than export an empty artifact.
    if args.since and not (args.since.endswith("Z") or len(args.since) == 10):
        print(f"--since must be UTC ('...Z') or a bare date; got {args.since!r}",
              file=sys.stderr)
        return 2

    out: Path = args.out
    photos_dir = out / "photos"
    out.mkdir(parents=True, exist_ok=True)
    photos_dir.mkdir(exist_ok=True)

    bootstrap()
    conn = connect()
    try:
        where = "source = ?"
        params: list = [SOURCE_PG]
        if args.since:
            where += " AND updated_at >= ?"
            params.append(args.since)

        rows = conn.execute(
            f"SELECT id, {', '.join(EXPORT_COLUMNS)}, updated_at FROM listings "
            f"WHERE {where} ORDER BY id",
            params,
        ).fetchall()

        ids = [r["id"] for r in rows]
        transports: dict[int, list] = {}
        images: dict[int, list] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for t in conn.execute(
                f"SELECT listing_id, line, station, walk_minutes, sort_order "
                f"FROM listing_transports WHERE listing_id IN ({ph}) ORDER BY listing_id, sort_order",
                chunk,
            ):
                transports.setdefault(t["listing_id"], []).append({
                    "line": t["line"], "station": t["station"],
                    "walk_minutes": t["walk_minutes"], "sort_order": t["sort_order"],
                })
            for im in conn.execute(
                f"SELECT listing_id, kind, rel_path, label, sort_order "
                f"FROM listing_images WHERE listing_id IN ({ph}) ORDER BY listing_id, sort_order",
                chunk,
            ):
                images.setdefault(im["listing_id"], []).append(dict(im))

        watermark = max((r["updated_at"] for r in rows), default=args.since)
        n_photos = 0
        copied = 0
        skipped_missing = 0

        with gzip.open(out / "listings.ndjson.gz", "wt", encoding="utf-8") as fh:
            for r in rows:
                photos = []
                for im in images.get(r["id"], []):
                    src = REPO_ROOT / im["rel_path"]
                    if not src.is_file():
                        # The row survives without the photo rather than
                        # shipping a reference the far side can never resolve.
                        skipped_missing += 1
                        continue
                    name = src.name
                    photos.append({
                        "kind": im["kind"], "label": im["label"],
                        "sort_order": im["sort_order"], "file": name,
                    })
                    n_photos += 1
                    if not args.no_photos:
                        dst = photos_dir / name
                        if not dst.exists():
                            shutil.copy2(src, dst)
                            copied += 1
                fh.write(json.dumps({
                    "listing": {c: r[c] for c in EXPORT_COLUMNS},
                    "transports": transports.get(r["id"], []),
                    "photos": photos,
                }, ensure_ascii=False) + "\n")

        manifest = {
            "artifact_version": ARTIFACT_VERSION,
            "listings": len(rows),
            "photo_refs": n_photos,
            "photo_files": copied,
            "photos_included": not args.no_photos,
            "since": args.since,
            "watermark": watermark,
            "missing_photo_files": skipped_missing,
        }
        (out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
