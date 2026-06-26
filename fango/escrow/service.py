"""Open + adjudicate earnest-money escrows against finalized agreements.

Synchronous (arbiter/deal-finalization actions, not the consult hot path). Every
op is best-effort and idempotent; status tracks the on-chain escrow (chain is the
source of truth, reconciled via ``sync_state``). Degrades to off-chain-only when
the escrow isn't configured. Mirrors fango/agreements/service.py conventions.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import identity
from ..chain.client import get_chain_client
from ..config import load_chain_settings
from ..db import connect, transaction
from .models import Escrow

log = logging.getLogger(__name__)

_DEFAULT_DEADLINE_SEC = 48 * 3600
_LIVE = ("open", "funding", "funded")


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _map_status(state: dict[str, Any] | None, fallback: str) -> str:
    """Map an on-chain escrow_state() to our status vocabulary."""
    if not state:
        return fallback
    cs = state.get("status")
    if cs == "open":
        return "funding" if (state.get("funded_a") or state.get("funded_b")) else "open"
    return cs or fallback   # funded/settled/slashed/cancelled/expired


def _record_event(conn, escrow_id, kind, result, *, party_agent_id=None, amount=None):
    now = _now_iso()
    st = result.get("status")
    ev_status = {"confirmed": "confirmed", "skipped": "skipped"}.get(st, "failed")
    conn.execute(
        "INSERT INTO escrow_events(escrow_id, kind, party_agent_id, amount, chain_id,"
        " contract_addr, tx_hash, block_number, status, error, submitted_at, confirmed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (escrow_id, kind, party_agent_id, str(amount) if amount is not None else None,
         result.get("chain_id"), result.get("contract_addr"), result.get("tx_hash"),
         result.get("block_number"), ev_status,
         result.get("error") or result.get("reason"), now,
         now if ev_status == "confirmed" else None),
    )


def _refresh_status(conn, escrow_id, onchain_id, fallback) -> str:
    state = get_chain_client().escrow_state(onchain_id) if onchain_id is not None else None
    status = _map_status(state, fallback)
    conn.execute("UPDATE escrows SET status=?, updated_at=? WHERE id=?",
                 (status, _now_iso(), escrow_id))
    return status


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------

def open_escrow(agreement_id: int, *, deposit_a: int, deposit_b: int,
                token: str | None = None, deadline_ts: int | None = None,
                conn: sqlite3.Connection | None = None) -> Escrow:
    """Create an escrow for an agreement and (if configured) open it on chain.
    ``deposit_a/b`` are ERC-20 BASE UNITS (the CLI scales whole tokens). Idempotent
    per agreement while a live escrow exists. Raises ValueError on missing identity."""
    s = load_chain_settings()
    token = token or s.token_addr or "0x0"
    if deadline_ts is None:
        deadline_ts = _now_ts() + _DEFAULT_DEADLINE_SEC

    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        ag = conn.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
        if ag is None:
            raise ValueError(f"agreement {agreement_id} not found")
        addr_a = identity.address_for(ag["party_a_agent_id"], conn=conn)
        addr_b = identity.address_for(ag["party_b_agent_id"], conn=conn)
        if not addr_a or not addr_b:
            raise ValueError("both parties need a durable identity (address) before escrow")

        # Idempotent: return the live escrow for this agreement if one exists.
        existing = conn.execute(
            "SELECT * FROM escrows WHERE agreement_id = ? AND status IN ('open','funding','funded')"
            " ORDER BY id DESC LIMIT 1", (agreement_id,)
        ).fetchone()
        if existing:
            return Escrow.from_row(existing)

        try:
            with transaction(conn):
                cur = conn.execute(
                    "INSERT INTO escrows(agreement_id, content_hash, token_address, token_decimals,"
                    " party_a_agent_id, party_b_agent_id, party_a_address, party_b_address,"
                    " deposit_a, deposit_b, deadline_ts, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'open')",
                    (agreement_id, ag["content_hash"], token, s.token_decimals,
                     ag["party_a_agent_id"], ag["party_b_agent_id"], addr_a, addr_b,
                     str(int(deposit_a)), str(int(deposit_b)), deadline_ts),
                )
                escrow_id = cur.lastrowid
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM escrows WHERE agreement_id = ? AND status IN ('open','funding','funded')"
                " ORDER BY id DESC LIMIT 1", (agreement_id,)).fetchone()
            if row is None:
                raise
            return Escrow.from_row(row)

        # Open on chain (owner). Best-effort; off-chain row already persisted.
        client = get_chain_client()
        if client.escrow_configured():
            res = client.open_escrow(
                agreement_hash=ag["content_hash"], token=token,
                party_a=addr_a, party_b=addr_b,
                deposit_a=int(deposit_a), deposit_b=int(deposit_b), deadline_ts=deadline_ts)
            with transaction(conn):
                _record_event(conn, escrow_id, "open", res)
                if res.get("status") == "confirmed":
                    conn.execute(
                        "UPDATE escrows SET onchain_escrow_id=?, escrow_contract_addr=?,"
                        " chain_id=?, updated_at=? WHERE id=?",
                        (res.get("onchain_escrow_id"), res.get("contract_addr"),
                         res.get("chain_id"), _now_iso(), escrow_id))
                elif res.get("status") == "failed":
                    conn.execute("UPDATE escrows SET status='failed', updated_at=? WHERE id=?",
                                 (_now_iso(), escrow_id))
        row = conn.execute("SELECT * FROM escrows WHERE id = ?", (escrow_id,)).fetchone()
        return Escrow.from_row(row)
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# fund
# ---------------------------------------------------------------------------

def fund(escrow_id: int, which_party: str, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Stake a party's deposit (custodial approve+deposit). which_party ∈ {'a','b'}."""
    if which_party not in ("a", "b"):
        raise ValueError("which_party must be 'a' or 'b'")
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        e = conn.execute("SELECT * FROM escrows WHERE id = ?", (escrow_id,)).fetchone()
        if e is None:
            return {"status": "failed", "error": "escrow not found"}
        kind = f"deposit_{which_party}"
        already = conn.execute(
            "SELECT 1 FROM escrow_events WHERE escrow_id=? AND kind=? AND status='confirmed' LIMIT 1",
            (escrow_id, kind)).fetchone()
        if already:
            return {"status": "confirmed", "reason": "already funded"}

        client = get_chain_client()
        if not client.escrow_configured() or e["onchain_escrow_id"] is None:
            with transaction(conn):
                _record_event(conn, escrow_id, kind,
                              {"status": "skipped", "reason": "escrow not on chain"},
                              party_agent_id=e[f"party_{which_party}_agent_id"])
            return {"status": "skipped", "reason": "escrow not configured / not opened"}

        agent_id = e[f"party_{which_party}_agent_id"]
        address = e[f"party_{which_party}_address"]
        amount = int(e[f"deposit_{which_party}"])
        res = client.agent_deposit(
            agent_id=agent_id, onchain_escrow_id=e["onchain_escrow_id"],
            token=e["token_address"], escrow_addr=e["escrow_contract_addr"] or "",
            amount=amount, from_address=address)
        with transaction(conn):
            _record_event(conn, escrow_id, kind, res, party_agent_id=agent_id, amount=amount)
            _refresh_status(conn, escrow_id, e["onchain_escrow_id"], e["status"])
        return res
    finally:
        if owns:
            conn.close()


# ---------------------------------------------------------------------------
# adjudication (owner)
# ---------------------------------------------------------------------------

def _owner_op(escrow_id, kind, call, *, outcome=None, require_status=None,
              party_agent_id=None, conn=None) -> dict[str, Any]:
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        e = conn.execute("SELECT * FROM escrows WHERE id = ?", (escrow_id,)).fetchone()
        if e is None:
            return {"status": "failed", "error": "escrow not found"}
        if require_status and e["status"] != require_status:
            return {"status": "failed", "error": f"escrow not {require_status} (is {e['status']})"}
        client = get_chain_client()
        if not client.escrow_configured() or e["onchain_escrow_id"] is None:
            return {"status": "skipped", "reason": "escrow not configured / not opened"}
        res = call(client, e)
        with transaction(conn):
            _record_event(conn, escrow_id, kind, res, party_agent_id=party_agent_id)
            new_status = _refresh_status(conn, escrow_id, e["onchain_escrow_id"], e["status"])
            if res.get("status") == "confirmed" and outcome:
                conn.execute("UPDATE escrows SET outcome=?, updated_at=? WHERE id=?",
                             (outcome, _now_iso(), escrow_id))
        return res
    finally:
        if owns:
            conn.close()


def settle(escrow_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    return _owner_op(escrow_id, "settle",
                     lambda c, e: c.settle(e["onchain_escrow_id"]),
                     outcome="completed", require_status="funded", conn=conn)


def slash(escrow_id: int, loser_agent_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        e = conn.execute("SELECT * FROM escrows WHERE id = ?", (escrow_id,)).fetchone()
        if e is None:
            return {"status": "failed", "error": "escrow not found"}
        if loser_agent_id == e["party_a_agent_id"]:
            loser_addr = e["party_a_address"]
        elif loser_agent_id == e["party_b_agent_id"]:
            loser_addr = e["party_b_address"]
        else:
            return {"status": "failed", "error": "loser is not a party"}
        return _owner_op(
            escrow_id, "slash",
            lambda c, _e: c.slash(onchain_escrow_id=e["onchain_escrow_id"], loser_address=loser_addr),
            outcome=f"slashed:{loser_agent_id}", require_status="funded",
            party_agent_id=loser_agent_id, conn=conn)
    finally:
        if owns:
            conn.close()


def cancel(escrow_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    return _owner_op(escrow_id, "cancel",
                     lambda c, e: c.cancel(e["onchain_escrow_id"]), outcome="cancelled", conn=conn)


def refund_expired(escrow_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    return _owner_op(escrow_id, "refund_expired",
                     lambda c, e: c.refund_expired(e["onchain_escrow_id"]),
                     outcome="expired", conn=conn)


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def get_escrow(escrow_id: int, conn: sqlite3.Connection | None = None) -> Escrow | None:
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute("SELECT * FROM escrows WHERE id = ?", (escrow_id,)).fetchone()
        return Escrow.from_row(row) if row else None
    finally:
        if owns:
            conn.close()


def get_escrow_for_agreement(agreement_id: int, conn: sqlite3.Connection | None = None) -> Escrow | None:
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM escrows WHERE agreement_id = ? ORDER BY id DESC LIMIT 1",
            (agreement_id,)).fetchone()
        return Escrow.from_row(row) if row else None
    finally:
        if owns:
            conn.close()


def list_events(escrow_id: int, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM escrow_events WHERE escrow_id = ? ORDER BY id", (escrow_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if owns:
            conn.close()


def sync_state(escrow_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Reconcile the DB status with on-chain escrow_state (chain = source of truth)."""
    owns = conn is None
    if conn is None:
        conn = connect()
    try:
        e = conn.execute("SELECT * FROM escrows WHERE id = ?", (escrow_id,)).fetchone()
        if e is None:
            return {"found": False}
        state = get_chain_client().escrow_state(e["onchain_escrow_id"]) if e["onchain_escrow_id"] is not None else None
        status = _map_status(state, e["status"])
        if status != e["status"]:
            with transaction(conn):
                conn.execute("UPDATE escrows SET status=?, updated_at=? WHERE id=?",
                             (status, _now_iso(), escrow_id))
        return {"found": True, "status": status, "on_chain": state}
    finally:
        if owns:
            conn.close()
