"""Structured logging (T4-4).

`structlog` was declared in `pyproject.toml` from the first commit and never
imported: until now the backend emitted **no logs at all** beyond the one
`logging.getLogger` in `rate_limit.py`. A "my agent got blocked at 2am" report
had no forensic artifact other than database rows.

Two rules shape this module:

* **One stream, one format.** Application logs, `uvicorn`'s, SQLAlchemy's and
  Alembic's all pass through the same `structlog` formatter, so a log shipper
  never has to parse two syntaxes out of one stdout. JSON off a deployment,
  human-readable colour on a laptop (`Settings.resolved_log_format`).
* **Request context is ambient.** `bind_contextvars` in the middleware means
  every line emitted while serving a request carries its `request_id` without a
  single call site having to thread it through — which is the only version of
  this that survives contact with 70 source files.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from lucnt_api.config import settings

# Loggers that would otherwise double-report what our own access log already
# says, one line per request, in a second format.
_SILENCED = ("uvicorn.access",)


def configure_logging() -> None:
    """Install the structlog + stdlib logging pipeline. Idempotent."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Processors applied to *every* event, whether it originated in structlog or
    # in the stdlib. `merge_contextvars` is what injects `request_id`.
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.resolved_log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            *shared,
            # Hands off to the stdlib formatter below rather than rendering here,
            # so structlog and stdlib records cannot drift apart in shape.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # `format_exc_info` must run only for stdlib records; structlog events
        # arriving via `wrap_for_formatter` already carry a rendered exception.
        foreign_pre_chain=[*shared, structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace rather than append: `configure_logging()` runs once per process in
    # normal operation, but gunicorn's own logging setup and repeated calls from
    # tests would otherwise stack duplicate handlers on the root logger.
    root.handlers = [handler]
    root.setLevel(log_level)

    for name in _SILENCED:
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = False


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for `name`."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
