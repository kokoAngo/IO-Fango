"""Broker entity: onboarding, profile, identity, and own-inventory CRUD.

A broker is an ``agents`` row with ``vendor='broker'`` plus a 1:1 ``brokers``
profile row. Brokers get a *fixed* durable on-chain identity at creation
(:func:`fango.identity.provision_server_identity`), in contrast to customer
agents which get a fresh random one per session. Inventory is partitioned by
``listings.broker_agent_id``; every CRUD path here is scoped to one broker so a
broker can only ever touch its own rows.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..auth import create_agent
from ..db import connect, transaction
from ..models import Agent

BROKER_VENDOR = "broker"

# Singleton broker that owns central / ingested 在庫 (the rows that predate any
# real broker). Mirrors auth.get_or_create_system_agent.
HOUSE_BROKER_NAME = "FANGO在庫"
HOUSE_BROKER_COMPANY = "FANGO"
_house_broker_id: int | None = None


class BrokerError(Exception):
    """Raised on broker onboarding / scoping errors."""


# ---------------------------------------------------------------------------
# Onboarding + profile
# ---------------------------------------------------------------------------

def create_broker(
    name: str,
    company: str,
    *,
    areas: list[str] | None = None,
    license_no: str | None = None,
    bio: str | None = None,
    contact: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[Agent, str]:
    """Create a broker agent + profile and provision its fixed on-chain identity.

    Returns ``(Agent, plaintext_key)`` — the key is shown once to the operator,
    then only its hash is stored (same contract as :func:`auth.create_agent`).
    """
    if not (company or "").strip():
        raise BrokerError("company (商号) required")
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        with transaction(conn):
            agent, key = create_agent(name, vendor=BROKER_VENDOR, conn=conn)
            conn.execute(
                """INSERT INTO brokers(agent_id, company, license_no, areas_json, bio, contact)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    agent.id,
                    company.strip(),
                    (license_no or None),
                    json.dumps(areas or [], ensure_ascii=False),
                    (bio or None),
                    (contact or None),
                ),
            )
        # Fixed durable identity — best-effort, no-op without libs/secret.
        from ..identity import provision_server_identity
        provision_server_identity(agent.id, conn=conn)
        return agent, key
    finally:
        if owns_conn:
            conn.close()


def get_broker(agent_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    """Return the broker profile dict for ``agent_id`` or None if not a broker."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT agent_id, company, license_no, areas_json, bio, contact, active, created_at"
            " FROM brokers WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "agent_id": row["agent_id"],
            "company": row["company"],
            "license_no": row["license_no"],
            "areas": json.loads(row["areas_json"] or "[]"),
            "bio": row["bio"],
            "contact": row["contact"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
        }
    finally:
        if owns_conn:
            conn.close()


def is_broker(agent_id: int, conn: sqlite3.Connection | None = None) -> bool:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM brokers WHERE agent_id = ? AND active = 1", (agent_id,)
        ).fetchone()
        return row is not None
    finally:
        if owns_conn:
            conn.close()


def list_brokers(active_only: bool = True, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        sql = ("SELECT agent_id, company, license_no, areas_json, bio, contact, active, created_at"
               " FROM brokers")
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY created_at"
        rows = conn.execute(sql).fetchall()
        return [
            {
                "agent_id": r["agent_id"],
                "company": r["company"],
                "license_no": r["license_no"],
                "areas": json.loads(r["areas_json"] or "[]"),
                "bio": r["bio"],
                "contact": r["contact"],
                "active": bool(r["active"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        if owns_conn:
            conn.close()


def get_or_create_house_broker(conn: sqlite3.Connection | None = None) -> Agent:
    """Return the singleton house broker (owns central/ingested inventory).

    Created once; the plaintext key is discarded (this identity is never
    authenticated as a caller, like the system narrator).
    """
    global _house_broker_id
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        if _house_broker_id is not None:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (_house_broker_id,)).fetchone()
            if row is not None:
                return Agent.from_row(row)
        row = conn.execute("SELECT * FROM agents WHERE name = ?", (HOUSE_BROKER_NAME,)).fetchone()
        if row is not None:
            agent = Agent.from_row(row)
        else:
            try:
                agent, _key = create_broker(HOUSE_BROKER_NAME, HOUSE_BROKER_COMPANY, conn=conn)
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT * FROM agents WHERE name = ?", (HOUSE_BROKER_NAME,)
                ).fetchone()
                if row is None:
                    raise
                agent = Agent.from_row(row)
        _house_broker_id = agent.id
        return agent
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Own-inventory CRUD (always scoped to one broker)
# ---------------------------------------------------------------------------

def list_listings(
    broker_agent_id: int,
    limit: int = 50,
    offset: int = 0,
    conn: sqlite3.Connection | None = None,
) -> list:
    """A broker's own inventory, including non-advertisable (draft/held) rows."""
    from ..listings import service as svc
    return svc.search_listings(
        criteria={"broker_agent_id": broker_agent_id, "include_non_advertisable": True},
        limit=limit,
        offset=offset,
        conn=conn,
    )


def upsert_listing(
    broker_agent_id: int,
    payload: dict[str, Any],
    listing_id: int | None = None,
    conn: sqlite3.Connection | None = None,
):
    """Insert (listing_id is None) or update one of the broker's own listings.

    On insert the row is stamped with ``broker_agent_id``. On update the row
    must already belong to this broker (ownership re-checked in the WHERE
    clause) — otherwise :class:`BrokerError`. After the write the new/changed
    listing is fed to saved-search matching so customer subscriptions fire.
    """
    from ..listings import service as svc
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        data = dict(payload or {})
        data["broker_agent_id"] = broker_agent_id  # always self-owned
        if listing_id is None:
            listing = svc.insert_listing(data, conn=conn)
        else:
            owner = conn.execute(
                "SELECT broker_agent_id FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
            if owner is None:
                raise BrokerError(f"listing {listing_id} not found")
            if owner["broker_agent_id"] != broker_agent_id:
                raise BrokerError(f"listing {listing_id} is not owned by this broker")
            cols = [c for c in svc.LISTING_COLUMNS if c in data]
            if cols:
                assigns = ",".join(f"{c} = ?" for c in cols)
                conn.execute(
                    f"UPDATE listings SET {assigns}, "
                    f"updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    f"WHERE id = ? AND broker_agent_id = ?",
                    [data[c] for c in cols] + [listing_id, broker_agent_id],
                )
            listing = svc.get_listing(listing_id, conn=conn)
        # Fire customer saved-search notifications for the touched row.
        try:
            from ..listings.saved_search import match_new_listings
            if listing is not None:
                match_new_listings([listing.id], conn=conn)
        except Exception:
            pass
        return listing
    finally:
        if owns_conn:
            conn.close()


def remove_listing(
    broker_agent_id: int,
    listing_id: int,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Withdraw a broker's listing from the public surface (soft-delete).

    Sets ad_status to a withheld value rather than DELETEing, so any thread that
    already attached the listing doesn't dangle. Scoped to the owning broker.
    """
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        cur = conn.execute(
            "UPDATE listings SET ad_status = '不可（取り下げ）', "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND broker_agent_id = ?",
            (listing_id, broker_agent_id),
        )
        return cur.rowcount > 0
    finally:
        if owns_conn:
            conn.close()
