"""Gunicorn configuration for the deployable image.

This file is where the deployment topology stops being implicit. Until now
"deployment" was a laptop running `uvicorn`, so the pool sizing and the rate
limiter both rested on an unstated assumption about how many processes
exist. Both assumptions are settled here.

**The connection arithmetic (why `workers` defaults to 2).** Every worker builds
its own SQLAlchemy engine, so the real ceiling is

    workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW)  ≤  postgres max_connections − reserve

With the pool defaults (10 + 10) and Postgres' default `max_connections` of 100,
two workers need 40 connections and leave room for migrations, psql and a second
container during a rolling deploy. The commonly copied `2 × CPU + 1` would ask
for 180 on a 4-core box and exhaust the server instead. Raise `WEB_CONCURRENCY`
only together with `max_connections`, and read the `startup_pool_budget` log line
the app emits to confirm the arithmetic.

**No preloading, deliberately.** `preload_app` would import the app — and build
the engine, and its TCP connections — in the master process before forking, so
every worker would inherit and concurrently use the *same* sockets. That
corrupts the protocol stream in ways that surface as random driver errors on the
money path. Forking before any connection exists is the only safe order.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

# --- Socket -----------------------------------------------------------------
bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")

# --- Workers ----------------------------------------------------------------
worker_class = "uvicorn_worker.UvicornWorker"
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
preload_app = False

# gunicorn 26 opens a control socket under `~/.gunicorn/` by default. The image's
# service account has no home directory (`useradd --no-create-home`), so that
# fails at boot with `Permission denied: '/home/lucnt'` — noisy, and a wart on
# every container's first log lines. Nothing here drives gunicorn through that
# socket (the orchestrator sends signals), so the surface is removed rather than
# relocated.
control_socket_disable = True

# The worker heartbeat file lives in /tmp by default. On a disk-backed /tmp under
# load, writing it can block long enough for the master to declare a healthy
# worker dead and kill it mid-request; /dev/shm is a tmpfs in every container
# runtime.
worker_tmp_dir = "/dev/shm"

# Every route is a sync `def` served on the AnyIO worker-thread pool, which
# `main.py` pins to the DB pool's capacity. Recycling workers periodically caps
# the blast radius of any slow leak in a long-lived process; the jitter stops all
# workers restarting in the same instant.
max_requests = 2000
max_requests_jitter = 200

# Above the 30 s `DB_STATEMENT_TIMEOUT_MS`, so Postgres kills a runaway query and
# the request returns an error rather than gunicorn killing the worker and losing
# every other request it was serving.
timeout = 60
graceful_timeout = 30
# Must exceed the idle timeout of whatever fronts this, or the proxy will reuse a
# connection gunicorn has already closed and surface it as a spurious 502.
keepalive = 65

# --- Logging ----------------------------------------------------------------
# stdout/stderr only: the container runtime owns log shipping. The access log is
# disabled because `middleware.py` emits a richer structured line per request —
# two access logs in two formats is worse than one.
accesslog = None
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()


def on_starting(server: Any) -> None:
    """Clear the Prometheus multiprocess directory before workers start.

    `prometheus_client` accumulates one shared-memory file per worker PID. Files
    left by a previous container generation would otherwise be aggregated into
    the current scrape, reporting counters for processes that no longer exist.
    """
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not multiproc_dir:
        return
    shutil.rmtree(multiproc_dir, ignore_errors=True)
    os.makedirs(multiproc_dir, exist_ok=True)
    server.log.info("cleared PROMETHEUS_MULTIPROC_DIR=%s", multiproc_dir)


def child_exit(server: Any, worker: Any) -> None:
    """Reap a dead worker's metric files so `/metrics` stops reporting them."""
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    from prometheus_client import multiprocess

    # prometheus_client ships no type stubs for the multiprocess module.
    multiprocess.mark_process_dead(worker.pid)  # type: ignore[no-untyped-call]
