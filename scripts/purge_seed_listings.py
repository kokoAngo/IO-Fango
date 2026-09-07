#!/usr/bin/env python3
"""Drop the seeded/demo listing inventory before the first real upstream load.

    python -m scripts.purge_seed_listings              # dry run: report only
    python -m scripts.purge_seed_listings --yes        # actually delete

Most children of ``listings`` are ``ON DELETE CASCADE`` and go quietly:
price_history, listing_images, listing_transports, listing_notes,
listing_external_links, post_listing_refs.

Two are not. ``broker_proposals.listing_id`` and ``agreements.listing_id``
reference listings with no ON DELETE action, so a blanket DELETE raises
``FOREIGN KEY constraint failed``. Those rows are also the ones worth
protecting — an agreement is a concluded deal (and may be anchored on-chain),
not demo data. Listings they point at are therefore **kept**, and the script
says how many and which.

Note for the operator: this drops ``broker_agent_id`` along with the rows, so
broker inventory has to be re-assigned after the real load
(``scripts/assign_broker_inventory.py``, once per transaction type).
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.config import load_settings
from fango.db import bootstrap, connect, transaction

# Tables whose rows block a listing delete (FK without ON DELETE CASCADE).
PROTECTING_TABLES = ("broker_proposals", "agreements")

CHILD_TABLES = (
    "price_history", "listing_images", "listing_transports", "listing_notes",
    "listing_external_links", "post_listing_refs", "saved_search_matches",
)


def protected_ids(conn) -> set[int]:
    keep: set[int] = set()
    for table in PROTECTING_TABLES:
        rows = conn.execute(
            f"SELECT DISTINCT listing_id FROM {table} WHERE listing_id IS NOT NULL"
        ).fetchall()
        keep.update(r[0] for r in rows)
    return keep


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--yes", action="store_true", help="Actually delete (default: dry run)")
    p.add_argument("--no-backup", action="store_true",
                   help="Skip the data/fango.db.backup-* copy")
    args = p.parse_args(argv)

    bootstrap()
    conn = connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        keep = protected_ids(conn)
        keep = {i for i in keep if conn.execute(
            "SELECT 1 FROM listings WHERE id = ?", (i,)).fetchone()}
        doomed = total - len(keep)

        print(f"listings total      : {total:,}")
        print(f"kept (referenced by {'/'.join(PROTECTING_TABLES)}): {len(keep):,} {sorted(keep) or ''}")
        print(f"to delete           : {doomed:,}")
        for t in CHILD_TABLES:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  cascade {t:<24} {n:,}")

        if not args.yes:
            print("\ndry run — nothing deleted. Re-run with --yes.")
            return 0

        if not args.no_backup:
            db_path = load_settings().db_path
            # WAL: checkpoint first so the copy is a complete database.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            backup = db_path.with_name(f"{db_path.name}.backup-purge-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(db_path, backup)
            print(f"\nbackup: {backup}")

        with transaction(conn):
            if keep:
                placeholders = ",".join("?" * len(keep))
                conn.execute(
                    f"DELETE FROM listings WHERE id NOT IN ({placeholders})", tuple(keep)
                )
            else:
                conn.execute("DELETE FROM listings")
            # building_stats is a denormalised rollup with no FK, so it does not
            # cascade and would keep advertising buildings that no longer exist.
            conn.execute("DELETE FROM building_stats")

        remaining = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        print(f"deleted: {total - remaining:,}   remaining: {remaining:,}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
