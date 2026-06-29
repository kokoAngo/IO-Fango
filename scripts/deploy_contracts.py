#!/usr/bin/env python3
"""Compile + deploy the AgreementRegistry contract to your chain.

Compiles fango/chain/contracts/AgreementRegistry.sol with solc 0.8.20 (via
py-solc-x), deploys it from the signer key, verifies the compiled ABI matches the
checked-in AgreementRegistry.abi.json, and prints the .env lines to set.

    python -m scripts.deploy_contracts \
        --rpc https://your-chain/rpc --key 0x<owner_privkey> [--legacy-gas]
    # or rely on env: FANGO_CHAIN_RPC_URL / FANGO_CHAIN_PRIVATE_KEY / FANGO_CHAIN_LEGACY_GAS

Only AgreementRegistry is deployed in this phase (escrow's DealEscrow + an ERC-20
come with the escrow phase). Run scripts/chain_probe.py first to confirm chain id
and gas mode.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_CONTRACTS = REPO_ROOT / "fango" / "chain" / "contracts"
_SOLC_VERSION = "0.8.20"   # minimum (pragma ^0.8.20); a newer system solc is fine


def _ensure_solc() -> str:
    """Return a usable solc version, preferring a native system solc.

    solcx's downloaded macOS binaries are x86_64; on Apple Silicon without Rosetta
    they fail with 'Bad CPU type'. If a native solc is on PATH (e.g.
    `brew install solidity`), import and use it; otherwise fall back to the
    solcx-managed download (works on x86 / Linux / CI)."""
    import solcx
    try:
        solcx.import_installed_solc()
        versions = solcx.get_installed_solc_versions()
        usable = [v for v in versions if str(v) >= _SOLC_VERSION]
        if usable:
            v = max(usable)
            solcx.set_solc_version(v)
            return str(v)
    except Exception:
        pass
    solcx.install_solc(_SOLC_VERSION)
    solcx.set_solc_version(_SOLC_VERSION)
    return _SOLC_VERSION


def _compile(name: str, evm_version: str = "istanbul") -> tuple[list, str]:
    """Compile <name>.sol → (abi, bytecode_hex). Installs solc if needed.

    evm_version defaults to ``istanbul`` so the bytecode runs on older EVM nodes:
    solc 0.8.20 targets ``shanghai`` by default and emits the PUSH0 opcode, which
    pre-Shanghai clients (e.g. Geth 1.9.x) reject as an invalid opcode. Istanbul
    bytecode runs on any Istanbul-or-newer chain.
    """
    from solcx import compile_standard
    solc_version = _ensure_solc()
    src = (_CONTRACTS / f"{name}.sol").read_text("utf-8")
    out = compile_standard(
        {
            "language": "Solidity",
            "sources": {f"{name}.sol": {"content": src}},
            "settings": {
                "optimizer": {"enabled": True, "runs": 200},
                "evmVersion": evm_version,
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
            },
        },
        solc_version=solc_version,
    )
    c = out["contracts"][f"{name}.sol"][name]
    return c["abi"], c["evm"]["bytecode"]["object"]


def _gas_fields(w3, legacy: bool) -> dict:
    if legacy:
        return {"gasPrice": w3.eth.gas_price}
    try:
        w3.eth.fee_history(1, "latest", [50])
        return {"maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(1.5, "gwei")}
    except Exception:
        return {"gasPrice": w3.eth.gas_price}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deploy AgreementRegistry to your chain.")
    parser.add_argument("--rpc", default=os.environ.get("FANGO_CHAIN_RPC_URL"))
    parser.add_argument("--key", default=os.environ.get("FANGO_CHAIN_PRIVATE_KEY"))
    parser.add_argument("--legacy-gas", action="store_true",
                        default=os.environ.get("FANGO_CHAIN_LEGACY_GAS", "").lower() in ("1", "true", "yes"))
    parser.add_argument("--evm-version", default="istanbul",
                        help="solc target EVM version (default istanbul, for older nodes)")
    args = parser.parse_args(argv)
    if not args.rpc or not args.key:
        parser.error("--rpc and --key required (or set FANGO_CHAIN_RPC_URL / FANGO_CHAIN_PRIVATE_KEY)")

    try:
        from web3 import Web3
    except ImportError:
        print("error: web3 not installed — pip install -e '.[chain]'", file=sys.stderr)
        return 2
    try:
        import solcx  # noqa: F401
    except ImportError:
        print("error: py-solc-x not installed — pip install -e '.[chain]'", file=sys.stderr)
        return 2

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 60}))
    if not w3.is_connected():
        print(f"NOT reachable: {args.rpc}", file=sys.stderr)
        return 1
    acct = w3.eth.account.from_key(args.key)
    chain_id = w3.eth.chain_id
    bal = w3.eth.get_balance(acct.address)
    print(f"deployer: {acct.address}  balance={w3.from_wei(bal, 'ether')}  chain_id={chain_id}")
    if bal == 0:
        print("⚠ deployer has zero balance — fund it for gas before deploying", file=sys.stderr)
        return 1

    print(f"compiling AgreementRegistry.sol (solc {_SOLC_VERSION}, evm={args.evm_version}) …")
    abi, bytecode = _compile("AgreementRegistry", evm_version=args.evm_version)

    # Sanity: compiled ABI should match the checked-in one the client loads.
    checked_in = json.loads((_CONTRACTS / "AgreementRegistry.abi.json").read_text("utf-8"))
    def _sig(a):
        return sorted((e.get("type"), e.get("name")) for e in a if e.get("type") in ("function", "event"))
    if _sig(abi) != _sig(checked_in):
        print("⚠ compiled ABI differs from AgreementRegistry.abi.json — the client may "
              "not decode correctly. Review before using in production.", file=sys.stderr)
    else:
        print("ABI matches checked-in AgreementRegistry.abi.json ✓")

    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    nonce = w3.eth.get_transaction_count(acct.address, "pending")
    ctor = contract.constructor()
    try:
        gas = int(ctor.estimate_gas({"from": acct.address}) * 1.2)
    except Exception as exc:
        gas = 1_500_000   # old nodes reject estimateGas's block param; use a ceiling
        print(f"estimate_gas unavailable ({exc}); using fixed gas={gas}")
    tx = ctor.build_transaction({
        "chainId": chain_id, "from": acct.address, "nonce": nonce,
        "gas": gas, **_gas_fields(w3, args.legacy_gas),
    })
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    print("deploying …")
    tx_hash = w3.eth.send_raw_transaction(raw)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt["status"] != 1:
        print(f"deploy reverted (tx {tx_hash.hex()})", file=sys.stderr)
        return 1
    addr = receipt["contractAddress"]
    print(f"\nAgreementRegistry deployed at {addr}  (tx {tx_hash.hex()}, block {receipt['blockNumber']})")
    print("\n# Add to .env (private key not echoed — keep the one you deployed with):")
    print(f"FANGO_CHAIN_RPC_URL={args.rpc}")
    print("FANGO_CHAIN_PRIVATE_KEY=<the key you used>")
    print(f"FANGO_CHAIN_CONTRACT_ADDR={addr}")
    print(f"FANGO_CHAIN_ID={chain_id}")
    if args.legacy_gas:
        print("FANGO_CHAIN_LEGACY_GAS=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
