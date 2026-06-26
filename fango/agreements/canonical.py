"""Deterministic canonicalization + hashing of a finalized agreement.

The whole anti-reneging guarantee rests on one property: **anyone holding the
record can reproduce the exact same hash**. So serialization must be byte-stable
across machines, languages, and time. The rules below are load-bearing — do not
change them without bumping ``SCHEMA_VERSION`` (old hashes stay verifiable under
their own version; new records use the new rules).

Rules:
  * Keys sorted lexicographically at every level (``sort_keys=True``).
  * No whitespace (``separators=(",", ":")``).
  * Real UTF-8, not ``\\uXXXX`` escapes (``ensure_ascii=False``) — Japanese
    building/agent text stays as characters.
  * NaN/Infinity rejected (``allow_nan=False``) — non-portable.
  * **Money is integer yen only. Floats are forbidden** (rejected at build
    time). JSON float repr is not portable; integers are. Any future decimal
    field MUST be a fixed-precision *string*, never a float.
  * Optional fields are *omitted* when absent (never serialized as ``null``) so
    the byte stream is stable regardless of which optionals were supplied.

The hash placed on chain is ``"0x" + sha256(canonical_bytes).hexdigest()`` — the
raw 32 bytes of that digest become the contract's ``bytes32``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1"

AGREEMENT_TYPES = ("rental", "sale")

# Closed term field sets — unknown keys are rejected so no stray (possibly PII)
# data sneaks into the hashed record. ``?`` marks optional (omitted when absent).
_RENTAL_REQUIRED = ("monthly_rent_yen", "deposit_yen", "key_money_yen",
                    "maintenance_fee_yen", "contract_months")
_RENTAL_OPTIONAL = ("move_in_date",)
_SALE_REQUIRED = ("price_yen", "deposit_yen")
_SALE_OPTIONAL = ("closing_date",)

# Term fields that must be whole-yen integers.
_MONEY_FIELDS = frozenset((
    "monthly_rent_yen", "deposit_yen", "key_money_yen",
    "maintenance_fee_yen", "price_yen",
))


class CanonicalError(ValueError):
    """Raised when a record can't be canonicalized (bad type/field/value)."""


def normalize_finalized_at(value: datetime | str) -> str:
    """UTC millisecond ISO-8601 (``YYYY-MM-DDTHH:MM:SS.sssZ``).

    Deliberately millisecond precision (cross-language stable), distinct from the
    DB's ``%f`` microsecond default. Naive datetimes are rejected — an ambiguous
    instant must never be hashed.
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        raise CanonicalError("finalized_at must be a datetime or ISO string")
    if value.tzinfo is None:
        raise CanonicalError("finalized_at datetime must be timezone-aware (UTC)")
    dt = value.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _check_money(name: str, val: Any) -> int:
    # bool is an int subclass — exclude it explicitly.
    if isinstance(val, bool) or not isinstance(val, int):
        raise CanonicalError(f"{name} must be an integer number of yen (got {type(val).__name__})")
    if val < 0:
        raise CanonicalError(f"{name} must be non-negative")
    return val


def _build_terms(agreement_type: str, terms: dict[str, Any]) -> dict[str, Any]:
    if agreement_type == "rental":
        required, optional = _RENTAL_REQUIRED, _RENTAL_OPTIONAL
    else:
        required, optional = _SALE_REQUIRED, _SALE_OPTIONAL
    allowed = set(required) | set(optional)
    unknown = set(terms) - allowed
    if unknown:
        raise CanonicalError(f"unknown terms for {agreement_type}: {sorted(unknown)}")

    out: dict[str, Any] = {}
    for key in required:
        if key not in terms or terms[key] is None:
            raise CanonicalError(f"missing required term: {key}")
        out[key] = _check_money(key, terms[key]) if key in _MONEY_FIELDS \
            else _coerce_scalar(key, terms[key])
    for key in optional:
        if terms.get(key) is None:
            continue  # omit absent optionals entirely
        out[key] = _check_money(key, terms[key]) if key in _MONEY_FIELDS \
            else _coerce_scalar(key, terms[key])
    return out


def _coerce_scalar(name: str, val: Any) -> Any:
    if isinstance(val, bool):
        raise CanonicalError(f"{name}: booleans not allowed")
    if isinstance(val, float):
        raise CanonicalError(f"{name}: floats are forbidden (use int yen or a string)")
    if isinstance(val, (int, str)):
        return val
    raise CanonicalError(f"{name}: unsupported type {type(val).__name__}")


def build_canonical_record(
    *,
    agreement_type: str,
    listing_id: int,
    listing_reins_id: str,
    party_a_agent_id: int,
    party_b_agent_id: int,
    finalized_at: datetime | str,
    terms: dict[str, Any],
) -> dict[str, Any]:
    """Assemble and validate the canonical record dict. Raises CanonicalError on
    any bad field. Does not hash — see :func:`content_hash`."""
    if agreement_type not in AGREEMENT_TYPES:
        raise CanonicalError(f"agreement_type must be one of {AGREEMENT_TYPES}")
    for name, val in (("listing_id", listing_id),
                      ("party_a_agent_id", party_a_agent_id),
                      ("party_b_agent_id", party_b_agent_id)):
        if isinstance(val, bool) or not isinstance(val, int):
            raise CanonicalError(f"{name} must be an integer id")
    return {
        "schema_version": SCHEMA_VERSION,
        "agreement_type": agreement_type,
        "listing_id": listing_id,
        "listing_reins_id": str(listing_reins_id),
        "party_a_agent_id": party_a_agent_id,
        "party_b_agent_id": party_b_agent_id,
        "finalized_at": normalize_finalized_at(finalized_at),
        "terms": _build_terms(agreement_type, terms),
    }


def canonical_bytes(record: dict[str, Any]) -> bytes:
    """Deterministic UTF-8 serialization (see module docstring for the rules)."""
    text = json.dumps(
        record,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return text.encode("utf-8")


def canonical_json(record: dict[str, Any]) -> str:
    """The exact string that gets hashed (stored in agreements.canonical_json)."""
    return canonical_bytes(record).decode("utf-8")


def content_hash(record: dict[str, Any]) -> str:
    """0x-prefixed lowercase hex SHA-256 of the canonical bytes."""
    return "0x" + hashlib.sha256(canonical_bytes(record)).hexdigest()
