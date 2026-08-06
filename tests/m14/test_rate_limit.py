"""M14 — Token-bucket rate limiter tests.

The limiter is the line of defence between a partner and the
Coordinator. The tests below prove the public contract:

  * a bucket of capacity N allows N requests in a burst,
  * the bucket refills at the configured rate,
  * a denied request returns the time until one token is free,
  * a request for 0 (or negative) tokens is always allowed (a
    misconfigured caller must not be able to starve itself),
  * a bucket with refill_rate <= 0 is always denied (fail loud,
    not silent — a SRE notices immediately),
  * different tenants have independent buckets.
"""

from __future__ import annotations

import time

import pytest

from orchestra.observability import Metrics, render_prometheus
from orchestra.runtime.rate_limit import RateLimiter, TokenBucket


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


def test_bucket_allows_burst_up_to_capacity():
    """A fresh bucket has capacity tokens; the first capacity
    requests all succeed, the next one is denied (refill rate
    is much slower than the test, so the burst is the dominant
    factor)."""
    b = TokenBucket(capacity=3, refill_rate=0.001)  # 1 token / 1000s
    for _ in range(3):
        allowed, _, _ = b.try_acquire()
        assert allowed
    allowed, retry_after, remaining = b.try_acquire()
    assert not allowed
    assert remaining < 0
    assert retry_after > 0


def test_bucket_refills_at_configured_rate():
    """After waiting, a bucket with refill_rate > 0 has more tokens."""
    b = TokenBucket(capacity=2, refill_rate=10)  # 10 tokens / second
    # Drain the bucket.
    b.try_acquire()
    b.try_acquire()
    allowed, _, _ = b.try_acquire()
    assert not allowed
    # 200ms of waiting at 10 RPS = 2 new tokens.
    time.sleep(0.25)
    allowed, _, _ = b.try_acquire()
    assert allowed


def test_bucket_caps_at_capacity_after_long_idle():
    """A bucket that sits idle for an hour doesn't unbounded-burst
    on the next request — the cap holds."""
    b = TokenBucket(capacity=5, refill_rate=1000)
    # Drain, then wait long enough to fill ~50 tokens at the raw
    # rate. The bucket must clamp to capacity (5).
    b.try_acquire()
    time.sleep(0.05)
    snap = b.snapshot()
    assert snap["tokens"] <= snap["capacity"]


def test_bucket_with_zero_refill_rate_always_denies():
    """A misconfigured ``refill_rate=0`` must fail loud: every
    request after the initial burst is denied."""
    b = TokenBucket(capacity=2, refill_rate=0)
    b.try_acquire()
    b.try_acquire()
    allowed, _, _ = b.try_acquire()
    assert not allowed


def test_bucket_with_infinite_refill_rate_lets_through():
    """A non-finite refill rate is a configuration error; the
    bucket should NOT silently drop traffic. The dev impl
    rejects it instead — partner traffic must never vanish."""
    b = TokenBucket(capacity=1, refill_rate=float("inf"))
    allowed, _, _ = b.try_acquire()
    # Per the docstring: a non-finite rate is "fail loud" so a
    # SRE catches the misconfiguration. Deny everything.
    assert not allowed


def test_bucket_zero_or_negative_request_always_allowed():
    """A request for 0 or negative tokens is a bug, not a
    user-facing decision. The bucket must not let a buggy
    caller starve itself in a tight loop."""
    b = TokenBucket(capacity=1, refill_rate=1)
    for n in (0, -1, -100):
        allowed, _, _ = b.try_acquire(n=n)
        assert allowed


def test_bucket_snapshot_is_a_readonly_view():
    snap = TokenBucket(capacity=5, refill_rate=1).snapshot()
    assert snap["capacity"] == 5
    assert snap["refill_rate"] == 1
    # Modifying the returned dict must not affect the bucket.
    snap["capacity"] = 999
    fresh = TokenBucket(capacity=5, refill_rate=1).snapshot()
    assert fresh["capacity"] == 5


# ---------------------------------------------------------------------------
# RateLimiter (multi-tenant)
# ---------------------------------------------------------------------------


def test_limiter_creates_a_bucket_per_tenant_on_demand():
    metrics = Metrics()
    limiter = RateLimiter(
        bucket_factory=lambda _key: TokenBucket(capacity=2, refill_rate=0.001),
        metrics=metrics,
    )
    d1 = limiter.check("tenant-a")
    d2 = limiter.check("tenant-b")
    d3 = limiter.check("tenant-a")
    assert d1.allowed
    assert d2.allowed
    assert d3.allowed  # tenant-a still has 1 token left
    d4 = limiter.check("tenant-a")
    assert not d4.allowed  # bucket exhausted


def test_limiter_metrics_tick_on_throttle():
    metrics = Metrics()
    limiter = RateLimiter(
        bucket_factory=lambda _key: TokenBucket(capacity=1, refill_rate=0.001),
        metrics=metrics,
    )
    limiter.check("tA")  # allowed
    limiter.check("tA")  # throttled
    limiter.check("tB")  # allowed
    limiter.check("tB")  # throttled
    out = render_prometheus(metrics)
    assert ('orchestra_rate_limit_throttled_total{tenant="tA",reason="bucket_empty"} 1') in out
    assert ('orchestra_rate_limit_throttled_total{tenant="tB",reason="bucket_empty"} 1') in out
    assert ('orchestra_rate_limit_checked_total{tenant="tA",outcome="allowed"} 1') in out
    assert ('orchestra_rate_limit_checked_total{tenant="tA",outcome="throttled"} 1') in out


def test_limiter_remaining_gauge_is_published():
    metrics = Metrics()
    limiter = RateLimiter(
        bucket_factory=lambda _key: TokenBucket(capacity=5, refill_rate=0.001),
        metrics=metrics,
    )
    limiter.check("tA")  # 4 remaining
    limiter.check("tA")  # 3 remaining
    out = render_prometheus(metrics)
    assert 'orchestra_rate_limit_remaining{tenant="tA"} 3' in out


def test_limiter_works_without_metrics():
    """The default code path (no metrics) must behave as before."""
    limiter = RateLimiter(
        bucket_factory=lambda _key: TokenBucket(capacity=1, refill_rate=0.001),
    )
    assert limiter.check("tA").allowed
    assert not limiter.check("tA").allowed


def test_limiter_keys_lists_seen_tenants():
    limiter = RateLimiter(
        bucket_factory=lambda _key: TokenBucket(capacity=1, refill_rate=1),
    )
    limiter.check("a")
    limiter.check("b")
    limiter.check("c")
    assert limiter.keys() == ["a", "b", "c"]


def test_limiter_decision_includes_retry_after():
    """A denied request carries the seconds-until-one-token
    in :attr:`RateLimitDecision.retry_after`."""
    limiter = RateLimiter(
        bucket_factory=lambda _key: TokenBucket(capacity=1, refill_rate=2),
    )
    limiter.check("tA")  # allowed
    d = limiter.check("tA")  # denied
    assert not d.allowed
    # refill_rate=2 means 1 token every 0.5s; retry_after ≈ 0.5.
    assert 0.4 < d.retry_after < 0.6
