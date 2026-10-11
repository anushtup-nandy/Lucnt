"""Structured logging: one structlog formatter for app and library logs.

`request_id` is bound via contextvars in the middleware, not threaded through.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from lucnt_api.config import settings

# Duplicates our own access log.
_SILENCED = ("uvicorn.access",)


def configure_logging() -> None:
    """Install the structlog + stdlib logging pipeline. Idempotent."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Applied to structlog and stdlib events alike.
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
            # Render in the stdlib formatter so both record kinds share a shape.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
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
    # Replace, not append: gunicorn and repeated test calls would stack handlers.
    root.handlers = [handler]
    root.setLevel(log_level)

    for name in _SILENCED:
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = False


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for `name`."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
