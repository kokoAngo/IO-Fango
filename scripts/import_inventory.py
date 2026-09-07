#!/usr/bin/env python3
"""Import an inventory artifact produced by scripts/export_inventory.py.

Runs on the app server, which cannot reach the upstream database or its photo
bucket. Everything it needs is in the artifact directory.

    python -m scripts.import_inventory --in /srv/incoming/inv --dry-run
    python -m scripts.import_inventory --in /srv/incoming/inv

Only ``listings`` rows whose provenance is the upstream sync are touched
(``source='pg'``), keyed on ``reins_id``. Forum threads, agents, agreements,
brokers and anything a broker created here are never read or written.

``broker_agent_id`` is deliberately not carried across: agent ids differ per
environment, so broker inventory is assigned on this side afterwards with
``scripts/assign_broker_inventory.py``. An existing assignment survives an
import — upsert only writes the columns the artifact carries.
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.db import bootstrap, connect, transaction
from fango.listings import service as ls
from fango.uploads import UPLOADS_DIR

from scripts.export_inventory import ARTIFACT_VERSION

BATCH = 200


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--in", dest="src", required=True, type=Path,
                   help="Artifact directory (as written by export_inventory)")
    p.add_argument("--dry-run", action="store_true", help="Report only; write nothing")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)

    src: Path = args.src
    manifest_path = src / "manifest.json"
    if not manifest_path.is_file():
        print(f"no manifest.json in {src}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = manifest.get("artifact_version")
    if version != ARTIFACT_VERSION:
        # Importing a layout this code does not understand would write
        # half-mapped rows; refuse instead.
        print(f"artifact_version {version} != {ARTIFACT_VERSION} — refusing",
              file=sys.stderr)
        return 2
    print(f"artifact: {manifest['listings']:,} listing(s), "
          f"{manifest.get('photo_files', 0):,} photo file(s), "
          f"watermark {manifest.get('watermark')}")

    ndjson = src / "listings.ndjson.gz"
    if not ndjson.is_file():
        print(f"no listings.ndjson.gz in {src}", file=sys.stderr)
        return 2

    bootstrap()
    conn = connect()
    written = photos_linked = photos_missing = 0
    try:
        pending: list[dict] = []

        def flush() -> None:
            nonlocal written, photos_linked, photos_missing
            with transaction(conn):
                for rec in pending:
                    listing = ls.upsert_listing(rec["listing"], conn=conn)
                    if rec["transports"]:
                        ls.replace_transports(listing.id, rec["transports"], conn=conn)
                    images = []
                    for ph in rec["photos"]:
                        blob = src / "photos" / ph["file"]
                        dst = UPLOADS_DIR / ph["file"]
                        if not dst.exists():
                            if not blob.is_file():
                                # Metadata-only artifact, or a photo that did
                                # not travel. Skip the reference rather than
                                # record a path that 404s.
                                photos_missing += 1
                                continue
                            UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(blob, dst)
                        images.append({
                            "kind": ph["kind"],
                            "rel_path": str(dst.relative_to(REPO_ROOT)),
                            "label": ph["label"],
                            "sort_order": ph["sort_order"],
                        })
                        photos_linked += 1
                    # Replace even when empty, so a listing whose photos were
                    # withdrawn upstream loses them here too.
                    ls.replace_images(listing.id, images, conn=conn)
                    written += 1
            pending.clear()

        seen = 0
        with gzip.open(ndjson, "rt", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                seen += 1
                if args.limit is not None and seen > args.limit:
                    seen -= 1
                    break
                if args.dry_run:
                    continue
                pending.append(json.loads(line))
                if len(pending) >= BATCH:
                    flush()
            if pending:
                flush()

        if args.dry_run:
            print(f"dry run: {seen:,} listing(s) in the artifact; nothing written.")
            return 0
        print(f"imported: {written:,} listing(s)")
        print(f"photos linked: {photos_linked:,}  (missing from artifact: {photos_missing:,})")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
