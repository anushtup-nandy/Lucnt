"""SDK-wide configuration: `configure()` and the active config singleton."""

from __future__ import annotations

import os

from lucnt.exceptions import ConfigurationError
from lucnt.models import LucntConfig

_DEFAULT_BASE_URL = "https://api.lucnt.dev"
_DEFAULT_TIMEOUT = 30.0

_active_config: LucntConfig | None = None


def configure(
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> LucntConfig:
    """Configure the SDK's API key, base URL, and transport timeout.

    Falls back to the `LUCNT_API_KEY` / `LUCNT_BASE_URL` environment
    variables when the corresponding argument is omitted. Call this once
    before any other `lucnt` call in the process.

    Args:
        api_key: Live or sandbox API key. Falls back to `LUCNT_API_KEY`.
        base_url: Backend base URL. Falls back to `LUCNT_BASE_URL`, then a
            built-in default.
        timeout: Per-request timeout in seconds.

    Returns:
        The resolved, now-active `LucntConfig`.

    Raises:
        ConfigurationError: No API key was provided or found in the
            environment.
    """
    global _active_config

    resolved_key = api_key or os.environ.get("LUCNT_API_KEY")
    if not resolved_key:
        raise ConfigurationError(
            "No API key provided. Pass configure(api_key=...) or set "
            "the LUCNT_API_KEY environment variable."
        )
    resolved_base_url = (
        base_url or os.environ.get("LUCNT_BASE_URL") or _DEFAULT_BASE_URL
    )

    _active_config = LucntConfig(
        api_key=resolved_key, base_url=resolved_base_url, timeout=timeout
    )
    return _active_config


def get_config() -> LucntConfig:
    """Return the active configuration set by `configure()`.

    Raises:
        ConfigurationError: `configure()` has not been called yet.
    """
    if _active_config is None:
        raise ConfigurationError(
            "lucnt.configure() must be called before making any lucnt calls."
        )
    return _active_config


def _reset_config() -> None:
    """Clear the active configuration singleton. Test-only."""
    global _active_config
    _active_config = None
