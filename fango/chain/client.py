"""web3.py-backed anchoring client.

Mirrors the codebase's external-service conventions: a lazy import so ``web3`` is
optional (like ``google-genai`` for consult), an ``is_configured()`` gate (like
the Notion client), graceful degradation, and a module-level singleton with a
``set_chain_client`` test hook (like :func:`fango.consult.engine.set_engine`).

``anchor`` / ``verify`` **never raise** — they return status dicts so the
fire-and-forget worker can record a failure and move on without ever breaking
agreement creation.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..config import ChainSettings, load_chain_settings

log = logging.getLogger(__name__)

_ABI_PATH = Path(__file__).parent / "contracts" / "AgreementRegistry.abi.json"


@runtime_checkable
class ChainClient(Protocol):
    def is_configured(self) -> bool: ...
    def anchor(self, content_hash: str, metadata: dict[str, Any]) -> dict[str, Any]: ...
    def verify(self, content_hash: str) -> dict[str, Any] | None: ...


class Web3ChainClient:
    """Anchors agreement hashes via a deployed ``AgreementRegistry`` contract.

    ``metadata`` = ``{"listing_id": int, "party_a": int, "party_b": int}``. The
    on-chain timestamp comes from ``block.timestamp`` (not metadata); the
    agreement's own ``finalized_at`` lives inside the hashed record.
    """

    def __init__(self, settings: ChainSettings | None = None):
        self.s = settings or load_chain_settings()
        self._w3 = None
        self._acct = None
        self._contract = None
        self._degraded = False
        self._init()

    # -- lifecycle ---------------------------------------------------------
    def _init(self) -> None:
        if not self.s.is_configured():
            self._degraded = True          # unconfigured ⇒ silently off
            return
        try:
            from web3 import Web3          # lazy: web3 is an optional dep
        except ImportError:
            log.warning("web3 not installed — chain anchoring disabled (pip install '.[chain]')")
            self._degraded = True
            return
        try:
            self._w3 = Web3(Web3.HTTPProvider(self.s.rpc_url, request_kwargs={"timeout": 30}))
            self._acct = self._w3.eth.account.from_key(self.s.private_key)
            abi = json.loads(_ABI_PATH.read_text("utf-8"))
            self._contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(self.s.contract_addr), abi=abi
            )
        except Exception as exc:           # pragma: no cover - env/network dependent
            log.warning("chain client init failed: %s", exc)
            self._degraded = True

    def is_configured(self) -> bool:
        return self.s.is_configured() and not self._degraded

    # -- operations --------------------------------------------------------
    def anchor(self, content_hash: str, metadata: dict[str, Any]) -> dict[str, Any]:
        """Submit an anchoring tx and wait for the receipt. Never raises."""
        if not self.is_configured():
            return {"status": "skipped", "reason": "chain not configured"}
        try:
            from web3 import Web3
            h = Web3.to_bytes(hexstr=content_hash)        # 32-byte digest -> bytes32
            fn = self._contract.functions.anchor(
                h,
                int(metadata["listing_id"]),
                int(metadata["party_a"]),
                int(metadata["party_b"]),
            )
            nonce = self._w3.eth.get_transaction_count(self._acct.address, "pending")
            gas = fn.estimate_gas({"from": self._acct.address})
            tx = fn.build_transaction({
                "chainId": self.s.chain_id,
                "from": self._acct.address,
                "nonce": nonce,
                "gas": int(gas * 1.2),
                "maxFeePerGas": self._w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self._w3.to_wei(1.5, "gwei"),
            })
            signed = self._acct.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = self._w3.eth.send_raw_transaction(raw)
            receipt = self._w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=self.s.confirm_timeout_sec
            )
            ok = receipt["status"] == 1
            return {
                "status": "confirmed" if ok else "failed",
                "tx_hash": tx_hash.hex(),
                "block_number": receipt["blockNumber"],
                "chain_id": self.s.chain_id,
                "contract_addr": self.s.contract_addr,
                "error": None if ok else "transaction reverted",
            }
        except Exception as exc:
            log.warning("anchor failed for %s: %s", content_hash, exc)
            return {"status": "failed", "tx_hash": None, "block_number": None,
                    "chain_id": self.s.chain_id, "contract_addr": self.s.contract_addr,
                    "error": str(exc)}

    def verify(self, content_hash: str) -> dict[str, Any] | None:
        """Read the contract: returns the on-chain record or None. Never raises."""
        if not self.is_configured():
            return None
        try:
            from web3 import Web3
            h = Web3.to_bytes(hexstr=content_hash)
            anchored, ts = self._contract.functions.isAnchored(h).call()
            if not anchored:
                return None
            return {"content_hash": content_hash, "anchored_at_ts": int(ts),
                    "chain_id": self.s.chain_id, "contract_addr": self.s.contract_addr}
        except Exception as exc:           # pragma: no cover - network dependent
            log.warning("verify failed for %s: %s", content_hash, exc)
            return None


# Module singleton + test hook (mirrors consult.engine.get_engine/set_engine).
_client: ChainClient | None = None


def get_chain_client() -> ChainClient:
    global _client
    if _client is None:
        _client = Web3ChainClient()
    return _client


def set_chain_client(client: ChainClient | None) -> None:
    """Install a client (e.g. FakeChainClient) or reset to None. Tests must reset
    in teardown so a fake never leaks across tests."""
    global _client
    _client = client
