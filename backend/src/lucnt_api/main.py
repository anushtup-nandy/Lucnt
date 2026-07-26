"""FastAPI application entry point (backend/PLAN.md §1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI

from lucnt_api.config import settings
from lucnt_api.routers import (
    agents,
    approvals,
    books,
    budgets,
    insights,
    intelligence,
    memory,
    policies,
    recon,
    transactions,
    vendors,
)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Reconcile the worker-thread pool with the DB connection pool (T3-4).

    Every route here is a sync `def`, so FastAPI runs it on AnyIO's worker
    thread pool — 40 threads by default. Against a 20-connection pool that
    means up to 20 threads are admitted only to block in `pool_timeout`
    waiting for a connection that a *queued* request will never release,
    surfacing as opaque `TimeoutError`s under load rather than honest
    backpressure. Pinning the limiter to the pool's real capacity pushes the
    queue up to the ASGI layer, where waiting costs no thread and no
    connection.
    """
    anyio.to_thread.current_default_thread_limiter().total_tokens = (
        settings.db_pool_size + settings.db_max_overflow
    )
    yield


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    # T0-16: the interactive docs expose the entire financial API surface; serve
    # them only in development, never on a deployed environment.
    docs_enabled = settings.environment == "development"
    app = FastAPI(
        title="LUCNT API",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=_lifespan,
    )
    app.include_router(transactions.router)
    app.include_router(approvals.router)
    app.include_router(agents.router)
    app.include_router(vendors.router)
    app.include_router(books.router)
    app.include_router(budgets.router)
    app.include_router(policies.router)
    app.include_router(recon.router)
    app.include_router(intelligence.router)
    app.include_router(insights.router)
    app.include_router(memory.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness/readiness probe (backend/PLAN.md §6)."""
        return {"status": "ok"}

    return app


app = create_app()
