"""Application configuration: Pydantic Settings loaded from environment/.env."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The value shipped in `.env.example` since the first commit, therefore identical
# in every checkout and effectively public. Rejected outside development (T4-9).
_EXAMPLE_SECRET_KEY = "dev_secret_change_me"
_MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Runtime configuration, sourced from environment variables / `.env`."""

    # T4-12: `extra="ignore"` meant a typo'd variable was silently discarded —
    # exactly how `REDIS_URL` sat inert in `.env.example` through all of Tier 3
    # (see `redis_url` below). Forbidding extras turns "misspelled DB_POOL_SIZE"
    # from a mystery performance regression into a startup crash. Every key that
    # legitimately appears in `.env.example` must therefore be modelled here,
    # including ones nothing reads yet (`smtp_*`).
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")

    database_url: str
    # T4-12: tests bound to `DATABASE_URL` — i.e. to the developer's own database.
    # Harmless for the suite itself (every fixture rolls back), but CI's migration
    # round-trip runs `alembic downgrade base`, which does not. Set this and both
    # the suite and its Alembic setup target a throwaway database instead.
    test_database_url: str | None = None
    # T4-9: declared `str` (required) but read by nothing — there is no session or
    # JWT auth yet; API keys are hashed in `dependencies.py`. Now optional so a
    # deployment isn't forced to invent a secret for a feature that doesn't exist,
    # and validated below so that when one *is* supplied outside development it
    # cannot be the committed example.
    secret_key: str | None = None
    # T4-12/SCALABILITY_PLAN.md §501: this used to default to "development", so an
    # unset ENVIRONMENT in production served `/docs`, `/redoc` and `/openapi.json`
    # — the entire financial API surface — to the internet. Required and closed:
    # an unset or misspelled value now fails startup instead of failing open.
    environment: Literal["development", "staging", "production"]
    # Connection pool + timeouts (T3-4). A bare `create_engine()` inherits
    # SQLAlchemy's 5+10 defaults with no liveness check and no server-side cap:
    # a connection dropped while idle 500s the next request, and one slow query
    # can hold a connection indefinitely. Sizing note: FastAPI runs `def`
    # endpoints on the AnyIO worker thread pool, so a thread without a
    # connection just blocks holding a slot — `main.py` pins the thread limiter
    # to `db_pool_size + db_max_overflow` so the two can never disagree. Keep the
    # total modest: it is multiplied by the number of gunicorn workers, against a
    # Postgres `max_connections` of 100 by default.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    # Wait this long for a free connection before failing fast (vs hanging).
    db_pool_timeout: int = 10
    # Recycle below any proxy/Postgres idle-connection reaper (default 30 min).
    db_pool_recycle: int = 1800
    # TCP connect timeout for a new connection (libpq `connect_timeout`).
    db_connect_timeout: int = 10
    # Server-side ceiling on any single statement. The backstop for T3-2's
    # unbounded full-ledger scans: a runaway query is killed by Postgres rather
    # than pinning a pool connection until the client gives up.
    db_statement_timeout_ms: int = 30_000
    # Rate limiting (T3-7). Shared state in Redis, because a per-process counter
    # multiplies the effective limit by the worker count. `REDIS_URL` was present
    # in `.env.example` but absent from Settings until now, so it was being
    # silently dropped by `extra="ignore"`.
    redis_url: str = "redis://localhost:6379/0"
    # Kill switch. On means the limiter is consulted; an unreachable Redis still
    # fails *open* (see `rate_limit.py`) — this flag is for turning the feature
    # off deliberately, not for surviving an outage.
    rate_limit_enabled: bool = True
    # Ordinary authenticated calls, per API key. Generous: this exists to stop a
    # runaway agent loop inflating rows and telemetry, not to shape normal use.
    rate_limit_default_per_minute: int = 600
    # Failed authentications, per client IP. Tight — each attempt otherwise costs
    # a key-hash DB lookup, which is the brute-force cost T3-7 names.
    rate_limit_auth_failure_per_minute: int = 20
    # Ledger-walking endpoints (`/recon/scan`, `/intelligence/recompute`). Valid
    # credentials are enough to pin a worker on these today (T3-2).
    rate_limit_expensive_per_minute: int = 5
    # Socket budget for a limiter round trip. Deliberately small: the check sits
    # on the blocking money path, so a Redis stall must degrade to fail-open fast
    # rather than adding its full timeout to every authorize().
    rate_limit_redis_timeout: float = 0.25
    # Max ratio a capture may settle over its authorized amount (T0-3). The
    # policy verdict + budget reservation were computed on the authorized figure;
    # a true cost beyond this tolerance must be re-authorized, not silently
    # settled. 1.25 = allow up to 125% of the estimate.
    settlement_buffer: Decimal = Decimal("1.25")
    # India s.195 withholding on payments to non-residents (TE-3). PROVISIONAL —
    # confirm with a CA before a pilot: the DTAA treaty rate (with a valid TRC +
    # Form 10F) vs the domestic default (incl. surcharge/cess), and the ₹5-lakh
    # Form-15CB threshold above which a CA certificate is required. Rates move with
    # notifications and treaties; keep them config, not code.
    withholding_treaty_rate: Decimal = Decimal("0.15")
    withholding_domestic_rate: Decimal = Decimal("0.20")
    withholding_15cb_threshold: Decimal = Decimal("500000")
    # M3 invoice/email ingestion (not built). Modelled only because `.env.example`
    # and compose's mailpit already define them and `extra="forbid"` would now
    # reject them — declaring a field is the price of documenting a variable.
    smtp_host: str | None = None
    smtp_port: int | None = None
    # --- Observability (T4-4) ----------------------------------------------
    log_level: str = "INFO"
    # None = decide from `environment`: JSON where something ships logs to an
    # aggregator, human-readable colour where a developer is reading them.
    log_format: Literal["json", "console"] | None = None
    # Errors go nowhere today. Optional so the dependency stays optional: unset
    # means Sentry is never imported, let alone initialised.
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.0
    # `/metrics` carries no authentication and must never be internet-reachable —
    # `infra/nginx/nginx.conf` denies it at the edge. This flag is the second lock.
    metrics_enabled: bool = True
    # Lifespan DB ping + "schema is at head" assertion (T4-12). The suite turns
    # this off: the app under test is served sessions through
    # `dependency_overrides`, so a check against the module-level engine would be
    # asserting about a database the tests never touch. It is covered directly by
    # `tests/integration/test_startup_and_health.py` instead.
    startup_checks_enabled: bool = True

    @model_validator(mode="after")
    def _validate_deployment_safety(self) -> Settings:
        """Reject configurations that are only safe on a laptop (T4-9/T4-12)."""
        if self.environment != "development":
            if self.test_database_url is not None:
                raise ValueError(
                    "TEST_DATABASE_URL must not be set outside development — the "
                    "suite truncates and downgrades whatever it points at."
                )
            if self.secret_key is not None and (
                self.secret_key == _EXAMPLE_SECRET_KEY
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
    def resolved_log_format(self) -> Literal["json", "console"]:
        """Explicit `log_format`, else JSON everywhere but development."""
        if self.log_format is not None:
            return self.log_format
        return "console" if self.environment == "development" else "json"


# pydantic-settings fills required fields from the environment at runtime.
settings = Settings()  # type: ignore[call-arg]
