# Contributing to LUCNT

Thanks for taking the time. Issues and pull requests are both welcome.

## Before you start

- **Bug reports** — open an issue with what you did, what happened, and what you
  expected. A failing test is the best possible bug report.
- **Features** — open an issue to discuss it first. LUCNT sits in the money path,
  so the bar for new surface area in `authorize` / `capture` is deliberately high.
- **Security issues** — do not open an issue. See [SECURITY.md](SECURITY.md).

## Setup

See [Running locally](README.md#running-locally) in the README. Short version:

```bash
cd infra && docker compose up -d        # Postgres, Redis, Mailpit

cd backend && python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" && cp .env.example .env && alembic upgrade head

cd ../sdk && python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cd ../frontend && npm ci
```

Use a separate virtualenv for `backend/` and `sdk/`. They are two distinct
packages with different Python floors (3.12 and 3.10) and different dependency
sets; one shared environment will resolve to something neither of them asked for.

## The gate

Every one of these runs in CI on each push and PR. Run them locally first — it is
much faster than a red pipeline.

```bash
# backend
cd backend
ruff check src/ tests/ scripts/ && ruff format --check src/ tests/ scripts/
mypy src/ scripts/
pytest
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

CI additionally runs an Alembic upgrade → downgrade → upgrade round-trip, a
secret scan over the full history, a dependency audit against the hashed
lockfile, and an image build that asserts the container does not run as root.

## House rules

**Python.** Strict type hints, `from __future__ import annotations`, no bare
`Any` without a reason. Ruff for lint and format, line length 88. Google-style
docstrings on public APIs. Never silence an exception — log it at `warning` or
`error`.

**Money is `Decimal`, never `float`,** everywhere, including the database
(`NUMERIC`). This is not negotiable and there is no case where a float is close
enough.

**TypeScript.** Strict mode, no `any`. Functional components, one per file.
Server state via TanStack Query, client state via Zustand. Tailwind for styling.

**Comments explain *why*, not *what*.** The code already says what it does. A
comment earns its place by recording a decision, a constraint, or a trap — the
reason a line is the way it is rather than the obvious alternative.

## Changing the API contract

`contracts/` (via `backend/scripts/export_contract.py`) and the frontend's
generated `src/types/api.ts` are **gated by CI**. If you change a route, schema,
or verdict code, regenerate them in the same commit:

```bash
cd backend && python scripts/export_contract.py
cd ../frontend && npm run gen:types
```

A PR that changes the API without regenerating these will go red.

## Migrations

One migration per logical schema change, named descriptively. Every migration
must be reversible — CI runs `downgrade base` and back up. After adding one,
`alembic check` must report no new operations, which means the models and the
migration agree.

## Commits and pull requests

- Format: `type(scope): description` — e.g. `feat(backend): add capture idempotency key`
- One logical change per commit; keep the PR reviewable
- Say what changed and why. If it fixes an issue, link it.
- New behaviour needs a test. A bug fix needs the test that would have caught it.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
