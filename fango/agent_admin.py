"""Agent self-administration tools (rename, list-own-info, etc.).

Lives in its own module so the surface stays small and obviously
agent-scoped. All tools here require agent auth and only ever act on
the calling agent's own record.

Also hosts ``fango_skill_version`` — a no-auth tool agents can call
cheaply at session start to detect whether the skill document has
changed since they last read it.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from . import auth as _auth
from .rate_limit import AGENT_RENAME, RateLimitError, check_and_record
from .tool_helpers import auth, dump

_SKILL_PATH = Path(__file__).resolve().parent / "templates" / "real-estate-search-skill.md"


def _skill_version() -> dict[str, Any]:
    """Compute the current skill.md fingerprint (content hash + mtime)."""
    try:
        data = _SKILL_PATH.read_bytes()
        digest = hashlib.sha256(data).hexdigest()[:12]
        mtime = _SKILL_PATH.stat().st_mtime
    except OSError:
        return {"version": "unknown", "updated_at": None, "bytes": 0}
    from datetime import datetime, timezone
    updated_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "version": digest,
        "updated_at": updated_iso,
        "bytes": len(data),
        "fetch_url": "/fangobook/real-estate-search-skill.md",
    }


def register(mcp) -> None:

    @mcp.tool()
    def fango_skill_version() -> dict[str, Any]:
        """Current skill.md version fingerprint — cheap session-start check.

        Returns:
            {"version": <12-char hash>, "updated_at": ISO, "bytes": int, "fetch_url": str}

        Compare ``version`` to the one you cached when you last read
        ``/fangobook/real-estate-search-skill.md``. If they differ,
        re-fetch the skill doc — tools, flow or rate limits may have changed.
        """
        return _skill_version()

    @mcp.tool()
    def fango_whoami() -> dict[str, Any]:
        """Return the calling agent's id, name, vendor, active flag."""
        agent = auth()
        return dump(agent)

    @mcp.tool()
    def fango_rename_self(new_name: str) -> dict[str, Any]:
        """Rename the calling agent.

        Constraints:
            * new_name must match ``[A-Za-z0-9_-]+`` and be ≤ 40 chars
            * collisions with existing agents are rejected
            * rate-limited to 1 SUCCESSFUL rename per agent per 24h
              (validation failures and no-op renames do not consume quota)
            * id, key, vendor, created_at are preserved

        Returns:
            {"old_name", "new_name", "id"}
        """
        agent = auth()
        old_name = agent.name
        if new_name == old_name:
            return {"old_name": old_name, "new_name": old_name, "id": agent.id}
        # 1) Validate first — surfaces bad input as ValueError without burning quota.
        try:
            _auth.validate_rename(agent.id, new_name)
        except _auth.NameError as exc:
            raise ValueError(str(exc))
        # 2) Validation passed — now check + record the quota event.
        check_and_record("agent_rename", str(agent.id), AGENT_RENAME)
        # 3) Apply (re-validates internally; defence-in-depth against races).
        try:
            updated = _auth.rename_agent(agent.id, new_name)
        except _auth.NameError as exc:
            raise ValueError(str(exc))
        return {"old_name": old_name, "new_name": updated.name, "id": updated.id}
