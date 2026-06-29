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

_CONTRACTS = Path(__file__).parent / "contracts"
_ABI_PATH = _CONTRACTS / "AgreementRegistry.abi.json"
_ESCROW_ABI_PATH = _CONTRACTS / "DealEscrow.abi.json"
_ERC20_ABI_PATH = _CONTRACTS / "IERC20.abi.json"

# DealEscrow.Status enum → name (index matches the Solidity enum order).
_ESCROW_STATUS = ("none", "open", "funded", "settled", "slashed", "cancelled", "expired")


@runtime_checkable
class ChainClient(Protocol):
    def is_configured(self) -> bool: ...
    def anchor(self, content_hash: str, metadata: dict[str, Any]) -> dict[str, Any]: ...
    def verify(self, content_hash: str) -> dict[str, Any] | None: ...
    # escrow
    def escrow_configured(self) -> bool: ...
    def open_escrow(self, **kw) -> dict[str, Any]: ...
    def agent_deposit(self, **kw) -> dict[str, Any]: ...
    def settle(self, onchain_escrow_id: int) -> dict[str, Any]: ...
    def slash(self, *, onchain_escrow_id: int, loser_address: str) -> dict[str, Any]: ...
    def cancel(self, onchain_escrow_id: int) -> dict[str, Any]: ...
    def refund_expired(self, onchain_escrow_id: int) -> dict[str, Any]: ...
    def escrow_state(self, onchain_escrow_id: int) -> dict[str, Any] | None: ...


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
        self._contract = None          # AgreementRegistry
        self._escrow = None            # DealEscrow
        self._erc20_abi = None
        self._degraded = False
        self._init()

    # -- lifecycle ---------------------------------------------------------
    def _init(self) -> None:
        # Set up web3 + signer if EITHER anchoring or escrow is configured.
        if not (self.s.is_configured() or self.s.escrow_is_configured()):
            self._degraded = True
            return
        try:
            from web3 import Web3          # lazy: web3 is an optional dep
        except ImportError:
            log.warning("web3 not installed — chain features disabled (pip install '.[chain]')")
            self._degraded = True
            return
        try:
            self._w3 = Web3(Web3.HTTPProvider(self.s.rpc_url, request_kwargs={"timeout": 30}))
            self._acct = self._w3.eth.account.from_key(self.s.private_key)
            if self.s.is_configured():
                self._contract = self._w3.eth.contract(
                    address=Web3.to_checksum_address(self.s.contract_addr),
                    abi=json.loads(_ABI_PATH.read_text("utf-8")),
                )
            if self.s.escrow_is_configured():
                self._escrow = self._w3.eth.contract(
                    address=Web3.to_checksum_address(self.s.escrow_addr),
                    abi=json.loads(_ESCROW_ABI_PATH.read_text("utf-8")),
                )
                self._erc20_abi = json.loads(_ERC20_ABI_PATH.read_text("utf-8"))
        except Exception as exc:           # pragma: no cover - env/network dependent
            log.warning("chain client init failed: %s", exc)
            self._degraded = True

    def is_configured(self) -> bool:
        return self.s.is_configured() and not self._degraded and self._contract is not None

    # -- gas pricing (EIP-1559 with legacy fallback) -----------------------
    def _gas_fields(self) -> dict[str, Any]:
        """Return the gas-pricing keys for build_transaction.

        Prefers EIP-1559 (``maxFeePerGas``/``maxPriorityFeePerGas``); falls back
        to legacy ``gasPrice`` when ``FANGO_CHAIN_LEGACY_GAS`` is set or the node
        doesn't expose ``eth_feeHistory`` (private/older EVM chains). Detection is
        cached on first use."""
        if self.s.legacy_gas:
            return {"gasPrice": self._w3.eth.gas_price}
        cached = getattr(self, "_supports_1559", None)
        if cached is None:
            try:
                self._w3.eth.fee_history(1, "latest", [50])
                cached = True
            except Exception:
                cached = False
            self._supports_1559 = cached
        if not cached:
            return {"gasPrice": self._w3.eth.gas_price}
        return {
            "maxFeePerGas": self._w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": self._w3.to_wei(1.5, "gwei"),
        }

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
                **self._gas_fields(),
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

    # -- escrow ------------------------------------------------------------
    def escrow_configured(self) -> bool:
        return self.s.escrow_is_configured() and not self._degraded and self._escrow is not None

    def _send_owner(self, fn, *, label: str) -> dict[str, Any]:
        """Build/sign/send a tx from the FANGO owner key (mirrors anchor())."""
        try:
            nonce = self._w3.eth.get_transaction_count(self._acct.address, "pending")
            gas = fn.estimate_gas({"from": self._acct.address})
            tx = fn.build_transaction({
                "chainId": self.s.chain_id, "from": self._acct.address, "nonce": nonce,
                "gas": int(gas * 1.2), **self._gas_fields(),
            })
            signed = self._acct.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = self._w3.eth.send_raw_transaction(raw)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.s.confirm_timeout_sec)
            ok = receipt["status"] == 1
            return {"status": "confirmed" if ok else "failed", "tx_hash": tx_hash.hex(),
                    "block_number": receipt["blockNumber"], "chain_id": self.s.chain_id,
                    "contract_addr": self.s.escrow_addr, "receipt": receipt,
                    "error": None if ok else "transaction reverted"}
        except Exception as exc:
            log.warning("escrow %s failed: %s", label, exc)
            return {"status": "failed", "tx_hash": None, "block_number": None,
                    "chain_id": self.s.chain_id, "contract_addr": self.s.escrow_addr, "error": str(exc)}

    def open_escrow(self, *, agreement_hash: str, token: str, party_a: str, party_b: str,
                    deposit_a: int, deposit_b: int, deadline_ts: int) -> dict[str, Any]:
        if not self.escrow_configured():
            return {"status": "skipped", "reason": "escrow not configured"}
        from web3 import Web3
        fn = self._escrow.functions.open(
            Web3.to_bytes(hexstr=agreement_hash), Web3.to_checksum_address(token),
            Web3.to_checksum_address(party_a), Web3.to_checksum_address(party_b),
            int(deposit_a), int(deposit_b), int(deadline_ts))
        res = self._send_owner(fn, label="open")
        if res["status"] == "confirmed":
            eid = None
            try:
                evs = self._escrow.events.Opened().process_receipt(res.get("receipt"))
                if evs:
                    eid = int(evs[0]["args"]["id"])
            except Exception:
                eid = None
            if eid is None:
                # Fallback: FANGO is the sole serial opener, so nextId-1 is this
                # escrow right after a confirmed open.
                try:
                    eid = int(self._escrow.functions.nextId().call()) - 1
                except Exception:
                    eid = None
            res["onchain_escrow_id"] = eid
        res.pop("receipt", None)
        return res

    def agent_deposit(self, *, agent_id: int, onchain_escrow_id: int, token: str,
                      escrow_addr: str, amount: int, from_address: str = "") -> dict[str, Any]:
        """Custodial approve(exact) + deposit, both signed by the agent's key.
        ``from_address`` is unused here (the key derives the sender) — it exists
        for the FakeChainClient ledger."""
        if not self.escrow_configured():
            return {"status": "skipped", "reason": "escrow not configured"}
        from web3 import Web3
        from .. import identity
        token_c = self._w3.eth.contract(address=Web3.to_checksum_address(token), abi=self._erc20_abi)
        approve_fn = token_c.functions.approve(Web3.to_checksum_address(escrow_addr), int(amount))
        ar = identity.sign_and_send_tx(agent_id, approve_fn, w3=self._w3, chain_id=self.s.chain_id)
        if ar["status"] != "confirmed":
            return {"status": ar["status"], "approve": ar, "deposit": None,
                    "error": ar.get("error") or ar.get("reason")}
        deposit_fn = self._escrow.functions.deposit(int(onchain_escrow_id))
        dr = identity.sign_and_send_tx(agent_id, deposit_fn, w3=self._w3, chain_id=self.s.chain_id)
        return {"status": dr["status"], "approve": ar, "deposit": dr,
                "tx_hash": dr.get("tx_hash"), "block_number": dr.get("block_number"),
                "chain_id": self.s.chain_id, "contract_addr": self.s.escrow_addr,
                "error": dr.get("error")}

    def settle(self, onchain_escrow_id: int) -> dict[str, Any]:
        if not self.escrow_configured():
            return {"status": "skipped", "reason": "escrow not configured"}
        r = self._send_owner(self._escrow.functions.settle(int(onchain_escrow_id)), label="settle")
        r.pop("receipt", None); return r

    def slash(self, *, onchain_escrow_id: int, loser_address: str) -> dict[str, Any]:
        if not self.escrow_configured():
            return {"status": "skipped", "reason": "escrow not configured"}
        from web3 import Web3
        r = self._send_owner(self._escrow.functions.slash(
            int(onchain_escrow_id), Web3.to_checksum_address(loser_address)), label="slash")
        r.pop("receipt", None); return r

    def cancel(self, onchain_escrow_id: int) -> dict[str, Any]:
        if not self.escrow_configured():
            return {"status": "skipped", "reason": "escrow not configured"}
        r = self._send_owner(self._escrow.functions.cancel(int(onchain_escrow_id)), label="cancel")
        r.pop("receipt", None); return r

    def refund_expired(self, onchain_escrow_id: int) -> dict[str, Any]:
        if not self.escrow_configured():
            return {"status": "skipped", "reason": "escrow not configured"}
        r = self._send_owner(self._escrow.functions.refundExpired(int(onchain_escrow_id)), label="refund_expired")
        r.pop("receipt", None); return r

    def escrow_state(self, onchain_escrow_id: int) -> dict[str, Any] | None:
        if not self.escrow_configured():
            return None
        try:
            e = self._escrow.functions.getEscrow(int(onchain_escrow_id)).call()
            # tuple order matches the Escrow struct
            status_idx = int(e[9])
            return {"agreement_hash": "0x" + e[0].hex(), "token": e[1],
                    "party_a": e[2], "party_b": e[3], "deposit_a": int(e[4]), "deposit_b": int(e[5]),
                    "funded_a": bool(e[6]), "funded_b": bool(e[7]), "deadline": int(e[8]),
                    "status": _ESCROW_STATUS[status_idx] if status_idx < len(_ESCROW_STATUS) else str(status_idx)}
        except Exception as exc:           # pragma: no cover
            log.warning("escrow_state failed for %s: %s", onchain_escrow_id, exc)
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
