#!/usr/bin/env python3
"""Open and adjudicate earnest-money (保証金) escrows.

Deposits are entered in WHOLE tokens and scaled by FANGO_CHAIN_TOKEN_DECIMALS.

    python -m scripts.escrow open  --agreement-id 7 --deposit 5 [--deadline-hours 48]
    python -m scripts.escrow fund  --escrow-id 3 --party a
    python -m scripts.escrow settle --escrow-id 3
    python -m scripts.escrow slash  --escrow-id 3 --loser-agent 12
    python -m scripts.escrow show   --escrow-id 3 [--json]

Needs FANGO_CHAIN_RPC_URL + PRIVATE_KEY + ESCROW_ADDR + TOKEN_ADDR for on-chain
ops (+ `pip install '.[chain]'`). Local test: deploy TestToken + DealEscrow on a
ganache node, mint token + gas-ETH to the agents' addresses, set the envs.
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
from fango.config import load_chain_settings
from fango.escrow import service as esvc


def _scale(whole: float) -> int:
    return int(whole * (10 ** load_chain_settings().token_decimals))


def main() -> int:
    p = argparse.ArgumentParser(description="Earnest-money escrow")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("open"); o.add_argument("--agreement-id", type=int, required=True)
    o.add_argument("--deposit", type=float, required=True, help="whole tokens, each party")
    o.add_argument("--deposit-b", type=float, help="different B deposit (default = --deposit)")
    o.add_argument("--deadline-hours", type=int, default=48)

    f = sub.add_parser("fund"); f.add_argument("--escrow-id", type=int, required=True)
    f.add_argument("--party", choices=("a", "b"), required=True)

    for name in ("settle", "cancel", "refund-expired"):
        sp = sub.add_parser(name); sp.add_argument("--escrow-id", type=int, required=True)
    sl = sub.add_parser("slash"); sl.add_argument("--escrow-id", type=int, required=True)
    sl.add_argument("--loser-agent", type=int, required=True)

    sh = sub.add_parser("show"); sh.add_argument("--escrow-id", type=int, required=True)
    sh.add_argument("--json", action="store_true")

    args = p.parse_args()
    bootstrap()

    if args.cmd == "open":
        import time
        dl = int(time.time()) + args.deadline_hours * 3600
        esc = esvc.open_escrow(args.agreement_id, deposit_a=_scale(args.deposit),
                               deposit_b=_scale(args.deposit_b if args.deposit_b is not None else args.deposit),
                               deadline_ts=dl)
        print(json.dumps({"escrow_id": esc.id, "status": esc.status,
                          "onchain_escrow_id": esc.onchain_escrow_id,
                          "content_hash": esc.content_hash}, indent=2))
    elif args.cmd == "fund":
        print(json.dumps(esvc.fund(args.escrow_id, args.party), indent=2, default=str))
    elif args.cmd == "settle":
        print(json.dumps(esvc.settle(args.escrow_id), indent=2, default=str))
    elif args.cmd == "slash":
        print(json.dumps(esvc.slash(args.escrow_id, args.loser_agent), indent=2, default=str))
    elif args.cmd == "cancel":
        print(json.dumps(esvc.cancel(args.escrow_id), indent=2, default=str))
    elif args.cmd == "refund-expired":
        print(json.dumps(esvc.refund_expired(args.escrow_id), indent=2, default=str))
    elif args.cmd == "show":
        esc = esvc.get_escrow(args.escrow_id)
        if esc is None:
            print("not found"); return 1
        out = {"escrow": esc.__dict__, "events": esvc.list_events(args.escrow_id),
               "sync": esvc.sync_state(args.escrow_id)}
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
