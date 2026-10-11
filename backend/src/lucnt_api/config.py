"""Application configuration: Pydantic Settings loaded from environment/.env."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The values shipped in `.env.example`, therefore identical in every checkout and
# effectively public. Rejected outside development. Both the original 20-char
# form and the current padded one are listed: the padding exists only so a dev
# run does not trip PyJWT's InsecureKeyLengthWarning on every token, and a
# deployment that still carries the old value must keep failing.
_EXAMPLE_SECRET_KEYS = frozenset(
    {"dev_secret_change_me", "dev_secret_change_me_before_deploying_lucnt"}
)
_MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Runtime configuration, sourced from environment variables / `.env`."""

    # `extra="ignore"` meant a typo'd variable was silently discarded — exactly
    # how `REDIS_URL` sat inert in `.env.example` for a long time (see
    # `redis_url` below). Forbidding extras turns "misspelled DB_POOL_SIZE"
    # from a mystery performance regression into a startup crash. Every key that
    # legitimately appears in `.env.example` must therefore be modelled here,
    # including ones nothing reads yet (`smtp_*`).
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")

    database_url: str
    # Tests bound to `DATABASE_URL` — i.e. to the developer's own database.
    # Harmless for the suite itself (every fixture rolls back), but CI's migration
    # round-trip runs `alembic downgrade base`, which does not. Set this and both
    # the suite and its Alembic setup target a throwaway database instead.
    test_database_url: str | None = None
    # P7.0: REQUIRED again, and now actually load-bearing — it is the HS256 key
    # that signs dashboard access and refresh tokens (`security.py`). It was once
    # optional precisely because nothing read it ("Reinstate them only alongside
    # the auth feature that needs them"); that feature is here, so rather than
    # inventing a second `JWT_SECRET` alongside an unused `SECRET_KEY`, the
    # existing setting is promoted. Fail-loud with no default, matching the
    # `ENVIRONMENT` precedent: a signing key with a dev default is how dev secrets
    # reach production. The validator below still rejects the committed example
    # value and anything under 32 chars outside development.
    secret_key: str
    # Dashboard session lifetimes (P7.0). Short access token, held in memory by
    # the SPA; longer refresh token in an httpOnly SameSite=Strict cookie.
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    # Browsers only. The SDK is not a browser and never sends an Origin, so this
    # is not an authorization boundary — it is what lets the dashboard's fetch see
    # the response at all. Comma-separated; `cors_origin_list` splits it.
    # NEVER a wildcard: `allow_credentials=True` is required for the refresh
    # cookie, and the two combined are rejected by the CORS spec anyway.
    cors_origins: str = "http://localhost:5173"
    # This used to default to "development", so an unset ENVIRONMENT in
    # production served `/docs`, `/redoc` and `/openapi.json` — the entire
    # financial API surface — to the internet. Required and closed: an unset or
    # misspelled value now fails startup instead of failing open.
    environment: Literal["development", "staging", "production"]
    # Connection pool + timeouts. A bare `create_engine()` inherits
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
    # Server-side ceiling on any single statement. The backstop for the
    # unbounded full-ledger scans: a runaway query is killed by Postgres rather
    # than pinning a pool connection until the client gives up.
    db_statement_timeout_ms: int = 30_000
    # Rate limiting. Shared state in Redis, because a per-process counter
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
    # a key-hash DB lookup, which is the brute-force cost.
    rate_limit_auth_failure_per_minute: int = 20
    # Ledger-walking endpoints (`/recon/scan`, `/intelligence/recompute`). Valid
    # credentials are enough to pin a worker on these today.
    rate_limit_expensive_per_minute: int = 5
    # Socket budget for a limiter round trip. Deliberately small: the check sits
    # on the blocking money path, so a Redis stall must degrade to fail-open fast
    # rather than adding its full timeout to every authorize().
    rate_limit_redis_timeout: float = 0.25
    # Max ratio a capture may settle over its authorized amount. The
    # policy verdict + budget reservation were computed on the authorized figure;
    # a true cost beyond this tolerance must be re-authorized, not silently
    # settled. 1.25 = allow up to 125% of the estimate.
    settlement_buffer: Decimal = Decimal("1.25")
    # How long an approved-but-uncaptured hold keeps reserving budget.
    # Past this, `reserved_spend` stops counting it, so a hold orphaned by a
    # vendor error or a crash between authorize and capture cannot shrink an
    # agent's budget forever. Expiry releases the *reservation* only — a capture
    # arriving later is still booked, because losing a real spend from the ledger
    # is worse than a stale forecast. Generous by default: the window must
    # comfortably cover the slowest legitimate authorize→capture gap.
    authorization_ttl_hours: int = 24
    # India s.195 withholding on payments to non-residents. PROVISIONAL —
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
    # --- Observability ----------------------------------------------------
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
    # Lifespan DB ping + "schema is at head" assertion. The suite turns
    # this off: the app under test is served sessions through
    # `dependency_overrides`, so a check against the module-level engine would be
    # asserting about a database the tests never touch. It is covered directly by
    # `tests/integration/test_startup_and_health.py` instead.
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
