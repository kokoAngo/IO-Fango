"""Canonicalization must be deterministic + reproducible — the whole anti-renege
guarantee rests on it."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fango.agreements import canonical as c

_RENTAL_TERMS = {
    "monthly_rent_yen": 120000,
    "deposit_yen": 240000,
    "key_money_yen": 120000,
    "maintenance_fee_yen": 8000,
    "contract_months": 24,
}

EXPECTED_CANONICAL = (
    '{"agreement_type":"rental","finalized_at":"2026-01-02T03:04:05.678Z",'
    '"listing_id":1,"listing_reins_id":"R-1","party_a_agent_id":2,'
    '"party_b_agent_id":3,"schema_version":"1","terms":'
    '{"contract_months":24,"deposit_yen":240000,"key_money_yen":120000,'
    '"maintenance_fee_yen":8000,"monthly_rent_yen":120000}}'
)


def _record(**over):
    kw = dict(
        agreement_type="rental", listing_id=1, listing_reins_id="R-1",
        party_a_agent_id=2, party_b_agent_id=3,
        finalized_at="2026-01-02T03:04:05.678Z", terms=dict(_RENTAL_TERMS),
    )
    kw.update(over)
    return c.build_canonical_record(**kw)


def test_canonical_string_is_pinned():
    assert c.canonical_json(_record()) == EXPECTED_CANONICAL


def test_hash_shape():
    h = c.content_hash(_record())
    assert h.startswith("0x") and len(h) == 66


def test_key_order_independence():
    # Same logical terms, different insertion order → identical bytes/hash.
    reordered = {
        "contract_months": 24, "maintenance_fee_yen": 8000, "monthly_rent_yen": 120000,
        "key_money_yen": 120000, "deposit_yen": 240000,
    }
    assert c.content_hash(_record()) == c.content_hash(_record(terms=reordered))


def test_floats_for_money_rejected():
    bad = dict(_RENTAL_TERMS, monthly_rent_yen=120000.0)
    with pytest.raises(c.CanonicalError):
        _record(terms=bad)


def test_unknown_term_rejected():
    bad = dict(_RENTAL_TERMS, sneaky_pii="田中太郎 090-1234-5678")
    with pytest.raises(c.CanonicalError):
        _record(terms=bad)


def test_optional_field_omitted_when_absent_is_stable():
    # Absent optional vs explicit None → same bytes (omitted, not null).
    with_none = dict(_RENTAL_TERMS, move_in_date=None)
    assert c.content_hash(_record()) == c.content_hash(_record(terms=with_none))


def test_optional_field_present_changes_hash():
    with_date = dict(_RENTAL_TERMS, move_in_date="2026-04-01")
    assert c.content_hash(_record(terms=with_date)) != c.content_hash(_record())


def test_japanese_survives_as_utf8():
    rec = _record(listing_reins_id="六本木ヒルズ-1203")
    assert "六本木ヒルズ" in c.canonical_json(rec)        # not \uXXXX escaped


def test_naive_datetime_rejected():
    with pytest.raises(c.CanonicalError):
        _record(finalized_at=datetime(2026, 1, 2, 3, 4, 5))   # no tzinfo


def test_aware_datetime_normalized_to_ms():
    dt = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)
    assert c.normalize_finalized_at(dt) == "2026-01-02T03:04:05.678Z"


def test_sale_record():
    rec = c.build_canonical_record(
        agreement_type="sale", listing_id=9, listing_reins_id="S-9",
        party_a_agent_id=1, party_b_agent_id=2,
        finalized_at="2026-01-02T03:04:05.678Z",
        terms={"price_yen": 85000000, "deposit_yen": 8500000},
    )
    assert rec["terms"]["price_yen"] == 85000000
    assert c.content_hash(rec).startswith("0x")
