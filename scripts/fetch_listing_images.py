#!/usr/bin/env python3
"""Re-host upstream listing photos locally, then point listing_images at them.

    python -m scripts.fetch_listing_images --dry-run
    python -m scripts.fetch_listing_images --limit 50
    python -m scripts.fetch_listing_images --per-listing 4 --refetch

Why re-host rather than hot-link: the bucket is a Garage instance on the
office LAN with no anonymous access, and the app server is off-LAN. The image
endpoint also opens ``listing_images.rel_path`` as a file on disk, so a URL in
that column is a 404 by construction.

Bytes land in ``data/uploads/<sha256>.<ext>`` (content-addressed, so the same
photo attached to two listings is stored once) and the row records the
repo-relative path.

The photo manifest is read live from the upstream Postgres rather than cached
in SQLite: it is the source of truth for which photos are still publishable
(a photo can be disqualified, or reclassified as having a person in frame,
after we first saw it), and re-reading costs one query per page of listings.
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
from fango.listings.ingestion.postgres import IMAGE_COLUMNS, PgListingAdapter, map_images
from fango.listings.objectstore import ObjectStore, ObjectStoreError
from fango.uploads import UPLOADS_DIR, UploadError, save_image_bytes

# Listings per upstream images query. The manifest lookup is one
# `bukken_no = ANY(...)` per chunk.
CHUNK = 200

# Every consumer today shows only the first raw photo (the listing page's
# single thumbnail, the forum feed's topic image), so the default is a small
# cap: pulling all ~10 photos per listing would be ~2 GB of bytes that also
# have to be shipped to the app server, for pixels nothing renders yet.
DEFAULT_PER_LISTING = 4


def _local_sale_listings(conn, limit: int | None) -> dict[str, int]:
    """{reins_id (= upstream bukken_no): listing_id} for sale rows."""
    sql = ("SELECT id, reins_id FROM listings "
           "WHERE COALESCE(transaction_type,'') = 'sale' AND reins_id IS NOT NULL "
           "ORDER BY id")
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return {r["reins_id"]: r["id"] for r in conn.execute(sql).fetchall()}


def _chunks(seq, n=CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_manifests(pgconn, dict_row, bukken_nos: list[str]) -> dict[str, list[dict]]:
    """{bukken_no: [upstream image row, ...]} for one chunk of listings."""
    out: dict[str, list[dict]] = {}
    with pgconn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"SELECT {', '.join(IMAGE_COLUMNS)} FROM baibai.images "
            "WHERE bukken_no = ANY(%s) ORDER BY bukken_no, id",
            (bukken_nos,),
        )
        for img in cur:
            out.setdefault(img["bukken_no"], []).append(img)
    return out


def save_photo(store, photo: dict) -> tuple[dict | None, str | None]:
    """Fetch + persist one photo. Returns (listing_images fields, error)."""
    try:
        data = store.get(photo["storage_key"])
    except ObjectStoreError as exc:
        return None, str(exc)
    try:
        # save_image_bytes sniffs magic bytes and rejects anything that is not
        # a real image — upstream 物件画像 rows occasionally point at a PDF.
        saved = save_image_bytes(data)
    except UploadError as exc:
        return None, f"{photo['storage_key']}: {exc}"
    # save_image_bytes returns the public URL, not a path; the file itself is
    # <sha256>.<ext> under UPLOADS_DIR.
    path = UPLOADS_DIR / saved["url"].rsplit("/", 1)[-1]
    return {
        "kind": "raw",
        # Repo-relative: the image endpoint resolves rel_path against the repo
        # root and refuses anything that escapes it.
        "rel_path": str(path.relative_to(REPO_ROOT)),
        "label": photo.get("label"),
        "reused": saved["reused"],
        "size_bytes": saved["size_bytes"],
    }, None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be fetched; download nothing")
    p.add_argument("--limit", type=int, default=None, help="Cap to the first N sale listings")
    p.add_argument("--per-listing", type=int, default=DEFAULT_PER_LISTING,
                   help=f"Max photos per listing (default {DEFAULT_PER_LISTING})")
    p.add_argument("--refetch", action="store_true",
                   help="Re-fetch listings that already have local images")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("fetch_listing_images")

    adapter = PgListingAdapter()
    if not adapter.is_configured():
        log.error("FANGO_PG_DSN is not set — cannot read the photo manifest.")
        return 2
    store = ObjectStore()
    if not store.is_configured() and not args.dry_run:
        log.error("FANGO_S3_* is not set — cannot fetch photo bytes.")
        return 2

    bootstrap()
    conn = connect()
    try:
        # Rows written before the fetcher existed carry an upstream URL in
        # rel_path, which the image endpoint can only 404 on. Clear them so a
        # listing never advertises a photo that cannot be served.
        stale = conn.execute(
            "SELECT COUNT(*) FROM listing_images WHERE rel_path LIKE 'http%'"
        ).fetchone()[0]
        if stale and not args.dry_run:
            with transaction(conn):
                conn.execute("DELETE FROM listing_images WHERE rel_path LIKE 'http%'")
            log.info("cleared %s unfetchable image row(s)", f"{stale:,}")
        elif stale:
            log.info("would clear %s unfetchable image row(s)", f"{stale:,}")

        by_reins = _local_sale_listings(conn, args.limit)
        if not args.refetch:
            have = {r[0] for r in conn.execute(
                "SELECT DISTINCT listing_id FROM listing_images "
                "WHERE rel_path NOT LIKE 'http%'").fetchall()}
            by_reins = {k: v for k, v in by_reins.items() if v not in have}
        log.info("sale listings to consider: %s", f"{len(by_reins):,}")

        from psycopg.rows import dict_row
        pgconn = adapter._connect()
        listings_with_photos = 0
        fetched = fetch_failed = reused = 0
        total_bytes = 0
        try:
            keys = list(by_reins)
            for i, chunk in enumerate(_chunks(keys)):
                manifests = fetch_manifests(pgconn, dict_row, chunk)

                for bukken, raw_images in manifests.items():
                    photos = map_images(raw_images)[: args.per_listing]
                    if not photos:
                        continue
                    listings_with_photos += 1
                    if args.dry_run:
                        continue
                    records = []
                    for photo in photos:
                        rec, err = save_photo(store, photo)
                        if rec is None:
                            fetch_failed += 1
                            log.warning("skipped: %s", err)
                            continue
                        if rec.pop("reused"):
                            reused += 1
                        else:
                            fetched += 1
                            total_bytes += rec["size_bytes"]
                        rec.pop("size_bytes")
                        rec["sort_order"] = len(records)
                        records.append(rec)
                    if records:
                        with transaction(conn):
                            ls.replace_images(by_reins[bukken], records, conn=conn)
                log.info("chunk %s: listings_with_photos=%s fetched=%s failed=%s",
                         i + 1, f"{listings_with_photos:,}", f"{fetched:,}", fetch_failed)
        finally:
            pgconn.close()

        print(f"listings with publishable photos: {listings_with_photos:,}")
        if args.dry_run:
            print("dry run — nothing downloaded.")
            return 0
        print(f"downloaded: {fetched:,}  ({total_bytes / 1e6:.1f} MB)")
        print(f"deduped (already on disk): {reused:,}")
        print(f"failed/rejected: {fetch_failed:,}")
        rows = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT listing_id) l FROM listing_images"
        ).fetchone()
        print(f"listing_images now: {rows['n']:,} rows on {rows['l']:,} listing(s)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
