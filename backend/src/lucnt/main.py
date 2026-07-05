"""FastAPI application entry point (backend/PLAN.md §1)."""

from __future__ import annotations

from fastapi import FastAPI

from lucnt.routers import agents, books, budgets, policies, transactions


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    app = FastAPI(title="LUCNT API")
    app.include_router(transactions.router)
    app.include_router(agents.router)
    app.include_router(books.router)
    app.include_router(budgets.router)
    app.include_router(policies.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness/readiness probe (backend/PLAN.md §6)."""
        return {"status": "ok"}

    return app


app = create_app()
