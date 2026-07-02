"""Application settings."""

from __future__ import annotations

import secrets

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Pydantic settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://postgres:postgres@localhost:5432/lucnt"
    secret_key: str = secrets.token_urlsafe(32)
    access_token_expire_minutes: int = 30
    env: str = "development"
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    def is_production(self) -> bool:
        """Return True if the environment is production."""
        return self.env.lower() == "production"


settings = Settings()
