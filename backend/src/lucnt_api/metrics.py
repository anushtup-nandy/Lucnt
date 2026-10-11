"""Prometheus instrumentation.

Label by route template, never raw path (UUIDs explode cardinality).
`PROMETHEUS_MULTIPROC_DIR` makes `render()` aggregate across gunicorn workers.
"""

from __future__ import annotations

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    multiprocess,
)
from prometheus_client import generate_latest as _generate_latest

CONTENT_TYPE = CONTENT_TYPE_LATEST

# Tuned for the ~100 ms authorize() path up to the 30 s statement_timeout.
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

http_requests_total = Counter(
    "lucnt_http_requests_total",
    "HTTP requests, by route template, method and status class.",
    ("method", "route", "status"),
)

http_request_duration_seconds = Histogram(
    "lucnt_http_request_duration_seconds",
    "HTTP request latency in seconds, by route template.",
    ("method", "route"),
    buckets=_LATENCY_BUCKETS,
)

# A shift in the verdict mix is a policy change or an incident.
authorize_verdicts_total = Counter(
    "lucnt_authorize_verdicts_total",
    "Authorization verdicts returned by the policy engine.",
    ("verdict",),
)

# Any increase means brute-force protection is OFF; must page
# (infra/prometheus/alerts.yml).
rate_limiter_fail_open_total = Counter(
    "lucnt_rate_limiter_fail_open_total",
    "Requests admitted without rate limiting because Redis was unreachable.",
    ("bucket",),
)


def render() -> bytes:
    """Serialise the current metrics, aggregating across workers if configured."""
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not multiproc_dir:
        return _generate_latest()
    registry = CollectorRegistry()
    # prometheus_client ships no type stubs for the multiprocess module.
    multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    return _generate_latest(registry)
