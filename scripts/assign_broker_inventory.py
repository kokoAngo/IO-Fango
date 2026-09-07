#!/usr/bin/env python3
"""Give a broker inventory across many wards so it routes + can propose everywhere.

For each ward it tops the broker up to ``--per-ward`` listings of one transaction
type (drawn from the unowned house pool), marks them advertisable so they surface
in search and feed broker_propose_terms, and merges those wards into the broker's
``areas`` (so area-based routing fires for them too).

``--transaction-type`` picks which pool to draw from (default ``rental``):
  * ``rental`` — non-sale rows that have a rent (``rent_yen``).
  * ``sale``   — ``transaction_type='sale'`` rows that have a price (``price_man``).

The pool is restricted to rows that are ALREADY cleared for public advertising
(the same predicate ``service.is_advertisable`` applies), and ad_status is left
untouched. Assigning a listing to a broker says who owns it; it is not a
judgement about whether the source cleared it for advertising, and rewriting
ad_status to make an assignment "work" is おとり広告 on real inventory.

    # give broker 49 rental inventory (current default behaviour)
    python -m scripts.assign_broker_inventory --broker-id 49 --per-ward 15
    # ALSO give it sale inventory so it can serve 買房 customers
    python -m scripts.assign_broker_inventory --broker-id 49 --per-ward 15 --transaction-type sale

Idempotent per type: the per-ward top-up counts only the broker's listings OF THE
SAME type, so assigning ``sale`` doesn't get blocked by rentals it already owns,
and re-running with the same --per-ward is a no-op once each ward is full.
Ward key = COALESCE(NULLIF(ward,''), city), so it works whether the ward name
lives in the `ward` or the `city` column.

``--force-ad-status`` restores the old behaviour of overwriting ad_status so
any assigned row surfaces. That is for a prototype/simulation fleet on synthetic
inventory ONLY — never point it at real listings.
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
from fango.listings.service import (
    ADVERTISABLE_RENTAL_AD_STATUS,
    ADVERTISABLE_SALE_AD_STATUSES,
    OFF_MARKET_RENTAL_STATUSES,
    OFF_MARKET_SALE_STATUSES,
    WINDOWED_SOURCE,
    window_start,
)

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
    ap.add_argument("--force-ad-status", action="store_true",
                    help="Overwrite ad_status on the assigned rows so they surface even "
                         "if the source never cleared them. SIMULATION FLEETS ONLY — on "
                         "real inventory this is おとり広告.")
    args = ap.parse_args(argv)

    # `type_where` identifies the transaction type (used for the idempotent
    # per-ward count of what the broker already owns). `pool_where` narrows the
    # candidates further to rows that are actually surfaceable — it mirrors the
    # public gate in service._build_search_where, built from the same constants
    # so the two cannot drift.
    pool_params: list = []
    if args.transaction_type == "sale":
        type_where = ("COALESCE(listings.transaction_type,'') = 'sale' "
                      "AND listings.price_man IS NOT NULL")
        # 成約/申込あり is excluded even under --force-ad-status: an off-market
        # listing is not a compliance preference, it is simply gone.
        off = ",".join("?" * len(OFF_MARKET_SALE_STATUSES))
        pool_where = f"{type_where} AND COALESCE(listings.tenancy_status,'') NOT IN ({off})"
        pool_params += list(OFF_MARKET_SALE_STATUSES)
        if not args.force_ad_status:
            ok = ",".join("?" * len(ADVERTISABLE_SALE_AD_STATUSES))
            pool_where += f" AND listings.ad_status IN ({ok})"
            pool_params += list(ADVERTISABLE_SALE_AD_STATUSES)
        new_ad_status = ADVERTISABLE_SALE_AD_STATUSES[0]
    else:  # rental
        type_where = ("COALESCE(listings.transaction_type,'') != 'sale' "
                      "AND listings.rent_yen IS NOT NULL")
        # 成約済 is excluded even under --force-ad-status, for the same reason
        # 成約/申込あり is on the sale side: an already-let flat is not a
        # compliance preference, it is simply gone.
        off_r = ",".join("?" * len(OFF_MARKET_RENTAL_STATUSES))
        pool_where = f"{type_where} AND COALESCE(listings.tenancy_status,'') NOT IN ({off_r})"
        pool_params += list(OFF_MARKET_RENTAL_STATUSES)
        if not args.force_ad_status:
            pool_where += " AND listings.ad_status = ?"
            pool_params.append(ADVERTISABLE_RENTAL_AD_STATUS)
        new_ad_status = ADVERTISABLE_RENTAL_AD_STATUS

    # The visibility window applies here too. Handing a broker a listing that
    # has aged out would put inventory in its shopfront that the public gate
    # will not show — and, worse, that the broker would go on proposing to
    # customers. Not overridable by --force-ad-status: that flag is about ad
    # clearance on synthetic inventory, not about age.
    pool_where += (f" AND (COALESCE(listings.source,'') != ?"
                   f" OR (listings.posted_at IS NOT NULL AND listings.posted_at >= ?))")
    pool_params.append(WINDOWED_SOURCE)
    pool_params.append(window_start("sale" if args.transaction_type == "sale" else "rent"))

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
        f"WHERE broker_agent_id IS NULL AND {pool_where} "
        f"AND {_WARD} IS NOT NULL AND {_WARD} != '' "
        f"ORDER BY w, listings.id",
        pool_params,
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
    ad_note = (f"OVERWRITING ad_status='{new_ad_status}'" if args.force_ad_status
               else "ad_status untouched (pool is already advertisable)")
    if args.dry_run:
        print(f"\ndry-run: would assign {len(assign_ids)} {args.transaction_type} "
              f"listing(s), {ad_note}; areas would cover {len(covered)} ward(s).")
        return 0

    for chunk in _chunks(assign_ids):
        ph = ",".join("?" * len(chunk))
        if args.force_ad_status:
            conn.execute(
                f"UPDATE listings SET broker_agent_id = ?, ad_status = ? WHERE id IN ({ph})",
                [args.broker_id, new_ad_status, *chunk],
            )
        else:
            conn.execute(
                f"UPDATE listings SET broker_agent_id = ? WHERE id IN ({ph})",
                [args.broker_id, *chunk],
            )
    print(f"assigned {len(assign_ids)} {args.transaction_type} listing(s) → "
          f"broker {args.broker_id}; {ad_note}")

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
