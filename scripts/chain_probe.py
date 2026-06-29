#!/usr/bin/env python3
"""Probe an EVM chain before wiring FangoCity to it.

Checks reachability, chain id, gas mode (EIP-1559 vs legacy), client version, and
— if a key is given — the signer's balance. Use it to fill FANGO_CHAIN_ID and
decide whether FANGO_CHAIN_LEGACY_GAS=1 is needed.

    python -m scripts.chain_probe --rpc https://your-chain/rpc [--key 0x...]
    # or rely on env: FANGO_CHAIN_RPC_URL / FANGO_CHAIN_PRIVATE_KEY
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe an EVM chain for FangoCity.")
    parser.add_argument("--rpc", default=os.environ.get("FANGO_CHAIN_RPC_URL"),
                        help="JSON-RPC URL (or set FANGO_CHAIN_RPC_URL)")
    parser.add_argument("--key", default=os.environ.get("FANGO_CHAIN_PRIVATE_KEY"),
                        help="signer private key, to report its address + balance")
    args = parser.parse_args(argv)
    if not args.rpc:
        parser.error("--rpc required (or set FANGO_CHAIN_RPC_URL)")

    try:
        from web3 import Web3
    except ImportError:
        print("error: web3 not installed — pip install -e '.[chain]'", file=sys.stderr)
        return 2

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        print(f"NOT reachable: {args.rpc}", file=sys.stderr)
        return 1
    print(f"reachable: {args.rpc}")

    try:
        cid = w3.eth.chain_id
        print(f"chain_id: {cid}   → set FANGO_CHAIN_ID={cid}")
    except Exception as exc:
        print(f"chain_id: error ({exc}) — may not be EVM/JSON-RPC")
        return 1

    try:
        print(f"client: {w3.client_version}")
    except Exception:
        pass

    # Gas mode: EIP-1559 if eth_feeHistory works, else legacy gasPrice.
    legacy = False
    try:
        w3.eth.fee_history(1, "latest", [50])
        print("gas mode: EIP-1559 (eth_feeHistory OK) → leave FANGO_CHAIN_LEGACY_GAS unset")
    except Exception as exc:
        legacy = True
        print(f"gas mode: LEGACY (eth_feeHistory unsupported: {exc}) → set FANGO_CHAIN_LEGACY_GAS=1")
    try:
        gp = w3.eth.gas_price
        print(f"gas_price: {gp} wei ({w3.from_wei(gp, 'gwei')} gwei)")
    except Exception as exc:
        print(f"gas_price: error ({exc})")

    if args.key:
        try:
            acct = w3.eth.account.from_key(args.key)
            bal = w3.eth.get_balance(acct.address)
            print(f"signer: {acct.address}")
            print(f"balance: {w3.from_wei(bal, 'ether')} (native token)"
                  f"{'   ⚠ ZERO — fund it for gas' if bal == 0 else ''}")
        except Exception as exc:
            print(f"signer: error ({exc})")

    print("\nsummary: " + ("EVM-compatible, LEGACY gas" if legacy else "EVM-compatible, EIP-1559"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
