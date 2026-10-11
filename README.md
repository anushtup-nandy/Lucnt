# LUCNT

**Financial infrastructure that attaches to your existing AI agents.**

[![CI](https://github.com/anushtup-nandy/lucnt/actions/workflows/ci.yml/badge.svg)](https://github.com/anushtup-nandy/lucnt/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

Give an agent's spending a **gate** and a **ledger**. LUCNT sits in the money path: policy runs
*before* the charge, so it can block; the committed spend is booked into double-entry journals with
tax treatment applied. It works the same for a flight, a SaaS seat, or a batch of LLM tokens.

LUCNT does **not** own the "agent" noun. There is no `Agent` class competing with your framework —
identity is a plain string handle you set once.

---

## Quickstart

```bash
pip install lucnt
```

```python
import lucnt

lucnt.configure()   # reads LUCNT_API_KEY; LUCNT_BASE_URL defaults to http://localhost:8000

with lucnt.task(agent="travel-bot", entity="acme-in", intent="founder summit"):
    verdict = lucnt.authorize(
        vendor="United Airlines",
        amount="842.50",
        currency="USD",
        reason="SFO->EWR, 12 Aug",
    )

    if not verdict.approved:
        print(f"blocked: {verdict.reason}")   # policy said no — nothing was charged
    else:
        book_the_flight()                     # your code, your payment rail
        tx = lucnt.capture(verdict)           # commit the real spend to the ledger
```

`spend()` is the atomic `authorize` + `capture` convenience when you don't need a gap between the
two. Async variants (`authorize_async`, `capture_async`, …) mirror every call.

---

## How it works

```
lucnt.authorize()  ──▶  policy engine  ──▶  approved | blocked | review | human_hold
                                              │
                              your purchase happens here
                                              │
lucnt.capture()    ──▶  transaction committed, then synchronously:
                          ├─ tax engine        (IN GST reverse-charge, US sales/use, SG)
                          ├─ journal generator (double-entry against a chart of accounts)
                          ├─ invoice hook      (attach, or queue for matching)
                          └─ events            (agent.spend.created, budget.exhausted)
```

**Authorization runs before money moves.** That is the whole point — it's the only place a spend can
actually be stopped rather than merely recorded after the fact.

**Capture is source-agnostic.** Spend also enters from a card/wallet rail or from invoice ingestion,
and commits through the same path, so journals, tax and analytics never care where it came from.

Two more SDK reads let an agent self-regulate: `check_budget()` (returns `reserved` / `available`)
and `query()`. `meter(client)` auto-instruments an OpenAI or Anthropic client when you want token
spend booked without writing the calls yourself.

---

## Repository layout

| Path | What it is | Stack |
|---|---|---|
| `sdk/` | The `lucnt` Python package — the product's front door | Python 3.10+, httpx, Pydantic v2 |
| `backend/` | FastAPI service: authorize/capture, books, budgets, policies, reconciliation, intelligence | Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, Postgres 16, Redis 7 |
| `frontend/` | Dashboard SPA — agents, transactions, books, budgets, policies, approvals, recon, insights | React 19, TypeScript 5.9, Vite 8, Tailwind 4, TanStack Query v5 |
| `infra/` | Docker Compose, Nginx, Prometheus alerts | |

The SDK installs as `lucnt`; the backend package is `lucnt_api`. They do not collide.

---

## Running locally

### Prerequisites
- Python 3.12 (backend) — the SDK supports 3.10+
- Node 22 (frontend)
- Docker, for Postgres / Redis / Mailpit

### 1. Infrastructure

```bash
cd infra
docker compose up -d       # Postgres :5432, Redis :6379, Mailpit :8025
```

### 2. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # defaults already match infra/docker-compose.yml
alembic upgrade head
python scripts/seed_dev.py    # demo company, agents, logins; prints one API key
uvicorn lucnt_api.main:app --reload --port 8000
```

API docs are served at `/docs` only when `ENVIRONMENT=development`.

To run the API as its deployable container instead:

```bash
cd infra && docker compose --profile backend up -d --build
curl localhost:8000/health/ready
```

### 3. SDK

```bash
cd sdk
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                      # fully mocked — no backend or network needed
```

Point it at the backend — `http://localhost:8000` is the default, so locally only the key is
needed:

```bash
export LUCNT_API_KEY=lucnt_live_...                # printed by seed_dev.py
export LUCNT_BASE_URL=https://lucnt.example.com    # only when not on localhost
```

`sdk/examples/example_agent.py` is the end-to-end acceptance driver and needs a live backend plus a
valid `LUCNT_API_KEY`.

### 4. Frontend

```bash
cd frontend
npm ci
npm run dev                 # http://localhost:5173
```

---

## Configuration

Backend settings are read from the environment or `backend/.env` by `lucnt_api/config.py`
(Pydantic Settings). **`backend/.env.example` is the canonical, commented template** and its
defaults already match `infra/docker-compose.yml`.

Three variables are required and have no default:

| Variable | Notes |
|---|---|
| `ENVIRONMENT` | One of `development` / `staging` / `production`. Deliberately has no default — an unset value must never silently publish `/docs` in production. |
| `DATABASE_URL` | Postgres DSN |
| `SECRET_KEY` | Signs dashboard JWTs. Outside `development` it must be ≥32 chars and must not be the committed example value. |

Everything else has a working default. The two groups worth knowing about are the connection pool
(`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_STATEMENT_TIMEOUT_MS`, …) and rate limiting
(`RATE_LIMIT_ENABLED`, `RATE_LIMIT_DEFAULT_PER_MINUTE`, …); both are documented inline in
`.env.example`.

Two things that will bite you if you skip them:

- **Total DB connections are `(DB_POOL_SIZE + DB_MAX_OVERFLOW) × workers`** and must stay well under
  Postgres `max_connections`. Because every route is a sync `def`, `main.py` pins AnyIO's
  worker-thread limiter to the same total, so raising the pool raises request concurrency with it and
  the two can never disagree.
- **Rate limiting is Redis-backed and keyed on client IP for auth failures.** Once Nginx fronts the
  app it *must* set `set_real_ip_from` / `real_ip_header`, or every client collapses into one bucket
  and the limiter becomes a lockout for all of them. An unreachable Redis **fails open** — the
  request is admitted and a warning logged, because a cache blip must not take down `authorize()`.

---

## Development

Every command below runs on each push and PR — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) for the authoritative gate, which also covers
an Alembic upgrade/downgrade round-trip, schema-drift detection, secret scanning over full history,
a hashed-lockfile dependency audit, and an image build asserting the container runs non-root.

```bash
# backend
cd backend
ruff check src/ tests/ scripts/ && ruff format --check src/ tests/ scripts/
mypy src/ scripts/
alembic upgrade head && pytest
alembic check                    # must report no new operations

# sdk
cd sdk
ruff check . && ruff format --check .
mypy src/
pytest

# frontend
cd frontend
npm run lint && npm run typecheck && npm run test && npm run build
```

The SDK is tested against Python 3.10 and 3.12 in CI — 3.10 is the floor the published package
promises, 3.12 matches the backend runtime.

With Redis down the backend suite is still green: the rate-limit tests skip rather than fail, so a
CI environment without Redis is not a false failure.

---

## Contributing

Issues and pull requests are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup and the
review checklist, and [`SECURITY.md`](SECURITY.md) to report a vulnerability privately.

## License

[Apache License 2.0](LICENSE).
