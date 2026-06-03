"""HOMES lookup OOM guards: the global browser-concurrency cap and the bounded
enrich pool. These bound how many headless Chrome processes can exist at once."""
from __future__ import annotations

import threading

from fango.listings import external_lookup as el


def test_lookup_concurrency_env(monkeypatch):
    monkeypatch.setenv("FANGO_LOOKUP_CONCURRENCY", "3")
    assert el._lookup_concurrency() == 3
    monkeypatch.setenv("FANGO_LOOKUP_CONCURRENCY", "garbage")
    assert el._lookup_concurrency() == 1  # falls back, never < 1
    monkeypatch.setenv("FANGO_LOOKUP_CONCURRENCY", "0")
    assert el._lookup_concurrency() == 1


def test_run_isolated_runs_when_slot_free(monkeypatch):
    # Fresh semaphore so background enrich jobs in the full suite can't contend.
    monkeypatch.setattr(el, "_BROWSER_SEM", threading.BoundedSemaphore(1))
    monkeypatch.setattr(el, "_SLOT_WAIT_SECONDS", 0.5)
    assert el._run_isolated(lambda: 42, timeout=2.0) == 42


def test_run_isolated_skips_when_at_capacity(monkeypatch):
    # Exhaust the single browser slot, then a new lookup must skip (return None)
    # rather than launch another browser.
    full = threading.BoundedSemaphore(1)
    assert full.acquire(timeout=1.0)
    monkeypatch.setattr(el, "_BROWSER_SEM", full)
    monkeypatch.setattr(el, "_SLOT_WAIT_SECONDS", 0.2)
    ran = []
    out = el._run_isolated(lambda: ran.append(1), timeout=2.0)
    assert out is None
    assert ran == []  # the browser fn never executed


def test_run_isolated_releases_slot_after_use(monkeypatch):
    # After a normal run the slot is returned, so back-to-back lookups both work.
    monkeypatch.setattr(el, "_BROWSER_SEM", threading.BoundedSemaphore(1))
    monkeypatch.setattr(el, "_SLOT_WAIT_SECONDS", 0.5)
    assert el._run_isolated(lambda: "a", timeout=2.0) == "a"
    assert el._run_isolated(lambda: "b", timeout=2.0) == "b"
