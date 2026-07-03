"""Broker term proposals → finalized, on-chain-anchored agreements.

The deal-formation bridge between a broker↔customer conversation and an
anchored ``agreements`` record:

1. A broker, on an inquiry routed to it, proposes structured terms for one of
   its listings (:func:`create_proposal`). Terms are validated against the
   canonical agreement schema up front.
2. The customer accepts (:func:`accept_proposal`), which creates the agreement
   (broker = party A / fixed identity, customer = party B / random identity) and
   anchors it synchronously. The agreement is the source of truth from there on;
   this table just links the inquiry → proposal → agreement.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..db import connect, transaction
from . import inquiries as _inq


class ProposalError(Exception):
    """Raised on invalid proposal create/accept (ownership, state, terms)."""


def create_proposal(
    broker_agent_id: int,
    inquiry_id: int,
    listing_id: int,
    agreement_type: str,
    terms: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Create a term proposal for an inquiry routed to this broker.

    Validates: the inquiry is routed to the broker, the listing is the broker's,
    and ``terms`` satisfy the canonical schema (integer yen, required fields).
    Returns ``{proposal_id, thread_id, forum, customer_agent_id}``.
    """
    from ..agreements import canonical
    from ..listings import service as ls

    if agreement_type not in ("rental", "sale"):
        raise ProposalError("agreement_type must be 'rental' or 'sale'")

    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        routed = _inq.get_routed_inquiry(inquiry_id, broker_agent_id, conn=conn)
        if routed is None:
            raise ProposalError(f"inquiry {inquiry_id} is not routed to this broker")
        listing = ls.get_listing(listing_id, conn=conn)
        if listing is None:
            raise ProposalError(f"listing {listing_id} not found")
        owner = conn.execute(
            "SELECT broker_agent_id FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        if owner is None or owner["broker_agent_id"] != broker_agent_id:
            raise ProposalError(f"listing {listing_id} is not owned by this broker")

        # Validate the terms now (raises canonical.CanonicalError on bad fields)
        # so a broker learns immediately, not at accept time. We don't persist the
        # canonical record here — accept rebuilds it as the source of truth.
        try:
            canonical.build_canonical_record(
                agreement_type=agreement_type,
                listing_id=listing_id,
                listing_reins_id=listing.reins_id or "",
                party_a_agent_id=broker_agent_id,
                party_b_agent_id=routed["customer_agent_id"],
                finalized_at=datetime.now(timezone.utc),
                terms=terms,
            )
        except canonical.CanonicalError as exc:
            raise ProposalError(f"invalid terms: {exc}") from exc

        cur = conn.execute(
            """INSERT INTO broker_proposals
               (inquiry_id, broker_agent_id, customer_agent_id, listing_id,
                agreement_type, terms_json, thread_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (inquiry_id, broker_agent_id, routed["customer_agent_id"], listing_id,
             agreement_type, json.dumps(terms, ensure_ascii=False), routed.get("thread_id")),
        )
        return {
            "proposal_id": int(cur.lastrowid),
            "thread_id": routed.get("thread_id"),
            "forum": routed.get("forum"),
            "customer_agent_id": routed["customer_agent_id"],
        }
    finally:
        if owns_conn:
            conn.close()


def get_proposal(proposal_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM broker_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        if owns_conn:
            conn.close()


def list_for_customer(
    customer_agent_id: int,
    status: str = "proposed",
    limit: int = 50,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Proposals addressed to this customer (default: still awaiting acceptance).

    The customer-facing counterpart of the broker's inquiry mailbox: a keyed
    agent (or a keyless consult caller) lists proposals it can act on and passes
    ``proposal_id`` straight to :func:`accept_proposal` — no thread scraping.
    """
    from . import service as bsvc
    from ..listings import service as ls

    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        sql = ("SELECT id AS proposal_id, inquiry_id, broker_agent_id, listing_id, "
               "agreement_type, terms_json, thread_id, status, created_at "
               "FROM broker_proposals WHERE customer_agent_id = ?")
        params: list[Any] = [customer_agent_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        out: list[dict[str, Any]] = []
        for r in conn.execute(sql, params).fetchall():
            try:
                terms = json.loads(r["terms_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                terms = {}
            listing = ls.get_listing(r["listing_id"], conn=conn)
            broker = bsvc.get_broker(r["broker_agent_id"], conn=conn)
            out.append({
                "proposal_id": r["proposal_id"],
                "status": r["status"],
                "agreement_type": r["agreement_type"],
                "terms": terms,
                "thread_id": r["thread_id"],
                "listing_id": r["listing_id"],
                "building_name": listing.building_name if listing else None,
                "broker_company": broker["company"] if broker else None,
            })
        return out
    finally:
        if owns_conn:
            conn.close()


def accept_proposal(
    proposal_id: int,
    customer_agent_id: int,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Customer accepts a proposal → create + anchor the agreement.

    Verifies the caller is the proposal's customer and the proposal is still open.
    Returns ``{agreement, anchor}`` where anchor is the chain client result dict
    (``status`` confirmed/failed/skipped). Raises :class:`ProposalError`.
    """
    from ..agreements import service as agsvc
    from ..listings import service as ls

    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        p = conn.execute(
            "SELECT * FROM broker_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if p is None:
            raise ProposalError(f"proposal {proposal_id} not found")
        if p["customer_agent_id"] != customer_agent_id:
            raise ProposalError("only the inquiring customer can accept this proposal")
        if p["status"] != "proposed":
            raise ProposalError(f"proposal already {p['status']}")

        listing = ls.get_listing(p["listing_id"], conn=conn)
        reins_id = (listing.reins_id if listing else "") or ""
        terms = json.loads(p["terms_json"])

        # Off-chain create, then anchor synchronously so the caller sees the final
        # state (auto_anchor=False avoids a duplicate background submission).
        agreement = agsvc.create_agreement(
            agreement_type=p["agreement_type"],
            listing_id=p["listing_id"],
            listing_reins_id=reins_id,
            party_a_agent_id=p["broker_agent_id"],   # fixed identity
            party_b_agent_id=customer_agent_id,       # random ephemeral identity
            terms=terms,
            source="negotiation",
            auto_anchor=False,
            conn=conn,
        )
        anchor = agsvc.anchor_now(agreement.id, conn=conn)

        with transaction(conn):
            conn.execute(
                "UPDATE broker_proposals SET status='accepted', agreement_id=?, "
                "accepted_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                (agreement.id, proposal_id),
            )
            conn.execute(
                "UPDATE broker_inquiries SET status='closed' WHERE id=?",
                (p["inquiry_id"],),
            )
        # Re-read for the post-anchor status.
        agreement = agsvc.get_agreement(agreement.id, conn=conn)
        return {"agreement": agreement, "anchor": anchor}
    finally:
        if owns_conn:
            conn.close()
