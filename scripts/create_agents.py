#!/usr/bin/env python3
"""Batch-create generic keyed agents (demand / worker identities).

Each agent gets a stable identity + its own key (used as ``?agent_key=<key>`` on
the MCP endpoint). Keys are shown ONCE at creation — only their SHA-256 is
stored — so capture the output. Run this on the server so the keys land in the
production DB that authenticates them.

    python -m scripts.create_agents --count 30
    python -m scripts.create_agents --count 30 --prefix cluster --vendor cluster \
        --out agent_keys.json
    python -m scripts.create_agents --count 30 --dry-run     # show names only

Names are ``<prefix>-NN`` (zero-padded, unique). A name that already exists is
skipped (its key can't be re-shown) — pick a fresh --prefix to add more.
For a broker (supply-side) fleet use scripts/create_broker.py instead.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.auth import create_agent
from fango.db import bootstrap, connect

DEFAULT_URL = "https://fango.city/mcp2/mcp"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Batch-create generic keyed agents.")
    ap.add_argument("--count", type=int, default=30, help="how many agents to create")
    ap.add_argument("--prefix", default="agent", help="name prefix → <prefix>-NN")
    ap.add_argument("--vendor", default=None, help="optional vendor tag (helps identify the cluster)")
    ap.add_argument("--start", type=int, default=1, help="first index (default 1)")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"MCP endpoint for the printed connect URL (default {DEFAULT_URL})")
    ap.add_argument("--out", default=None, help="also write the keys to this JSON file (chmod 600)")
    ap.add_argument("--dry-run", action="store_true", help="print the names that would be created, create nothing")
    args = ap.parse_args(argv)

    if args.count < 1:
        print("error: --count must be >= 1", file=sys.stderr)
        return 2

    bootstrap()
    conn = connect()
    width = max(2, len(str(args.start + args.count - 1)))
    created: list[dict] = []
    skipped: list[str] = []
    try:
        for i in range(args.start, args.start + args.count):
            name = f"{args.prefix}-{i:0{width}d}"
            if args.dry_run:
                print(f"  would create: {name}")
                continue
            try:
                agent, key = create_agent(name, vendor=args.vendor, conn=conn)
            except sqlite3.IntegrityError:
                skipped.append(name)
                continue
            created.append({
                "agent_id": agent.id,
                "name": name,
                "key": key,
                "mcp_url": f"{args.url}?agent_key={key}",
            })
    finally:
        conn.close()

    if args.dry_run:
        print(f"\ndry-run: {args.count} agent(s) would be created ({args.prefix}-*)")
        return 0

    # Print the keys (shown once).
    print(f"\n{'agent_id':>8}  {'name':<16}  key")
    print("-" * 72)
    for a in created:
        print(f"{a['agent_id']:>8}  {a['name']:<16}  {a['key']}")
    print(f"\ncreated {len(created)} agent(s)"
          + (f"; skipped {len(skipped)} existing ({', '.join(skipped)})" if skipped else ""))

    if args.out and created:
        out = Path(args.out)
        out.write_text(json.dumps(created, ensure_ascii=False, indent=2), encoding="utf-8")
        out.chmod(0o600)
        print(f"wrote {len(created)} key(s) → {out} (chmod 600). Keep it secret; keys are not recoverable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
