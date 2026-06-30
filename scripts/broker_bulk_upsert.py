#!/usr/bin/env python3
"""Bulk-upsert a broker's own listings from a JSON file (admin / server-side).

Bypasses MCP — use it to seed or refresh a broker's inventory directly. Each
listing is stamped with the broker's agent id and made advertisable by default so
it routes to customers. Idempotent per ``reins_id`` (give each listing a unique
one so re-runs update instead of duplicating).

    python -m scripts.broker_bulk_upsert --broker-id 49 --file listings.json [--dry-run]

``listings.json`` is a JSON array of objects. Recognised fields (others ignored):
  reins_id, building_name, prefecture, city, ward, station, station_line,
  walk_minutes, layout, area_sqm, rent_yen (賃貸), price_man (売買, 万円),
  deposit_text, key_money_text, maintenance_fee_yen, built_year, structure,
  floor, total_floors, direction, parking, pet_allowed, transaction_type
  ('sale' for 売買; omit/blank for 賃貸), ad_status, url.

Rental rows default ad_status="可"; sale rows default "公開中" (both = advertisable).
Run with --dry-run first to see what would be written.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.brokers import service as bsvc
from fango.db import bootstrap, connect
from fango.listings import service as ls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bulk-upsert a broker's listings.")
    parser.add_argument("--broker-id", type=int, required=True, help="vendor='broker' agent id")
    parser.add_argument("--file", required=True, help="path to a JSON array of listings")
    parser.add_argument("--dry-run", action="store_true", help="show what would be written, don't write")
    args = parser.parse_args(argv)

    bootstrap()
    conn = connect()
    if not bsvc.is_broker(args.broker_id, conn=conn):
        print(f"error: agent {args.broker_id} is not an active broker", file=sys.stderr)
        return 2

    try:
        rows = json.loads(Path(args.file).read_text("utf-8"))
    except Exception as exc:
        print(f"error reading {args.file}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(rows, list):
        print("error: JSON must be an array of listing objects", file=sys.stderr)
        return 2

    written = 0
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            print(f"  [{i}] skipped (not an object)")
            continue
        data = dict(row)
        data["broker_agent_id"] = args.broker_id
        is_sale = (data.get("transaction_type") or "").lower() == "sale"
        data.setdefault("ad_status", "公開中" if is_sale else "可")
        label = data.get("building_name") or data.get("reins_id") or f"#{i}"
        money = f"price_man={data.get('price_man')}" if is_sale else f"rent_yen={data.get('rent_yen')}"
        if args.dry_run:
            print(f"  [{i}] WOULD upsert: {label}  ward={data.get('ward')}  {money}  ad={data['ad_status']}")
            continue
        try:
            listing = ls.upsert_listing(data, conn=conn)   # keyed by reins_id when present
            written += 1
            print(f"  [{i}] ok id={listing.id}: {label}  ward={data.get('ward')}  {money}")
        except Exception as exc:
            print(f"  [{i}] FAILED ({label}): {exc}", file=sys.stderr)

    if args.dry_run:
        print(f"\ndry-run: {len(rows)} listing(s) would be upserted for broker {args.broker_id}")
    else:
        total = conn.execute(
            "SELECT COUNT(*) c FROM listings WHERE broker_agent_id = ?", (args.broker_id,)
        ).fetchone()["c"]
        print(f"\nwrote {written}/{len(rows)}; broker {args.broker_id} now owns {total} listing(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
