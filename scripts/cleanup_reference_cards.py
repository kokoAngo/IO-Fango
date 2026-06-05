#!/usr/bin/env python3
"""Remove HOMES link cards that aren't a confirmed EXACT-unit match.

Since we now only link a unit we can confirm by building + floor + rent (room
page WITH a photo), the old "same building reference" cards — building pages
(…/chintai/b-<id>, …/mansion/b-<id>), image-less DuckDuckGo results, and anything
carrying the 参考 note — should no longer be shown. This deletes those cards from
posts (post_link_previews) and clears their cache rows (listing_external_links)
so the listing re-resolves under the strict rule next time it's proposed.

KEEPS: HOMES room-page cards (…/chintai/room/…) that have an image and no note.
Touches only HOMES links — other previews are left alone.

    .venv/bin/python scripts/cleanup_reference_cards.py --dry-run   # preview
    .venv/bin/python scripts/cleanup_reference_cards.py             # apply

Run on the server, from the repo root. It loads .env itself. Deletions are
irreversible — run --dry-run first.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A card is a confirmed exact unit only if its URL is a HOMES room page. Anything
# else (building page, sale page, search page) is a reference we now drop.
_KEEP_URL = "%/chintai/room/%"

# Match HOMES previews regardless of whether `source` was recorded.
_IS_HOMES = "(source = 'homes' OR url LIKE '%homes.co.jp%')"

# Remove = HOMES AND NOT (room-page AND has-image AND no-note).
_PREVIEW_REMOVE = f"""
    {_IS_HOMES}
    AND NOT (url LIKE ? AND image_url IS NOT NULL AND image_url != ''
             AND (description IS NULL OR description = ''))
"""
_CACHE_REMOVE = f"""
    (source = 'homes' OR url LIKE '%homes.co.jp%')
    AND url IS NOT NULL
    AND NOT (url LIKE ? AND image_url IS NOT NULL AND image_url != ''
             AND (note IS NULL OR note = ''))
"""


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
    from fango.db import connect
    conn = connect()
    conn.row_factory = sqlite3.Row

    previews = conn.execute(
        f"SELECT id, post_id, url, image_url, description FROM post_link_previews WHERE {_PREVIEW_REMOVE}",
        (_KEEP_URL,),
    ).fetchall()
    cache = conn.execute(
        f"SELECT listing_id, url, image_url, note FROM listing_external_links WHERE {_CACHE_REMOVE}",
        (_KEEP_URL,),
    ).fetchall()

    kept = conn.execute(
        f"SELECT count(*) FROM post_link_previews WHERE {_IS_HOMES} AND url LIKE ? "
        "AND image_url IS NOT NULL AND image_url != '' AND (description IS NULL OR description = '')",
        (_KEEP_URL,),
    ).fetchone()[0]

    print(f"HOMES cards to REMOVE (reference/no-image/building-page): {len(previews)}")
    print(f"HOMES cards to KEEP   (exact room page + image):         {kept}")
    print(f"cache rows to clear (re-resolve under strict rule):       {len(cache)}")
    for r in previews[:20]:
        why = []
        if "/chintai/room/" not in (r["url"] or ""):
            why.append("not-a-room-page")
        if not r["image_url"]:
            why.append("no-image")
        if r["description"]:
            why.append("has-note")
        print(f"  - post {r['post_id']}: {r['url']}  [{', '.join(why)}]")
    if len(previews) > 20:
        print(f"  … and {len(previews) - 20} more")

    if dry:
        print("\n--dry-run: nothing deleted. Re-run without --dry-run to apply.")
        return 0
    if not previews and not cache:
        print("\nnothing to clean.")
        return 0

    conn.execute(f"DELETE FROM post_link_previews WHERE {_PREVIEW_REMOVE}", (_KEEP_URL,))
    conn.execute(f"DELETE FROM listing_external_links WHERE {_CACHE_REMOVE}", (_KEEP_URL,))
    conn.commit()
    print(f"\ndeleted {len(previews)} card(s), cleared {len(cache)} cache row(s).")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
