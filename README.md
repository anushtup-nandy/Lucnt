# LUCNT

> AI-driven spend capture, policy enforcement, and automated accounting for modern companies.

---

## Tech Stack

- **Frontend**: React 18, TypeScript, Vite, Tailwind CSS, Zustand, TanStack Query
- **Backend**: Python 3.11+, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2
- **Database**: PostgreSQL 15+

---

## Quick Start

### Prerequisites
- Python 3.11+
- Poetry (`pip install poetry`)
- Node.js 20+ & npm
- PostgreSQL 15+

### 1. Clone & setup database

```bash
git clone <repo-url> && cd lucnt
createdb lucnt
```

### 2. Backend

```bash
cd backend
cp .env.example .env   # edit DATABASE_URL if needed
poetry install
poetry run alembic upgrade head
poetry run uvicorn src.lucnt.main:app --reload --port 8000
```

API docs: `http://localhost:8000/docs`

### 3. Frontend

```bash
cd frontend
cp .env.example .env   # edit VITE_API_URL if needed
npm install
npm run dev
```

App: `http://localhost:5173`

---

## Project Structure

```
lucnt/
├── backend/     # FastAPI application
├── frontend/    # React SPA
├── docs/        # Design mockups & production plans
├── infra/       # Infrastructure documentation
└── logs/        # Daily session logs
```

---

## Verification

**Backend:**
```bash
cd backend
poetry run ruff check src/
poetry run mypy src/
poetry run pytest
```

**Frontend:**
```bash
cd frontend
npm run lint
npm run test
```

---

## License

MIT
