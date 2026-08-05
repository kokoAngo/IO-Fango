#!/usr/bin/env python3
"""Give a broker inventory across many wards so it routes + can propose everywhere.

For each ward it tops the broker up to ``--per-ward`` listings of one transaction
type (drawn from the unowned house pool), marks them advertisable so they surface
in search and feed broker_propose_terms, and merges those wards into the broker's
``areas`` (so area-based routing fires for them too).

``--transaction-type`` picks which pool to draw from (default ``rental``):
  * ``rental`` — non-sale rows that have a rent (``rent_yen``); marked ad_status='可'.
  * ``sale``   — ``transaction_type='sale'`` rows that have a price (``price_man``);
                 marked ad_status='公開中' (the on-market gate sale rows need to
                 surface — '可' is the RENTAL gate and would leave sale rows hidden).

    # give broker 49 rental inventory (current default behaviour)
    python -m scripts.assign_broker_inventory --broker-id 49 --per-ward 15
    # ALSO give it sale inventory so it can serve 買房 customers
    python -m scripts.assign_broker_inventory --broker-id 49 --per-ward 15 --transaction-type sale

Idempotent per type: the per-ward top-up counts only the broker's listings OF THE
SAME type, so assigning ``sale`` doesn't get blocked by rentals it already owns,
and re-running with the same --per-ward is a no-op once each ward is full.
Ward key = COALESCE(NULLIF(ward,''), city), so it works whether the ward name
lives in the `ward` or the `city` column.

NOTE: forcing an advertisable status presents these as advertisable. Fine for a
prototype/simulation fleet; for real listings, only advertise ones that genuinely
are (avoid おとり広告).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.brokers import service as bsvc
from fango.db import bootstrap, connect

_ADMIN_SUFFIX = re.compile(r"[都道府県区市町村]+$")
_WARD = "COALESCE(NULLIF(listings.ward,''), listings.city)"


def _core(s: str) -> str:
    return _ADMIN_SUFFIX.sub("", str(s or "").strip())


def _chunks(seq, n=500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Assign per-ward inventory to a broker.")
    ap.add_argument("--broker-id", type=int, required=True)
    ap.add_argument("--per-ward", type=int, default=15, help="target listings per ward (default 15)")
    ap.add_argument("--transaction-type", choices=["rental", "sale"], default="rental",
                    help="which pool to draw from (default rental)")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    ap.add_argument("--no-areas", action="store_true", help="don't touch the broker's areas")
    args = ap.parse_args(argv)

    # Type-specific candidate predicate + advertisable status. Sale rows surface
    # only when ad_status='公開中' (on-market); rentals only when ad_status='可'
    # (advertising cleared) — using the wrong gate leaves the assigned rows hidden.
    if args.transaction_type == "sale":
        type_where = "COALESCE(listings.transaction_type,'') = 'sale' AND listings.price_man IS NOT NULL"
        new_ad_status = "公開中"
    else:  # rental
        type_where = "COALESCE(listings.transaction_type,'') != 'sale' AND listings.rent_yen IS NOT NULL"
        new_ad_status = "可"

    bootstrap()
    conn = connect()
    if not bsvc.is_broker(args.broker_id, conn=conn):
        print(f"error: agent {args.broker_id} is not an active broker", file=sys.stderr)
        return 2

    # How many of THIS type the broker already owns per ward (for idempotent
    # top-up). Scoped to the type so a broker's existing rentals don't block a
    # sale top-up (and vice-versa).
    owned: dict[str, int] = {}
    for r in conn.execute(
        f"SELECT {_WARD} AS w, COUNT(*) n FROM listings "
        f"WHERE broker_agent_id = ? AND {type_where} "
        f"AND {_WARD} IS NOT NULL AND {_WARD} != '' GROUP BY w",
        (args.broker_id,),
    ).fetchall():
        owned[r["w"]] = r["n"]

    # Candidate unowned listings of this type, grouped by ward.
    by_ward: dict[str, list[int]] = {}
    for r in conn.execute(
        f"SELECT listings.id AS id, {_WARD} AS w FROM listings "
        f"WHERE broker_agent_id IS NULL AND {type_where} "
        f"AND {_WARD} IS NOT NULL AND {_WARD} != '' "
        f"ORDER BY w, listings.id"
    ).fetchall():
        by_ward.setdefault(r["w"], []).append(r["id"])

    plan: list[tuple[str, int, int]] = []   # (ward, already_owned, to_assign)
    assign_ids: list[int] = []
    for ward, ids in sorted(by_ward.items()):
        have = owned.get(ward, 0)
        need = max(0, args.per_ward - have)
        take = ids[:need]
        if take:
            plan.append((ward, have, len(take)))
            assign_ids.extend(take)

    print(f"broker {args.broker_id}: {len(plan)} ward(s) to top up, "
          f"{len(assign_ids)} {args.transaction_type} listing(s) to assign "
          f"(target {args.per_ward}/ward)")
    for ward, have, take in plan:
        print(f"  {ward:<10} owned {have:>3} → +{take}")

    # Wards the broker will cover after this (owned + newly assigned).
    covered = {_core(w) for w in owned} | {_core(w) for w, _, _ in plan}
    if args.dry_run:
        print(f"\ndry-run: would assign {len(assign_ids)} {args.transaction_type} "
              f"listing(s) (ad_status='{new_ad_status}'); "
              f"areas would cover {len(covered)} ward(s).")
        return 0

    for chunk in _chunks(assign_ids):
        ph = ",".join("?" * len(chunk))
        conn.execute(
            f"UPDATE listings SET broker_agent_id = ?, ad_status = ? WHERE id IN ({ph})",
            [args.broker_id, new_ad_status, *chunk],
        )
    print(f"assigned {len(assign_ids)} {args.transaction_type} listing(s) → "
          f"broker {args.broker_id} (ad_status='{new_ad_status}')")

    if not args.no_areas:
        existing = set(bsvc.get_broker(args.broker_id, conn=conn)["areas"])
        merged = sorted({_core(a) for a in existing if a} | covered)
        conn.execute("UPDATE brokers SET areas_json = ? WHERE agent_id = ?",
                     (json.dumps(merged, ensure_ascii=False), args.broker_id))
        print(f"areas updated → {len(merged)} ward(s): {', '.join(merged)}")

    total = conn.execute(
        "SELECT COUNT(*) n FROM listings WHERE broker_agent_id = ?", (args.broker_id,)
    ).fetchone()["n"]
    print(f"broker {args.broker_id} now owns {total} listing(s) total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
