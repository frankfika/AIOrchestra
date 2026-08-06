"""M20 — Per-task event bus for SSE streaming.

The Coordinator writes audit events to the EventStore for
durable replay; the EventBus mirrors those writes into a
per-task pub/sub so the SSE endpoint can stream them
live to a partner who has subscribed.

Design notes:

  * The bus is **in-process** in the dev path. A
    production swap uses Redis pub/sub or NATS; the
    shape of the events doesn't change, only the
    transport.
  * Subscribers are asyncio.Queue instances. A slow
    subscriber slows the bus only by filling its
    queue; the Coordinator never blocks on a
    subscriber (this is the ``put_nowait`` path).
  * The bus is closed when the task reaches a terminal
    state. Subscribers see a ``None`` sentinel and
    exit their read loop.
  * The bus also replays a snapshot of past events to
    late subscribers, so a partner who subscribes
    mid-task still sees the audit timeline.
"""

from __future__ import annotations

import asyncio
import threading
from collections import defaultdict, deque
from typing import Any, Optional


class EventBus:
    """A per-task pub/sub for live audit events.

    The class is thread-safe (the Coordinator writes
    from the asyncio loop, but a future swap to a
    multi-threaded worker must not race). The
    subscription model is a per-subscriber
    :class:`asyncio.Queue`; a subscriber pops events
    in a ``while True`` loop and exits on the ``None``
    close sentinel.
    """

    def __init__(self, *, replay_buffer: int = 256) -> None:
        self._lock = threading.Lock()
        # A bounded buffer of recent events per task so
        # a late subscriber sees the timeline so far.
        # 256 covers a typical contract-review task
        # (which emits ~12 events); a partner who joins
        # later still sees the audit context.
        self._history: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=replay_buffer)
        )
        # A per-task set of active subscribers. A
        # subscriber is an asyncio.Queue; the bus pushes
        # the same event into every queue.
        self._subscribers: dict[str, set[asyncio.Queue[Any]]] = defaultdict(set)
        self._closed: set[str] = set()

    def publish(self, task_run_id: str, event: dict[str, Any]) -> None:
        """Append an event to the bus.

        Called by the Coordinator's emit path so a
        partner's SSE subscription sees the event live.
        """
        with self._lock:
            buf = self._history[task_run_id]
            for sub in self._subscribers.get(task_run_id, ()):
                # ``put_nowait`` means a slow subscriber
                # drops events rather than blocking the
                # bus. The dev path has exactly one
                # subscriber per active SSE connection;
                # the path is hot only when a partner is
                # actively watching.
                try:
                    sub.put_nowait(event)
                except asyncio.QueueFull:
                    pass
            buf.append(event)

    def close(self, task_run_id: str) -> None:
        """Mark the bus closed for a task.

        Subscribers see ``None`` on their next ``get``
        and exit their read loop. A subscriber who
        subscribes after the close is replayed the
        full history and then sees ``None`` immediately.
        """
        with self._lock:
            if task_run_id in self._closed:
                return
            self._closed.add(task_run_id)
            for sub in self._subscribers.get(task_run_id, ()):
                try:
                    sub.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    def is_closed(self, task_run_id: str) -> bool:
        with self._lock:
            return task_run_id in self._closed

    def replay(self, task_run_id: str) -> list[dict[str, Any]]:
        """Return the per-task history snapshot."""
        with self._lock:
            return list(self._history.get(task_run_id, ()))

    async def subscribe(self, task_run_id: str) -> asyncio.Queue[Any]:
        """Subscribe to live events for a task.

        The returned queue is fed the per-task history
        first, then live events. A ``None`` sentinel
        signals the task is closed; the subscriber
        exits its read loop.

        The queue size defaults to a comfortable
        buffer; a partner who reads slowly gets
        ``QueueFull`` drops rather than backpressure on
        the bus.
        """
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=512)
        with self._lock:
            for event in self._history.get(task_run_id, ()):
                # Pre-fill with the history so a late
                # subscriber sees the timeline.
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    break
            if task_run_id in self._closed:
                # The task is already terminal; the
                # subscriber should drain the history
                # and then exit.
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass
                return q
            self._subscribers[task_run_id].add(q)
        return q

    def unsubscribe(self, task_run_id: str, q: asyncio.Queue[Any]) -> None:
        with self._lock:
            subs = self._subscribers.get(task_run_id)
            if subs is not None:
                subs.discard(q)
                if not subs:
                    self._subscribers.pop(task_run_id, None)
