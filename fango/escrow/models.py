"""Dataclasses mirroring the escrows + escrow_events tables."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _row(d: Mapping[str, Any] | None, key: str, default: Any = None) -> Any:
    if d is None:
        return default
    try:
        return d[key]
    except (KeyError, IndexError):
        return default


@dataclass
class Escrow:
    id: int
    agreement_id: int
    content_hash: str
    token_address: str
    token_decimals: int
    party_a_agent_id: int
    party_b_agent_id: int
    party_a_address: str
    party_b_address: str
    deposit_a: str           # base-unit integer as string
    deposit_b: str
    deadline_ts: int | None
    onchain_escrow_id: int | None
    escrow_contract_addr: str | None
    chain_id: int | None
    status: str
    outcome: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Escrow":
        return cls(
            id=row["id"], agreement_id=row["agreement_id"], content_hash=row["content_hash"],
            token_address=row["token_address"], token_decimals=_row(row, "token_decimals", 18),
            party_a_agent_id=row["party_a_agent_id"], party_b_agent_id=row["party_b_agent_id"],
            party_a_address=row["party_a_address"], party_b_address=row["party_b_address"],
            deposit_a=row["deposit_a"], deposit_b=row["deposit_b"],
            deadline_ts=_row(row, "deadline_ts"), onchain_escrow_id=_row(row, "onchain_escrow_id"),
            escrow_contract_addr=_row(row, "escrow_contract_addr"), chain_id=_row(row, "chain_id"),
            status=row["status"], outcome=_row(row, "outcome"),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )
