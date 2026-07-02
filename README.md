# LUCNT

> Financial infrastructure that attaches to your existing agents — policy-gated spend
> (`authorize` → `capture`), live accounting, and recovery for AI agents that transact.

---

## Status

**SDK-first build.** The `lucnt` Python SDK is the product's front door and is built
contract-first alongside the backend; the dashboard is deferred to the last phase. See
[`docs/STATUS.md`](docs/STATUS.md) for the full done-vs-planned breakdown and
[`PLAN.md`](PLAN.md) for the phase-by-phase roadmap.

| Component | State |
|---|---|
| `sdk/` (`lucnt` package) | `task()`/`authorize()`/`capture()`/`spend()` — done, green (unit-tested, no backend needed) |
| `backend/` | DB schema + first migration done (`companies`/`entities`/`users`); no FastAPI app yet |
| `frontend/` | Not started — deferred to Phase 7 |

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
| **Frontend** (deferred to Phase 7) | React 18, TypeScript, Vite, Tailwind CSS |

---

## Quick Start (local dev)

### Prerequisites
- Python 3.10+
- Docker (for Postgres/Redis/MailHog)

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
```

The FastAPI app itself (`main.py`, routers) isn't built yet — see
[`docs/STATUS.md`](docs/STATUS.md). Today, `backend/` gets you a migrated schema you can
inspect directly:

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

`sdk/examples/example_agent.py` is the SDK's E2E acceptance driver, but it needs the
backend's `/transactions/authorize` + `/transactions/capture` endpoints, which don't exist
yet — it will fail with a connection error until then. That's expected, not a bug.

### 4. Frontend

Not started. Deferred to Phase 7 — see [`frontend/PLAN.md`](frontend/PLAN.md).

---

## Project Structure

```
lucnt/
├── sdk/         # ★ primary deliverable — the `lucnt` Python SDK
├── backend/     # FastAPI application (DB schema done; app not started)
├── frontend/    # React SPA — deferred to Phase 7, no code yet
├── infra/       # docker-compose.yml, Nginx config
├── docs/        # Design docs, status tracking, production-hardening plan
└── logs/        # Daily session logs
```

---

## Verification

**SDK:**
```bash
cd sdk
ruff check src/ tests/
mypy src/
pytest
```

**Backend:**
```bash
cd backend
ruff check src/ tests/
mypy src/
pytest   # requires `alembic upgrade head` against a running Postgres first
```

---

## License

MIT
