"""Request-scoped logging context, access logging and metrics.

Raw ASGI, not `BaseHTTPMiddleware`: its separate task breaks contextvars and
exception propagation.
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

# Oversized inbound ids are replaced, not truncated (log injection).
REQUEST_ID_HEADER = "x-request-id"
_MAX_INBOUND_REQUEST_ID = 64

# Probe/scrape noise; `/metrics` must not count itself.
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
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
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
        # Unmatched requests have no route; never label by raw path.
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
