"""SQLite-backed consult session state.

Sessions outlive a single MCP call: the caller passes ``session_id`` from
the previous response back in to continue a multi-turn dialogue. Sessions
hard-expire on TTL and soft-cap at ``MAX_TURNS`` (set ``state='done'``).
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from ..db import connect, transaction


def _row_get(row, key: str, default=None):
    """Safe column access for sqlite3.Row (which raises IndexError on a missing
    key). Tolerates rows from a DB that predates a migration."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


@dataclass
class ConsultSession:
    id: str
    agent_id: int | None
    started_at: str
    last_active_at: str
    expires_at: str
    total_turns: int
    total_input_tokens: int
    total_output_tokens: int
    last_criteria: dict[str, Any]
    state: str  # 'asking' | 'ready' | 'done'
    log_forum: str | None = None
    log_thread_id: int | None = None
    log_area_key: str | None = None
    post_agent_id: int | None = None

    @classmethod
    def from_row(cls, row) -> "ConsultSession":
        try:
            crit = json.loads(row["last_criteria_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            crit = {}
        return cls(
            id=row["id"],
            agent_id=row["agent_id"],
            started_at=row["started_at"],
            last_active_at=row["last_active_at"],
            expires_at=row["expires_at"],
            total_turns=row["total_turns"],
            total_input_tokens=row["total_input_tokens"],
            total_output_tokens=row["total_output_tokens"],
            last_criteria=crit,
            state=row["state"],
            log_forum=_row_get(row, "log_forum"),
            log_thread_id=_row_get(row, "log_thread_id"),
            log_area_key=_row_get(row, "log_area_key"),
            post_agent_id=_row_get(row, "post_agent_id"),
        )


@dataclass
class ConsultMessage:
    turn_index: int
    role: str  # 'user' | 'model' | 'system_note'
    content: dict[str, Any]
    created_at: str

    @classmethod
    def from_row(cls, row) -> "ConsultMessage":
        try:
            content = json.loads(row["content_json"])
        except (TypeError, json.JSONDecodeError):
            content = {"raw": row["content_json"]}
        return cls(
            turn_index=row["turn_index"],
            role=row["role"],
            content=content,
            created_at=row["created_at"],
        )


# ---------------------------------------------------------------------------
# Time helpers (centralised so tests can patch one place if needed)
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%fZ")


def _parse_iso(s: str) -> datetime:
    # SQLite default uses microsecond precision; %f handles 6-digit fraction.
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return _utc_now()


# ---------------------------------------------------------------------------
# Public CRUD
# ---------------------------------------------------------------------------

def new_session_id() -> str:
    return "cs_" + secrets.token_urlsafe(16)


def create_session(
    agent_id: int | None,
    ttl_seconds: int,
    conn: sqlite3.Connection | None = None,
) -> ConsultSession:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        sid = new_session_id()
        now = _utc_now()
        expires = now + timedelta(seconds=ttl_seconds)
        conn.execute(
            """INSERT INTO consult_sessions(
                   id, agent_id, started_at, last_active_at,
                   expires_at, last_criteria_json, state)
               VALUES (?, ?, ?, ?, ?, ?, 'asking')""",
            (sid, agent_id, _iso(now), _iso(now), _iso(expires), "{}"),
        )
        row = conn.execute(
            "SELECT * FROM consult_sessions WHERE id = ?", (sid,)
        ).fetchone()
        return ConsultSession.from_row(row)
    finally:
        if owns_conn:
            conn.close()


def get_session(
    session_id: str,
    conn: sqlite3.Connection | None = None,
) -> ConsultSession | None:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM consult_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return ConsultSession.from_row(row) if row else None
    finally:
        if owns_conn:
            conn.close()


def is_expired(sess: ConsultSession) -> bool:
    try:
        return _utc_now() >= _parse_iso(sess.expires_at)
    except Exception:
        return False


def get_messages(
    session_id: str,
    conn: sqlite3.Connection | None = None,
) -> list[ConsultMessage]:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        rows = conn.execute(
            """SELECT turn_index, role, content_json, created_at
               FROM consult_messages
               WHERE session_id = ?
               ORDER BY turn_index, id""",
            (session_id,),
        ).fetchall()
        return [ConsultMessage.from_row(r) for r in rows]
    finally:
        if owns_conn:
            conn.close()


def append_message(
    session_id: str,
    turn_index: int,
    role: str,
    content: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> None:
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        conn.execute(
            """INSERT INTO consult_messages(session_id, turn_index, role, content_json)
               VALUES (?, ?, ?, ?)""",
            (session_id, turn_index, role, json.dumps(content, ensure_ascii=False)),
        )
    finally:
        if owns_conn:
            conn.close()


def update_session_state(
    session_id: str,
    *,
    state: str | None = None,
    last_criteria: dict[str, Any] | None = None,
    increment_turn: bool = False,
    add_input_tokens: int = 0,
    add_output_tokens: int = 0,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Update mutable session fields. Always bumps last_active_at."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        sets = ["last_active_at = ?"]
        params: list[Any] = [_iso(_utc_now())]
        if state is not None:
            sets.append("state = ?")
            params.append(state)
        if last_criteria is not None:
            sets.append("last_criteria_json = ?")
            params.append(json.dumps(last_criteria, ensure_ascii=False))
        if increment_turn:
            sets.append("total_turns = total_turns + 1")
        if add_input_tokens:
            sets.append("total_input_tokens = total_input_tokens + ?")
            params.append(add_input_tokens)
        if add_output_tokens:
            sets.append("total_output_tokens = total_output_tokens + ?")
            params.append(add_output_tokens)
        params.append(session_id)
        conn.execute(
            f"UPDATE consult_sessions SET {', '.join(sets)} WHERE id = ?",
            params,
        )
    finally:
        if owns_conn:
            conn.close()


def set_log_thread(
    session_id: str,
    forum: str,
    thread_id: int,
    area_key: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Pin the auto-post mirror thread (and its settled area) for this session.

    ``area_key`` is only written when non-empty, so an early turn that opened the
    thread before the area was known doesn't clobber the area a later turn set.
    """
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        if area_key:
            conn.execute(
                "UPDATE consult_sessions SET log_forum = ?, log_thread_id = ?, "
                "log_area_key = ? WHERE id = ?",
                (forum, thread_id, area_key, session_id),
            )
        else:
            conn.execute(
                "UPDATE consult_sessions SET log_forum = ?, log_thread_id = ? WHERE id = ?",
                (forum, thread_id, session_id),
            )
    finally:
        if owns_conn:
            conn.close()


def get_or_create_post_agent_id(
    session: "ConsultSession",
    keyed_agent_id: int | None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Resolve the agent id to author this session's posts under.

    Keyed callers post under their own id (stable by key). Keyless callers get
    one anonymous agent minted per session and reused for the whole dialogue, so
    their pseudonym is stable within the session but reveals nothing.
    """
    if keyed_agent_id is not None:
        return keyed_agent_id
    if session.post_agent_id is not None:
        return session.post_agent_id

    import secrets
    from ..auth import create_agent

    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        name = "anon_" + secrets.token_urlsafe(8)
        agent, _key = create_agent(name, vendor="anon", conn=conn)
        conn.execute(
            "UPDATE consult_sessions SET post_agent_id = ? WHERE id = ?",
            (agent.id, session.id),
        )
        session.post_agent_id = agent.id
        return agent.id
    finally:
        if owns_conn:
            conn.close()


# ---------------------------------------------------------------------------
# History compaction
# ---------------------------------------------------------------------------

def maybe_compact_history(
    session_id: str,
    compact_after_turns: int,
    keep_recent_turns: int = 3,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Replace early messages with a single ``system_note`` summary stub.

    The stub is a structured dict that downstream prompt builders can render
    deterministically — we don't recurse into the LLM here (cheap, lossy).
    Returns ``True`` if compaction happened.
    """
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        msgs = get_messages(session_id, conn=conn)
        # Already has a system_note at the front? Don't re-compact.
        if msgs and msgs[0].role == "system_note":
            return False
        if len(msgs) <= compact_after_turns * 2:
            return False
        # Keep the last ``keep_recent_turns`` user+model pairs intact.
        keep_from_idx = max(0, len(msgs) - keep_recent_turns * 2)
        old = msgs[:keep_from_idx]
        # Build a compact summary: concatenate user lines + criteria seen.
        user_lines = [m.content.get("text", "") for m in old if m.role == "user"]
        criteria_seen: dict[str, Any] = {}
        for m in old:
            if m.role == "model":
                delta = m.content.get("criteria_delta") or {}
                if isinstance(delta, dict):
                    criteria_seen.update(delta)
        note_content = {
            "summary": " | ".join(s for s in user_lines if s)[:1500],
            "criteria_seen": criteria_seen,
            "compacted_turns": len(old) // 2,
        }
        with transaction(conn):
            # Strategy: delete the old rows, insert a single note at turn_index 0,
            # leave the kept rows in place.
            conn.execute(
                """DELETE FROM consult_messages
                   WHERE session_id = ? AND id IN (
                       SELECT id FROM consult_messages
                       WHERE session_id = ?
                       ORDER BY turn_index, id
                       LIMIT ?
                   )""",
                (session_id, session_id, len(old)),
            )
            conn.execute(
                """INSERT INTO consult_messages(session_id, turn_index, role, content_json)
                   VALUES (?, ?, 'system_note', ?)""",
                (session_id, -1, json.dumps(note_content, ensure_ascii=False)),
            )
        return True
    finally:
        if owns_conn:
            conn.close()
