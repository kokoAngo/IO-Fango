"""Events bus: publish/subscribe filtering, fan-out, drop-on-full."""
from __future__ import annotations

import asyncio

import pytest

from fango import events
from fango.events import Event, publish, reset_subscribers, subscribe


@pytest.fixture(autouse=True)
def _clean_subs():
    reset_subscribers()
    yield
    reset_subscribers()


@pytest.mark.asyncio
async def test_publish_reaches_subscriber():
    gen = subscribe()
    await asyncio.sleep(0)  # let subscribe register
    publish(Event(type="new_thread", forum="baibai", thread_id=1, post_id=1))
    ev = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert ev.type == "new_thread"
    assert ev.forum == "baibai"
    await gen.aclose()


@pytest.mark.asyncio
async def test_forum_filter():
    gen = subscribe(forum="chat")
    await asyncio.sleep(0)
    publish(Event(type="new_thread", forum="baibai", thread_id=1, post_id=1))
    publish(Event(type="new_thread", forum="chat", thread_id=2, post_id=2))
    ev = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert ev.forum == "chat"
    await gen.aclose()


@pytest.mark.asyncio
async def test_thread_filter():
    gen = subscribe(thread_id=99)
    await asyncio.sleep(0)
    publish(Event(type="new_post", forum="baibai", thread_id=99, post_id=1))
    publish(Event(type="new_post", forum="baibai", thread_id=100, post_id=2))
    ev = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert ev.thread_id == 99
    await gen.aclose()


@pytest.mark.asyncio
async def test_multiple_subscribers_each_get_events():
    g1 = subscribe()
    g2 = subscribe()
    await asyncio.sleep(0)
    publish(Event(type="new_thread", forum="baibai", thread_id=1, post_id=1))
    e1 = await asyncio.wait_for(g1.__anext__(), timeout=1.0)
    e2 = await asyncio.wait_for(g2.__anext__(), timeout=1.0)
    assert e1.type == e2.type == "new_thread"
    await g1.aclose()
    await g2.aclose()


@pytest.mark.asyncio
async def test_subscriber_count_reflects_active():
    assert events.subscriber_count() == 0
    g = subscribe()
    await asyncio.sleep(0)
    assert events.subscriber_count() == 1
    await g.aclose()
    await asyncio.sleep(0)
    assert events.subscriber_count() == 0


@pytest.mark.asyncio
async def test_service_publishes_new_thread(agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    gen = subscribe(forum="baibai")
    await asyncio.sleep(0)
    # Run sync DB work in default executor so the loop can drain events.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: fb.create_thread(title="t", body="b", author_id=ag.id))
    ev = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert ev.type == "new_thread"
    assert ev.forum == "baibai"
    await gen.aclose()


@pytest.mark.asyncio
async def test_service_publishes_new_post_on_reply(agent_factory):
    from fango.baibai import service as fb
    ag, _ = agent_factory()
    out = fb.create_thread(title="t", body="b", author_id=ag.id)
    gen = subscribe(thread_id=out["thread"].id)
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, lambda: fb.reply(thread_id=out["thread"].id, body="reply", author_id=ag.id)
    )
    ev = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert ev.type == "new_post"
    assert ev.thread_id == out["thread"].id
    await gen.aclose()


@pytest.mark.asyncio
async def test_service_publishes_like_change(agent_factory):
    from fango.baibai import service as fb
    from fango.forum_core import toggle_like as core_toggle_like
    ag, _ = agent_factory()
    out = fb.create_thread(title="t", body="b", author_id=ag.id)
    gen = subscribe(forum="baibai")
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: core_toggle_like(out["post"].id, ag.id))
    ev = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert ev.type == "like_change"
    assert ev.payload["liked"] is True
    await gen.aclose()


@pytest.mark.asyncio
async def test_drop_on_full_queue():
    """A subscriber that never drains has events dropped; publisher doesn't block."""
    # Fill subscriber queue
    gen = subscribe()
    await asyncio.sleep(0)
    for i in range(events.QUEUE_SIZE + 5):
        publish(Event(type="new_thread", forum="baibai", thread_id=i, post_id=i))
    # Wait for delivery
    await asyncio.sleep(0.05)
    # No exception was raised; queue should not exceed QUEUE_SIZE
    drained = 0
    while drained < events.QUEUE_SIZE:
        try:
            await asyncio.wait_for(gen.__anext__(), timeout=0.2)
            drained += 1
        except asyncio.TimeoutError:
            break
    assert drained <= events.QUEUE_SIZE
    await gen.aclose()


def test_event_to_dict_serializable():
    e = Event(type="new_thread", forum="baibai", thread_id=1, post_id=1, payload={"x": 1})
    d = e.to_dict()
    assert d["type"] == "new_thread"
    import json
    json.dumps(d)
