#!/usr/bin/env python3
"""Pull listings from the local .Spotlight-V100/ crawl into SQLite.

Modes:
    python -m scripts.ingest_spotlight              # full sync
    python -m scripts.ingest_spotlight --dry-run    # iterate & count, no DB write
    python -m scripts.ingest_spotlight --limit 5    # cap to first N records
    python -m scripts.ingest_spotlight --no-match   # skip saved-search matching
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.db import bootstrap, connect, transaction
from fango.listings import service as ls
from fango.listings.ingestion.spotlight import SpotlightListingAdapter


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sync listings from .Spotlight-V100/")
    p.add_argument("--dry-run", action="store_true", help="Iterate but don't write")
    p.add_argument("--limit", type=int, default=None, help="Cap to first N records")
    p.add_argument("--no-match", action="store_true",
                   help="Skip saved-search match pass at the end")
    p.add_argument("--root", type=Path, default=None,
                   help="Override Spotlight root (default: <repo>/.Spotlight-V100)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("ingest_spotlight")

    bootstrap()
    adapter = SpotlightListingAdapter(root=args.root)
    if not adapter.is_configured():
        log.error("Spotlight root not found: %s", adapter.root)
        return 2

    seen = 0
    written = 0
    listing_ids: list[int] = []

    conn = connect()
    try:
        for rec in adapter.iter_records():
            seen += 1
            if args.limit is not None and seen > args.limit:
                seen -= 1
                break
            if args.dry_run:
                log.info(
                    "seen reins_id=%s building=%r rent_yen=%s transports=%d images=%d",
                    rec.payload.get("reins_id"), rec.payload.get("building_name"),
                    rec.payload.get("rent_yen"), len(rec.transports), len(rec.images),
                )
                continue
            with transaction(conn):
                listing = ls.upsert_listing(rec.payload, conn=conn)
                if rec.transports:
                    ls.replace_transports(listing.id, rec.transports, conn=conn)
                if rec.images:
                    ls.replace_images(listing.id, rec.images, conn=conn)
            written += 1
            listing_ids.append(listing.id)
    finally:
        conn.close()

    print(f"seen={seen} written={written}")

    if listing_ids and not args.no_match:
        try:
            from fango.listings.saved_search import match_new_listings
        except ImportError:
            log.debug("saved_search module not present; skipping match pass")
            return 0
        matched = match_new_listings(listing_ids)
        print(f"saved_search_matches_inserted={matched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
