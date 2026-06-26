"""In-memory fake chain client for tests — no node required.

Returns the same dict shapes as :class:`Web3ChainClient` so service code is
exercised identically. Install via ``set_chain_client(FakeChainClient())`` and
reset to ``None`` in teardown.
"""
from __future__ import annotations

from typing import Any


class FakeChainClient:
    def __init__(self, *, configured: bool = True, fail: bool = False,
                 escrow_configured: bool = True, now_ts: int = 1_700_000_000):
        self._store: dict[str, dict[str, Any]] = {}
        self._configured = configured
        self._escrow_configured = configured and escrow_configured
        self._fail = fail
        self._n = 0
        # In-memory ERC-20 ledger + escrows for the escrow tests.
        self.balances: dict[str, int] = {}
        self.escrows: dict[int, dict[str, Any]] = {}
        self._eid = 0
        self.now_ts = now_ts

    def is_configured(self) -> bool:
        return self._configured

    # -- token ledger helpers (tests use mint) -----------------------------
    def mint(self, address: str, amount: int) -> None:
        self.balances[address] = self.balances.get(address, 0) + int(amount)

    def balance_of(self, address: str) -> int:
        return self.balances.get(address, 0)

    def _credit(self, addr: str, amt: int) -> None:
        self.balances[addr] = self.balances.get(addr, 0) + amt

    def _debit(self, addr: str, amt: int) -> None:
        self.balances[addr] = self.balances.get(addr, 0) - amt

    def anchor(self, content_hash: str, metadata: dict[str, Any]) -> dict[str, Any]:
        if not self._configured:
            return {"status": "skipped", "reason": "not configured"}
        if self._fail:
            return {"status": "failed", "tx_hash": None, "block_number": None,
                    "chain_id": 31337, "contract_addr": "0xFAKE", "error": "boom"}
        if content_hash in self._store:
            return {"status": "failed", "tx_hash": None, "block_number": None,
                    "chain_id": 31337, "contract_addr": "0xFAKE",
                    "error": f"AlreadyAnchored({content_hash})"}
        self._n += 1
        rec = {
            "status": "confirmed",
            "tx_hash": "0x" + f"{self._n:064x}",
            "block_number": 1000 + self._n,
            "chain_id": 31337,
            "contract_addr": "0xFAKE",
            "error": None,
        }
        self._store[content_hash] = {**rec, **metadata, "anchored_at_ts": 1_700_000_000 + self._n}
        return rec

    def verify(self, content_hash: str) -> dict[str, Any] | None:
        rec = self._store.get(content_hash)
        if rec is None:
            return None
        return {"content_hash": content_hash, "anchored_at_ts": rec["anchored_at_ts"],
                "chain_id": rec["chain_id"], "contract_addr": rec["contract_addr"]}

    # -- escrow (in-memory mirror of DealEscrow) ---------------------------
    def escrow_configured(self) -> bool:
        return self._escrow_configured

    def _ok(self, **extra):
        self._n += 1
        return {"status": "confirmed", "tx_hash": "0x" + f"{self._n:064x}",
                "block_number": 2000 + self._n, "chain_id": 31337,
                "contract_addr": "0xESCROW", "error": None, **extra}

    def open_escrow(self, *, agreement_hash, token, party_a, party_b,
                    deposit_a, deposit_b, deadline_ts) -> dict[str, Any]:
        if not self._escrow_configured:
            return {"status": "skipped", "reason": "escrow not configured"}
        self._eid += 1
        self.escrows[self._eid] = {
            "agreement_hash": agreement_hash, "token": token,
            "party_a": party_a, "party_b": party_b,
            "deposit_a": int(deposit_a), "deposit_b": int(deposit_b),
            "funded_a": False, "funded_b": False, "deadline": int(deadline_ts),
            "status": "open",
        }
        return self._ok(onchain_escrow_id=self._eid)

    def agent_deposit(self, *, agent_id, onchain_escrow_id, token, escrow_addr,
                      amount, from_address="") -> dict[str, Any]:
        if not self._escrow_configured:
            return {"status": "skipped", "reason": "escrow not configured"}
        e = self.escrows.get(int(onchain_escrow_id))
        if e is None or e["status"] != "open":
            return {"status": "failed", "error": "bad escrow state"}
        amt = int(amount)
        if self.balances.get(from_address, 0) < amt:
            return {"status": "failed", "error": "insufficient balance"}
        if from_address == e["party_a"]:
            e["funded_a"] = True
        elif from_address == e["party_b"]:
            e["funded_b"] = True
        else:
            return {"status": "failed", "error": "not a party"}
        self._debit(from_address, amt)
        self._credit("0xESCROW", amt)
        if e["funded_a"] and e["funded_b"]:
            e["status"] = "funded"
        return self._ok()

    def settle(self, onchain_escrow_id: int) -> dict[str, Any]:
        if not self._escrow_configured:
            return {"status": "skipped", "reason": "escrow not configured"}
        e = self.escrows.get(int(onchain_escrow_id))
        if e is None or e["status"] != "funded":
            return {"status": "failed", "error": "bad escrow state"}
        self._debit("0xESCROW", e["deposit_a"] + e["deposit_b"])
        self._credit(e["party_a"], e["deposit_a"]); self._credit(e["party_b"], e["deposit_b"])
        e["status"] = "settled"
        return self._ok()

    def slash(self, *, onchain_escrow_id: int, loser_address: str) -> dict[str, Any]:
        if not self._escrow_configured:
            return {"status": "skipped", "reason": "escrow not configured"}
        e = self.escrows.get(int(onchain_escrow_id))
        if e is None or e["status"] != "funded":
            return {"status": "failed", "error": "bad escrow state"}
        if loser_address == e["party_a"]:
            winner, loss, win_stake = e["party_b"], e["deposit_a"], e["deposit_b"]
        elif loser_address == e["party_b"]:
            winner, loss, win_stake = e["party_a"], e["deposit_b"], e["deposit_a"]
        else:
            return {"status": "failed", "error": "loser not party"}
        self._debit("0xESCROW", loss + win_stake)
        self._credit(winner, loss + win_stake)
        e["status"] = "slashed"
        return self._ok()

    def cancel(self, onchain_escrow_id: int) -> dict[str, Any]:
        return self._refund(onchain_escrow_id, "cancelled")

    def refund_expired(self, onchain_escrow_id: int) -> dict[str, Any]:
        e = self.escrows.get(int(onchain_escrow_id))
        if e is not None and e["status"] == "open" and self.now_ts < e["deadline"]:
            return {"status": "failed", "error": "not yet expired"}
        return self._refund(onchain_escrow_id, "expired")

    def _refund(self, onchain_escrow_id, new_status) -> dict[str, Any]:
        if not self._escrow_configured:
            return {"status": "skipped", "reason": "escrow not configured"}
        e = self.escrows.get(int(onchain_escrow_id))
        if e is None or e["status"] not in ("open", "funded"):
            return {"status": "failed", "error": "bad escrow state"}
        if e["funded_a"]:
            e["funded_a"] = False; self._debit("0xESCROW", e["deposit_a"]); self._credit(e["party_a"], e["deposit_a"])
        if e["funded_b"]:
            e["funded_b"] = False; self._debit("0xESCROW", e["deposit_b"]); self._credit(e["party_b"], e["deposit_b"])
        e["status"] = new_status
        return self._ok()

    def escrow_state(self, onchain_escrow_id: int) -> dict[str, Any] | None:
        e = self.escrows.get(int(onchain_escrow_id))
        return dict(e) if e else None
