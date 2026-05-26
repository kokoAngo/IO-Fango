#!/usr/bin/env python3
"""Pull listings from a Notion database into the SQLite store.

Configure via env:
  NOTION_TOKEN
  NOTION_LISTINGS_DATABASE_ID

Modes:
    python -m scripts.ingest_notion              # full sync
    python -m scripts.ingest_notion --dry-run    # iterate & count, no DB write
    python -m scripts.ingest_notion --probe      # dump DB schema + 2 sample rows
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.db import bootstrap
from fango.listings import service as ls
from fango.listings.ingestion.notion import NotionListingAdapter


def _probe(adapter: NotionListingAdapter) -> int:
    """Fetch DB schema + 2 sample rows from Notion and print to stdout."""
    try:
        import httpx
    except ImportError:
        print("install httpx first: .venv/bin/pip install httpx", file=sys.stderr)
        return 2
    headers = {
        "Authorization": f"Bearer {adapter.token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30.0, headers=headers) as client:
        # 1. Database metadata (title + property schema)
        meta = client.get(f"https://api.notion.com/v1/databases/{adapter.database_id}")
        if meta.status_code != 200:
            print(f"GET /databases/{adapter.database_id} → {meta.status_code}", file=sys.stderr)
            print(meta.text, file=sys.stderr)
            return 2
        meta_data = meta.json()
        print("=" * 60)
        print("DATABASE")
        print("=" * 60)
        title_parts = meta_data.get("title", [])
        title = "".join(t.get("plain_text", "") for t in title_parts) or "(no title)"
        print(f"title: {title}")
        print(f"id:    {meta_data.get('id')}")
        print()
        print("=" * 60)
        print("PROPERTIES (column name → type [+ enum values if any])")
        print("=" * 60)
        props = meta_data.get("properties", {})
        for name, p in props.items():
            t = p.get("type", "?")
            extra = ""
            if t == "select":
                opts = [o["name"] for o in p.get("select", {}).get("options", [])]
                extra = f"  options: {opts[:8]}{'...' if len(opts) > 8 else ''}"
            elif t == "multi_select":
                opts = [o["name"] for o in p.get("multi_select", {}).get("options", [])]
                extra = f"  options: {opts[:8]}{'...' if len(opts) > 8 else ''}"
            elif t == "number":
                fmt = p.get("number", {}).get("format")
                extra = f"  format: {fmt}" if fmt else ""
            print(f"  {name!r:30s}  {t}{extra}")

        # 2. Two sample rows
        print()
        print("=" * 60)
        print("SAMPLE ROWS (max 2)")
        print("=" * 60)
        sample = client.post(
            f"https://api.notion.com/v1/databases/{adapter.database_id}/query",
            json={"page_size": 2},
        )
        if sample.status_code != 200:
            print(f"query → {sample.status_code} {sample.text}", file=sys.stderr)
            return 2
        for i, page in enumerate(sample.json().get("results", []), 1):
            print(f"\n--- row {i} ---")
            for name, val in page.get("properties", {}).items():
                short = _summarize(val)
                print(f"  {name!r:30s} = {short}")
    return 0


def _summarize(prop: dict) -> str:
    t = prop.get("type")
    if t in ("title", "rich_text"):
        arr = prop.get(t) or []
        s = "".join(p.get("plain_text", "") for p in arr)
        return repr(s[:80] + ("…" if len(s) > 80 else ""))
    if t == "number":
        return repr(prop.get("number"))
    if t == "select":
        sel = prop.get("select") or {}
        return repr(sel.get("name"))
    if t == "multi_select":
        return repr([o.get("name") for o in prop.get("multi_select", [])])
    if t == "url":
        return repr(prop.get("url"))
    if t == "files":
        files = prop.get("files") or []
        return f"<{len(files)} file(s)>"
    if t == "date":
        d = prop.get("date") or {}
        return repr(d.get("start"))
    if t == "checkbox":
        return repr(prop.get("checkbox"))
    if t == "people":
        return f"<{len(prop.get('people', []))} person(s)>"
    if t == "relation":
        return f"<{len(prop.get('relation', []))} relation(s)>"
    return f"<{t}>"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sync listings from Notion.")
    p.add_argument("--dry-run", action="store_true",
                   help="Iterate and count without writing to the DB")
    p.add_argument("--probe", action="store_true",
                   help="Print DB schema + 2 sample rows, then exit")
    args = p.parse_args(argv)

    bootstrap()
    adapter = NotionListingAdapter()
    if not adapter.is_configured():
        print(
            "Notion not configured. Set NOTION_TOKEN and NOTION_LISTINGS_DATABASE_ID "
            "to enable. No-op.",
            file=sys.stderr,
        )
        return 0

    if args.probe:
        return _probe(adapter)

    n_seen = 0
    n_written = 0
    for record in adapter.iter_listings():
        n_seen += 1
        if not args.dry_run:
            ls.upsert_listing(record)
            n_written += 1
    print(f"seen={n_seen} written={n_written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
