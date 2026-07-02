"""FastAPI application factory."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from lucnt.config import settings
from lucnt.routers import auth, entities


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title="LUCNT API", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins.split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(entities.router)

    @app.get("/health")
    def health_check() -> dict[str, Any]:
        return {"status": "ok"}

    return app


app = create_app()
