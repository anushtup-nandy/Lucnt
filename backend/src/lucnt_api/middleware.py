"""Request-scoped logging context, access logging and metrics (T4-4).

Written as raw ASGI rather than `BaseHTTPMiddleware` on purpose. The latter runs
the downstream app in a separate anyio task and re-raises exceptions across that
boundary, which both perturbs the failure mode of the money path and makes the
`contextvars` binding below unreliable — precisely the two things this module
exists to make trustworthy.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lucnt_api import metrics
from lucnt_api.logging_config import get_logger

logger = get_logger("lucnt_api.access")

# Callers may supply their own correlation id; anything longer is a log-injection
# vector and a cardinality risk, so it is replaced rather than truncated.
REQUEST_ID_HEADER = "x-request-id"
_MAX_INBOUND_REQUEST_ID = 64

# Routes whose own traffic must not be logged or measured: an access line per
# scrape or per health probe is pure noise, and `/metrics` counting itself is a
# feedback loop.
_UNINSTRUMENTED = frozenset({"/metrics", "/health/live", "/health/ready"})

_UNMATCHED_ROUTE = "__unmatched__"


def _inbound_request_id(scope: Scope) -> str:
    """Reuse a sane caller-supplied request id, else mint one."""
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for raw_name, raw_value in headers:
        if raw_name == REQUEST_ID_HEADER.encode():
            candidate = raw_value.decode("latin-1").strip()
            if candidate and len(candidate) <= _MAX_INBOUND_REQUEST_ID:
                return candidate
            break
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Bind a request id, emit one access log line, and record metrics."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the downstream ASGI application."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one ASGI event cycle, instrumenting HTTP scopes."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _inbound_request_id(scope)
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                # Echo the id so a caller can quote it in a bug report and an
                # engineer can find every line the request produced.
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # The exception handler in `main.py` turns this into a 500 response
            # for the client; re-raising keeps the ASGI contract intact while
            # still letting the `finally` block record the failure.
            self._record(scope, started, status_code, failed=True)
            raise
        else:
            self._record(scope, started, status_code, failed=False)
        finally:
            structlog.contextvars.clear_contextvars()

    def _record(
        self, scope: Scope, started: float, status_code: int, *, failed: bool
    ) -> None:
        """Emit the access line and observe metrics for a finished request."""
        path: str = scope["path"]
        if path in _UNINSTRUMENTED:
            return

        duration = time.perf_counter() - started
        route: Any = scope.get("route")
        # `scope["route"]` is set by Starlette's router once a route matches, so
        # a 404 (or a request rejected before routing) has none. Naming those by
        # their raw path is how a metrics endpoint acquires a million series.
        route_template = getattr(route, "path", None) or _UNMATCHED_ROUTE
        method: str = scope.get("method", "")

        metrics.http_requests_total.labels(
            method=method, route=route_template, status=str(status_code)
        ).inc()
        metrics.http_request_duration_seconds.labels(
            method=method, route=route_template
        ).observe(duration)

        client = scope.get("client")
        event = "request_failed" if failed else "request"
        log = logger.error if (failed or status_code >= 500) else logger.info
        log(
            event,
            method=method,
            path=path,
            route=route_template,
            status=status_code,
            duration_ms=round(duration * 1000, 2),
            client_ip=client[0] if client else None,
        )
