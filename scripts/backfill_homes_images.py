#!/usr/bin/env python3
"""Backfill photos onto HOMES link cards that were resolved image-less.

Cards created before the Playwright browser was installed came from the
DuckDuckGo fallback: a building-page URL (…/chintai/b-<id>/) with NO image and
the "同じ建物の別の住戸（参考…）" note. Now that the browser works, this re-resolves
each such listing through HOMES (room page + og:image + same-floor matching) and
updates BOTH the cache (listing_external_links) and the already-attached cards
(post_link_previews).

Run on the server, from the repo root, with the browser working
(verify first: scripts/diagnose_homes.py). It loads .env itself.

    .venv/bin/python scripts/backfill_homes_images.py --dry-run   # list only
    .venv/bin/python scripts/backfill_homes_images.py             # apply

Each listing costs one browser session (~30-40s) and one external_lookup rate
token (cap 60/hour) — so a large backlog may need several runs. Safe to re-run:
it only touches rows that are still image-less.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env, override=False)
        except Exception:
            pass


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    _load_env()

    from fango.config import load_settings
    if not load_settings().external_lookup_enabled:
        print("FANGO_EXTERNAL_LOOKUP_ENABLED is off — set it to 1 in .env so the")
        print("re-resolve can run a browser lookup, then retry.")
        return 1

    from fango.db import connect
    from fango.listings.enrich import resolve_external_link

    conn = connect()
    conn.row_factory = sqlite3.Row

    # The image-less HOMES rows = DDG fallbacks (status ok, url set, no image).
    stale = conn.execute(
        """SELECT e.listing_id, e.url AS old_url, l.building_name
             FROM listing_external_links e
             JOIN listings l ON l.id = e.listing_id
            WHERE e.source = 'homes' AND e.status = 'ok'
              AND e.url IS NOT NULL
              AND (e.image_url IS NULL OR e.image_url = '')""",
    ).fetchall()

    print(f"image-less HOMES cache rows: {len(stale)}")
    if not stale:
        print("nothing to backfill.")
        return 0

    fixed = still_none = failed = 0
    for i, r in enumerate(stale, 1):
        lid, old_url, name = r["listing_id"], r["old_url"], r["building_name"]
        label = f"[{i}/{len(stale)}] listing {lid} ({name or '無名'})"
        if dry:
            n_prev = conn.execute(
                "SELECT count(*) FROM post_link_previews WHERE url = ? AND (image_url IS NULL OR image_url='')",
                (old_url,),
            ).fetchone()[0]
            print(f"{label}: would re-resolve; {n_prev} card(s) on old_url")
            continue
        try:
            # Drop the stale cache row so resolve does a fresh browser lookup.
            conn.execute("DELETE FROM listing_external_links WHERE listing_id = ?", (lid,))
            conn.commit()
            new = resolve_external_link(lid, conn=conn, allow_lookup=True)
            conn.commit()
        except Exception as exc:
            print(f"{label}: ERROR {exc!r}")
            failed += 1
            continue
        if not new or not new.get("image_url"):
            print(f"{label}: re-resolved but still no image (WAF blip / not found)")
            still_none += 1
            continue
        new_url, img = new["url"], new["image_url"]
        # Update every card that pointed at the old (image-less) URL.
        rows = conn.execute(
            "SELECT id, post_id FROM post_link_previews WHERE url = ?", (old_url,)
        ).fetchall()
        for pv in rows:
            try:
                conn.execute(
                    """UPDATE post_link_previews
                          SET url = ?, image_url = ?, title = ?, description = ?, source = 'homes'
                        WHERE id = ?""",
                    (new_url, img, new.get("title"), new.get("note"), pv["id"]),
                )
            except sqlite3.IntegrityError:
                # That post already has a card on the new URL — drop the stale one.
                conn.execute("DELETE FROM post_link_previews WHERE id = ?", (pv["id"],))
        conn.commit()
        print(f"{label}: ✓ image + {('room' if '/room/' in new_url else 'page')} url; {len(rows)} card(s) updated")
        fixed += 1

    print(f"\ndone — fixed {fixed}, still-no-image {still_none}, failed {failed}")
    if still_none:
        print("re-run later to retry the still-image-less ones (likely WAF blips).")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
