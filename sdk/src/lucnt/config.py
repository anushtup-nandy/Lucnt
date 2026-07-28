"""SDK-wide configuration: `configure()` and the active config singleton."""

from __future__ import annotations

import os
from typing import TypeVar
from urllib.parse import urlparse

from pydantic import SecretStr, ValidationError

from lucnt.exceptions import ConfigurationError
from lucnt.models import LucntConfig

_DEFAULT_BASE_URL = "https://api.lucnt.dev"
_DEFAULT_TIMEOUT = 30.0
# Safety control, not a knob (T5-12): http:// is permitted only against these
# hosts. Exposing this to configuration would be a switch that disables TLS.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_active_config: LucntConfig | None = None

_T = TypeVar("_T")

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})


def _env_float(name: str) -> float | None:
    """Read a float env var, or None if unset. Raises on an unparseable value."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name}={raw!r} is not a number.") from exc


def _env_int(name: str) -> int | None:
    """Read an int env var, or None if unset. Raises on an unparseable value."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name}={raw!r} is not an integer.") from exc


def _env_bool(name: str) -> bool | None:
    """Read a bool env var, or None if unset. Raises on an unparseable value."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSEY:
        return False
    raise ConfigurationError(
        f"{name}={raw!r} is not a boolean (use one of {sorted(_TRUTHY | _FALSEY)})."
    )


def _first(*candidates: _T | None) -> _T | None:
    """Return the first non-None candidate — explicit arg, then env var."""
    return next((c for c in candidates if c is not None), None)


def _resolve(explicit: _T | None, env: _T | None, default: _T) -> _T:
    """Resolve one knob: explicit argument, then env var, then default."""
    resolved = _first(explicit, env)
    return default if resolved is None else resolved


# Defaults are declared once, on `LucntConfig`, and read back here rather than
# retyped — the same single-source discipline `_version.py` gets (T5-7). A
# second copy of "max_retries is 2" is a copy that eventually disagrees.
_DEFAULT_MAX_RETRIES: int = LucntConfig.model_fields["max_retries"].default
_DEFAULT_BACKOFF_BASE: float = LucntConfig.model_fields["backoff_base"].default
_DEFAULT_BACKOFF_MAX: float = LucntConfig.model_fields["backoff_max"].default
_DEFAULT_RESPECT_RETRY_AFTER: bool = LucntConfig.model_fields[
    "respect_retry_after"
].default


def _require_secure_base_url(base_url: str) -> None:
    """Reject a plaintext base URL so the Bearer key never crosses the wire raw.

    `http://` is allowed only for a localhost dev backend (T0-19).
    """
    parsed = urlparse(base_url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return
    raise ConfigurationError(
        f"Insecure base_url {base_url!r}: use https:// (http:// is allowed only "
        "for a localhost dev backend), so the API key is never sent in cleartext."
    )


def configure(
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
    *,
    connect_timeout: float | None = None,
    read_timeout: float | None = None,
    max_retries: int | None = None,
    backoff_base: float | None = None,
    backoff_max: float | None = None,
    respect_retry_after: bool | None = None,
) -> LucntConfig:
    """Configure the SDK's credentials, endpoint, timeouts, and retry policy.

    Every argument resolves in the same order: **explicit argument → `LUCNT_*`
    environment variable → built-in default**. Call this once before any other
    `lucnt` call in the process.

    Args:
        api_key: Live or sandbox API key. Env: `LUCNT_API_KEY`.
        base_url: Backend base URL. Env: `LUCNT_BASE_URL`. Must be `https://`
            unless it points at localhost.
        timeout: Overall per-request budget in seconds, and the default for
            any phase without its own value. Env: `LUCNT_TIMEOUT`.
        connect_timeout: Connect-phase timeout. A dead host should fail fast
            rather than burn the whole budget. Env: `LUCNT_CONNECT_TIMEOUT`.
        read_timeout: Read-phase timeout. Policy evaluation is the slow part
            and wants more room than connect. Env: `LUCNT_READ_TIMEOUT`.
        max_retries: Retries **after** the first attempt, so 2 means up to 3
            requests. 0 disables retrying. Env: `LUCNT_MAX_RETRIES`.
        backoff_base: First backoff delay in seconds; doubles per attempt and
            is then jittered. Env: `LUCNT_BACKOFF_BASE`.
        backoff_max: Ceiling for any single backoff delay, including one taken
            from `Retry-After`. Env: `LUCNT_BACKOFF_MAX`.
        respect_retry_after: Honour a server `Retry-After` header in place of
            computed backoff. Env: `LUCNT_RESPECT_RETRY_AFTER`.

    Returns:
        The resolved, now-active `LucntConfig`.

    Raises:
        ConfigurationError: No API key was found, the base URL is insecure, or
            a `LUCNT_*` variable holds an unparseable value.
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
    _require_secure_base_url(resolved_base_url)

    try:
        _active_config = LucntConfig(
            api_key=SecretStr(resolved_key),
            base_url=resolved_base_url,
            timeout=_resolve(
                timeout, _env_float("LUCNT_TIMEOUT"), _DEFAULT_TIMEOUT
            ),
            # Both stay None when unset — the transport falls back to `timeout`.
            connect_timeout=_first(
                connect_timeout, _env_float("LUCNT_CONNECT_TIMEOUT")
            ),
            read_timeout=_first(read_timeout, _env_float("LUCNT_READ_TIMEOUT")),
            max_retries=_resolve(
                max_retries, _env_int("LUCNT_MAX_RETRIES"), _DEFAULT_MAX_RETRIES
            ),
            backoff_base=_resolve(
                backoff_base,
                _env_float("LUCNT_BACKOFF_BASE"),
                _DEFAULT_BACKOFF_BASE,
            ),
            backoff_max=_resolve(
                backoff_max, _env_float("LUCNT_BACKOFF_MAX"), _DEFAULT_BACKOFF_MAX
            ),
            respect_retry_after=_resolve(
                respect_retry_after,
                _env_bool("LUCNT_RESPECT_RETRY_AFTER"),
                _DEFAULT_RESPECT_RETRY_AFTER,
            ),
        )
    except ValidationError as exc:
        # A negative max_retries or zero backoff is a caller mistake, not an
        # internal error — surface it as ConfigurationError like every other
        # bad-configuration path here.
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc
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
