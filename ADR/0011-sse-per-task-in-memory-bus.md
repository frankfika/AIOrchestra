# ADR-0011 — SSE streaming is a per-task in-memory bus with replay

- Status: Accepted
- Date: 2026-08-06
- Deciders: Orchestra core team
- Relates to: M20, AGENTS.md §2

## Context

A partner who wants real-time task updates has three paths (M20):

  1. **Poll** — `GET /tasks/{id}/events`. Simple, but bursts at
     short intervals.
  2. **Webhook** — server POSTs the terminal-state payload
     (M17). Doesn't carry live updates.
  3. **Stream** — server pushes events as they happen. Live
     updates, but requires a long-lived connection.

For path 3, the dev path's options were:

  * **WebSocket** — full duplex; over-engineered for a
    unidirectional push.
  * **Server-Sent Events (SSE)** — text stream, one-way, native
    browser support, retry semantics. The right tool for a
    server-to-partner push.
  * **Long-poll** — HTTP 1.1 friendly but doesn't compose
    with the M14 metrics middleware.

## Decision

The dev path uses SSE (`orchestra.streaming.event_bus.EventBus`).
The Coordinator's `_emit` writes to both the EventStore and
the in-memory bus. A late subscriber sees the per-task
history first, then live events, then a `None` sentinel
the handler turns into `event: done`. The bus is closed
when the Coordinator reaches a terminal state (success,
failure, cancellation).

## Consequences

  * **+** SSE is one-way and text — a partner with `curl`
    can debug a stream. `wsdump`-style tools aren't
    needed.
  * **+** The bus keeps a 256-event replay buffer per
    task. A partner who joins mid-task still sees the
    audit context — they don't have to fetch the full
    history separately to understand what's happening.
  * **+** The `EventBus` is a process-local primitive
    that the Coordinator doesn't know about. The
    production swap to Redis pub/sub or NATS is a
    one-line change in the bus class; the wire
    format (`text/event-stream`) doesn't change.
  * **+** Three consumption paths now exist (poll,
    webhook, stream) so a partner picks the one that
    fits their stack.
  * **−** SSE has no native multiplexing — a partner
    who wants events for many tasks must open
    many streams. The M14 rate limit caps that.
  * **−** The in-memory bus is process-local. A
    multi-replica deployment needs Redis pub/sub
    so all replicas see the same events.

## Alternatives considered

  * **WebSocket** — full duplex. The dev path doesn't
    have a use case for client-to-server traffic
    (the partner's SDK already has HTTP for that).
    Over-engineered.
  * **Long-poll** — works, but doesn't compose with
    the M14 rate-limit middleware (each poll is a
    separate request). A partner who long-polls
    every second burns the bucket.
  * **WebSocket via ASGI** — same as WebSocket above.
