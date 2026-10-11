"""Gunicorn configuration for the deployable image.

Each worker owns a DB pool: workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW) must stay
below Postgres max_connections, hence 2 workers rather than `2 × CPU + 1`.
No preload: forked workers would share the master's DB sockets.
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

# The default ~/.gunicorn control socket fails: the service user has no home.
control_socket_disable = True

# tmpfs heartbeat: a blocked disk write can get a healthy worker killed.
worker_tmp_dir = "/dev/shm"

# Recycle workers to cap slow leaks; jitter avoids simultaneous restarts.
max_requests = 2000
max_requests_jitter = 200

# Above the 30 s DB statement timeout, so Postgres (not gunicorn) kills runaways.
timeout = 60
graceful_timeout = 30
# Must exceed the proxy's idle timeout, or reused closed connections cause 502s.
keepalive = 65

# --- Logging ----------------------------------------------------------------
# Access log off: middleware.py emits a structured line per request.
accesslog = None
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()


def on_starting(server: Any) -> None:
    """Clear stale Prometheus multiprocess files from previous runs."""
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
