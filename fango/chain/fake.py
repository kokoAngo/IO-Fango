"""In-memory fake chain client for tests — no node required.

Returns the same dict shapes as :class:`Web3ChainClient` so service code is
exercised identically. Install via ``set_chain_client(FakeChainClient())`` and
reset to ``None`` in teardown.
"""
from __future__ import annotations

from typing import Any


class FakeChainClient:
    def __init__(self, *, configured: bool = True, fail: bool = False):
        self._store: dict[str, dict[str, Any]] = {}
        self._configured = configured
        self._fail = fail
        self._n = 0

    def is_configured(self) -> bool:
        return self._configured

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
