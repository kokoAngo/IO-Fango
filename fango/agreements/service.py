"""Create finalized agreements and anchor their hash on chain.

``create_agreement`` is synchronous and always succeeds off-chain (it never
touches the chain). Anchoring runs best-effort: either fire-and-forget in a
background single-worker pool (mirrors ``autopost._spawn_enrich``) or inline via
``anchor_now`` (used by the CLI and tests). A chain outage can never break
agreement creation.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from ..chain.client import get_chain_client
from ..db import connect, transaction
from . import canonical
from .models import Agreement

log = logging.getLogger(__name__)

# Single worker so anchoring tx nonces are naturally serialized; queue cap so a
# burst can't pile up unbounded background work (anchor failures are recorded and
# retryable via anchor_now). Mirrors autopost._spawn_enrich.
_ANCHOR_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="anchor")
_ANCHOR_MAX_QUEUED = 32
_anchor_lock = threading.Lock()
_anchor_pending = 0


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _price_yen(agreement_type: str, terms: dict[str, Any]) -> int | None:
    if agreement_type == "rental":
        return terms.get("monthly_rent_yen")
    return terms.get("price_yen")


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def create_agreement(
    *,
    agreement_type: str,
    listing_id: int,
    listing_reins_id: str,
    party_a_agent_id: int,
    party_b_agent_id: int,
    terms: dict[str, Any],
    finalized_at: datetime | str | None = None,
    source: str = "service",
    auto_anchor: bool = True,
    conn: sqlite3.Connection | None = None,
) -> Agreement:
    """Build + persist the canonical agreement record (synchronous, off-chain).

    Idempotent: a second call producing the same canonical bytes returns the
    existing row (no second anchor). If ``auto_anchor`` and the chain is
    configured, a background anchoring attempt is spawned.
    """
    if finalized_at is None:
        finalized_at = datetime.now(timezone.utc)
    record = canonical.build_canonical_record(
        agreement_type=agreement_type,
        listing_id=listing_id,
        listing_reins_id=listing_reins_id,
        party_a_agent_id=party_a_agent_id,
        party_b_agent_id=party_b_agent_id,
        finalized_at=finalized_at,
        terms=terms,
    )
    chash = canonical.content_hash(record)
    cjson = canonical.canonical_json(record)
    terms_built = record["terms"]

    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        existing = conn.execute(
            "SELECT * FROM agreements WHERE content_hash = ?", (chash,)
        ).fetchone()
        if existing is not None:
            return Agreement.from_row(existing)   # idempotent — no re-anchor
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO agreements"
                "(agreement_type, listing_id, listing_reins_id, party_a_agent_id,"
                " party_b_agent_id, price_yen, terms_json, canonical_json, content_hash,"
                " schema_version, source, status, finalized_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unanchored', ?)",
                (agreement_type, listing_id, listing_reins_id, party_a_agent_id,
                 party_b_agent_id, _price_yen(agreement_type, terms_built),
                 json.dumps(terms_built, ensure_ascii=False), cjson, chash,
                 record["schema_version"], source, record["finalized_at"]),
            )
            agreement_id = cur.lastrowid
        row = conn.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
        agreement = Agreement.from_row(row)
    except sqlite3.IntegrityError:
        # Could be the UNIQUE(content_hash) race (a concurrent insert of the same
        # canonical bytes) — idempotent, return the winner. Anything else (e.g. a
        # FOREIGN KEY violation from a bad agent/listing id) has no such row, so
        # re-raise it instead of crashing on from_row(None).
        row = conn.execute("SELECT * FROM agreements WHERE content_hash = ?", (chash,)).fetchone()
        if row is None:
            raise
        return Agreement.from_row(row)
    finally:
        if owns_conn:
            conn.close()

    if auto_anchor and get_chain_client().is_configured():
        _spawn_anchor(agreement.id)
    return agreement


# ---------------------------------------------------------------------------
# anchoring
# ---------------------------------------------------------------------------

def _has_confirmed_anchor(conn: sqlite3.Connection, agreement_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agreement_anchors WHERE agreement_id = ? AND status = 'confirmed' LIMIT 1",
        (agreement_id,),
    ).fetchone()
    return row is not None


def _do_anchor(agreement_id: int, conn: sqlite3.Connection) -> dict[str, Any]:
    """Submit one anchoring attempt for an agreement. Records an attempt row and
    updates the agreement status. Returns the client result dict. Never raises."""
    client = get_chain_client()
    if not client.is_configured():
        return {"status": "skipped", "reason": "chain not configured"}

    ag = conn.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
    if ag is None:
        return {"status": "failed", "error": f"agreement {agreement_id} not found"}
    if _has_confirmed_anchor(conn, agreement_id):
        return {"status": "confirmed", "reason": "already anchored"}

    now = _now_iso()
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO agreement_anchors"
            "(agreement_id, content_hash, status, submitted_at) VALUES (?, ?, 'pending', ?)",
            (agreement_id, ag["content_hash"], now),
        )
        anchor_id = cur.lastrowid
        conn.execute("UPDATE agreements SET status = 'pending' WHERE id = ?", (agreement_id,))

    metadata = {
        "listing_id": ag["listing_id"] or 0,
        "party_a": ag["party_a_agent_id"],
        "party_b": ag["party_b_agent_id"],
    }
    result = client.anchor(ag["content_hash"], metadata)
    _apply_anchor_result(conn, agreement_id, anchor_id, result)
    return result


def _apply_anchor_result(conn, agreement_id, anchor_id, result: dict[str, Any]) -> None:
    status = result.get("status")
    now = _now_iso()
    with transaction(conn):
        if status == "confirmed":
            conn.execute(
                "UPDATE agreement_anchors SET status='confirmed', tx_hash=?, block_number=?,"
                " chain_id=?, contract_addr=?, confirmations=1, confirmed_at=?, error=NULL,"
                " updated_at=? WHERE id=?",
                (result.get("tx_hash"), result.get("block_number"), result.get("chain_id"),
                 result.get("contract_addr"), now, now, anchor_id),
            )
            conn.execute("UPDATE agreements SET status='anchored' WHERE id=?", (agreement_id,))
        else:
            conn.execute(
                "UPDATE agreement_anchors SET status='failed', tx_hash=?, chain_id=?,"
                " contract_addr=?, error=?, updated_at=? WHERE id=?",
                (result.get("tx_hash"), result.get("chain_id"), result.get("contract_addr"),
                 result.get("error") or result.get("reason") or "unknown error", now, anchor_id),
            )
            conn.execute("UPDATE agreements SET status='failed' WHERE id=?", (agreement_id,))


def anchor_now(agreement_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Anchor synchronously (CLI/tests). Returns the client result dict. Never raises."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        return _do_anchor(agreement_id, conn)
    except Exception as exc:  # pragma: no cover - defensive; client already swallows
        log.warning("anchor_now failed for agreement %s: %s", agreement_id, exc)
        return {"status": "failed", "error": str(exc)}
    finally:
        if owns_conn:
            conn.close()


def _spawn_anchor(agreement_id: int) -> None:
    """Fire-and-forget background anchor. Best-effort; failures are recorded in
    agreement_anchors and retryable via anchor_now."""
    global _anchor_pending
    with _anchor_lock:
        if _anchor_pending >= _ANCHOR_MAX_QUEUED:
            log.debug("anchor queue full (%d); deferring agreement %s", _anchor_pending, agreement_id)
            return
        _anchor_pending += 1

    def _run():
        global _anchor_pending
        try:
            anchor_now(agreement_id)        # opens its own connection
        except Exception as exc:            # pragma: no cover - best effort
            log.warning("background anchor failed (agreement %s): %s", agreement_id, exc)
        finally:
            with _anchor_lock:
                _anchor_pending -= 1

    _ANCHOR_POOL.submit(_run)


# ---------------------------------------------------------------------------
# read / verify
# ---------------------------------------------------------------------------

def get_agreement(agreement_id: int, conn: sqlite3.Connection | None = None) -> Agreement | None:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
        return Agreement.from_row(row) if row else None
    finally:
        if owns_conn:
            conn.close()


def latest_anchor(agreement_id: int, conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM agreement_anchors WHERE agreement_id = ? ORDER BY id DESC LIMIT 1",
        (agreement_id,),
    ).fetchone()


def list_recent_agreements(limit: int = 50, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Recent agreements with their latest confirmed anchor (tx/block/chain),
    newest first — the explorer's list view. Terms are NOT included (private)."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            "SELECT a.id, a.agreement_type, a.status, a.content_hash, a.listing_id,"
            " a.party_a_agent_id, a.party_b_agent_id, a.finalized_at, a.created_at,"
            " (SELECT tx_hash FROM agreement_anchors x WHERE x.agreement_id=a.id"
            "    AND x.status='confirmed' ORDER BY x.id DESC LIMIT 1) AS tx_hash,"
            " (SELECT block_number FROM agreement_anchors x WHERE x.agreement_id=a.id"
            "    AND x.status='confirmed' ORDER BY x.id DESC LIMIT 1) AS block_number,"
            " (SELECT chain_id FROM agreement_anchors x WHERE x.agreement_id=a.id"
            "    AND x.status='confirmed' ORDER BY x.id DESC LIMIT 1) AS chain_id"
            " FROM agreements a ORDER BY a.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def count_agreements(conn: sqlite3.Connection | None = None) -> dict[str, int]:
    """Totals for the explorer header: total + anchored."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM agreements").fetchone()[0]
        anchored = conn.execute("SELECT COUNT(*) FROM agreements WHERE status='anchored'").fetchone()[0]
        return {"total": total, "anchored": anchored}
    finally:
        if owns_conn:
            conn.close()


def find_agreement_id(query: str, conn: sqlite3.Connection | None = None) -> int | None:
    """Resolve a free-text explorer search to an agreement id. Accepts a numeric
    id, a content hash (0x… 64-hex), or a tx hash."""
    q = (query or "").strip()
    if not q:
        return None
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        if q.isdigit():
            row = conn.execute("SELECT id FROM agreements WHERE id = ?", (int(q),)).fetchone()
            if row:
                return row["id"]
        ql = q.lower()
        h = ql if ql.startswith("0x") else "0x" + ql
        # content hash (66 chars incl 0x)
        if len(h) == 66:
            row = conn.execute("SELECT id FROM agreements WHERE lower(content_hash) = ?", (h,)).fetchone()
            if row:
                return row["id"]
        # tx hash — stored with or without 0x; match both
        bare = ql[2:] if ql.startswith("0x") else ql
        row = conn.execute(
            "SELECT agreement_id FROM agreement_anchors"
            " WHERE lower(tx_hash) = ? OR lower(tx_hash) = ? ORDER BY id DESC LIMIT 1",
            (bare, "0x" + bare),
        ).fetchone()
        if row:
            return row["agreement_id"]
        return None
    finally:
        if owns_conn:
            conn.close()


def verify_agreement(agreement_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Recompute the hash from the stored canonical_json (tamper check), then
    check the chain. Returns {found, record_ok, on_chain, content_hash, tx_hash,
    block_number, status}."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
        if row is None:
            return {"found": False, "record_ok": False, "on_chain": False}
        # Recompute the digest of the exact stored canonical string.
        import hashlib
        recomputed = "0x" + hashlib.sha256(row["canonical_json"].encode("utf-8")).hexdigest()
        record_ok = (recomputed == row["content_hash"])

        anchor = latest_anchor(agreement_id, conn)
        on_chain_rec = get_chain_client().verify(row["content_hash"])
        return {
            "found": True,
            "record_ok": record_ok,
            "recomputed_hash": recomputed,
            "content_hash": row["content_hash"],
            "status": row["status"],
            "on_chain": on_chain_rec is not None,
            "on_chain_record": on_chain_rec,
            "tx_hash": anchor["tx_hash"] if anchor else None,
            "block_number": anchor["block_number"] if anchor else None,
        }
    finally:
        if owns_conn:
            conn.close()
