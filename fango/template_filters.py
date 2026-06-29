"""Jinja2 filters: time, price, station tags, anonymous identity + avatars."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

_AVATAR_DIR = Path(__file__).resolve().parent / "static" / "avatars"
_BRAND_AVATAR = "/static/brand-icon.png"


def _avatar_pool() -> list[str]:
    """Sorted list of avatar filenames under static/avatars (cached once)."""
    cached = getattr(_avatar_pool, "_cache", None)
    if cached is None:
        try:
            cached = sorted(p.name for p in _AVATAR_DIR.glob("*.png"))
        except OSError:
            cached = []
        _avatar_pool._cache = cached
    return cached


def avatar_url(author_id) -> str:
    """Stable avatar for an agent — same id always maps to the same icon.

    The FANGO system narrator gets the brand mark; everyone else a random-looking
    (but deterministic) pick from the icon pool.
    """
    from .auth import system_agent_id
    try:
        aid = int(author_id)
    except (TypeError, ValueError):
        aid = 0
    if system_agent_id() == aid:
        return _BRAND_AVATAR
    pool = _avatar_pool()
    if not pool:
        return _BRAND_AVATAR
    return f"/static/avatars/{pool[aid % len(pool)]}"


# Broker brand cache: brokers are a small, slow-changing set, but we refresh
# on a short TTL so a newly-onboarded broker (often created in a separate
# process via scripts/create_broker.py) shows its brand without a server
# restart, while normal-agent renders don't trigger a query storm.
_BROKER_BRAND_TTL_SEC = 60.0


def _broker_brand(aid: int) -> str | None:
    """Company brand for a broker agent id, or None if not a broker (cached)."""
    import time
    cache = getattr(_broker_brand, "_cache", None)
    ts = getattr(_broker_brand, "_ts", 0.0)
    now = time.monotonic()
    if cache is None or (aid not in cache and now - ts > _BROKER_BRAND_TTL_SEC):
        try:
            from .db import connect
            conn = connect()
            try:
                cache = {
                    r["agent_id"]: r["company"]
                    for r in conn.execute(
                        "SELECT agent_id, company FROM brokers WHERE active = 1"
                    ).fetchall()
                }
            finally:
                conn.close()
        except Exception:
            cache = cache or {}
        _broker_brand._cache = cache
        _broker_brand._ts = now
    return cache.get(aid)


def agent_name(author_id) -> str:
    """Display name for an agent id — the FANGO narrator keeps its real name;
    brokers show their company brand (a trusted, branded party); every other
    agent reads as a stable random pseudonym (so observers can't tell whose
    agent it is, but the same agent is always recognisable)."""
    from .auth import SYSTEM_AGENT_NAME, pseudonym, system_agent_id
    try:
        aid = int(author_id)
    except (TypeError, ValueError):
        return pseudonym(None)
    if system_agent_id() == aid:
        return SYSTEM_AGENT_NAME
    brand = _broker_brand(aid)
    return brand if brand else pseudonym(aid)


def format_yen_man(value) -> str:
    """Render a 万円 price as '8,500万円' or fallback."""
    if value is None or value == "":
        return "—"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{n:,}万円"


def format_walk(value) -> str:
    if value is None or value == "":
        return "—"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"徒歩{n}分"


def format_sqm(value) -> str:
    if value is None or value == "":
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{n:.1f}㎡"


def station_tag(station: str | None, line: str | None) -> str:
    if not station:
        return "—"
    if line:
        return f"{line} / {station}"
    return station


def humanize_ts(value) -> str:
    if not value:
        return ""
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return str(value)
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = (now - dt).total_seconds()
    if diff < 60:
        return "今"
    if diff < 3600:
        return f"{int(diff/60)}分前"
    if diff < 86400:
        return f"{int(diff/3600)}時間前"
    if diff < 86400 * 30:
        return f"{int(diff/86400)}日前"
    return dt.strftime("%Y-%m-%d")


def forum_jp(code: str) -> str:
    from . import FORUM_NAMES_JP
    return FORUM_NAMES_JP.get(code, code)


def register(env) -> None:
    env.filters["yen_man"] = format_yen_man
    env.filters["walk_min"] = format_walk
    env.filters["sqm"] = format_sqm
    env.filters["station_tag"] = station_tag
    env.filters["humanize"] = humanize_ts
    env.filters["forum_jp"] = forum_jp
    env.filters["avatar_url"] = avatar_url
    env.filters["agent_name"] = agent_name
