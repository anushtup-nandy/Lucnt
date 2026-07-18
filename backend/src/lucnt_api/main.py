"""FastAPI application entry point (backend/PLAN.md §1)."""

from __future__ import annotations

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
)


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
    )
    app.include_router(transactions.router)
    app.include_router(approvals.router)
    app.include_router(agents.router)
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
