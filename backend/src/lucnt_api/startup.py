"""Database readiness: connectivity plus schema-at-head (T4-5/T4-12).

One function serves two callers with deliberately different reactions to the
same failure:

* the **lifespan** refuses to finish starting — a process that cannot reach its
  database, or that is running against a schema older than its models, should
  never reach a load balancer's healthy pool in the first place;
* **`GET /health/ready`** reports it as a 503 — by then the process is already
  serving, and the useful behaviour is to be taken *out* of rotation rather than
  to die and take its in-flight requests with it.

The migration check is the half that matters in practice. A deploy that ships new
model code against an un-migrated database does not fail at startup today; it
fails later, on the first request that touches the new column, as a 500 from deep
inside a router.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lucnt_api.logging_config import get_logger

logger = get_logger(__name__)

# Escape hatch for layouts this module's search cannot guess (a non-editable
# install whose working directory is not the project root, say).
_SCRIPT_LOCATION_ENV = "ALEMBIC_SCRIPT_LOCATION"


@dataclass(frozen=True)
class ReadinessReport:
    """Outcome of a readiness probe."""

    database_reachable: bool
    schema_at_head: bool | None
    """`None` when the migration scripts could not be located to compare against."""
    current_revision: str | None
    expected_head: str | None
    detail: str

    @property
    def ready(self) -> bool:
        """True when nothing found is a reason to withhold traffic."""
        return self.database_reachable and self.schema_at_head is not False


def _script_location() -> Path | None:
    """Locate the Alembic scripts directory, or `None` if it isn't shipped."""
    override = os.environ.get(_SCRIPT_LOCATION_ENV)
    candidates = (
        [Path(override)]
        if override
        else [
            # Source checkout / editable install: src/lucnt_api → backend/alembic.
            Path(__file__).resolve().parents[2] / "alembic",
            # Installed into site-packages but run from the project root.
            Path.cwd() / "alembic",
        ]
    )
    return next((c for c in candidates if (c / "versions").is_dir()), None)


def _expected_head() -> str | None:
    """The head revision of the migration scripts on disk, if they are present."""
    location = _script_location()
    if location is None:
        return None
    config = Config()
    config.set_main_option("script_location", str(location))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        # Multiple heads means an un-merged branch point; refusing to guess is
        # better than silently comparing against an arbitrary one.
        raise RuntimeError(f"expected exactly one Alembic head, found {heads!r}")
    return heads[0]


def check_readiness(db: Session) -> ReadinessReport:
    """Ping the database and compare its schema revision against the code's head.

    Takes a `Session` rather than an `Engine` so `GET /health/ready` can be served
    through the ordinary `get_db` dependency. That matters twice: the probe then
    exercises the same connection path as every real route (a pool that cannot
    hand out a connection is exactly what readiness should catch), and it honours
    `dependency_overrides`, so the suite's probe reports on the database the suite
    is actually bound to.
    """
    try:
        db.execute(text("SELECT 1"))
        current = db.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
    except SQLAlchemyError as exc:
        # The message can carry the DSN, so it is logged rather than returned to
        # an unauthenticated prober; `detail` stays deliberately vague.
        logger.error("readiness_database_unreachable", error=str(exc))
        return ReadinessReport(
            database_reachable=False,
            schema_at_head=None,
            current_revision=None,
            expected_head=None,
            detail="database unreachable",
        )

    expected = _expected_head()
    if expected is None:
        logger.warning("readiness_migrations_not_found", searched=_SCRIPT_LOCATION_ENV)
        return ReadinessReport(
            database_reachable=True,
            schema_at_head=None,
            current_revision=current,
            expected_head=None,
            detail="database reachable; migration scripts not found, head unverified",
        )

    at_head = current == expected
    return ReadinessReport(
        database_reachable=True,
        schema_at_head=at_head,
        current_revision=current,
        expected_head=expected,
        detail=(
            "ready"
            if at_head
            else f"schema at {current or 'no revision'}, expected {expected}"
        ),
    )


def verify_ready_or_raise(db: Session) -> ReadinessReport:
    """Readiness check for the lifespan: log the outcome, raise if not ready."""
    report = check_readiness(db)
    if not report.ready:
        raise RuntimeError(f"startup readiness check failed: {report.detail}")
    logger.info(
        "startup_readiness_ok",
        current_revision=report.current_revision,
        expected_head=report.expected_head,
        head_verified=report.schema_at_head is True,
    )
    return report
