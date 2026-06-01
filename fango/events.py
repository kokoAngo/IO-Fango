"""In-process pub/sub for the event firehose.

Publishers call ``publish(event)`` from sync code (service layer).
Consumers (SSE endpoints, tests) ``async for ev in subscribe(...)``.

Slow consumers whose queues fill up just drop events — never block the publisher.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

QUEUE_SIZE = 128

EVENT_TYPES = ("new_thread", "new_post", "like_change", "mcp_call", "agent_activity")


@dataclass
class Event:
    type: str
    forum: str | None = None
    thread_id: int | None = None
    post_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Subscriber:
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop
    forum: Optional[str] = None
    thread_id: Optional[int] = None
    dropped: int = 0


_subscribers: list[_Subscriber] = []
_lock = threading.Lock()


def _matches(sub: _Subscriber, event: Event) -> bool:
    if sub.forum is not None and event.forum != sub.forum:
        return False
    if sub.thread_id is not None and event.thread_id != sub.thread_id:
        return False
    return True


def publish(event: Event) -> None:
    """Fan-out to all matching subscribers. Non-blocking; drops on full queue."""
    with _lock:
        subs = list(_subscribers)
    for sub in subs:
        if not _matches(sub, event):
            continue
        try:
            sub.loop.call_soon_threadsafe(_put_or_drop, sub, event)
        except RuntimeError:
            # event loop is closed; subscriber is stale, skip.
            pass


def _put_or_drop(sub: _Subscriber, event: Event) -> None:
    try:
        sub.queue.put_nowait(event)
    except asyncio.QueueFull:
        sub.dropped += 1


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)


def reset_subscribers() -> None:
    """Test-only: drop all subscribers (e.g. between tests)."""
    with _lock:
        _subscribers.clear()


class Subscription:
    """Eagerly registered subscriber. Use as ``async for ev in sub:``."""

    def __init__(self, *, forum: str | None = None, thread_id: int | None = None):
        loop = asyncio.get_running_loop()
        self._sub = _Subscriber(
            queue=asyncio.Queue(maxsize=QUEUE_SIZE),
            loop=loop,
            forum=forum,
            thread_id=thread_id,
        )
        with _lock:
            _subscribers.append(self._sub)
        self._closed = False

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Event:
        if self._closed:
            raise StopAsyncIteration
        return await self._sub.queue.get()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with _lock:
            try:
                _subscribers.remove(self._sub)
            except ValueError:
                pass

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()


def subscribe(*, forum: str | None = None, thread_id: int | None = None) -> Subscription:
    """Register a subscriber synchronously and return an async-iterable handle."""
    return Subscription(forum=forum, thread_id=thread_id)
