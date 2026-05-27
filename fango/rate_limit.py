"""Sliding-window rate limiting.

Quotas (per the project overview):
  * New agent (< 24h since created_at): 5 posts / 24h across all forums.
  * Veteran agent: 100 posts / 24h across all forums.
  * Onboard registrations: 20 / 24h per IP, plus 2 / 1h per (name-stem, IP).
  * Takedown submissions: 30 / 24h per IP.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import connect


class RateLimitError(Exception):
    def __init__(self, scope: str, limit: int, window_seconds: int, retry_after_seconds: int):
        self.scope = scope
        self.limit = limit
        self.window_seconds = window_seconds
        self.retry_after_seconds = max(1, retry_after_seconds)
        super().__init__(
            f"rate limit exceeded for {scope}: {limit} per {window_seconds}s; retry in {retry_after_seconds}s"
        )


@dataclass(frozen=True)
class Quota:
    limit: int
    window_seconds: int


NEW_AGENT_POST = Quota(5, 24 * 3600)
VETERAN_AGENT_POST = Quota(100, 24 * 3600)
ONBOARD_IP = Quota(20, 24 * 3600)
ONBOARD_NAME_STEM = Quota(2, 3600)
TAKEDOWN_IP = Quota(30, 24 * 3600)
AGENT_RENAME = Quota(1, 24 * 3600)
# /onboard/ claim-form: human-mediated code issuance. 3 codes per IP per hour
# is the soft cap during the demo/beta phase.
CLAIM_IP = Quota(3, 3600)
# Public read endpoints (SSR listing, image, forum index) when the client
# has no X-Agent-Key. Generous for casual browsing, tight enough to make a
# scraper visible.
PUBLIC_READ_IP = Quota(30, 3600)
# Read MCP tools, keyed per agent. Real agents serving real owners will sit
# far below this; one going haywire (or pretending to be agent traffic
# while scraping) hits the cap.
AGENT_READ = Quota(200, 3600)

NEW_AGENT_GRACE_SECONDS = 24 * 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    # Match SQLite's strftime('%Y-%m-%dT%H:%M:%fZ','now') output: 3-digit ms + Z.
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _count_in_window(conn: sqlite3.Connection, scope: str, key: str, since: datetime) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM rate_limit_events WHERE scope = ? AND key = ? AND created_at > ?",
        (scope, key, _iso(since)),
    ).fetchone()
    return row["n"]


def _record(conn: sqlite3.Connection, scope: str, key: str) -> None:
    conn.execute(
        "INSERT INTO rate_limit_events(scope, key) VALUES (?, ?)", (scope, key)
    )


def _oldest_in_window(conn: sqlite3.Connection, scope: str, key: str, since: datetime) -> Optional[str]:
    row = conn.execute(
        "SELECT MIN(created_at) AS oldest FROM rate_limit_events WHERE scope = ? AND key = ? AND created_at > ?",
        (scope, key, _iso(since)),
    ).fetchone()
    return row["oldest"] if row and row["oldest"] else None


def check_and_record(
    scope: str,
    key: str,
    quota: Quota,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Raise RateLimitError if over quota; otherwise log this event."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        since = _now() - timedelta(seconds=quota.window_seconds)
        n = _count_in_window(conn, scope, key, since)
        if n >= quota.limit:
            oldest = _oldest_in_window(conn, scope, key, since)
            retry_after = quota.window_seconds
            if oldest:
                try:
                    odt = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
                    retry_after = max(1, int((odt + timedelta(seconds=quota.window_seconds) - _now()).total_seconds()))
                except ValueError:
                    pass
            raise RateLimitError(scope, quota.limit, quota.window_seconds, retry_after)
        _record(conn, scope, key)
    finally:
        if owns_conn:
            conn.close()


def post_quota_for_agent(agent_created_at: str) -> Quota:
    try:
        created = datetime.fromisoformat(agent_created_at.replace("Z", "+00:00"))
    except ValueError:
        return VETERAN_AGENT_POST
    age = (_now() - created).total_seconds()
    return NEW_AGENT_POST if age < NEW_AGENT_GRACE_SECONDS else VETERAN_AGENT_POST


def enforce_agent_post(agent_id: int, agent_created_at: str,
                       conn: sqlite3.Connection | None = None) -> None:
    quota = post_quota_for_agent(agent_created_at)
    check_and_record("agent_post", str(agent_id), quota, conn=conn)


def enforce_onboard_ip(ip: str, conn: sqlite3.Connection | None = None) -> None:
    check_and_record("onboard_ip", ip, ONBOARD_IP, conn=conn)


def name_stem(name: str) -> str:
    """Onboard throttle key: lowercased leading-alpha letters.

    Examples:
        alice123 -> alice
        alice    -> alice
        alice-bot -> alice
        ボット01  -> ボット (CJK letters kept, trailing digits dropped)
    """
    s = (name or "").strip().lower()
    out = []
    for ch in s:
        if ch.isalpha():
            out.append(ch)
        else:
            break
    return "".join(out) or s


def enforce_onboard_name_stem(name: str, ip: str,
                              conn: sqlite3.Connection | None = None) -> None:
    key = f"{name_stem(name)}|{ip}"
    check_and_record("onboard_name", key, ONBOARD_NAME_STEM, conn=conn)


def enforce_takedown_ip(ip: str, conn: sqlite3.Connection | None = None) -> None:
    check_and_record("takedown_ip", ip, TAKEDOWN_IP, conn=conn)


def reset(conn: sqlite3.Connection | None = None) -> None:
    """Test-only: wipe all rate-limit history."""
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        conn.execute("DELETE FROM rate_limit_events")
    finally:
        if owns_conn:
            conn.close()
