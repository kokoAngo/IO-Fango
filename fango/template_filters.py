"""Jinja2 filters: time, price, station tags."""
from __future__ import annotations

from datetime import datetime, timezone


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
