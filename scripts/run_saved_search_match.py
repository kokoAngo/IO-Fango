#!/usr/bin/env python3
"""Manually run the saved-search match engine across recently-inserted listings.

Typical use: a partner system uploads listings via the API (not via the
spotlight ingester) and wants saved searches to pick them up.

    python -m scripts.run_saved_search_match --since 1h
    python -m scripts.run_saved_search_match --listing-ids 12 34 56
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.db import bootstrap, connect
from fango.listings.saved_search import match_new_listings


_DUR_RE = re.compile(r"^(\d+)([smhd])$")


def _parse_duration(text: str) -> timedelta:
    m = _DUR_RE.match(text.strip())
    if not m:
        raise ValueError(f"bad duration {text!r} — expected like 30s / 5m / 2h / 1d")
    n = int(m.group(1))
    unit = m.group(2)
    return {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }[unit]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--since", type=str, help="Match listings inserted within this duration (e.g. 1h, 30m, 1d)")
    g.add_argument("--listing-ids", nargs="+", type=int, help="Match against these listing ids explicitly")
    g.add_argument("--all", action="store_true", help="Match every listing in the DB (heavy)")
    args = p.parse_args(argv)

    bootstrap()
    conn = connect()
    try:
        if args.listing_ids:
            ids = list(args.listing_ids)
        elif args.all:
            rows = conn.execute("SELECT id FROM listings").fetchall()
            ids = [r["id"] for r in rows]
        else:
            delta = _parse_duration(args.since)
            cutoff = (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%fZ")
            rows = conn.execute(
                "SELECT id FROM listings WHERE created_at >= ?", (cutoff,)
            ).fetchall()
            ids = [r["id"] for r in rows]
        inserted = match_new_listings(ids, conn=conn)
        print(f"checked={len(ids)} new_matches_inserted={inserted}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
