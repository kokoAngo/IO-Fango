"""scripts/ingest_pg.py write path + scripts/purge_seed_listings.py.

The upstream Postgres is LAN-only, so the adapter is stubbed: these cover the
SQLite side (upsert keying, child replacement, skeleton filtering, the
dry-run/write split) without a database on the other end.
"""
from __future__ import annotations

import pytest

from fango.listings import service as ls
from fango.listings.ingestion.postgres import PgRecord


def _rec(reins_id: str, **over) -> PgRecord:
    payload = {
        "reins_id": reins_id,
        "building_name": f"B-{reins_id}",
        "address": "東京都港区六本木6-12-1",
        "prefecture": "東京都", "city": "港区", "ward": "港区",
        "layout": "1LDK", "rent_yen": 150_000,
        "transaction_type": "rent", "ad_status": "可",
    }
    payload.update(over)
    return PgRecord(
        payload=payload,
        transports=[{"line": "日比谷線", "station": "六本木", "walk_minutes": 3, "sort_order": 0}],
    )


class _StubAdapter:
    """Stands in for PgListingAdapter; records the kinds it was asked for."""

    def __init__(self, records, **kwargs):
        self._records = records
        self.kwargs = kwargs
        self.asked = []

    def is_configured(self):
        return True

    def iter_records(self, kinds=("rent", "sale")):
        self.asked.append(tuple(kinds))
        yield from self._records


@pytest.fixture
def stub(monkeypatch):
    """Install a stub adapter and hand back a setter for its records."""
    holder = {}

    def install(records):
        holder["adapter"] = _StubAdapter(records)
        monkeypatch.setattr(
            "scripts.ingest_pg.PgListingAdapter",
            lambda **kw: holder["adapter"],
        )
        return holder["adapter"]

    return install


def _run(argv):
    from scripts.ingest_pg import main
    return main(argv)


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------

def test_requires_exactly_one_mode(tmp_db, stub):
    stub([_rec("1")])
    assert _run([]) == 2                      # neither
    assert _run(["--dry-run", "--write"]) == 2  # both


def test_dry_run_writes_nothing(tmp_db, stub, capsys):
    stub([_rec("100000000001"), _rec("100000000002")])
    assert _run(["--dry-run", "--kind", "rent"]) == 0
    assert ls.count_listings(criteria={"include_non_advertisable": True}) == 0
    assert "[rent] 2 rows" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def test_write_inserts_listings_and_transports(tmp_db, stub, capsys):
    stub([_rec("100000000001"), _rec("100000000002")])
    assert _run(["--write", "--kind", "rent"]) == 0
    assert "written: 2" in capsys.readouterr().out

    found = ls.search_listings(criteria={"prefecture": "東京都"}, limit=10)
    assert {l.reins_id for l in found} == {"100000000001", "100000000002"}
    assert ls.get_listing_transports(found[0].id)[0]["station"] == "六本木"


def test_write_is_idempotent_on_reins_id(tmp_db, stub):
    stub([_rec("100000000001")])
    _run(["--write", "--kind", "rent"])
    stub([_rec("100000000001", building_name="RENAMED")])
    _run(["--write", "--kind", "rent"])

    rows = ls.search_listings(criteria={"prefecture": "東京都"}, limit=10)
    assert len(rows) == 1                       # upserted, not duplicated
    assert rows[0].building_name == "RENAMED"


def test_write_replaces_transports_rather_than_appending(tmp_db, stub):
    stub([_rec("100000000001")])
    _run(["--write", "--kind", "rent"])
    listing_id = ls.search_listings(criteria={"prefecture": "東京都"}, limit=1)[0].id

    moved = _rec("100000000001")
    moved.transports = [
        {"line": "大江戸線", "station": "麻布十番", "walk_minutes": 5, "sort_order": 0}
    ]
    stub([moved])
    _run(["--write", "--kind", "rent"])

    stations = [t["station"] for t in ls.get_listing_transports(listing_id)]
    assert stations == ["麻布十番"]


def test_write_skips_skeleton_rows(tmp_db, stub, capsys):
    """An upstream row with an id and nothing else must not become a blank card."""
    stub([
        _rec("100000000001"),
        PgRecord(payload={"reins_id": "100000000002", "transaction_type": "rent"}),
    ])
    assert _run(["--write", "--kind", "rent"]) == 0
    out = capsys.readouterr().out
    assert "written: 1" in out
    assert "skeleton rows" in out
    assert ls.count_listings(criteria={"include_non_advertisable": True}) == 1


def test_write_batches_across_the_flush_boundary(tmp_db, stub, monkeypatch):
    """More records than one transaction's worth still all land."""
    monkeypatch.setattr("scripts.ingest_pg.WRITE_BATCH", 3)
    stub([_rec(f"10000000000{i}") for i in range(7)])
    assert _run(["--write", "--kind", "rent"]) == 0
    assert ls.count_listings(criteria={"include_non_advertisable": True}) == 7


def test_limit_caps_the_sweep(tmp_db, stub):
    stub([_rec(f"10000000000{i}") for i in range(5)])
    _run(["--write", "--kind", "rent", "--limit", "2"])
    assert ls.count_listings(criteria={"include_non_advertisable": True}) == 2


def test_kind_is_passed_through_to_the_adapter(tmp_db, stub):
    adapter = stub([])
    _run(["--dry-run", "--kind", "sale"])
    assert adapter.asked == [("sale",)]


# ---------------------------------------------------------------------------
# Seed purge
# ---------------------------------------------------------------------------

def test_purge_dry_run_keeps_everything(tmp_db, stub, capsys):
    stub([_rec("100000000001")])
    _run(["--write", "--kind", "rent"])

    from scripts.purge_seed_listings import main as purge
    assert purge([]) == 0
    assert "dry run" in capsys.readouterr().out
    assert ls.count_listings(criteria={"include_non_advertisable": True}) == 1


def test_purge_deletes_listings_and_cascades(tmp_db, stub):
    stub([_rec("100000000001"), _rec("100000000002")])
    _run(["--write", "--kind", "rent"])

    from fango.db import connect
    from scripts.purge_seed_listings import main as purge
    assert purge(["--yes", "--no-backup"]) == 0

    assert ls.count_listings(criteria={"include_non_advertisable": True}) == 0
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM listing_transports").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM building_stats").fetchone()[0] == 0


def test_purge_keeps_listings_an_agreement_points_at(tmp_db, stub, agent_factory):
    """agreements.listing_id has no ON DELETE action — a blanket DELETE would
    raise, and the row is a concluded deal, not demo data."""
    stub([_rec("100000000001"), _rec("100000000002")])
    _run(["--write", "--kind", "rent"])
    listings = ls.search_listings(criteria={"include_non_advertisable": True}, limit=10)
    assert len(listings) == 2
    keep_id, drop_id = listings[0].id, listings[1].id

    a, _ = agent_factory("party-a")   # the factory returns (Agent, plaintext_key)
    b, _ = agent_factory("party-b")
    from fango.db import connect
    with connect() as conn:
        conn.execute(
            "INSERT INTO agreements(agreement_type, listing_id, party_a_agent_id, "
            "party_b_agent_id, terms_json, canonical_json, content_hash, finalized_at) "
            "VALUES ('rental', ?, ?, ?, '{}', '{}', '0xdeadbeef', '2026-09-04T00:00:00Z')",
            (keep_id, a.id, b.id),
        )

    from scripts.purge_seed_listings import main as purge
    assert purge(["--yes", "--no-backup"]) == 0

    survivors = [l.id for l in ls.search_listings(
        criteria={"include_non_advertisable": True}, limit=10)]
    assert survivors == [keep_id]
    assert drop_id not in survivors


# ---------------------------------------------------------------------------
# Reconcile — the pass that keeps an incremental sync from advertising
# listings upstream has since withdrawn.
# ---------------------------------------------------------------------------

class _GateAdapter(_StubAdapter):
    """Adapter whose gate_status() answers from a canned dict."""

    def __init__(self, gate):
        super().__init__([])
        self.gate = gate
        self.asked_gate = []

    def gate_status(self, kind, keys):
        self.asked_gate.append((kind, sorted(keys)))
        return {k: v for k, v in self.gate.get(kind, {}).items() if k in set(keys)}


@pytest.fixture
def gate_stub(monkeypatch):
    def install(gate):
        adapter = _GateAdapter(gate)
        monkeypatch.setattr("scripts.ingest_pg.PgListingAdapter", lambda **kw: adapter)
        return adapter
    return install


def _seed_pg_listing(reins_id, **over):
    payload = {
        "reins_id": reins_id, "building_name": reins_id, "prefecture": "東京都",
        "city": "港区", "layout": "1LDK", "rent_yen": 150_000,
        "transaction_type": "rent", "ad_status": "可", "source": "pg",
    }
    payload.update(over)
    return ls.insert_listing(payload)


def test_reconcile_withdraws_a_listing_upstream_no_longer_clears(tmp_db, gate_stub):
    keep = _seed_pg_listing("100000000001")
    pulled = _seed_pg_listing("100000000002")
    gate_stub({"rent": {
        "100000000001": {"ad_status": "可", "tenancy_status": None},
        "100000000002": {"ad_status": "不可（仲介）", "tenancy_status": None},
    }})
    assert _run(["--reconcile", "--kind", "rent"]) == 0

    assert ls.get_listing(keep.id).extra["ad_status"] == "可"
    assert ls.get_listing(pulled.id).extra["ad_status"] == "不可（仲介）"
    visible = {l.id for l in ls.search_listings(criteria={"prefecture": "東京都"}, limit=10)}
    assert visible == {keep.id}


def test_reconcile_marks_contracted_rentals(tmp_db, gate_stub):
    row = _seed_pg_listing("100000000001")
    gate_stub({"rent": {"100000000001": {"ad_status": "可", "tenancy_status": "成約済"}}})
    _run(["--reconcile", "--kind", "rent"])
    assert ls.get_listing(row.id).extra["tenancy_status"] == "成約済"


def test_reconcile_retires_listings_that_vanished_upstream(tmp_db, gate_stub, capsys):
    gone = _seed_pg_listing("100000000001")
    gate_stub({"rent": {}})            # upstream returns nothing for it
    assert _run(["--reconcile", "--kind", "rent"]) == 0
    assert ls.get_listing(gone.id).extra["ad_status"] == ls.RETIRED_AD_STATUS
    assert "retired 1" in capsys.readouterr().out
    # Retired, not deleted: agreements and broker proposals point at listings,
    # and a concluded deal must keep pointing at what it was about.
    assert ls.get_listing(gone.id) is not None


def test_reconcile_leaves_broker_created_listings_alone(tmp_db, gate_stub):
    """A broker's own listing has no upstream row. Retiring it for failing to
    match one would silently pull that broker's inventory off the site."""
    theirs = ls.insert_listing({
        "reins_id": "BROKER-XYZ", "building_name": "自社物件", "prefecture": "東京都",
        "city": "港区", "rent_yen": 90_000, "transaction_type": "rent",
        "ad_status": "可",            # source deliberately left NULL
    })
    adapter = gate_stub({"rent": {}})
    _run(["--reconcile", "--kind", "rent"])
    assert ls.get_listing(theirs.id).extra["ad_status"] == "可"
    # It is never even asked about.
    assert all("BROKER-XYZ" not in keys for _, keys in adapter.asked_gate)


def test_reconcile_is_idempotent(tmp_db, gate_stub, capsys):
    _seed_pg_listing("100000000001")
    gate_stub({"rent": {"100000000001": {"ad_status": "不可（仲介）", "tenancy_status": None}}})
    _run(["--reconcile", "--kind", "rent"])
    assert "updated 1" in capsys.readouterr().out
    _run(["--reconcile", "--kind", "rent"])
    assert "updated 0" in capsys.readouterr().out


def test_reconcile_rejects_being_combined_with_a_sync_mode(tmp_db, gate_stub):
    gate_stub({})
    assert _run(["--reconcile", "--write"]) == 2
    assert _run(["--reconcile", "--dry-run"]) == 2
