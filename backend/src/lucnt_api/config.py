"""Application configuration: Pydantic Settings loaded from environment/.env."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Public `.env.example` values (old and current); rejected outside development.
_EXAMPLE_SECRET_KEYS = frozenset(
    {"dev_secret_change_me", "dev_secret_change_me_before_deploying_lucnt"}
)
_MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Runtime configuration, sourced from environment variables / `.env`."""

    # Forbid extras so a typo'd variable crashes startup instead of being ignored.
    # Every key in `.env.example` must therefore be modelled here.
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")

    database_url: str
    # Throwaway DB for the suite; the migration round-trip downgrades to base.
    test_database_url: str | None = None
    # HS256 key for dashboard tokens. No default: a dev default leaks to prod.
    secret_key: str
    # Access token lives in SPA memory; refresh token in an httpOnly cookie.
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    # Comma-separated; not an auth boundary. Never a wildcard: credentials are on.
    cors_origins: str = "http://localhost:5173"
    # Required: an unset value must fail startup, not fail open (e.g. `/docs`).
    environment: Literal["development", "staging", "production"]
    # Pool total also sizes the AnyIO thread limiter (`main.py`). Keep it modest:
    # it is multiplied by gunicorn workers against Postgres max_connections=100.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_timeout: int = 10
    # Recycle below any proxy/Postgres idle-connection reaper (default 30 min).
    db_pool_recycle: int = 1800
    db_connect_timeout: int = 10
    # Backstop for unbounded ledger scans pinning a pool connection.
    db_statement_timeout_ms: int = 30_000
    # Rate-limit state is shared in Redis; per-process counters scale with workers.
    redis_url: str = "redis://localhost:6379/0"
    # Deliberate kill switch; an unreachable Redis already fails open.
    rate_limit_enabled: bool = True
    # Per API key; generous — stops runaway loops, not normal use.
    rate_limit_default_per_minute: int = 600
    # Failed auths per client IP; each costs a key-hash DB lookup.
    rate_limit_auth_failure_per_minute: int = 20
    # Ledger-walking endpoints (`/recon/scan`, `/intelligence/recompute`).
    rate_limit_expensive_per_minute: int = 5
    # Small: the limiter sits on the money path, so a Redis stall must fail open fast.
    rate_limit_redis_timeout: float = 0.25
    # Max capture/authorized ratio; beyond it the spend must be re-authorized.
    settlement_buffer: Decimal = Decimal("1.25")
    # How long an uncaptured hold reserves budget. Expiry releases only the
    # reservation; a late capture is still booked.
    authorization_ttl_hours: int = 24
    # India s.195 withholding (DTAA vs domestic, Form-15CB threshold).
    # PROVISIONAL: confirm with a CA; rates change, so keep them config.
    withholding_treaty_rate: Decimal = Decimal("0.15")
    withholding_domestic_rate: Decimal = Decimal("0.20")
    withholding_15cb_threshold: Decimal = Decimal("500000")
    # Unused yet; declared because `.env.example` defines them (extra="forbid").
    smtp_host: str | None = None
    smtp_port: int | None = None
    # --- Observability ----------------------------------------------------
    log_level: str = "INFO"
    # None = derive from `environment`.
    log_format: Literal["json", "console"] | None = None
    # Unset means Sentry is never imported.
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.0
    # `/metrics` is unauthenticated; nginx denies it at the edge, this is lock two.
    metrics_enabled: bool = True
    # Lifespan DB ping + schema-at-head check; tests disable it (overridden sessions).
    startup_checks_enabled: bool = True

    @model_validator(mode="after")
    def _validate_deployment_safety(self) -> Settings:
        """Reject configurations that are only safe on a laptop."""
        if self.environment != "development":
            if self.test_database_url is not None:
                raise ValueError(
                    "TEST_DATABASE_URL must not be set outside development — the "
                    "suite truncates and downgrades whatever it points at."
                )
            if (
                self.secret_key in _EXAMPLE_SECRET_KEYS
                or len(self.secret_key) < _MIN_SECRET_KEY_LENGTH
            ):
                raise ValueError(
                    f"SECRET_KEY must be at least {_MIN_SECRET_KEY_LENGTH} "
                    "characters and must not be the committed example value."
                )
        if (
            self.test_database_url is not None
            and self.test_database_url == self.database_url
        ):
            raise ValueError(
                "TEST_DATABASE_URL is identical to DATABASE_URL, which defeats "
                "the point of having a separate test database."
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        """`CORS_ORIGINS` split into exact origins, blanks discarded."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def resolved_log_format(self) -> Literal["json", "console"]:
        """Explicit `log_format`, else JSON everywhere but development."""
        if self.log_format is not None:
            return self.log_format
        return "console" if self.environment == "development" else "json"


# pydantic-settings fills required fields from the environment at runtime.
settings = Settings()  # type: ignore[call-arg]
