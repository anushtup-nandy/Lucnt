"""Prometheus instrumentation (T4-4).

Deliberately small. Three series answer the questions an on-call engineer
actually asks about this system — is the money path up, is it slow, and what is
it deciding — and nothing else is exported until something asks for it.

**Label cardinality is the trap.** Every interesting path here ends in a UUID
(`/agents/{agent_id}`, `/transactions/{transaction_id}`), so labelling by raw
request path would mint a new time series per entity and eventually take the
scrape target down. The middleware labels by *route template*, resolved from
`scope["route"].path` after routing; anything that never matched a route is
bucketed under `__unmatched__` rather than being allowed to name itself.

**Multiprocess.** Under gunicorn each worker holds its own counters, so a scrape
lands on one arbitrary worker and reports a fraction of the truth. Setting
`PROMETHEUS_MULTIPROC_DIR` (the image does) switches `render()` to a registry
that aggregates every live worker's shared-memory files; `gunicorn.conf.py`'s
`child_exit` hook marks dead workers so their samples are reaped.
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

# Buckets chosen for a blocking, DB-bound API rather than prometheus' defaults:
# the interesting question is how much of the hot `authorize()` path stays under
# ~100 ms, and where the tail sits relative to the 30 s `statement_timeout`.
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

# The one business metric worth exporting from day one: `authorize()` is where
# LUCNT blocks spend, so a shift in the verdict mix is either a policy change or
# an incident, and both need to be visible without querying the ledger.
authorize_verdicts_total = Counter(
    "lucnt_authorize_verdicts_total",
    "Authorization verdicts returned by the policy engine.",
    ("verdict",),
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
