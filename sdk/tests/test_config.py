"""Unit tests for lucnt.config."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from lucnt import config
from lucnt.exceptions import ConfigurationError


@pytest.fixture(autouse=True)
def _reset_config() -> Iterator[None]:
    config._reset_config()
    yield
    config._reset_config()


def test_configure_reads_explicit_args() -> None:
    cfg = config.configure(
        api_key="sk_test", base_url="https://example.test", timeout=5.0
    )
    assert cfg.api_key.get_secret_value() == "sk_test"
    assert cfg.base_url == "https://example.test"
    assert cfg.timeout == 5.0


def test_configure_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUCNT_API_KEY", "sk_env")
    monkeypatch.setenv("LUCNT_BASE_URL", "https://env.example")
    cfg = config.configure()
    assert cfg.api_key.get_secret_value() == "sk_env"
    assert cfg.base_url == "https://env.example"


def test_configure_prefers_explicit_args_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUCNT_API_KEY", "sk_env")
    cfg = config.configure(api_key="sk_explicit")
    assert cfg.api_key.get_secret_value() == "sk_explicit"


def test_configure_without_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LUCNT_API_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        config.configure()


def test_get_config_before_configure_raises() -> None:
    with pytest.raises(ConfigurationError):
        config.get_config()


def test_get_config_returns_active_config() -> None:
    config.configure(api_key="sk_test")
    assert config.get_config().api_key.get_secret_value() == "sk_test"


def test_api_key_masked_in_repr() -> None:
    """SecretStr keeps the key out of any repr / log / traceback."""
    cfg = config.configure(api_key="sk_super_secret")
    assert "sk_super_secret" not in repr(cfg)
    assert "sk_super_secret" not in str(cfg)


def test_configure_rejects_plaintext_base_url() -> None:
    with pytest.raises(ConfigurationError):
        config.configure(api_key="sk_test", base_url="http://api.example.com")


def test_configure_allows_http_localhost() -> None:
    cfg = config.configure(api_key="sk_test", base_url="http://localhost:8000")
    assert cfg.base_url == "http://localhost:8000"
