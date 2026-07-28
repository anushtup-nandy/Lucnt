"""Unit tests for the async surface: AsyncTransport and the `_async` twins (T5-6).

Driven with `asyncio.run()` inside ordinary sync tests rather than a pytest
async plugin. That keeps the SDK's dev dependencies to what the package already
needs — a thin client library should not require a test-runner plugin to have
its own async path exercised — and it makes the per-event-loop transport
behaviour testable, since each `asyncio.run()` is a distinct loop.

All backend interaction is mocked via `httpx.MockTransport`; none of this needs a
live server, per sdk/PLAN.md §6.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from decimal import Decimal

import httpx
import pytest

from lucnt import client, config, ledger
from lucnt.client import AsyncTransport
from lucnt.context import operation, task
from lucnt.exceptions import (
    ConfigurationError,
    ConflictError,
    IdempotencyConflict,
    InvalidRequestError,
    PolicyBlocked,
    RateLimitError,
    TransportError,
)
from lucnt.models import BudgetStatus, Transaction, Verdict

Handler = Callable[[httpx.Request], httpx.Response]

# Captured at import, before the autouse `no_real_async_sleep` fixture replaces
# it. One test restores this to exercise the real backoff — see
# `test_async_backoff_yields_the_loop_instead_of_blocking_it`.
_REAL_ASLEEP = client._asleep


@pytest.fixture(autouse=True)
def _configured() -> Iterator[None]:
    config.configure(api_key="sk_test", base_url="https://api.test")
    yield
    config._reset_config()


@pytest.fixture(autouse=True)
def _reset_async_transport_cache() -> Iterator[None]:
    ledger._async_transports.clear()
    yield
    ledger._async_transports.clear()


def _async_stub(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    """Route every async ledger call through a MockTransport-backed pool.

    Built lazily inside the running loop: an `httpx.AsyncClient` binds to the
    loop that constructs it, so building it at patch time (outside
    `asyncio.run`) would produce a pool bound to no loop at all.
    """
    pools: dict[asyncio.AbstractEventLoop, AsyncTransport] = {}

    def _get() -> AsyncTransport:
        loop = asyncio.get_running_loop()
        if loop not in pools:
            pools[loop] = AsyncTransport(
                config.get_config(), transport=httpx.MockTransport(handler)
            )
        return pools[loop]

    monkeypatch.setattr(ledger, "_get_async_transport", _get)


def _approved(authorization_id: str = "auth_1") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "authorization_id": authorization_id,
            "status": "approved",
            "reason": None,
        },
    )


def _transaction(
    tx_id: str = "tx_1", authorization_id: str = "auth_1"
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": tx_id,
            "authorization_id": authorization_id,
            "status": "booked",
            "vendor": "openai",
            "amount": "1.83",
            "currency": "USD",
        },
    )


# --------------------------------------------------------------------------- #
# AsyncTransport — the same decisions as Transport, awaited
# --------------------------------------------------------------------------- #


def _make_async_transport(handler: Handler, **overrides: object) -> AsyncTransport:
    cfg = config.configure(
        api_key="sk_test",
        base_url="https://api.test",
        **overrides,  # type: ignore[arg-type]
    )
    return AsyncTransport(cfg, transport=httpx.MockTransport(handler))


def test_async_request_returns_the_successful_response() -> None:
    async def main() -> httpx.Response:
        transport = _make_async_transport(
            lambda request: httpx.Response(200, json={"ok": True})
        )
        return await transport.request("GET", "/health")

    assert asyncio.run(main()).json() == {"ok": True}


def test_async_request_maps_4xx_onto_the_typed_hierarchy() -> None:
    async def main() -> None:
        transport = _make_async_transport(
            lambda request: httpx.Response(409, json={"detail": "already captured"})
        )
        await transport.request("POST", "/x", retry_safe=True)

    with pytest.raises(ConflictError) as excinfo:
        asyncio.run(main())
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "already captured"


def test_async_request_retries_5xx_then_succeeds(
    no_real_async_sleep: list[float],
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    async def main() -> httpx.Response:
        return await _make_async_transport(handler).request("GET", "/health")

    assert asyncio.run(main()).status_code == 200
    assert attempts == 3
    # Two backoffs for two failures, and they were awaited rather than slept.
    assert len(no_real_async_sleep) == 2


def test_async_request_exhausts_retries_and_raises_transport_error(
    no_real_async_sleep: list[float],
) -> None:
    async def main() -> None:
        await _make_async_transport(lambda request: httpx.Response(500)).request(
            "GET", "/health"
        )

    with pytest.raises(TransportError):
        asyncio.run(main())
    assert len(no_real_async_sleep) == 2  # max_retries defaults to 2


def test_async_request_never_retries_a_4xx(no_real_async_sleep: list[float]) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(422, json={"detail": "bad currency"})

    async def main() -> None:
        await _make_async_transport(handler).request("GET", "/x")

    with pytest.raises(InvalidRequestError):
        asyncio.run(main())
    assert attempts == 1
    assert no_real_async_sleep == []


def test_async_exhausted_429_raises_typed_rate_limit_error(
    no_real_async_sleep: list[float],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "2"})

    async def main() -> None:
        await _make_async_transport(handler).request("GET", "/x")

    with pytest.raises(RateLimitError) as excinfo:
        asyncio.run(main())
    assert excinfo.value.retry_after == 2.0


def test_async_unkeyed_post_is_not_retried(no_real_async_sleep: list[float]) -> None:
    """The conservative eligibility rule holds on the async path too.

    An unkeyed POST may have landed server-side before the connection dropped,
    so repeating it is the double-book. Shared with the sync transport at module
    level — this pins that the async loop actually consults it.
    """
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500)

    async def main() -> None:
        await _make_async_transport(handler).request("POST", "/x")

    with pytest.raises(TransportError):
        asyncio.run(main())
    assert attempts == 1


def test_async_keyed_post_reuses_one_key_across_attempts(
    no_real_async_sleep: list[float],
) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Idempotency-Key"))
        return httpx.Response(500) if len(seen) < 2 else httpx.Response(200, json={})

    async def main() -> None:
        await _make_async_transport(handler).request(
            "POST", "/x", idempotency_key="stable-key"
        )

    asyncio.run(main())
    assert seen == ["stable-key", "stable-key"]


def test_async_backoff_yields_the_loop_instead_of_blocking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property the whole async path exists for: a backoff must not stall the loop.

    Every other test patches `_asleep` away, so none of them would notice
    `time.sleep` in there — and a blocking backoff is exactly the bug T5-6
    removes: one retrying request would freeze every unrelated coroutine in the
    process for the backoff duration.

    Measured by what a concurrent ticker achieves *while the request is
    waiting*, not by the end state: under a blocking sleep the ticker still
    finishes eventually, just not during the backoff. The delay comes from
    `Retry-After` rather than jittered exponential backoff so it is an exact
    20ms rather than a uniform draw that could round to nothing.
    """
    monkeypatch.setattr(client, "_asleep", _REAL_ASLEEP)
    attempts = 0
    ticks = 0
    ticks_when_request_finished = -1

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.02"})
        return httpx.Response(200, json={"ok": True})

    async def ticker() -> None:
        nonlocal ticks
        while ticks < 200:
            ticks += 1
            await asyncio.sleep(0)

    async def caller() -> None:
        nonlocal ticks_when_request_finished
        transport = _make_async_transport(handler, backoff_max=1.0)
        await transport.request("GET", "/health")
        ticks_when_request_finished = ticks

    async def main() -> None:
        await asyncio.gather(caller(), ticker())

    asyncio.run(main())
    assert attempts == 2
    # A blocking backoff lets the ticker manage only the handful of yields it
    # got in before the block; a yielding one lets it run to completion.
    assert ticks_when_request_finished == 200


def test_async_transport_closes_via_context_manager() -> None:
    async def main() -> bool:
        transport = _make_async_transport(lambda request: httpx.Response(200, json={}))
        async with transport:
            await transport.request("GET", "/health")
        return transport._client.is_closed

    assert asyncio.run(main()) is True


# --------------------------------------------------------------------------- #
# The async ledger twins
# --------------------------------------------------------------------------- #


def test_authorize_async_sends_the_request_and_returns_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transactions/authorize"
        assert request.headers.get("Idempotency-Key")
        bodies.append(json.loads(request.content))
        return _approved()

    _async_stub(monkeypatch, handler)

    async def main() -> Verdict:
        return await ledger.authorize_async(
            vendor="openai",
            amount="1.83",
            currency="USD",
            reason="test",
            agent="research",
            entity="in",
        )

    verdict = asyncio.run(main())
    assert verdict.approved
    assert Decimal(str(bodies[0]["amount"])) == Decimal("1.83")


def test_authorize_async_strict_raises_on_a_blocking_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_2",
                "status": "blocked",
                "code": "merchant_blocked",
                "reason": "vendor not allowed",
            },
        )

    _async_stub(monkeypatch, handler)

    async def main() -> Verdict:
        return await ledger.authorize_async(
            vendor="dodgy",
            amount="5",
            currency="USD",
            reason="test",
            agent="research",
            entity="in",
            strict=True,
        )

    with pytest.raises(PolicyBlocked) as excinfo:
        asyncio.run(main())
    assert excinfo.value.code == "merchant_blocked"


def test_spend_async_captures_when_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("authorize"):
            return _approved("auth_5")
        return _transaction("tx_5", "auth_5")

    _async_stub(monkeypatch, handler)

    async def main() -> Verdict | Transaction:
        return await ledger.spend_async(
            vendor="openai",
            amount="1.83",
            currency="USD",
            reason="test",
            agent="research",
            entity="in",
        )

    result = asyncio.run(main())
    assert isinstance(result, Transaction)
    assert result.id == "tx_5"
    assert paths == ["/transactions/authorize", "/transactions/capture"]


def test_spend_async_skips_capture_when_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_6",
                "status": "blocked",
                "reason": "over budget",
            },
        )

    _async_stub(monkeypatch, handler)

    async def main() -> Verdict | Transaction:
        return await ledger.spend_async(
            vendor="openai",
            amount="9999",
            currency="USD",
            reason="test",
            agent="research",
            entity="in",
        )

    result = asyncio.run(main())
    assert isinstance(result, Verdict)
    assert not result.approved
    assert paths == ["/transactions/authorize"]


def test_void_async_releases_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transactions/void"
        assert json.loads(request.content) == {
            "authorization_id": "auth_7",
            "reason": "vendor failed",
        }
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_7",
                "released_at": "2026-07-28T10:00:00Z",
                "release_reason": "vendor failed",
                "already_released": False,
            },
        )

    _async_stub(monkeypatch, handler)
    verdict = Verdict(authorization_id="auth_7", status="approved")

    async def main() -> object:
        return await ledger.void_async(verdict, reason="vendor failed")

    result = asyncio.run(main())
    assert result.authorization_id == "auth_7"  # type: ignore[attr-defined]


def test_check_budget_async_filters_server_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"agent": "research", "limit": "1"}
        return httpx.Response(
            200,
            json={
                "agents": [
                    {
                        "monthly_budget": "100.00",
                        "spent": "40.00",
                        "remaining": "60.00",
                        "pct_used": "40.00",
                        "status": "ok",
                    }
                ]
            },
        )

    _async_stub(monkeypatch, handler)

    async def main() -> BudgetStatus:
        return await ledger.check_budget_async(agent="research")

    status = asyncio.run(main())
    assert status.remaining == Decimal("60.00")
    assert not status.exhausted


def test_query_async_returns_financial_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"agent": "research", "q": "on track?"}
        return httpx.Response(
            200,
            json={
                "agent": "research",
                "currency": "USD",
                "headline": "on track",
                "budget": {
                    "monthly_budget": "100.00",
                    "spent": "40.00",
                    "remaining": "60.00",
                    "pct_used": "40.00",
                    "status": "ok",
                },
                "projection": {
                    "month_to_date": "40.00",
                    "projected_month_end": "80.00",
                    "monthly_budget": "100.00",
                    "on_track": True,
                    "projected_overrun": "0.00",
                    "day_of_month": 14,
                    "days_in_month": 28,
                },
            },
        )

    _async_stub(monkeypatch, handler)

    async def main() -> object:
        return await ledger.query_async(agent="research", question="on track?")

    memory = asyncio.run(main())
    assert memory.on_track  # type: ignore[attr-defined]


def test_mark_failed_delivery_async_reports_the_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/recon/failed-delivery"
        assert json.loads(request.content) == {
            "transaction_id": "tx_9",
            "reason": "corrupt dataset",
            "amount_at_stake": "12.50",
        }
        return httpx.Response(
            200,
            json={
                "id": "rex_1",
                "transaction_id": "tx_9",
                "exception_type": "failed_delivery",
                "status": "open",
                "amount_at_stake": "12.50",
            },
        )

    _async_stub(monkeypatch, handler)

    async def main() -> object:
        return await ledger.mark_failed_delivery_async(
            transaction_id="tx_9",
            reason="corrupt dataset",
            amount_at_stake="12.50",
        )

    assert asyncio.run(main()).id == "rex_1"  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Ambient context across an await
# --------------------------------------------------------------------------- #


def test_task_context_reaches_an_async_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """contextvars propagate into a coroutine, so `task()` needs no async variant."""
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _approved()

    _async_stub(monkeypatch, handler)

    async def main() -> None:
        with task(agent="researcher", entity="acme-us", project="q3"):
            await ledger.authorize_async(
                vendor="openai", amount="1", currency="USD", reason="test"
            )

    asyncio.run(main())
    assert bodies[0]["agent"] == "researcher"
    assert bodies[0]["entity"] == "acme-us"
    assert bodies[0]["project"] == "q3"


def test_operation_guardrail_holds_across_an_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second, different spend under one op id must still be refused."""
    _async_stub(monkeypatch, lambda request: _approved())

    async def main() -> None:
        with operation("order-7"):
            await ledger.authorize_async(
                vendor="openai",
                amount="10",
                currency="USD",
                reason="first",
                agent="a",
                entity="e",
            )
            await ledger.authorize_async(
                vendor="openai",
                amount="99",
                currency="USD",
                reason="second",
                agent="a",
                entity="e",
            )

    with pytest.raises(IdempotencyConflict):
        asyncio.run(main())


def test_concurrent_async_spends_each_get_their_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct logical spends fanned out with gather must not share a key."""
    keys: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("Idempotency-Key"))
        return _approved()

    _async_stub(monkeypatch, handler)

    async def main() -> None:
        await asyncio.gather(
            *(
                ledger.authorize_async(
                    vendor="openai",
                    amount=str(n),
                    currency="USD",
                    reason="test",
                    agent="a",
                    entity="e",
                )
                for n in range(1, 6)
            )
        )

    asyncio.run(main())
    assert len(keys) == 5
    assert len(set(keys)) == 5


# --------------------------------------------------------------------------- #
# Loop-safety guards
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("sync_name", "call"),
    [
        (
            "spend",
            lambda: ledger.spend(
                vendor="v", amount="1", currency="USD", reason="r",
                agent="a", entity="e",
            ),
        ),
        (
            "authorize",
            lambda: ledger.authorize(
                vendor="v", amount="1", currency="USD", reason="r",
                agent="a", entity="e",
            ),
        ),
        (
            "capture",
            lambda: ledger.capture(Verdict(authorization_id="a1", status="approved")),
        ),
        (
            "void",
            lambda: ledger.void(Verdict(authorization_id="a1", status="approved")),
        ),
        ("check_budget", lambda: ledger.check_budget(agent="a")),
        ("query", lambda: ledger.query(agent="a")),
        (
            "mark_failed_delivery",
            lambda: ledger.mark_failed_delivery(transaction_id="t1", reason="r"),
        ),
    ],
)
def test_sync_calls_refuse_to_block_a_running_loop(
    sync_name: str, call: Callable[[], object]
) -> None:
    """Every sync entry point refuses, and names its own async twin.

    The failure being prevented is not the caller's latency: a synchronous
    request from inside a loop stalls every other coroutine in the process for
    up to the request timeout, which surfaces as unrelated timeouts elsewhere.
    """

    async def main() -> None:
        call()

    with pytest.raises(ConfigurationError) as excinfo:
        asyncio.run(main())
    assert f"lucnt.{sync_name}_async()" in str(excinfo.value)


def test_sync_call_is_allowed_in_a_worker_thread_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`asyncio.to_thread(lucnt.spend, ...)` stays valid — no loop in that thread."""
    from lucnt.client import Transport

    transport = Transport(
        config.get_config(),
        transport=httpx.MockTransport(lambda request: _approved()),
    )
    monkeypatch.setattr(ledger, "_get_transport", lambda: transport)

    async def main() -> Verdict:
        return await asyncio.to_thread(
            ledger.authorize,
            vendor="openai",
            amount="1",
            currency="USD",
            reason="test",
            agent="a",
            entity="e",
        )

    assert asyncio.run(main()).approved


def test_async_calls_outside_a_loop_explain_themselves() -> None:
    coro = ledger.authorize_async(
        vendor="v", amount="1", currency="USD", reason="r", agent="a", entity="e"
    )
    with pytest.raises(ConfigurationError, match="running event loop"):
        coro.send(None)
    coro.close()


# --------------------------------------------------------------------------- #
# Per-loop transport lifecycle
# --------------------------------------------------------------------------- #


def test_async_transport_is_per_event_loop() -> None:
    """A pool built under one `asyncio.run` must not be reused by the next.

    An httpx.AsyncClient's pool holds loop-bound objects, so a process-wide
    singleton would hand the second `asyncio.run()` a pool attached to a closed
    loop. Two runs, two pools.
    """
    seen: list[int] = []

    async def main() -> None:
        seen.append(id(ledger._get_async_transport()))

    asyncio.run(main())
    asyncio.run(main())
    assert len(seen) == 2
    assert seen[0] != seen[1]


def test_async_transport_is_reused_within_one_loop() -> None:
    async def main() -> bool:
        return ledger._get_async_transport() is ledger._get_async_transport()

    assert asyncio.run(main()) is True


def test_async_transport_is_rebuilt_after_a_material_reconfigure() -> None:
    async def main() -> bool:
        first = ledger._get_async_transport()
        config.configure(api_key="sk_test", base_url="https://api.test", timeout=5.0)
        return ledger._get_async_transport() is not first

    assert asyncio.run(main()) is True


def test_identical_reconfigure_does_not_rebuild_the_async_pool() -> None:
    """Configs compare by value: `configure()` returns a fresh object each call."""

    async def main() -> bool:
        first = ledger._get_async_transport()
        config.configure(api_key="sk_test", base_url="https://api.test")
        return ledger._get_async_transport() is first

    assert asyncio.run(main()) is True


def test_aclose_closes_the_pool_for_this_loop() -> None:
    async def main() -> bool:
        transport = ledger._get_async_transport()
        await ledger.aclose()
        return transport._client.is_closed

    assert asyncio.run(main()) is True


def test_aclose_is_a_no_op_when_no_async_call_was_made() -> None:
    async def main() -> None:
        await ledger.aclose()

    asyncio.run(main())  # must not raise


# --------------------------------------------------------------------------- #
# The load-bearing test: sync and async must not drift
# --------------------------------------------------------------------------- #

_SPEND_ARGS: dict[str, object] = {
    "vendor": "united-airlines",
    "amount": "412.50",
    "currency": "USD",
    "reason": "founder summit",
    "agent": "travel-bot",
    "entity": "acme-us",
    "intent": "offsite",
    "project": "q3",
    "category": "travel",
    "tax_category": "gst_5",
    "tax_inclusive": False,
    "supply_type": "goods",
    "model": "gpt-4o",
    "tokens": 512,
}


def _record_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Capture every request a sync call makes."""
    from lucnt.client import Transport

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("authorize"):
            return _approved("auth_x")
        return _transaction("tx_x", "auth_x")

    transport = Transport(
        config.get_config(), transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(ledger, "_get_transport", lambda: transport)
    return seen


def _record_async_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Capture every request an async call makes."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("authorize"):
            return _approved("auth_x")
        return _transaction("tx_x", "auth_x")

    _async_stub(monkeypatch, handler)
    return seen


def test_spend_and_spend_async_issue_byte_identical_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole reason `_Call` exists.

    An async twin that posted to a different path, omitted the idempotency
    header, or serialized the body differently would double-book — and only for
    async callers, so a sync-only test suite would never see it. This asserts
    the two paths produce the same wire traffic for the same arguments, with the
    idempotency key normalised away (it is a fresh uuid per logical spend by
    design).
    """
    sync_seen = _record_requests(monkeypatch)
    ledger.spend(**_SPEND_ARGS, idempotency_key="op-1")  # type: ignore[arg-type]

    async_seen = _record_async_requests(monkeypatch)

    async def main() -> None:
        await ledger.spend_async(**_SPEND_ARGS, idempotency_key="op-1")  # type: ignore[arg-type]

    asyncio.run(main())

    assert len(sync_seen) == len(async_seen) == 2
    for sync_request, async_request in zip(sync_seen, async_seen, strict=True):
        assert sync_request.method == async_request.method
        assert sync_request.url.path == async_request.url.path
        assert dict(sync_request.url.params) == dict(async_request.url.params)
        assert sync_request.content == async_request.content
        assert sync_request.headers.get("Idempotency-Key") == async_request.headers.get(
            "Idempotency-Key"
        )


@pytest.mark.parametrize(
    "operation_name",
    ["authorize", "capture", "void", "spend", "check_budget", "query",
     "mark_failed_delivery"],
)
def test_every_sync_function_has_an_async_twin(operation_name: str) -> None:
    """A new ledger function must not ship sync-only.

    Cheap structural guard: the twin naming convention is the contract async
    callers rely on, so a missing one is a gap in the public surface rather than
    an implementation detail.
    """
    import lucnt

    assert callable(getattr(lucnt, operation_name))
    assert asyncio.iscoroutinefunction(getattr(lucnt, f"{operation_name}_async"))
