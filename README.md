# LUCNT

**Financial infrastructure that attaches to your existing AI agents.**

[![CI](https://github.com/anushtup-nandy/lucnt/actions/workflows/ci.yml/badge.svg)](https://github.com/anushtup-nandy/lucnt/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

LUCNT gives agent spending a **gate** and a **ledger**:

- **Policy runs before money moves**, so a spend can be blocked, not just logged.
- **Every committed spend is booked** into double-entry journals with tax treatment applied.
- **One primitive for all spend**: a flight, a SaaS seat, or a batch of LLM tokens.
- **No `Agent` class.** Identity is a string handle, so it works with any framework.

LUCNT is self-hosted and not yet published to PyPI. Install it from source.

---

## How it works

```text
authorize()  ──▶  policy engine  ──▶  approved | blocked | review | human_hold
                                        │
                          your purchase happens here
                                        │
capture()    ──▶  commit transaction, then:
                    ├─ tax engine         IN GST reverse charge, US sales/use, SG
                    ├─ journal generator  double-entry against a chart of accounts
                    ├─ invoice hook       attach, or queue for matching
                    └─ events             agent.spend.created, budget.exhausted
```

Capture is source-agnostic: SDK, card-rail, and invoice spend all commit through the same path.

---

## Quickstart

**Prerequisites:** Python 3.12, Docker. Node 22 only for the dashboard.

```bash
git clone https://github.com/anushtup-nandy/lucnt.git
cd lucnt

# 1. Postgres, Redis, Mailpit
docker compose -f infra/docker-compose.yml up -d

# 2. Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # defaults match infra/docker-compose.yml
alembic upgrade head
python scripts/seed_dev.py        # prints a dev API key
uvicorn lucnt_api.main:app --reload --port 8000

# 3. SDK (new terminal, from the repo root)
pip install -e ./sdk
export LUCNT_API_KEY=<key printed by seed_dev.py>
```

The SDK defaults to `http://localhost:8000`. To install it in another project without cloning:

```bash
pip install "git+https://github.com/anushtup-nandy/lucnt.git#subdirectory=sdk"
```

---

## Usage

```python
import lucnt

lucnt.configure()  # reads LUCNT_API_KEY and LUCNT_BASE_URL

with lucnt.task(agent="travel-bot", entity="acme-in", intent="founder summit"):
    verdict = lucnt.authorize(
        vendor="United Airlines", amount="842.50", currency="USD", reason="SFO->EWR"
    )
    if verdict.approved:
        book_the_flight()              # your code, your payment rail
        tx = lucnt.capture(verdict)    # journal + tax are on tx
    else:
        print("blocked:", verdict.reason)
```

| Call | Purpose |
|---|---|
| `spend()` | `authorize` + `capture` in one call |
| `meter(client)` | Auto-meters an OpenAI or Anthropic client |
| `check_budget()` / `query()` | Let an agent read its own budget and spend history |
| `*_async` | An async twin for every call |

See [`sdk/README.md`](sdk/README.md) for the full SDK guide.

---

## Configuration

| Where | Variable | Notes |
|---|---|---|
| SDK | `LUCNT_API_KEY` | Required |
| SDK | `LUCNT_BASE_URL` | Default `http://localhost:8000`. Non-local hosts must be `https://` |
| Backend | `ENVIRONMENT` | `development` / `staging` / `production`. No default |
| Backend | `DATABASE_URL` | Postgres DSN |
| Backend | `SECRET_KEY` | ≥32 chars outside `development` |

[`backend/.env.example`](backend/.env.example) documents every other setting.

**Production notes**

- Keep `(DB_POOL_SIZE + DB_MAX_OVERFLOW) × workers` under Postgres `max_connections`.
- Behind Nginx, set `set_real_ip_from` / `real_ip_header`. Otherwise all clients share one rate-limit bucket.
- If Redis is unreachable, rate limiting fails open, so `authorize()` keeps working.

---

## Repository layout

| Path | Contents | Stack |
|---|---|---|
| `sdk/` | `lucnt` Python SDK | Python 3.10+, httpx, Pydantic v2 |
| `backend/` | `lucnt_api` service: policy, books, budgets, reconciliation | FastAPI, SQLAlchemy 2.0, Alembic, Postgres 16, Redis 7 |
| `frontend/` | Dashboard | React 19, TypeScript, Vite, Tailwind, TanStack Query |
| `infra/` | Docker Compose, Nginx, Prometheus alerts | |
| `contracts/` | OpenAPI schema and verdict codes shared by SDK and frontend | |

---

## Development

```bash
# Backend
cd backend && ruff check src/ tests/ scripts/ && mypy src/ scripts/ && pytest && alembic check

# SDK (fully mocked, no backend needed)
cd sdk && pip install -e ".[dev]" && ruff check . && mypy src/ && pytest

# Dashboard at http://localhost:5173
cd frontend && npm ci && npm run dev
npm run lint && npm run typecheck && npm run test && npm run build
```

CI ([`ci.yml`](.github/workflows/ci.yml)) also runs these checks:

- An Alembic upgrade and downgrade round-trip
- An API contract diff
- A gitleaks scan over the full history
- `pip-audit` against the hashed `backend/requirements.lock`
- A check that the container image runs as non-root

---

## Contributing

Issues and PRs are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md). Report vulnerabilities privately per [`SECURITY.md`](SECURITY.md).

## License

[Apache-2.0](LICENSE)
