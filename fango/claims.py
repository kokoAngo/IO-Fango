"""Human-mediated onboarding handshake.

Flow:
1. Human passes CAPTCHA + submits desired (name, vendor)
2. Server mints a one-time code → returns it on-screen
3. Human copies a snippet (containing the code) to their agent
4. Agent POSTs /api/agent/redeem with the code
5. Server creates the actual agent record + returns plaintext key

Codes are 12-char base32 (formatted XXXX-XXXX-XXXX), single-use, 15-minute TTL.
"""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .auth import create_agent
from .db import connect, transaction
from .models import Agent

CODE_TTL_SECONDS = 15 * 60          # 15 minutes
CODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # base32 minus 0,1,I,O


class ClaimError(Exception):
    """Raised on invalid / expired / used code or invalid input."""


@dataclass
class Claim:
    code: str
    name: str
    vendor: str | None
    created_at: str
    redeemed_at: str | None
    agent_id: int | None
    company: str | None = None
    areas_json: str | None = None
    license_no: str | None = None


_CLAIM_FIELDS = ("code", "name", "vendor", "created_at", "redeemed_at",
                 "agent_id", "company", "areas_json", "license_no")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def generate_code() -> str:
    """12-char base32, formatted as XXXX-XXXX-XXXX for readability."""
    raw = "".join(secrets.choice(CODE_CHARS) for _ in range(12))
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def normalize_code(code: str) -> str:
    """Strip dashes/spaces, uppercase. Tolerant of how user pasted it."""
    return "".join(c for c in (code or "").upper() if c in CODE_CHARS or c in "-")


def create_claim(
    *, name: str, vendor: str | None, ip: str | None = None,
    company: str | None = None, areas: list[str] | None = None,
    license_no: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> Claim:
    """Reserve a (name, vendor) pair behind a one-time code.

    For broker self-onboarding (``vendor='broker'``) the broker profile fields
    (company/areas/license) are captured here and applied at redeem time.
    """
    import json as _json
    name = (name or "").strip()
    if not name:
        raise ClaimError("name required")
    if len(name) > 40:
        raise ClaimError("name too long (max 40 chars)")
    if vendor:
        vendor = vendor.strip()[:40]

    if vendor == "broker":
        company = (company or "").strip()
        if not company:
            raise ClaimError("company (商号) required for a broker")
        if len(company) > 80:
            raise ClaimError("company too long (max 80 chars)")
    areas_json = _json.dumps([a.strip() for a in (areas or []) if a.strip()],
                             ensure_ascii=False) if areas else None
    license_no = (license_no or "").strip()[:60] or None

    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        # Defensive: ensure code is unique (collisions ~impossible at 12 base32 chars)
        for _ in range(5):
            code = generate_code()
            try:
                conn.execute(
                    """INSERT INTO agent_claims(code, name, vendor, created_ip,
                                                company, areas_json, license_no)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (code, name, vendor, ip, company, areas_json, license_no),
                )
                break
            except sqlite3.IntegrityError:
                continue
        else:
            raise ClaimError("code generator collision (try again)")

        row = conn.execute(
            "SELECT * FROM agent_claims WHERE code = ?", (code,)
        ).fetchone()
        return Claim(**{k: row[k] for k in _CLAIM_FIELDS})
    finally:
        if owns_conn:
            conn.close()


def redeem_claim(
    code: str, conn: sqlite3.Connection | None = None,
) -> tuple[Agent, str]:
    """Validate code + mint real agent. Returns (Agent, plaintext_key).

    Raises ClaimError if code is missing, malformed, expired, or already used.
    """
    code = normalize_code(code)
    if not code or len(code.replace("-", "")) != 12:
        raise ClaimError("malformed code")

    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        with transaction(conn):
            row = conn.execute(
                "SELECT * FROM agent_claims WHERE code = ?", (code,)
            ).fetchone()
            if row is None:
                raise ClaimError("unknown code")
            if row["redeemed_at"] is not None:
                raise ClaimError("code already redeemed")

            try:
                created = datetime.fromisoformat(
                    row["created_at"].replace("Z", "+00:00")
                )
            except ValueError:
                raise ClaimError("malformed claim record")
            if (_now() - created).total_seconds() > CODE_TTL_SECONDS:
                raise ClaimError("code expired")

            # Mint the real agent. Brokers go through create_broker, which also
            # writes the broker profile row and provisions a FIXED on-chain
            # identity. Normal agents get a plain record + best-effort identity.
            if row["vendor"] == "broker" and (row["company"] or "").strip():
                import json as _json
                from .brokers.service import create_broker
                try:
                    areas = _json.loads(row["areas_json"] or "[]")
                except (ValueError, TypeError):
                    areas = []
                agent, key = create_broker(
                    row["name"], row["company"], areas=areas,
                    license_no=row["license_no"], conn=conn,
                )
            else:
                agent, key = create_agent(
                    name=row["name"], vendor=row["vendor"], conn=conn,
                )
                # Durable secp256k1 identity for this keyed agent (best-effort;
                # no-op when identity libs/secret are absent). Same conn ⇒ part
                # of this transaction, committed by the `with` block.
                try:
                    from .identity import provision_server_identity
                    provision_server_identity(agent.id, conn=conn)
                except Exception:  # pragma: no cover - identity must not block redeem
                    pass
            conn.execute(
                """UPDATE agent_claims SET redeemed_at = ?, agent_id = ?
                   WHERE code = ?""",
                (_iso(_now()), agent.id, code),
            )
            return agent, key
    finally:
        if owns_conn:
            conn.close()


def get_claim(code: str, conn: sqlite3.Connection | None = None) -> Optional[Claim]:
    code = normalize_code(code)
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM agent_claims WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            return None
        return Claim(**{k: row[k] for k in _CLAIM_FIELDS})
    finally:
        if owns_conn:
            conn.close()
