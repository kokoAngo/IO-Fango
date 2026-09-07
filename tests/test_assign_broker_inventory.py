"""scripts/assign_broker_inventory: which listings may be handed to a broker.

The pool this script draws from is a fourth expression of the public gate, and
the first three all went stale at some point. Assigning a listing the public
surface will not show is worse than a no-op: the broker then proposes it to
customers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fango.brokers import service as bsvc
from fango.listings import service as ls
from scripts.assign_broker_inventory import main as assign


def _ago(days: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _rent(name, ward="新宿区", **over):
    payload = {
        "reins_id": name, "building_name": name, "prefecture": "東京都",
        "ward": ward, "layout": "1LDK", "rent_yen": 150_000, "ad_status": "可",
        "transaction_type": "rent", "source": "pg", "posted_at": _ago(1),
    }
    payload.update(over)
    return ls.insert_listing(payload)


def _owned_names(broker_id):
    return {l.building_name for l in bsvc.list_listings(broker_id, limit=100)}


def test_assigns_only_listings_the_public_gate_would_show(tmp_db):
    broker, _ = bsvc.create_broker("b1", "テスト不動産")
    good = _rent("GOOD")
    _rent("LET", tenancy_status="成約済")       # already let
    _rent("EXPIRED", posted_at=_ago(ls.RENTAL_VISIBLE_DAYS + 2))
    _rent("UNDATED", posted_at=None)            # synced, unknown age
    _rent("REFUSED", ad_status="不可（仲介）")

    assert assign(["--broker-id", str(broker.id), "--per-ward", "10"]) == 0

    assert _owned_names(broker.id) == {"GOOD"}
    assert ls.get_listing(good.id).extra["ad_status"] == "可"  # never rewritten


def test_force_ad_status_still_refuses_let_and_expired_rows(tmp_db):
    """--force-ad-status is about ad clearance on synthetic inventory. It is not
    a licence to advertise a flat that is gone or a listing that has aged out."""
    broker, _ = bsvc.create_broker("b2", "テスト不動産")
    _rent("HELD", ad_status="確認待ち")          # force makes this one eligible
    _rent("LET", tenancy_status="成約済")
    _rent("EXPIRED", posted_at=_ago(ls.RENTAL_VISIBLE_DAYS + 2))

    assert assign(["--broker-id", str(broker.id), "--per-ward", "10",
                   "--force-ad-status"]) == 0
    assert _owned_names(broker.id) == {"HELD"}


def test_local_inventory_never_ages_out_of_the_pool(tmp_db):
    """A broker's own market has no upstream clock, so an old locally-created
    row stays assignable."""
    broker, _ = bsvc.create_broker("b3", "テスト不動産")
    _rent("OLDLOCAL", source=None, posted_at=None)
    assert assign(["--broker-id", str(broker.id), "--per-ward", "10"]) == 0
    assert _owned_names(broker.id) == {"OLDLOCAL"}
