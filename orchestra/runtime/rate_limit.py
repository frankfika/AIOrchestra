"""M14 — Token-bucket rate limiter.

The dev path needs a per-tenant rate limit before pilot traffic
hits a single shared instance. The limiter is intentionally small:

  * :class:`TokenBucket` — single-key bucket with capacity + refill
    rate. Refill is computed lazily on each call so a bucket that
    is never used costs nothing.
  * :class:`RateLimiter` — a dict of buckets keyed by tenant (or
    by any caller-supplied identity). The first call creates the
    bucket; subsequent calls share it.

The bucket uses :func:`time.monotonic` so wall-clock jumps (NTP
step, leap second) cannot poison the rate. The implementation
is thread-safe via a :class:`threading.Lock` because the FastAPI
worker is multi-threaded by default.

A real M14 production swap plugs in a Redis-backed token bucket
or a sliding-window counter; the dev impl is good enough for a
single-process SRE and the production swap is a config change,
not a re-implementation.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from orchestra.observability.metrics import Metrics


@dataclass
class RateLimitDecision:
    """The outcome of a single :meth:`RateLimiter.check` call.

    The middleware uses ``allowed`` to decide between forwarding
    the request (200 path) and returning 429. ``retry_after`` is
    the number of seconds the caller should wait before retrying
    — included in the ``Retry-After`` header per RFC 6585.
    """

    allowed: bool
    retry_after: float
    remaining: float  # tokens left after the call
    tenant: str


class TokenBucket:
    """A single token bucket.

    ``capacity`` is the burst size. ``refill_rate`` is tokens per
    second. A request that finds ``tokens >= 1`` consumes one
    token and is allowed; otherwise the request is denied and
    ``retry_after`` is the time until one token is available.
    """

    __slots__ = ("_capacity", "_refill_rate", "_tokens", "_last", "_lock")

    def __init__(self, capacity: float, refill_rate: float) -> None:
        # Capacity is rounded up so a request of "1 token" can
        # always be served when the bucket is "full".
        self._capacity = float(capacity)
        self._refill_rate = float(refill_rate)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def refill_rate(self) -> float:
        return self._refill_rate

    def try_acquire(self, n: float = 1.0) -> tuple[bool, float, float]:
        """Try to take ``n`` tokens.

        Returns ``(allowed, retry_after, remaining)``:
          * ``allowed`` — True if the request is allowed.
          * ``retry_after`` — seconds until ``n`` tokens are
            available (0 when allowed).
          * ``remaining`` — tokens left in the bucket after the
            call (negative if not allowed, the shortfall).
        """
        if n <= 0:
            # Defensive: a non-positive request is a bug, not a
            # user-facing decision. Allow it so the caller doesn't
            # get into a tight loop.
            return True, 0.0, self._capacity
        if math.isinf(self._refill_rate) or self._refill_rate <= 0:
            # A misconfigured limiter must never silently drop
            # traffic. Refusing everything is the loud-failure
            # mode a SRE notices.
            return False, 3600.0, 0.0
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._last
            if elapsed > 0:
                # Lazy refill. Clamp to capacity so a long idle
                # period doesn't create an unbounded burst.
                self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
                self._last = now
            if self._tokens >= n:
                self._tokens -= n
                return True, 0.0, self._tokens
            # Not enough tokens — how long until ``n`` is available?
            deficit = n - self._tokens
            retry_after = deficit / self._refill_rate
            return False, retry_after, -deficit

    def snapshot(self) -> dict[str, float]:
        """A read-only view of the current state. The middleware
        publishes this as a Prometheus gauge so a SRE can graph
        per-tenant saturation."""
        with self._lock:
            return {
                "tokens": self._tokens,
                "capacity": self._capacity,
                "refill_rate": self._refill_rate,
            }


class RateLimiter:
    """A multi-tenant token-bucket limiter.

    The first call for a new key creates a bucket. The bucket
    lives forever (the dev path is in-memory; a Redis swap drops
    the in-process state). Capacity and refill rate come from
    the ``bucket_factory``; the same factory is used for every
    key so a SRE who wants per-tenant quotas can later swap
    in a per-tenant lookup.
    """

    def __init__(
        self,
        bucket_factory: Callable[[str], TokenBucket],
        metrics: "Optional[Metrics]" = None,
    ) -> None:
        self._factory = bucket_factory
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        # M14 — when metrics is set, throttle events tick
        # ``orchestra_rate_limit_throttled_total{tenant,reason}``
        # and a per-tenant gauge publishes the live remaining
        # tokens so a SRE can graph saturation.
        self._metrics = metrics
        if metrics is not None:
            self._m_throttled = metrics.counter(
                "orchestra_rate_limit_throttled_total",
                "Total requests rejected by the rate limiter.",
                labels=("tenant", "reason"),
            )
            self._m_remaining = metrics.gauge(
                "orchestra_rate_limit_remaining",
                "Tokens remaining in the tenant's token bucket.",
                labels=("tenant",),
            )
            self._m_checked = metrics.counter(
                "orchestra_rate_limit_checked_total",
                "Total requests evaluated by the rate limiter.",
                labels=("tenant", "outcome"),
            )
        else:
            self._m_throttled = None
            self._m_remaining = None
            self._m_checked = None

    def check(self, key: str, *, n: float = 1.0) -> RateLimitDecision:
        """Evaluate a request against the bucket for ``key``."""
        bucket = self._bucket_for(key)
        allowed, retry_after, remaining = bucket.try_acquire(n=n)
        if self._m_throttled is not None and not allowed:
            self._m_throttled.inc(tenant=key, reason="bucket_empty")
        if self._m_remaining is not None:
            self._m_remaining.set(max(0.0, remaining), tenant=key)
        if self._m_checked is not None:
            self._m_checked.inc(tenant=key, outcome="allowed" if allowed else "throttled")
        return RateLimitDecision(
            allowed=allowed,
            retry_after=retry_after,
            remaining=max(0.0, remaining),
            tenant=key,
        )

    def _bucket_for(self, key: str) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is not None:
            return bucket
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._factory(key)
                self._buckets[key] = bucket
            return bucket

    def snapshot(self, key: str) -> dict[str, float]:
        """Read-only view of the bucket for ``key`` (used by the
        ``/__rate_limit/{tenant}`` debug endpoint, when enabled)."""
        bucket = self._buckets.get(key)
        if bucket is None:
            return {"tokens": 0.0, "capacity": 0.0, "refill_rate": 0.0}
        return bucket.snapshot()

    def keys(self) -> list[str]:
        """The list of tenants the limiter has seen (debug)."""
        return sorted(self._buckets.keys())
