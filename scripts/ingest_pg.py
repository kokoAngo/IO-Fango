#!/usr/bin/env python3
"""Sync listings from the upstream inventory Postgres into SQLite.

The adapter is read-only against Postgres in both modes; ``--write`` is what
decides whether SQLite is touched. One of ``--dry-run`` / ``--write`` is
required — there is no default, because the two differ by a full inventory
rewrite.

    python -m scripts.ingest_pg --dry-run                       # map + report, no writes
    python -m scripts.ingest_pg --dry-run --limit 500 --sample 3
    python -m scripts.ingest_pg --write --advertisable-only     # only 広告可 rows
    python -m scripts.ingest_pg --write --kind sale
    python -m scripts.ingest_pg --write --since 2026-09-01      # incremental
    python -m scripts.ingest_pg --reconcile                     # refresh the gate columns

``--reconcile`` re-reads the advertising/on-market columns for every listing
already held locally and writes back what upstream says now — including
retiring listings that have disappeared upstream entirely. An incremental
``--since`` sweep can only ever add and update rows it re-sees, so without
this a listing withdrawn after ingest stays advertised forever (おとり広告).
Run it on its own schedule, independent of the incremental pass.

``--dry-run`` prints a per-field coverage report — the cheapest way to notice
that an upstream column changed shape and a parser has started returning None.

Needs ``FANGO_PG_DSN`` (a SELECT-only role) and ``pip install -e '.[pg]'``.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.db import bootstrap, connect, transaction
from fango.listings import service as ls
from fango.listings.ingestion.postgres import (
    SOURCE_PG, PgListingAdapter, PgRecord, is_ingestable,
)

# Rows per SQLite transaction. One transaction per listing (the pattern the
# Spotlight ingest uses) costs a WAL commit each time, which is fine for a few
# hundred records and painful across a 60k-row sweep.
WRITE_BATCH = 200

# Fields whose parse rate tells us whether the mapping is healthy. A column
# that is legitimately sparse upstream (balcony, direction) will show low
# coverage and that is fine — the point is to spot a *parser* that broke, e.g.
# built_year at 0% when the upstream column is populated.
TRACKED_FIELDS = (
    "reins_id", "building_name", "address", "prefecture", "city", "ward",
    "station", "station_line", "walk_minutes", "layout", "area_sqm",
    "rent_yen", "price_man", "built_year", "built_month", "floor",
    "listing_type", "ad_status", "agent_company",
)


class Stats:
    def __init__(self) -> None:
        self.rows = 0
        self.present: Counter[str] = Counter()
        self.ad_status: Counter[str] = Counter()
        self.transports = 0
        self.images = 0
        self.with_images = 0
        self.no_reins_id = 0
        self.skeleton = 0

    def add(self, rec: PgRecord) -> None:
        self.rows += 1
        payload = rec.payload
        for f in TRACKED_FIELDS:
            if payload.get(f) is not None:
                self.present[f] += 1
        self.ad_status[str(payload.get("ad_status"))] += 1
        self.transports += len(rec.transports)
        self.images += len(rec.images)
        if rec.images:
            self.with_images += 1
        if not payload.get("reins_id"):
            self.no_reins_id += 1
        if not is_ingestable(payload):
            self.skeleton += 1

    def report(self, kind: str) -> str:
        if not self.rows:
            return f"[{kind}] 0 rows"
        lines = [f"[{kind}] {self.rows:,} rows"]
        if self.no_reins_id:
            # No id ⇒ upsert would degrade to a blind insert and duplicate on
            # every run. Loud on purpose.
            lines.append(f"  !! {self.no_reins_id:,} rows with NO reins_id — would duplicate on re-sync")
        if self.skeleton:
            lines.append(
                f"  skeleton rows (id only, no address/rent/layout): {self.skeleton:,} "
                f"({100.0 * self.skeleton / self.rows:.1f}%) — P2 will skip these"
            )
        lines.append("  field coverage:")
        for f in TRACKED_FIELDS:
            n = self.present[f]
            lines.append(f"    {f:<16} {n:>8,}  {100.0 * n / self.rows:5.1f}%")
        lines.append("  ad_status:")
        for val, n in self.ad_status.most_common(10):
            lines.append(f"    {val:<24} {n:>8,}")
        lines.append(f"  transports: {self.transports:,}")
        lines.append(
            f"  images: {self.images:,} on {self.with_images:,} listings "
            f"({100.0 * self.with_images / self.rows:.1f}% have a photo)"
        )
        return "\n".join(lines)


def _flush(conn, records: list[PgRecord], listing_ids: list[int]) -> int:
    """Upsert one batch inside a single transaction. Returns rows written."""
    with transaction(conn):
        for rec in records:
            listing = ls.upsert_listing(rec.payload, conn=conn)
            # replace_transports is a full replacement, so an upstream row
            # that lost a leg converges instead of accumulating stale children.
            if rec.transports:
                ls.replace_transports(listing.id, rec.transports, conn=conn)
            # rec.images is a manifest of storage keys, not listing_images
            # rows — the bytes have to be fetched and re-hosted first, which
            # is scripts/fetch_listing_images.py's job.
            listing_ids.append(listing.id)
    return len(records)


def reconcile(adapter, kinds, log) -> int:
    """Re-read the gate columns upstream and write back what they say now."""
    bootstrap()
    conn = connect()
    changed = retired = checked = 0
    try:
        for kind in kinds:
            tt = "sale" if kind == "sale" else "rent"
            type_where = ("COALESCE(transaction_type,'') = 'sale'" if tt == "sale"
                          else "COALESCE(transaction_type,'') != 'sale'")
            # Scoped by provenance, NOT just by "has a reins_id": a broker can
            # hand-create a listing and give it any id it likes, and retiring
            # that because no upstream row matches would silently pull a
            # broker's own inventory off the site.
            local = {
                r["reins_id"]: r
                for r in conn.execute(
                    "SELECT id, reins_id, ad_status, tenancy_status FROM listings "
                    f"WHERE {type_where} AND reins_id IS NOT NULL AND source = ?",
                    (SOURCE_PG,),
                ).fetchall()
            }
            if not local:
                continue
            checked += len(local)
            upstream = adapter.gate_status(tt, list(local))
            log.info("[%s] local=%s upstream_found=%s",
                     kind, f"{len(local):,}", f"{len(upstream):,}")

            updates: list[tuple] = []
            for key, row in local.items():
                now = upstream.get(key)
                if now is None:
                    # Gone upstream. Retire rather than delete: agreements and
                    # broker proposals reference listings, and a concluded deal
                    # must keep pointing at what it was about.
                    new_ad, new_ten = ls.RETIRED_AD_STATUS, row["tenancy_status"]
                    if row["ad_status"] == ls.RETIRED_AD_STATUS:
                        continue
                    retired += 1
                else:
                    new_ad, new_ten = now["ad_status"], now["tenancy_status"]
                    if (row["ad_status"], row["tenancy_status"]) == (new_ad, new_ten):
                        continue
                updates.append((new_ad, new_ten, row["id"]))

            for i in range(0, len(updates), WRITE_BATCH):
                with transaction(conn):
                    conn.executemany(
                        "UPDATE listings SET ad_status = ?, tenancy_status = ?, "
                        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                        updates[i:i + WRITE_BATCH],
                    )
            changed += len(updates)
    finally:
        conn.close()
    print(f"reconcile: checked {checked:,}, updated {changed:,}, of which retired {retired:,}")
    return 0


def _parse_since(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Map and report; never touches SQLite")
    p.add_argument("--write", action="store_true",
                   help="Upsert into SQLite (keyed on reins_id)")
    p.add_argument("--kind", choices=("rent", "sale", "both"), default="both")
    p.add_argument("--limit", type=int, default=None, help="Stop after N records per kind")
    p.add_argument("--sample", type=int, default=0, help="Print the first N mapped payloads")
    p.add_argument("--advertisable-only", action="store_true",
                   help="Only rows cleared for public advertising (賃貸 広告可=可 / 売買 ad_repost=広告可*)")
    p.add_argument("--include-contracted", action="store_true",
                   help="Keep 成約済み rentals (excluded by default)")
    p.add_argument("--since", default=None,
                   help="ISO timestamp: 賃貸 created_time / 売買 updated_at watermark")
    p.add_argument("--reconcile", action="store_true",
                   help="Refresh ad_status/tenancy_status for listings already held "
                        "locally (and retire ones upstream no longer offers)")
    p.add_argument("--match", action="store_true",
                   help="Run the saved-search match pass over what was written. Off by "
                        "default: on a full sweep it scores every active search against "
                        "tens of thousands of listings.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("ingest_pg")

    if args.reconcile:
        if args.dry_run or args.write:
            log.error("--reconcile runs on its own; pass it without --dry-run/--write.")
            return 2
    elif args.dry_run == args.write:
        log.error("Pass exactly one of --dry-run / --write.")
        return 2

    adapter = PgListingAdapter(
        advertisable_only=args.advertisable_only,
        include_contracted=args.include_contracted,
        since=_parse_since(args.since),
    )
    if not adapter.is_configured():
        log.error("FANGO_PG_DSN is not set — nothing to read.")
        return 2

    kinds = ("rent", "sale") if args.kind == "both" else (args.kind,)
    if args.reconcile:
        return reconcile(adapter, kinds, log)

    printed = 0
    exit_code = 0
    conn = connect() if args.write else None
    if conn is not None:
        bootstrap()
    try:
        for kind in kinds:
            stats = Stats()
            written = 0
            listing_ids: list[int] = []
            pending: list[PgRecord] = []
            for rec in adapter.iter_records(kinds=(kind,)):
                stats.add(rec)
                if printed < args.sample:
                    print(json.dumps(rec.payload, ensure_ascii=False, indent=2))
                    if rec.transports:
                        print("  transports:", json.dumps(rec.transports, ensure_ascii=False))
                    if rec.images:
                        print("  images:", json.dumps(rec.images[:3], ensure_ascii=False))
                    printed += 1
                if conn is not None and is_ingestable(rec.payload):
                    pending.append(rec)
                    if len(pending) >= WRITE_BATCH:
                        written += _flush(conn, pending, listing_ids)
                        pending = []
                        log.info("[%s] written=%s", kind, f"{written:,}")
                if args.limit is not None and stats.rows >= args.limit:
                    break
            if conn is not None and pending:
                written += _flush(conn, pending, listing_ids)
            print(stats.report(kind))
            if conn is not None:
                print(f"  written: {written:,}")
                if args.match and listing_ids:
                    from fango.listings.saved_search import match_new_listings
                    print(f"  saved_search_matches_inserted: {match_new_listings(listing_ids):,}")
            if stats.no_reins_id:
                exit_code = 1
    finally:
        if conn is not None:
            conn.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
