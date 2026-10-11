"""FastAPI application entry point."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio.to_thread
from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from lucnt_api import metrics
from lucnt_api.config import settings
from lucnt_api.database import SessionLocal, get_db
from lucnt_api.logging_config import configure_logging, get_logger
from lucnt_api.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from lucnt_api.routers import (
    agents,
    approvals,
    auth,
    authorizations,
    books,
    budgets,
    developers,
    insights,
    intelligence,
    memory,
    policies,
    pricing,
    recon,
    transactions,
    vendors,
    workspace,
)
from lucnt_api.startup import check_readiness, verify_ready_or_raise

logger = get_logger(__name__)


def _init_sentry() -> None:
    """Initialise Sentry when a DSN is configured; a no-op otherwise."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        # An operator who set a DSN clearly wanted error reporting; saying so is
        # more useful than silently discarding every exception report.
        logger.error("sentry_dsn_set_but_sdk_missing", hint="pip install '.[sentry]'")
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        # Bodies on this API are spend instructions and vendor identities; do not
        # ship them to a third party by default.
        send_default_pii=False,
    )
    logger.info("sentry_initialised", environment=settings.environment)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Reconcile the worker-thread pool with the DB connection pool.

    Every route here is a sync `def`, so FastAPI runs it on AnyIO's worker
    thread pool — 40 threads by default. Against a 20-connection pool that
    means up to 20 threads are admitted only to block in `pool_timeout`
    waiting for a connection that a *queued* request will never release,
    surfacing as opaque `TimeoutError`s under load rather than honest
    backpressure. Pinning the limiter to the pool's real capacity pushes the
    queue up to the ASGI layer, where waiting costs no thread and no
    connection.

    Startup readiness runs here too, so a process that cannot reach its
    database — or that is running ahead of its migrations — fails now, loudly,
    instead of on the first request that touches a missing column.
    """
    per_worker_connections = settings.db_pool_size + settings.db_max_overflow
    anyio.to_thread.current_default_thread_limiter().total_tokens = (
        per_worker_connections
    )
    # The deployment topology gates the pool sizing: the ceiling that matters is
    # workers × (pool_size + max_overflow) against Postgres `max_connections`
    # (100 by default). Logging the arithmetic makes an over-provisioned deploy
    # visible in a container's first log line rather than as connection
    # exhaustion under load.
    workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    logger.info(
        "startup_pool_budget",
        workers=workers,
        connections_per_worker=per_worker_connections,
        max_connections_required=workers * per_worker_connections,
        environment=settings.environment,
    )
    if settings.startup_checks_enabled:
        # A short-lived session of its own: the lifespan has no request, so no
        # `get_db` dependency to borrow one from.
        with SessionLocal() as session:
            verify_ready_or_raise(session)
    yield


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    configure_logging()
    _init_sentry()

    # The interactive docs expose the entire financial API surface; serve
    # them only in development, never on a deployed environment.
    docs_enabled = settings.environment == "development"
    app = FastAPI(
        title="LUCNT API",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=_lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    # P7.0. The dashboard is a separate origin in development (Vite on :5173) and
    # may be one in production. `allow_credentials=True` is required for the
    # refresh cookie — which is exactly why the origin list must stay exact:
    # the CORS spec forbids a wildcard alongside credentials, and a browser that
    # accepted one would let any site drive an authenticated session.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
    )

    app.include_router(auth.router)
    app.include_router(transactions.router)
    app.include_router(authorizations.router)
    app.include_router(approvals.router)
    app.include_router(workspace.router)
    app.include_router(developers.router)
    app.include_router(agents.router)
    app.include_router(vendors.router)
    app.include_router(books.router)
    app.include_router(budgets.router)
    app.include_router(policies.router)
    app.include_router(pricing.router)
    app.include_router(recon.router)
    app.include_router(intelligence.router)
    app.include_router(insights.router)
    app.include_router(memory.router)

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        """Log the traceback, return a body that leaks nothing.

        Without this, an unhandled error reached the ASGI server as a bare
        traceback on stderr with no request id attached — the failures hardest
        to diagnose were exactly the ones leaving no structured trace. The
        response carries the request id so a user's bug report and the server's
        log line can be joined.
        """
        request_id = request.headers.get(REQUEST_ID_HEADER)
        logger.exception(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error", "request_id": request_id},
        )

    @app.get("/health", include_in_schema=False)
    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        """Liveness: is this process running? Never touches a dependency.

        A liveness probe that checks the database is a way to turn one database
        blip into every container being killed at once. `/health` is retained as
        an alias so existing probes keep working.
        """
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready(
        response: Response, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        """Readiness: can this process actually serve?

        The old single `/health` returned a static dict, so a load balancer
        happily kept routing to a backend whose Postgres was gone. This one takes
        a connection out of the real pool, pings it, and checks the schema is at
        the migration head.
        """
        report = check_readiness(db)
        if not report.ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if report.ready else "not_ready",
            "database": "up" if report.database_reachable else "down",
            "schema_at_head": report.schema_at_head,
            "detail": report.detail,
        }

    if settings.metrics_enabled:

        @app.get("/metrics", include_in_schema=False)
        def prometheus_metrics() -> Response:
            """Prometheus scrape endpoint.

            Unauthenticated by design and therefore denied at the edge — see the
            `location /metrics` block in `infra/nginx/nginx.conf`. It exposes
            route-level latency and verdict counts, never money figures.
            """
            return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)

    return app


app = create_app()
