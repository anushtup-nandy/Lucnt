"""Application configuration: Pydantic Settings loaded from environment/.env."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, sourced from environment variables / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    secret_key: str
    environment: str = "development"


# pydantic-settings fills required fields from the environment at runtime.
settings = Settings()  # type: ignore[call-arg]
