#!/usr/bin/env python3
"""Create a finalized agreement and (optionally) anchor its hash on chain.

Phase entry point while the agent↔agent negotiation engine doesn't exist yet:
produce an agreement from the command line, hash it, and submit the anchor.

Examples:
    # Create only (no chain needed):
    python -m scripts.anchor_agreement --type rental --listing-id 1 \
        --party-a 1 --party-b 2 --rent-yen 150000 --no-anchor

    # Create + anchor (needs FANGO_CHAIN_* configured + a deployed contract):
    python -m scripts.anchor_agreement --type sale --listing-id 9 \
        --party-a 1 --party-b 2 --price-man 8500

Local EVM test (no real funds): run `anvil` (chain_id 31337), deploy
fango/chain/contracts/AgreementRegistry.sol (e.g. `forge create`), then export
FANGO_CHAIN_RPC_URL=http://127.0.0.1:8545, FANGO_CHAIN_PRIVATE_KEY=<anvil key>,
FANGO_CHAIN_CONTRACT_ADDR=<addr>, FANGO_CHAIN_ID=31337.
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
from fango.agreements import service as svc


def _terms(args) -> dict:
    if args.type == "rental":
        if args.rent_yen is None:
            raise SystemExit("--rent-yen is required for --type rental")
        return {
            "monthly_rent_yen": args.rent_yen,
            "deposit_yen": args.deposit_yen if args.deposit_yen is not None else args.rent_yen * 2,
            "key_money_yen": args.key_money_yen if args.key_money_yen is not None else 0,
            "maintenance_fee_yen": args.maintenance_yen if args.maintenance_yen is not None else 0,
            "contract_months": args.contract_months,
        }
    # sale
    if args.price_man is None:
        raise SystemExit("--price-man is required for --type sale")
    return {
        "price_yen": args.price_man * 10000,
        "deposit_yen": args.deposit_yen if args.deposit_yen is not None else 0,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Create + anchor a finalized agreement")
    p.add_argument("--type", choices=("rental", "sale"), required=True)
    p.add_argument("--listing-id", type=int, required=True)
    p.add_argument("--reins-id", default="")
    p.add_argument("--party-a", type=int, required=True, help="agent id (e.g. tenant/buyer side)")
    p.add_argument("--party-b", type=int, required=True, help="agent id (e.g. landlord/seller side)")
    p.add_argument("--rent-yen", type=int, help="rental: monthly rent in yen")
    p.add_argument("--price-man", type=int, help="sale: price in 万円")
    p.add_argument("--deposit-yen", type=int)
    p.add_argument("--key-money-yen", type=int)
    p.add_argument("--maintenance-yen", type=int)
    p.add_argument("--contract-months", type=int, default=24)
    p.add_argument("--no-anchor", action="store_true", help="create only; do not submit on chain")
    args = p.parse_args()

    bootstrap()
    ag = svc.create_agreement(
        agreement_type=args.type,
        listing_id=args.listing_id,
        listing_reins_id=args.reins_id,
        party_a_agent_id=args.party_a,
        party_b_agent_id=args.party_b,
        terms=_terms(args),
        source="cli",
        auto_anchor=False,
    )
    print(json.dumps({
        "agreement_id": ag.id,
        "content_hash": ag.content_hash,
        "status": ag.status,
        "canonical_json": ag.canonical_json,
    }, ensure_ascii=False, indent=2))

    if args.no_anchor:
        return 0

    result = svc.anchor_now(ag.id)
    print("anchor:", json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in ("confirmed", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
