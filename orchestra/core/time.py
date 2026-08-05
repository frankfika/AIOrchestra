"""Time helpers.

P0 stores everything in UTC ISO-8601 with ``Z`` suffix. The plan requires
that times are comparable across components, so we never use local time
anywhere in the system.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 with ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_utc_iso(s: str) -> datetime:
    """Parse an ISO-8601 string produced by :func:`utc_now_iso`."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def monotonic_ms() -> int:
    """Monotonic millisecond counter for latency measurements."""
    return int(time.monotonic() * 1000)
