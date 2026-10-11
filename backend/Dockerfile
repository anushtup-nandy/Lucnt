# syntax=docker/dockerfile:1.7
#
# LUCNT backend — deployable image.
#
# Before this existed there was no deployable artifact at all: "deployment" meant
# a laptop running `uvicorn` against a compose Postgres with the password
# `postgres`. Three properties are non-negotiable here and each is enforced below:
# the image runs as a non-root user, it carries no build toolchain into
# production, and it declares its own health.
#
# PYTHON VERSION: pinned to 3.12 to match `requires-python`, the CI backend job
# and the regenerated lockfile — bumped from 3.10 in one commit, exactly
# as the previous version of this comment demanded. The SDK keeps a 3.10 floor
# (it is a customer-facing library); only this application moved.
#
# DIGEST PINNING: the digest below is the manifest-list digest, valid
# across architectures. Re-pin deliberately on upgrades with:
#
#     docker buildx imagetools inspect python:<tag>
#
# and update the `@sha256:…` suffix — the tag stays for humans, the digest is
# what the build actually gets.

# ---------------------------------------------------------------------------
# Stage 1 — builder: compile wheels into a self-contained virtualenv.
# ---------------------------------------------------------------------------
FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# `libpq-dev` + `gcc` are needed to build psycopg against the system libpq and are
# the reason this stage exists: none of it ships in the runtime image.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# The lockfile alone first, so a source-only change reuses this (slow) layer.
# `--require-hashes` is implied by the hashes in the file: every artifact is
# verified, so a compromised or substituted wheel fails the build instead of
# shipping. This is what makes the image reproducible — `pip install .` alone
# re-resolves on every build and can silently pick up a new minor version.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

# Swap the binary psycopg2 wheel for a source build. `psycopg2-binary`
# statically bundles its own libssl/libcrypto, which `apt-get upgrade` cannot
# patch — an OpenSSL CVE would then require upstream to cut a new wheel before
# this image could be fixed. Building against the base image's libpq means a
# rebuild picks up Debian's security updates. The version is taken from the
# lockfile so the two cannot drift.
RUN PSYCOPG2_VERSION="$(sed -n 's/^psycopg2-binary==\([0-9.]*\).*/\1/p' requirements.lock)" \
    && pip uninstall -y psycopg2-binary \
    && pip install --no-cache-dir --no-binary :all: "psycopg2==${PSYCOPG2_VERSION}" \
    # Prove it linked the system libpq rather than reinstalling a bundled wheel;
    # a silent fallback here would defeat the entire point of this stage.
    && python - <<'PY'
import subprocess, psycopg2._psycopg as m
out = subprocess.run(["ldd", m.__file__], capture_output=True, text=True).stdout
assert "libpq.so" in out, f"psycopg2 is not dynamically linked against libpq:\n{out}"
print("psycopg2 links system libpq:", [ln.strip() for ln in out.splitlines() if "libpq" in ln])
PY

# The application itself, installed without touching the locked dependency set.
COPY pyproject.toml README.md* ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# ---------------------------------------------------------------------------
# Stage 2 — runtime: the venv, the app, and nothing that can compile code.
# ---------------------------------------------------------------------------
FROM python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Aggregate metrics across gunicorn workers rather than reporting whichever
    # worker the scrape happened to land on (see `metrics.py`).
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus \
    # Two workers × (10 + 10) pool = 40 connections. See `gunicorn.conf.py` for
    # why this is not `2 × CPU + 1`.
    WEB_CONCURRENCY=2

# `libpq5` is the runtime half of `libpq-dev`; psycopg needs it and nothing else
# from the builder stage is required.
RUN apt-get update \
    && apt-get install --no-install-recommends -y libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Non-root, fixed uid/gid so a mounted volume's ownership is predictable across
# hosts. A financial API has no business running as root.
RUN groupadd --system --gid 10001 lucnt \
    && useradd --system --uid 10001 --gid lucnt --no-create-home lucnt

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# `alembic/` and `alembic.ini` are shipped on purpose: migrations run from this
# same image (one artifact, one schema definition), and the readiness probe reads
# the scripts to compare the live schema against the expected head — it finds
# them via `Path.cwd()/"alembic"`, which is why WORKDIR must stay /app.
#
# `src/` is deliberately NOT copied here: the package is installed into
# /opt/venv, so a second copy at /app/src would be dead weight that is also on
# gunicorn's sys.path — two candidate copies of the money path in one image, and
# no way to tell from the outside which one is serving.
COPY --chown=lucnt:lucnt alembic ./alembic
COPY --chown=lucnt:lucnt alembic.ini gunicorn.conf.py ./

RUN install -d -o lucnt -g lucnt /tmp/prometheus

USER lucnt
EXPOSE 8000

# Liveness only — deliberately not `/health/ready`. A readiness failure means
# "stop sending me traffic"; wiring it here would mean "kill me", turning one
# database blip into a container restart storm. Uses stdlib urllib so the runtime
# image needs no curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"]

CMD ["gunicorn", "--config", "gunicorn.conf.py", "lucnt_api.main:app"]
