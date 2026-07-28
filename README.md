# LUCNT

> ⚠️ **Before publishing the SDK or starting a pilot, read [docs/production/plan2.md](docs/production/plan2.md)** —
> 27 open items compiled at Tier 5 close, split by which gate they block, with the six
> that need a decision listed first.

> Financial infrastructure that attaches to your existing agents — policy-gated spend
> (`authorize` → `capture`), live accounting, and recovery for AI agents that transact.

---

## Status

**SDK-first build.** The `lucnt` Python SDK is the product's front door and is built
contract-first alongside the backend; the dashboard is deferred to the last phase. See
[`docs/STATUS.md`](docs/STATUS.md) for the full done-vs-planned breakdown and
[`PLAN.md`](PLAN.md) for the phase-by-phase roadmap.

**Pre-pilot hardening: Tier 3 is complete (8/8 — T3-1…T3-8).** Tiers 0, 1, 1.5 and 2 of
[`docs/production/plan1.md`](docs/production/plan1.md) were already closed; Tier 3 ("scale,
performance & operability") adds hot-query indexes, DB-level uniqueness, an explicit connection
pool + statement timeout, one shared pagination contract, bounded request inputs, Redis-backed
rate limiting, and the removal of the full-ledger in-memory loads. Tiers 4–5 are open. Details in
[`CHANGELOG.md`](CHANGELOG.md).

| Component | State |
|---|---|
| `sdk/` (`lucnt` package) | `task()`/`authorize()`/`capture()`/`spend()` — done, green (unit-tested, no backend needed) |
| `backend/` | FastAPI app live (`authorize`→`capture`, books, budgets, policies, recon, intelligence); 33 migrations, head `e1f2a3b4c5d6` |
| `frontend/` | Not started — **next after Tier 5** (P7 was un-deferred 28 Jul 2026 and now runs before Phase 6) |

---

## Architecture

LUCNT attaches to your existing agents — it does not replace your framework or own the
"agent" noun. There is no `Agent` class. The SDK's universal primitive is
**`authorize()` → `capture()`**: policy gets a chance to block *before* money moves, then
the real spend is committed to the ledger. It works identically for a flight, a SaaS seat,
or a token batch.

```python
import lucnt
lucnt.configure()   # reads LUCNT_API_KEY / LUCNT_BASE_URL

with lucnt.task(agent="ops", entity="sg", intent="founder summit"):
    v = lucnt.authorize(vendor="singapore_airlines", amount=1240, currency="SGD",
                         reason="SIN->SFO, premium fare")
    if v.approved:
        book_flight(...)                    # your own purchase / payment
        tx = lucnt.capture(v)               # commit the real spend to the ledger
```

Full design docs: [`PLAN.md`](PLAN.md) (master plan), [`sdk/PLAN.md`](sdk/PLAN.md),
[`backend/PLAN.md`](backend/PLAN.md), [`infra/PLAN.md`](infra/PLAN.md).

---

## Tech Stack

| Layer | Stack |
|---|---|
| **SDK** (`lucnt`, ★ primary deliverable) | Python 3.10+, httpx, Pydantic v2 |
| **Backend** | Python 3.10+, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2 |
| **Database** | PostgreSQL 16 (Docker) |
| **Rate limiting** | Redis 7 — a **runtime dependency** of the backend (`redis>=5.0` in `backend/pyproject.toml`), not just a compose service |
| **Frontend** (P7 — next after Tier 5) | React 18, TypeScript, Vite, Tailwind CSS |

---

## Quick Start (local dev)

### Prerequisites
- Python 3.10+
- Docker (for Postgres/Redis/MailHog)
- **Redis** — rate limiting is on by default (`RATE_LIMIT_ENABLED=true`) and its counters live in
  Redis, so it must be reachable at `REDIS_URL` for limits to be enforced. An unreachable Redis
  **fails open**: the request is admitted and a warning is logged, because a cache blip must not
  take down `authorize()`. The backend test suite therefore still passes without Redis — 11
  rate-limit tests skip instead of failing.

### ⚠️ Use separate environments for `backend/` and `sdk/`

Both packages install a top-level Python module literally named `lucnt` (that's the
published package name — see `sdk/PLAN.md`). In *one shared* environment, whichever
installs second silently shadows the other's `import lucnt`. This never happens in
production (the SDK installs into a *customer's* process; the backend runs standalone) —
it's purely a local-dev footgun. Give each its own venv/conda env.

### 1. Bring up infrastructure

```bash
cd infra
docker compose up -d       # Postgres :5432, Redis :6379, MailHog(mailpit) :8025
```

### 2. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # separate env from sdk/, see warning above
pip install -e ".[dev]"
cp .env.example .env                                 # defaults already match docker compose
alembic upgrade head
uvicorn lucnt_api.main:app --reload --port 8000
```

To run the API as a container instead (the deployable artifact, T4-1):

```bash
cd infra && docker compose --profile backend up -d --build   # gunicorn, 2 workers, non-root
curl localhost:8000/health/ready                             # {"status":"ready", …}
```

`ENVIRONMENT` is **required** and must be one of `development` / `staging` / `production` — it no
longer defaults, because a default of `development` meant an unset value in production published
`/docs`, `/redoc` and `/openapi.json`. API docs are served only when `ENVIRONMENT=development`. To
inspect the migrated schema directly:

```bash
docker exec infra-postgres-1 psql -U postgres -d lucnt -c "\dt"
```

### 3. SDK

```bash
cd sdk
python -m venv .venv && source .venv/bin/activate   # separate env from backend/, see warning above
pip install -e ".[dev]"
pytest   # fully mocked -- no backend or network needed
```

`sdk/examples/example_agent.py` is the SDK's E2E acceptance driver and needs a live backend
(step 2) with a valid `LUCNT_API_KEY`; without one it fails with a connection error.

### 4. Frontend

Not started. **Next up after Tier 5** — P7 was un-deferred on 28 Jul 2026 and now runs ahead of
Phase 6. See [`frontend/PLAN.md`](frontend/PLAN.md).

---

## Configuration

All backend settings are read from the environment / `backend/.env` by
`backend/src/lucnt_api/config.py` (Pydantic Settings). `backend/.env.example` is the canonical
template and its defaults already match `infra/docker-compose.yml`.

`DATABASE_URL` and `SECRET_KEY` are **required**; everything below has a default and only needs
setting to override it.

**Connection pool & timeouts** (added in Tier 3, T3-4)

| Variable | Default | Purpose |
|---|---|---|
| `DB_POOL_SIZE` | `10` | Persistent connections per worker process |
| `DB_MAX_OVERFLOW` | `10` | Extra connections above `DB_POOL_SIZE` under burst |
| `DB_POOL_TIMEOUT` | `10` | Seconds to wait for a free connection before failing fast |
| `DB_POOL_RECYCLE` | `1800` | Recycle a connection after this many seconds, below any proxy/Postgres idle reaper |
| `DB_CONNECT_TIMEOUT` | `10` | TCP connect timeout for a new connection (libpq `connect_timeout`) |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | Server-side ceiling on any single statement; a runaway query is killed by Postgres rather than pinning a pool connection |

Two sizing rules follow from this. Total connections are `(DB_POOL_SIZE + DB_MAX_OVERFLOW) ×
worker count`, which must stay well under Postgres `max_connections` (100 by default). And
because every route is a sync `def`, `main.py` pins AnyIO's worker-thread limiter to
`DB_POOL_SIZE + DB_MAX_OVERFLOW` — so raising the pool raises request concurrency too, and the
two can never disagree.

**Rate limiting** (added in Tier 3, T3-7)

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Where the shared counters live |
| `RATE_LIMIT_ENABLED` | `true` | Kill switch. Turning the feature *off* deliberately — not how you survive a Redis outage (that is already fail-open) |
| `RATE_LIMIT_DEFAULT_PER_MINUTE` | `600` | Ordinary authenticated calls, per API key |
| `RATE_LIMIT_AUTH_FAILURE_PER_MINUTE` | `20` | Failed authentications, per client IP |
| `RATE_LIMIT_EXPENSIVE_PER_MINUTE` | `5` | Ledger-walking endpoints (`/recon/scan`, `/intelligence/recompute`), per API key |
| `RATE_LIMIT_REDIS_TIMEOUT` | `0.25` | Seconds a limiter round-trip may take before failing open |

> **Deployment requirement.** The `auth_failure` bucket is keyed on the client IP taken from the
> socket. Once nginx fronts the app it **must** set `set_real_ip_from` / `real_ip_header`, or every
> client collapses into one bucket and this becomes a lockout for all of them. See
> [`infra/PLAN.md`](infra/PLAN.md) §5.

---

## Project Structure

```
lucnt/
├── sdk/         # ★ primary deliverable — the `lucnt` Python SDK
├── backend/     # FastAPI application (DB schema done; app not started)
├── frontend/    # React SPA — P7, next after Tier 5; no code yet
├── infra/       # docker-compose.yml, Nginx config
├── docs/        # Design docs, status tracking, production-hardening plan
└── logs/        # Daily session logs
```

---

## Verification

**SDK:** `ruff` + `mypy` clean, **332 tests** green.
```bash
cd sdk
ruff check src/ tests/
mypy src/
pytest
```

**Backend:** `ruff` clean, `mypy` clean over **75 source files**, **395 tests** green, and
`alembic check` reporting no drift between the models and the migrations. The same gate runs on every
push/PR (`.github/workflows/ci.yml`), plus a migration round-trip, secret and dependency scans, and an
image build asserting the container is non-root.
```bash
cd backend
ruff check src/ tests/
mypy src/
pytest          # requires `alembic upgrade head` against a running Postgres first
alembic check   # must report "No new upgrade operations detected."
```

With Redis down the suite is still green — 11 rate-limit tests skip rather than fail, so CI
without Redis is not a false failure.

---

## License

MIT