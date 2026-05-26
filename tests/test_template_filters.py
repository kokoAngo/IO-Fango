"""Jinja filters."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fango import template_filters as tf


def test_format_yen_man():
    assert tf.format_yen_man(8500) == "8,500万円"
    assert tf.format_yen_man("8500") == "8,500万円"
    assert tf.format_yen_man(None) == "—"
    assert tf.format_yen_man("") == "—"
    assert tf.format_yen_man("not a number") == "not a number"


def test_format_walk():
    assert tf.format_walk(5) == "徒歩5分"
    assert tf.format_walk(None) == "—"
    assert tf.format_walk("abc") == "abc"


def test_format_sqm():
    assert tf.format_sqm(55.5) == "55.5㎡"
    assert tf.format_sqm("70") == "70.0㎡"
    assert tf.format_sqm(None) == "—"


def test_station_tag():
    assert tf.station_tag("六本木", "日比谷線") == "日比谷線 / 六本木"
    assert tf.station_tag("六本木", None) == "六本木"
    assert tf.station_tag(None, "日比谷線") == "—"


def test_humanize_recent():
    now = datetime.now(timezone.utc)
    assert tf.humanize_ts(now.isoformat()) == "今"


def test_humanize_minutes():
    ago = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert "分前" in tf.humanize_ts(ago.isoformat())


def test_humanize_hours():
    ago = datetime.now(timezone.utc) - timedelta(hours=3)
    assert "時間前" in tf.humanize_ts(ago.isoformat())


def test_humanize_days():
    ago = datetime.now(timezone.utc) - timedelta(days=4)
    assert "日前" in tf.humanize_ts(ago.isoformat())


def test_humanize_old_date():
    ago = datetime.now(timezone.utc) - timedelta(days=365)
    out = tf.humanize_ts(ago.isoformat())
    # falls back to absolute date
    assert "-" in out


def test_humanize_invalid():
    assert tf.humanize_ts("not-a-date") == "not-a-date"
    assert tf.humanize_ts(None) == ""


def test_forum_jp():
    assert tf.forum_jp("baibai") == "売買"
    assert tf.forum_jp("chintai") == "賃貸"
    assert tf.forum_jp("yobanashi") == "ツッコミ"
    assert tf.forum_jp("dojo") == "道場"
    assert tf.forum_jp("unknown") == "unknown"


def test_filters_registered_on_env():
    from jinja2 import Environment
    env = Environment()
    tf.register(env)
    assert "yen_man" in env.filters
    assert "humanize" in env.filters
    assert env.filters["yen_man"](100) == "100万円"
