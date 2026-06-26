"""Dataclasses mirroring the agreements + agreement_anchors tables."""
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
class Agreement:
    id: int
    agreement_type: str
    listing_id: int | None
    listing_reins_id: str | None
    party_a_agent_id: int
    party_b_agent_id: int
    price_yen: int | None
    terms_json: str
    canonical_json: str
    content_hash: str
    schema_version: str
    source: str
    status: str
    finalized_at: str
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Agreement":
        return cls(
            id=row["id"],
            agreement_type=row["agreement_type"],
            listing_id=_row(row, "listing_id"),
            listing_reins_id=_row(row, "listing_reins_id"),
            party_a_agent_id=row["party_a_agent_id"],
            party_b_agent_id=row["party_b_agent_id"],
            price_yen=_row(row, "price_yen"),
            terms_json=row["terms_json"],
            canonical_json=row["canonical_json"],
            content_hash=row["content_hash"],
            schema_version=row["schema_version"],
            source=row["source"],
            status=row["status"],
            finalized_at=row["finalized_at"],
            created_at=row["created_at"],
        )


@dataclass
class AgreementAnchor:
    id: int
    agreement_id: int
    content_hash: str
    chain_id: int | None
    contract_addr: str | None
    tx_hash: str | None
    block_number: int | None
    confirmations: int
    status: str
    error: str | None
    submitted_at: str | None
    confirmed_at: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "AgreementAnchor":
        return cls(
            id=row["id"],
            agreement_id=row["agreement_id"],
            content_hash=row["content_hash"],
            chain_id=_row(row, "chain_id"),
            contract_addr=_row(row, "contract_addr"),
            tx_hash=_row(row, "tx_hash"),
            block_number=_row(row, "block_number"),
            confirmations=_row(row, "confirmations", 0),
            status=row["status"],
            error=_row(row, "error"),
            submitted_at=_row(row, "submitted_at"),
            confirmed_at=_row(row, "confirmed_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
