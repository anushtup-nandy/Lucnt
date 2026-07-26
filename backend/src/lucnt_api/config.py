"""Application configuration: Pydantic Settings loaded from environment/.env."""

from __future__ import annotations

from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, sourced from environment variables / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    secret_key: str
    environment: str = "development"
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


# pydantic-settings fills required fields from the environment at runtime.
settings = Settings()  # type: ignore[call-arg]
