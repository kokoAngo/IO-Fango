"""SSE protocol formatting for the events bus."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from .events import Event, subscribe

HEARTBEAT_SECONDS = 30


def format_sse(event: Event) -> str:
    return f"event: {event.type}\ndata: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"


async def stream_events(
    *, forum: str | None = None, thread_id: int | None = None,
) -> AsyncIterator[str]:
    """SSE chunks. Yields heartbeats every HEARTBEAT_SECONDS to keep the socket alive."""
    sub = subscribe(forum=forum, thread_id=thread_id)
    try:
        yield ": connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(sub.__anext__(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue
            except StopAsyncIteration:
                break
            yield format_sse(event)
    finally:
        await sub.aclose()
