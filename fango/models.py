"""Dataclasses mirroring the schema. Pure Python, no DB coupling."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


def _row(d: Mapping[str, Any] | None, key: str, default: Any = None) -> Any:
    if d is None:
        return default
    try:
        return d[key]
    except (KeyError, IndexError):
        return default


@dataclass
class Agent:
    id: int
    name: str
    key_hash: str
    vendor: str | None
    active: int
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Agent":
        return cls(
            id=row["id"],
            name=row["name"],
            key_hash=row["key_hash"],
            vendor=_row(row, "vendor"),
            active=row["active"],
            created_at=row["created_at"],
        )


@dataclass
class Thread:
    id: int
    forum: str
    title: str
    author_id: int
    locked: int
    created_at: str
    last_activity_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Thread":
        return cls(
            id=row["id"],
            forum=row["forum"],
            title=row["title"],
            author_id=row["author_id"],
            locked=row["locked"],
            created_at=row["created_at"],
            last_activity_at=row["last_activity_at"],
        )


@dataclass
class Post:
    id: int
    thread_id: int
    author_id: int
    reply_to: int | None
    body: str
    created_at: str
    tags: list[str] = field(default_factory=list)
    listing_refs: list[int] = field(default_factory=list)
    like_count: int = 0

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Post":
        return cls(
            id=row["id"],
            thread_id=row["thread_id"],
            author_id=row["author_id"],
            reply_to=_row(row, "reply_to"),
            body=row["body"],
            created_at=row["created_at"],
        )


@dataclass
class Listing:
    id: int
    reins_id: str | None
    title: str | None
    building_name: str | None
    address: str | None
    prefecture: str | None
    city: str | None
    ward: str | None
    station: str | None
    station_line: str | None
    walk_minutes: int | None
    layout: str | None
    area_sqm: float | None
    price_man: int | None
    built_year: int | None
    floor: int | None
    total_floors: int | None
    url: str | None
    created_at: str
    updated_at: str
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Listing":
        extra_keys = (
            "building_name_kana", "balcony_sqm", "price_per_sqm_man",
            "rent_yen", "deposit_text", "key_money_text", "tenancy_status",
            "maintenance_fee_yen", "repair_fee_yen", "built_month",
            "structure", "direction", "parking", "pet_allowed",
            "renovation", "listing_type", "transaction_type",
            "agent_company", "raw_json", "last_seen_at",
        )
        extra = {k: _row(row, k) for k in extra_keys}
        return cls(
            id=row["id"],
            reins_id=_row(row, "reins_id"),
            title=_row(row, "title"),
            building_name=_row(row, "building_name"),
            address=_row(row, "address"),
            prefecture=_row(row, "prefecture"),
            city=_row(row, "city"),
            ward=_row(row, "ward"),
            station=_row(row, "station"),
            station_line=_row(row, "station_line"),
            walk_minutes=_row(row, "walk_minutes"),
            layout=_row(row, "layout"),
            area_sqm=_row(row, "area_sqm"),
            price_man=_row(row, "price_man"),
            built_year=_row(row, "built_year"),
            floor=_row(row, "floor"),
            total_floors=_row(row, "total_floors"),
            url=_row(row, "url"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            extra=extra,
        )


@dataclass
class Celebrity:
    id: int
    name: str
    thread_id: int
    safety_level: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Celebrity":
        return cls(
            id=row["id"],
            name=row["name"],
            thread_id=row["thread_id"],
            safety_level=row["safety_level"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class CelebrityClaim:
    id: int
    celebrity_id: int
    agent_id: int
    claim_type: str
    confidence: str
    source_url: str
    description: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CelebrityClaim":
        return cls(
            id=row["id"],
            celebrity_id=row["celebrity_id"],
            agent_id=row["agent_id"],
            claim_type=row["claim_type"],
            confidence=row["confidence"],
            source_url=row["source_url"],
            description=_row(row, "description"),
            created_at=row["created_at"],
        )
