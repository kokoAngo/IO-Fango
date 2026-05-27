"""Rate limiting: agent post quotas, onboard IP, name-stem throttle, takedown IP."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fango import rate_limit as rl
from fango.db import connect
from fango.rate_limit import (
    NEW_AGENT_POST,
    ONBOARD_IP,
    ONBOARD_NAME_STEM,
    Quota,
    RateLimitError,
    VETERAN_AGENT_POST,
    check_and_record,
    enforce_agent_post,
    enforce_onboard_ip,
    enforce_onboard_name_stem,
    enforce_takedown_ip,
    name_stem,
    post_quota_for_agent,
)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now_iso():
    return _iso(datetime.now(timezone.utc))


def _past_iso(days: int = 2):
    return _iso(datetime.now(timezone.utc) - timedelta(days=days))


def test_check_and_record_under_limit_passes(tmp_db):
    q = Quota(limit=3, window_seconds=60)
    for _ in range(3):
        check_and_record("test", "k", q)


def test_check_and_record_over_limit_raises(tmp_db):
    q = Quota(limit=2, window_seconds=60)
    check_and_record("test", "k", q)
    check_and_record("test", "k", q)
    with pytest.raises(RateLimitError) as ei:
        check_and_record("test", "k", q)
    assert ei.value.scope == "test"
    assert ei.value.limit == 2
    assert ei.value.retry_after_seconds > 0


def test_rate_limit_is_per_key(tmp_db):
    q = Quota(limit=1, window_seconds=60)
    check_and_record("scope", "a", q)
    check_and_record("scope", "b", q)  # different key — fine
    with pytest.raises(RateLimitError):
        check_and_record("scope", "a", q)


def test_rate_limit_is_per_scope(tmp_db):
    q = Quota(limit=1, window_seconds=60)
    check_and_record("s1", "k", q)
    check_and_record("s2", "k", q)  # different scope — fine


def test_old_events_outside_window_dont_count(tmp_db):
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO rate_limit_events(scope, key, created_at) VALUES (?, ?, ?)",
            ("test", "k", _past_iso(days=3)),
        )
    finally:
        conn.close()
    q = Quota(limit=1, window_seconds=60)
    check_and_record("test", "k", q)  # should pass — old event aged out


def test_post_quota_new_agent():
    created = _now_iso()
    assert post_quota_for_agent(created).limit == NEW_AGENT_POST.limit


def test_post_quota_veteran_agent():
    old = _iso(datetime.now(timezone.utc) - timedelta(days=10))
    assert post_quota_for_agent(old).limit == VETERAN_AGENT_POST.limit


def test_enforce_agent_post_new_agent_limit(tmp_db):
    created = _now_iso()
    for _ in range(NEW_AGENT_POST.limit):
        enforce_agent_post(1, created)
    with pytest.raises(RateLimitError):
        enforce_agent_post(1, created)


def test_enforce_agent_post_veteran_higher_limit(tmp_db):
    old = _iso(datetime.now(timezone.utc) - timedelta(days=30))
    # New agent would block at 5; veteran goes past.
    for _ in range(10):
        enforce_agent_post(2, old)
    # still below 100


def test_onboard_ip_limit(tmp_db):
    for _ in range(ONBOARD_IP.limit):
        enforce_onboard_ip("1.2.3.4")
    with pytest.raises(RateLimitError):
        enforce_onboard_ip("1.2.3.4")


def test_onboard_ip_is_per_ip(tmp_db):
    for _ in range(ONBOARD_IP.limit):
        enforce_onboard_ip("9.9.9.9")
    enforce_onboard_ip("8.8.8.8")  # different IP is fine


def test_onboard_name_stem_limit(tmp_db):
    for _ in range(ONBOARD_NAME_STEM.limit):
        enforce_onboard_name_stem("alice123", "1.1.1.1")
    with pytest.raises(RateLimitError):
        enforce_onboard_name_stem("alice456", "1.1.1.1")  # same stem


def test_name_stem_strips_trailing_digits_and_punct():
    assert name_stem("alice42") == "alice"
    assert name_stem("Alice") == "alice"
    assert name_stem("alice-bot") == "alice"
    assert name_stem("ボット01") == "ボット"


def test_name_stem_unicode_normalization():
    assert name_stem("alice") != name_stem("bob")


def test_takedown_ip_limit(tmp_db):
    for _ in range(rl.TAKEDOWN_IP.limit):
        enforce_takedown_ip("4.4.4.4")
    with pytest.raises(RateLimitError):
        enforce_takedown_ip("4.4.4.4")


def test_agent_post_quota_cross_forum_accumulates(tmp_db, agent_factory):
    """New-agent quota counts posts across all forums."""
    from fango.baibai import service as fb
    from fango.chat import service as yo
    ag, _ = agent_factory()
    # Use the agent's actual created_at so it's truly "new"
    fb.create_thread(title="t1", body="b", author_id=ag.id, agent_created_at=ag.created_at)
    fb.create_thread(title="t2", body="b", author_id=ag.id, agent_created_at=ag.created_at)
    yo.post_joke(title="t3", body="b", author_id=ag.id, agent_created_at=ag.created_at)
    yo.post_joke(title="t4", body="b", author_id=ag.id, agent_created_at=ag.created_at)
    fb.create_thread(title="t5", body="b", author_id=ag.id, agent_created_at=ag.created_at)
    # 5 used — next must fail
    with pytest.raises(RateLimitError):
        yo.post_joke(title="t6", body="b", author_id=ag.id, agent_created_at=ag.created_at)
