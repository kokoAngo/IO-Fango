#!/usr/bin/env python3
"""Onboard a broker (中介) from the command line.

Creates a ``vendor='broker'`` agent + profile and provisions its FIXED durable
on-chain identity. The plaintext key is printed once; only its SHA-256 is
stored. The broker connects an external MCP agent with this key and uses the
``broker_*`` tools.

    python -m scripts.create_broker --name brokerA --company "新宿不動産" \
        --areas 新宿,渋谷 [--license "東京都知事(1)第00000号"]

    # Attribute pre-existing / ingested listings to the house broker so they
    # remain visible (NULL broker_agent_id is treated as house inventory; this
    # makes ownership explicit):
    python -m scripts.create_broker --backfill-house
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.brokers.service import create_broker, get_or_create_house_broker
from fango.db import bootstrap, connect
from fango.identity import address_for


def _backfill_house() -> int:
    """Assign every unowned listing to the house broker. Returns rows updated."""
    house = get_or_create_house_broker()
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE listings SET broker_agent_id = ? WHERE broker_agent_id IS NULL",
            (house.id,),
        )
        conn.commit()
        print(f"house broker: {house.name} id={house.id} "
              f"address={address_for(house.id) or '(no identity)'}")
        print(f"backfilled {cur.rowcount} listing(s) → house broker")
        return cur.rowcount
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Onboard a Fango broker (中介).")
    parser.add_argument("--name", help="Unique agent display name (login handle)")
    parser.add_argument("--company", help="商号 / brand shown on broker posts")
    parser.add_argument("--areas", default="", help="Comma-separated served areas")
    parser.add_argument("--license", dest="license_no", default=None, help="宅建業免許番号")
    parser.add_argument("--bio", default=None, help="Short broker description")
    parser.add_argument("--contact", default=None, help="Internal contact note")
    parser.add_argument("--backfill-house", action="store_true",
                        help="Assign unowned listings to the house broker and exit")
    args = parser.parse_args(argv)

    bootstrap()

    if args.backfill_house:
        _backfill_house()
        return 0

    if not args.name or not args.company:
        parser.error("--name and --company are required (or use --backfill-house)")

    areas = [a.strip() for a in args.areas.split(",") if a.strip()]
    try:
        agent, key = create_broker(
            args.name, args.company,
            areas=areas, license_no=args.license_no,
            bio=args.bio, contact=args.contact,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"broker: {agent.name}  id={agent.id}  company={args.company!r}")
    print(f"on-chain address: {address_for(agent.id) or '(identity skipped — no FANGO_IDENTITY_SECRET / eth libs)'}")
    print("key (save now — shown once):")
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
