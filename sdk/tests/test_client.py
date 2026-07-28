"""Unit tests for lucnt.client.Transport, against a mocked httpx transport."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from lucnt.client import Transport
from lucnt.exceptions import (
    APIError,
    AuthError,
    ConflictError,
    InvalidRequestError,
    LucntError,
    NotFoundError,
    RateLimitError,
    TransportError,
)
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


# --- T5-9: typed 4xx mapping -------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, InvalidRequestError),
        (401, AuthError),
        (403, AuthError),
        (404, NotFoundError),
        (409, ConflictError),
        (422, InvalidRequestError),
        (429, RateLimitError),
        (418, APIError),  # unmapped 4xx falls back to the base APIError
    ],
)
def test_4xx_maps_to_typed_exception(
    status_code: int, expected: type[APIError]
) -> None:
    transport = _make_transport(lambda request: httpx.Response(status_code))
    with pytest.raises(expected) as excinfo:
        transport.request("POST", "/transactions/authorize")
    assert excinfo.value.status_code == status_code
    # every typed 4xx is still catchable as the base error
    assert isinstance(excinfo.value, LucntError)


def test_conflict_is_distinguishable_from_validation_error() -> None:
    """The point of T5-9: a caller can branch on 409 vs 422."""
    transport = _make_transport(
        lambda request: httpx.Response(409, json={"detail": "already captured"})
    )
    with pytest.raises(ConflictError) as excinfo:
        transport.request("POST", "/transactions/capture")
    assert not isinstance(excinfo.value, InvalidRequestError)
    assert excinfo.value.detail == "already captured"


def test_error_detail_parsed_from_json_body() -> None:
    transport = _make_transport(
        lambda request: httpx.Response(422, json={"detail": "amount must be positive"})
    )
    with pytest.raises(InvalidRequestError) as excinfo:
        transport.request("POST", "/transactions/authorize")
    assert excinfo.value.detail == "amount must be positive"
    assert "amount must be positive" in str(excinfo.value)


def test_html_error_body_is_truncated_not_interpolated_whole() -> None:
    """A proxy's HTML error page must not land in the message verbatim."""
    html = "<html><body>" + ("x" * 5000) + "</body></html>"
    transport = _make_transport(lambda request: httpx.Response(404, text=html))
    with pytest.raises(NotFoundError) as excinfo:
        transport.request("GET", "/agents/nope")
    assert excinfo.value.detail is not None
    assert len(excinfo.value.detail) <= 200
    assert len(str(excinfo.value)) < 300


def test_error_carries_request_id_when_present() -> None:
    transport = _make_transport(
        lambda request: httpx.Response(409, headers={"X-Request-Id": "req_abc123"})
    )
    with pytest.raises(ConflictError) as excinfo:
        transport.request("POST", "/transactions/capture")
    assert excinfo.value.request_id == "req_abc123"


def test_rate_limit_error_parses_retry_after() -> None:
    transport = _make_transport(
        lambda request: httpx.Response(429, headers={"Retry-After": "2.5"})
    )
    with pytest.raises(RateLimitError) as excinfo:
        transport.request("POST", "/transactions/authorize")
    assert excinfo.value.retry_after == 2.5


def test_rate_limit_error_ignores_http_date_retry_after() -> None:
    """An HTTP-date Retry-After is dropped rather than guessed at."""
    transport = _make_transport(
        lambda request: httpx.Response(
            429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )
    )
    with pytest.raises(RateLimitError) as excinfo:
        transport.request("POST", "/transactions/authorize")
    assert excinfo.value.retry_after is None


def test_5xx_stays_a_transport_error_not_an_api_error() -> None:
    """5xx must remain retryable TransportError, not the deliberate-4xx branch."""
    transport = _make_transport(lambda request: httpx.Response(503))
    with pytest.raises(TransportError) as excinfo:
        transport.request("POST", "/transactions/authorize")
    assert not isinstance(excinfo.value, APIError)


# --- T0-6: idempotency key forwarding ----------------------------------------


def test_idempotency_key_is_sent_as_header() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Idempotency-Key"))
        return httpx.Response(200, json={})

    _make_transport(handler).request("POST", "/x", idempotency_key="key-1")
    assert seen == ["key-1"]


def test_no_idempotency_header_when_key_omitted() -> None:
    seen: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append("Idempotency-Key" in request.headers)
        return httpx.Response(200, json={})

    _make_transport(handler).request("GET", "/x")
    assert seen == [False]


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
