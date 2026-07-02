"""Unit tests for lucnt.client.Transport, against a mocked httpx transport."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from lucnt.client import Transport
from lucnt.exceptions import AuthError, LucntError, TransportError
from lucnt.models import LucntConfig


def _make_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Transport:
    config = LucntConfig(api_key="sk_test", base_url="https://api.test", timeout=5.0)
    return Transport(config, transport=httpx.MockTransport(handler))


def test_request_returns_response_on_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk_test"
        assert request.headers["User-Agent"].startswith("lucnt-python/")
        return httpx.Response(200, json={"ok": True})

    response = _make_transport(handler).request("GET", "/health")
    assert response.json() == {"ok": True}


def test_request_raises_auth_error_on_401() -> None:
    transport = _make_transport(lambda request: httpx.Response(401))
    with pytest.raises(AuthError):
        transport.request("GET", "/health")


def test_request_raises_transport_error_on_500() -> None:
    transport = _make_transport(lambda request: httpx.Response(500))
    with pytest.raises(TransportError):
        transport.request("GET", "/health")


def test_request_raises_lucnt_error_on_400() -> None:
    transport = _make_transport(lambda request: httpx.Response(400, text="bad"))
    with pytest.raises(LucntError):
        transport.request("GET", "/health")


def test_request_raises_transport_error_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("boom", request=request)

    with pytest.raises(TransportError):
        _make_transport(handler).request("GET", "/health")


def test_context_manager_closes_client() -> None:
    transport = _make_transport(lambda request: httpx.Response(200))
    with transport as entered:
        assert entered is transport
    assert transport._client.is_closed
