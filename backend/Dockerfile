# syntax=docker/dockerfile:1.7
# LUCNT backend: non-root, no build toolchain at runtime. Python 3.12 matches
# requires-python and CI; re-pin digests with `docker buildx imagetools inspect`.

# Stage 1: build the virtualenv.
FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Needed to build psycopg2 against system libpq; never shipped.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Hashed lock first (layer cache); hashes make pip verify every artifact.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Source-build psycopg2 (lock's version) so OpenSSL patches come from Debian, not a bundled wheel.
RUN PSYCOPG2_VERSION="$(sed -n 's/^psycopg2-binary==\([0-9.]*\).*/\1/p' requirements.lock)" \
    && pip uninstall -y psycopg2-binary \
    && pip install --no-cache-dir --no-binary :all: "psycopg2==${PSYCOPG2_VERSION}" \
    # Assert it linked the system libpq.
    && python - <<'PY'
import subprocess, psycopg2._psycopg as m
out = subprocess.run(["ldd", m.__file__], capture_output=True, text=True).stdout
assert "libpq.so" in out, f"psycopg2 is not dynamically linked against libpq:\n{out}"
print("psycopg2 links system libpq:", [ln.strip() for ln in out.splitlines() if "libpq" in ln])
PY

COPY pyproject.toml README.md* ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# Stage 2: runtime.
FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Aggregate metrics across gunicorn workers.
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus \
    # 2 × (10 + 10) pool = 40 DB connections; see gunicorn.conf.py.
    WEB_CONCURRENCY=2

RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Non-root with fixed uid/gid for predictable volume ownership.
RUN groupadd --system --gid 10001 lucnt \
    && useradd --system --uid 10001 --gid lucnt --no-create-home lucnt

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Readiness finds alembic/ via Path.cwd(), so WORKDIR must stay /app. src/ is not
# copied: the package is in /opt/venv, and a second copy would shadow it.
COPY --chown=lucnt:lucnt alembic ./alembic
COPY --chown=lucnt:lucnt alembic.ini gunicorn.conf.py ./

RUN install -d -o lucnt -g lucnt /tmp/prometheus

USER lucnt
EXPOSE 8000

# Liveness, not readiness: a DB blip must not cause a restart storm.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"]

CMD ["gunicorn", "--config", "gunicorn.conf.py", "lucnt_api.main:app"]
