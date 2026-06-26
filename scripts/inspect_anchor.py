#!/usr/bin/env python3
"""Inspect a finalized agreement and its on-chain anchor.

Shows, for one agreement id:
  - the off-chain canonical record (terms stay off chain),
  - the verify result (recompute hash + check the chain),
  - the latest anchor attempt (tx hash, block, status),
  - and — when a tx exists and the chain is reachable — the actual on-chain
    content: the block, the transaction, the decoded calldata, the Anchored
    event log, and the contract storage (anchoredAt / isAnchored).

Usage:
    python -m scripts.inspect_anchor <agreement_id>
    python -m scripts.inspect_anchor <agreement_id> --json

Needs FANGO_CHAIN_* configured (+ `pip install '.[chain]'`) for the on-chain
sections; without them it still prints the off-chain record and stored anchor row.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fango.db import bootstrap, connect
from fango.agreements import service as svc
from fango.config import load_chain_settings
from fango.chain.client import _ABI_PATH


def _hx(s: str | None) -> str | None:
    if not s:
        return None
    return s if s.startswith("0x") else "0x" + s


def _gather(agreement_id: int) -> dict:
    """Collect everything into a dict (also what --json prints)."""
    out: dict = {"agreement_id": agreement_id}
    ag = svc.get_agreement(agreement_id)
    if ag is None:
        out["error"] = "agreement not found"
        return out
    out["record"] = {
        "id": ag.id, "type": ag.agreement_type, "status": ag.status,
        "content_hash": ag.content_hash, "listing_id": ag.listing_id,
        "party_a_agent_id": ag.party_a_agent_id, "party_b_agent_id": ag.party_b_agent_id,
        "price_yen": ag.price_yen, "finalized_at": ag.finalized_at,
        "terms": json.loads(ag.terms_json), "canonical_json": ag.canonical_json,
    }
    out["verify"] = svc.verify_agreement(agreement_id)

    conn = connect()
    try:
        anchor = svc.latest_anchor(agreement_id, conn)
    finally:
        conn.close()
    out["anchor_row"] = dict(anchor) if anchor else None

    # On-chain detail (best-effort; skipped if no tx / no chain / no web3).
    tx_hash = _hx(anchor["tx_hash"]) if anchor else None
    settings = load_chain_settings()
    if not (tx_hash and settings.is_configured()):
        out["onchain"] = {"available": False,
                          "reason": "no tx hash" if not tx_hash else "chain not configured"}
        return out
    try:
        from web3 import Web3
    except ImportError:
        out["onchain"] = {"available": False, "reason": "web3 not installed"}
        return out
    try:
        w3 = Web3(Web3.HTTPProvider(settings.rpc_url, request_kwargs={"timeout": 30}))
        abi = json.loads(Path(_ABI_PATH).read_text("utf-8"))
        c = w3.eth.contract(address=Web3.to_checksum_address(settings.contract_addr), abi=abi)
        tx = w3.eth.get_transaction(tx_hash)
        blk = w3.eth.get_block(tx.blockNumber)
        fn, args = c.decode_function_input(tx.input)
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        events = c.events.Anchored().process_receipt(receipt)
        h = Web3.to_bytes(hexstr=ag.content_hash)
        anchored, ts = c.functions.isAnchored(h).call()
        out["onchain"] = {
            "available": True,
            "block": {"number": blk.number, "hash": blk.hash.hex(), "timestamp": blk.timestamp},
            "tx": {"hash": tx_hash, "from": tx["from"], "to": tx.to,
                   "input_bytes": len(tx.input)},
            "calldata": {"function": fn.fn_name,
                         **{k: (v.hex() if isinstance(v, (bytes, bytearray)) else v)
                            for k, v in args.items()}},
            "events": [{"hash": "0x" + e["args"]["hash"].hex(),
                        "listingId": e["args"]["listingId"],
                        "partyA": e["args"]["partyA"], "partyB": e["args"]["partyB"],
                        "ts": e["args"]["ts"]} for e in events],
            "state": {"isAnchored": bool(anchored), "anchoredAt": int(ts)},
        }
    except Exception as exc:
        out["onchain"] = {"available": False, "reason": f"chain read failed: {exc}"}
    return out


def _print_human(d: dict) -> None:
    if "error" in d:
        print("ERROR:", d["error"]); return
    r = d["record"]; v = d["verify"]; a = d.get("anchor_row")
    print("=" * 64)
    print(f"AGREEMENT #{r['id']}  [{r['type']}]  status={r['status']}")
    print("=" * 64)
    print("  content_hash :", r["content_hash"])
    print("  parties      : a=%s b=%s   listing=%s" %
          (r["party_a_agent_id"], r["party_b_agent_id"], r["listing_id"]))
    print("  finalized_at :", r["finalized_at"])
    print("  terms        :", json.dumps(r["terms"], ensure_ascii=False))
    print("  (terms above stay OFF chain — only the hash is anchored)")

    print("\n-- verify -------------------------------------------------------")
    ok = "OK" if v.get("record_ok") else "TAMPERED"
    print(f"  record integrity : {ok} (recomputed == stored hash)")
    print(f"  on_chain         : {v.get('on_chain')}")

    print("\n-- latest anchor attempt ---------------------------------------")
    if not a:
        print("  (none — never anchored)")
    else:
        print("  status :", a["status"], "  tx:", a["tx_hash"] or "-",
              " block:", a["block_number"], " chain:", a["chain_id"])
        if a["error"]:
            print("  error  :", a["error"])

    oc = d.get("onchain", {})
    if not oc.get("available"):
        print("\n-- on-chain content --------------------------------------------")
        print("  (unavailable:", oc.get("reason"), ")")
        return
    print("\n-- ① block -----------------------------------------------------")
    b = oc["block"]
    print(f"  number={b['number']}  hash={b['hash']}  ts={b['timestamp']}")
    print("-- ② transaction -----------------------------------------------")
    t = oc["tx"]
    print(f"  from={t['from']}\n  to(contract)={t['to']}\n  input={t['input_bytes']} bytes")
    print("-- ③ decoded calldata (what was actually written) --------------")
    cd = oc["calldata"]
    for k, val in cd.items():
        print(f"  {k:10}: {val}")
    print("-- ④ Anchored event log ----------------------------------------")
    for e in oc["events"]:
        print(f"  hash={e['hash']}")
        print(f"  listingId={e['listingId']} partyA={e['partyA']} partyB={e['partyB']} ts={e['ts']}")
    print("-- ⑤ contract storage ------------------------------------------")
    print(f"  isAnchored={oc['state']['isAnchored']}  anchoredAt={oc['state']['anchoredAt']}")


def main() -> int:
    p = argparse.ArgumentParser(description="Inspect an agreement + its on-chain anchor")
    p.add_argument("agreement_id", type=int)
    p.add_argument("--json", action="store_true", help="emit raw JSON instead of a human view")
    args = p.parse_args()
    bootstrap()
    data = _gather(args.agreement_id)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(data)
    return 0 if "error" not in data else 1


if __name__ == "__main__":
    raise SystemExit(main())
