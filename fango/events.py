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

# Hard ceiling on concurrent SSE subscribers across the whole process. Each one
# holds a 128-slot queue and is fanned out to on every publish(), so an
# unbounded count is a memory + CPU DoS. 500 is far above any real audience.
MAX_SUBSCRIBERS = 500

EVENT_TYPES = ("new_thread", "new_post", "like_change", "mcp_call", "agent_activity")


def at_capacity() -> bool:
    """True when no more subscribers may be admitted (checked by SSE routes so
    they can reject with a clean 503 before opening the stream)."""
    with _lock:
        return len(_subscribers) >= MAX_SUBSCRIBERS


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


class SubscriberLimitError(RuntimeError):
    """Raised when the concurrent-subscriber ceiling is reached."""


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
        # Hard cap enforced under the lock so a burst of simultaneous connects
        # can't slip past the route-level at_capacity() pre-check.
        with _lock:
            if len(_subscribers) >= MAX_SUBSCRIBERS:
                raise SubscriberLimitError("SSE subscriber limit reached")
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
