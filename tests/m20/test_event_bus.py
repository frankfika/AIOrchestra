"""M20 — EventBus + SSE stream tests.

The bus is the in-process pub/sub that powers the
``GET /tasks/{task_run_id}/events/stream`` endpoint.
A partner's SSE subscription reads the per-task history
first, then live events, then a close sentinel when the
Coordinator reaches a terminal state.

The tests below cover the bus primitive (publish,
subscribe, close, replay) and the SDK's :class:`EventStream`
(which is what a partner actually calls).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request, Response

from orchestra.streaming import EventBus


# ---------------------------------------------------------------------------
# EventBus primitive
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_publish_then_subscribe_replays_history():
    """A subscriber joining after some events were
    published still sees the timeline from the start."""
    bus = EventBus()
    bus.publish("tA", {"kind": "task.received", "payload": {"k": 1}})
    bus.publish("tA", {"kind": "plan.created", "payload": {"k": 2}})
    # History snapshot.
    hist = bus.replay("tA")
    assert len(hist) == 2
    assert hist[0]["kind"] == "task.received"
    assert hist[1]["kind"] == "plan.created"


def test_subscribe_streams_live_events():
    """After the history replay, live events arrive in
    order. A subscriber exits the read loop when the
    bus emits the close sentinel."""
    bus = EventBus()
    bus.publish("tA", {"kind": "task.received"})

    async def consume():
        q = await bus.subscribe("tA")
        out = []
        while True:
            ev = await asyncio.wait_for(q.get(), timeout=1.0)
            if ev is None:
                break
            out.append(ev)
        return out

    async def driver():
        await asyncio.sleep(0.05)
        bus.publish("tA", {"kind": "plan.created"})
        bus.publish("tA", {"kind": "node.started"})
        await asyncio.sleep(0.05)
        bus.close("tA")

    async def main():
        driver_task = asyncio.create_task(driver())
        out = await consume()
        await driver_task
        return out

    out = asyncio.run(main())
    kinds = [e["kind"] for e in out]
    assert "plan.created" in kinds
    assert "node.started" in kinds


def test_subscribe_on_closed_bus_returns_history_then_sentinel():
    """A partner who subscribes to an already-terminal
    task sees the full history and then the close
    sentinel — they don't hang waiting for live events."""
    bus = EventBus()
    bus.publish("tA", {"kind": "task.received"})
    bus.publish("tA", {"kind": "task.completed"})
    bus.close("tA")

    async def consume():
        q = await bus.subscribe("tA")
        out = []
        while True:
            ev = await asyncio.wait_for(q.get(), timeout=1.0)
            if ev is None:
                break
            out.append(ev)
        return out

    out = asyncio.run(consume())
    assert len(out) == 2
    assert out[0]["kind"] == "task.received"
    assert out[1]["kind"] == "task.completed"


def test_unsubscribe_stops_live_events():
    """An unsubscribed subscriber doesn't see new
    publishes (the bus pushes to a set, not a queue
    snapshot)."""
    bus = EventBus()

    async def main():
        q = await bus.subscribe("tA")
        bus.unsubscribe("tA", q)
        bus.publish("tA", {"kind": "node.started"})
        # The queue was pre-filled with history (empty
        # here) and then unsubscribed; no new events
        # should arrive. Wait briefly to confirm.
        try:
            ev = await asyncio.wait_for(q.get(), timeout=0.2)
            # If we get here, the bus leaked an event.
            assert False, f"unexpected event: {ev}"
        except asyncio.TimeoutError:
            pass

    asyncio.run(main())


def test_bus_isolates_tasks():
    """Events for task A don't leak into task B's
    subscription."""
    bus = EventBus()
    bus.publish("tA", {"kind": "task.received", "id": "a-1"})
    bus.publish("tB", {"kind": "task.received", "id": "b-1"})

    async def consume(task):
        q = await bus.subscribe(task)
        out = []
        while True:
            ev = await asyncio.wait_for(q.get(), timeout=0.3)
            if ev is None:
                break
            out.append(ev)
        return out

    async def main():
        # Close both after a moment.
        async def closer():
            await asyncio.sleep(0.05)
            bus.close("tA")
            bus.close("tB")

        closer_task = asyncio.create_task(closer())
        out_a, out_b = await asyncio.gather(consume("tA"), consume("tB"))
        await closer_task
        return out_a, out_b

    a, b = asyncio.run(main())
    # Each task sees its own event, not the other's. The
    # event ``id`` field is the marker we set in publish.
    assert all(e.get("id") == "a-1" for e in a)
    assert all(e.get("id") == "b-1" for e in b)


# ---------------------------------------------------------------------------
# SSE endpoint integration
# ---------------------------------------------------------------------------


def test_sse_endpoint_emits_history_then_done():
    """The SSE endpoint emits the per-task history as
    ``data:`` lines, then a closing ``event: done``."""
    from fastapi.testclient import TestClient
    from orchestra.api.app import create_app, AppState

    class StubStore:
        def get_task_run(self, _tid):
            return None

        def close(self):
            pass

    state = AppState(store=StubStore(), coordinator=None, benchmark_runner=None)
    bus = EventBus()
    bus.publish("t1", {"task_run_id": "t1", "kind": "task.received", "payload": {"k": 1}})
    bus.publish("t1", {"task_run_id": "t1", "kind": "plan.created", "payload": {"k": 2}})
    bus.close("t1")
    state.event_bus = bus
    client = TestClient(create_app(state))
    with client.stream("GET", "/tasks/t1/events/stream") as r:
        assert r.status_code == 200
        body = r.read().decode("utf-8")
    # 2 history events + 1 close sentinel = 3 data: lines
    # (the close line is ``event: done`` followed by
    # ``data: {}``).
    assert body.count("data: ") == 3
    assert "event: done" in body
    # The two history events are present.
    assert "task.received" in body
    assert "plan.created" in body
