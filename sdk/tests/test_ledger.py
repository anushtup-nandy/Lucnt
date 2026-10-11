"""Unit tests for lucnt.ledger: authorize() / capture() / spend().

All backend interaction is mocked via httpx.MockTransport — none of this
needs a live server.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import httpx
import pytest

from lucnt import config, ledger
from lucnt.client import Transport
from lucnt.context import bind_context, get_current_task, operation, task
from lucnt.exceptions import (
    BudgetExhausted,
    ConfigurationError,
    ContractError,
    IdempotencyConflict,
    LucntError,
    PolicyBlocked,
)
from lucnt.models import BudgetStatus, SpendRequest, Transaction, Verdict


@pytest.fixture(autouse=True)
def _configured() -> Iterator[None]:
    config.configure(api_key="sk_test", base_url="https://api.test")
    yield
    config._reset_config()


@pytest.fixture(autouse=True)
def _reset_transport_cache() -> Iterator[None]:
    ledger._transport = None
    ledger._transport_config = None
    yield
    if ledger._transport is not None:
        ledger._transport.close()
    ledger._transport = None
    ledger._transport_config = None


def _stub(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    transport = Transport(config.get_config(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ledger, "_get_transport", lambda: transport)


def test_authorize_sends_the_resolved_request_and_returns_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transactions/authorize"
        body = json.loads(request.content)
        assert Decimal(str(body.pop("amount"))) == Decimal("1.83")
        assert body == {
            "agent": "research",
            "entity": "in",
            "vendor": "openai",
            "currency": "USD",
            "reason": "test",
            "intent": None,
            "project": None,
            "category": None,
            "tax_category": None,
            "tax_inclusive": True,
            "supply_type": "service",
            "model": None,
            "tokens": None,
        }
        return httpx.Response(
            200,
            json={"authorization_id": "auth_1", "status": "approved", "reason": None},
        )

    _stub(monkeypatch, handler)
    verdict = ledger.authorize(
        vendor="openai",
        amount="1.83",
        currency="USD",
        reason="test",
        agent="research",
        entity="in",
    )
    assert verdict.approved
    assert verdict.authorization_id == "auth_1"


def test_authorize_reports_blocked_verdict_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_2",
                "status": "blocked",
                "reason": "over monthly budget",
            },
        )

    _stub(monkeypatch, handler)
    verdict = ledger.authorize(
        vendor="openai",
        amount=999_999,
        currency="USD",
        reason="test",
        agent="research",
        entity="in",
    )
    assert not verdict.approved
    assert verdict.status == "blocked"
    assert verdict.reason == "over monthly budget"


def test_authorize_inherits_agent_and_entity_from_ambient_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["agent"] == "ops"
        assert body["entity"] == "sg"
        assert body["intent"] == "founder summit"
        return httpx.Response(
            200,
            json={"authorization_id": "auth_3", "status": "approved", "reason": None},
        )

    _stub(monkeypatch, handler)
    with task(agent="ops", entity="sg", intent="founder summit"):
        ledger.authorize(
            vendor="singapore_airlines", amount=1240, currency="SGD", reason="r"
        )


def test_authorize_explicit_agent_overrides_ambient_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["agent"] == "override"
        return httpx.Response(
            200,
            json={"authorization_id": "auth_4", "status": "approved", "reason": None},
        )

    _stub(monkeypatch, handler)
    with task(agent="ops", entity="sg"):
        ledger.authorize(
            vendor="v", amount=1, currency="USD", reason="r", agent="override"
        )


def test_authorize_without_agent_or_task_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        ledger.authorize(vendor="v", amount=1, currency="USD", reason="r", entity="sg")


def test_authorize_without_entity_or_task_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        ledger.authorize(vendor="v", amount=1, currency="USD", reason="r", agent="ops")


def test_capture_sends_the_authorization_id_and_returns_the_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transactions/capture"
        body = json.loads(request.content)
        assert body == {
            "authorization_id": "auth_1",
            "source": "sdk",
            "invoice_url": "https://example.test/invoice/1",
        }
        return httpx.Response(
            200,
            json={
                "id": "tx_1",
                "authorization_id": "auth_1",
                "status": "booked",
                "vendor": "singapore_airlines",
                "amount": 1240,
                "currency": "SGD",
                "invoice_url": "https://example.test/invoice/1",
            },
        )

    _stub(monkeypatch, handler)
    verdict = Verdict(authorization_id="auth_1", status="approved", reason=None)
    tx = ledger.capture(verdict, invoice_url="https://example.test/invoice/1")
    assert tx.id == "tx_1"
    assert tx.approved
    assert tx.estimated is False  # absent on the wire means settled truth
    # The evidence link round-trips instead of being silently dropped.
    assert tx.invoice_url == "https://example.test/invoice/1"


def test_capture_marks_an_estimate_booked_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When meter()'s best-effort fallback books the hold's estimate, the wire
    says so, and the backend echoes it back on the booked transaction."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["estimated"] is True
        return httpx.Response(
            200,
            json={
                "id": "tx_est",
                "authorization_id": "auth_1",
                "status": "completed",
                "vendor": "openai",
                "amount": 2,
                "currency": "USD",
                "estimated": True,
            },
        )

    _stub(monkeypatch, handler)
    verdict = Verdict(authorization_id="auth_1", status="approved", reason=None)
    tx = ledger.capture(verdict, estimated=True)
    assert tx.estimated is True


def test_capture_parses_journal_and_tax(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Phase 2 capture response deserializes journal lines + tax treatment."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "tx_2",
                "authorization_id": "auth_2",
                "status": "completed",
                "vendor": "openai",
                "amount": 100,
                "currency": "USD",
                "journal": [
                    {
                        "account_code": "6020",
                        "account_name": "AI & Compute",
                        "debit": 100,
                        "credit": 0,
                        "ledger": "in",
                        "currency": "INR",
                    },
                    {
                        "account_code": "1010",
                        "account_name": "Cash & Wallet",
                        "debit": 0,
                        "credit": 100,
                        "ledger": "in",
                        "currency": "INR",
                    },
                ],
                "tax": {
                    "tax_type": "GST_RCM",
                    "jurisdiction": "India",
                    "rate": 0.18,
                    "tax_amount": 18,
                    "is_reclaimable": True,
                    "reclaim_amount": 18,
                    "currency": "INR",
                    "legal_reference": "IGST Act 2017 s.5(3)",
                    "reverse_charge": True,
                    "self_invoice_generated": True,
                },
            },
        )

    _stub(monkeypatch, handler)
    verdict = Verdict(authorization_id="auth_2", status="approved", reason=None)
    tx = ledger.capture(verdict)
    assert len(tx.journal) == 2
    assert tx.journal[0].debit == Decimal("100")
    assert tx.tax is not None
    assert tx.tax.tax_type == "GST_RCM"
    assert tx.tax.reverse_charge is True
    assert tx.tax.tax_amount == Decimal("18")


def test_spend_captures_when_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/transactions/authorize":
            return httpx.Response(
                200,
                json={
                    "authorization_id": "auth_5",
                    "status": "approved",
                    "reason": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "tx_5",
                "authorization_id": "auth_5",
                "status": "booked",
                "vendor": "openai",
                "amount": 1.83,
                "currency": "USD",
            },
        )

    _stub(monkeypatch, handler)
    result = ledger.spend(
        vendor="openai",
        amount="1.83",
        currency="USD",
        reason="test",
        agent="research",
        entity="in",
        model="gpt-5",
        tokens=128_000,
    )
    assert isinstance(result, Transaction)
    assert result.id == "tx_5"


def test_spend_returns_the_blocking_verdict_without_capturing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_6",
                "status": "blocked",
                "reason": "over budget",
            },
        )

    _stub(monkeypatch, handler)
    result = ledger.spend(
        vendor="openai",
        amount=999_999,
        currency="USD",
        reason="test",
        agent="research",
        entity="in",
    )
    assert isinstance(result, Verdict)
    assert not result.approved
    assert requested_paths == ["/transactions/authorize"]


def _blocked_response(
    code: str, reason: str
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_b",
                "status": "blocked",
                "code": code,
                "reason": reason,
            },
        )

    return handler


def test_strict_authorize_raises_policy_blocked_on_merchant_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, _blocked_response("merchant_blocked", "vendor blocklisted"))
    with pytest.raises(PolicyBlocked) as exc_info:
        ledger.authorize(
            vendor="coinbase",
            amount=10,
            currency="USD",
            reason="r",
            agent="ops",
            entity="sg",
            strict=True,
        )
    assert exc_info.value.code == "merchant_blocked"
    assert exc_info.value.status == "blocked"
    assert not isinstance(exc_info.value, BudgetExhausted)


def test_strict_authorize_raises_budget_exhausted_on_budget_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, _blocked_response("budget_exhausted", "over monthly budget"))
    with pytest.raises(BudgetExhausted) as exc_info:
        ledger.authorize(
            vendor="v",
            amount=999_999,
            currency="USD",
            reason="r",
            agent="ops",
            entity="sg",
            strict=True,
        )
    # BudgetExhausted subclasses PolicyBlocked, so the broader catch works too.
    assert isinstance(exc_info.value, PolicyBlocked)
    assert exc_info.value.code == "budget_exhausted"


def test_strict_spend_raises_and_never_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _blocked_response("merchant_blocked", "blocked")(request)

    _stub(monkeypatch, handler)
    with pytest.raises(PolicyBlocked):
        ledger.spend(
            vendor="coinbase",
            amount=10,
            currency="USD",
            reason="r",
            agent="ops",
            entity="sg",
            strict=True,
        )
    assert paths == ["/transactions/authorize"]  # capture never attempted


def test_non_strict_blocked_verdict_still_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (strict=False) returns the blocking verdict, code included."""
    _stub(monkeypatch, _blocked_response("velocity_exceeded", "too fast"))
    verdict = ledger.authorize(
        vendor="v", amount=1, currency="USD", reason="r", agent="ops", entity="sg"
    )
    assert not verdict.approved
    assert verdict.code == "velocity_exceeded"


def test_check_budget_filters_server_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slug must be pushed to the server, not scanned for client-side.

    `GET /budgets` is paginated, so a client-side scan of the
    returned array would report "no budget found" for any agent past the first
    page — an agent that exists reading as absent.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/budgets"
        assert request.url.params["agent"] == "ops"
        return httpx.Response(
            200,
            json={
                "agents": [
                    {
                        "id": "a2",
                        "name": "Ops",
                        "slug": "ops",
                        "entity_id": "e1",
                        "monthly_budget": 1000,
                        "spent": 900,
                        "spent_today": 50,
                        "remaining": 100,
                        "reserved": 30,
                        "available": 70,
                        "pct_used": 90.0,
                        "status": "warning",
                    },
                ],
                "workflows": [],
                "total": 1,
            },
        )

    _stub(monkeypatch, handler)
    with task(agent="ops", entity="sg"):
        status = ledger.check_budget()
    assert isinstance(status, BudgetStatus)
    assert status.agent == "ops"
    assert status.remaining == Decimal("100")
    # The read reports what the gate enforces — holds included.
    assert status.reserved == Decimal("30")
    assert status.available == Decimal("70")
    assert status.status == "warning"
    assert status.exhausted is False


def test_check_budget_requires_the_reserved_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A money read must not silently degrade. A backend too old to
    report holds raises `ContractError` naming the mismatch, rather than
    handing the agent a headroom figure the gate would refuse."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "agents": [
                    {
                        "id": "a2",
                        "name": "Ops",
                        "slug": "ops",
                        "entity_id": "e1",
                        "monthly_budget": 1000,
                        "spent": 900,
                        "spent_today": 50,
                        "remaining": 100,
                        "pct_used": 90.0,
                        "status": "warning",
                    },
                ],
                "workflows": [],
                "total": 1,
            },
        )

    _stub(monkeypatch, handler)
    with pytest.raises(ContractError):
        ledger.check_budget(agent="ops")


def test_check_budget_raises_when_agent_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agents": [], "workflows": []})

    _stub(monkeypatch, handler)
    with pytest.raises(LucntError):
        ledger.check_budget(agent="ghost")


def test_get_transport_is_reused_across_calls() -> None:
    first = ledger._get_transport()
    second = ledger._get_transport()
    assert first is second


def test_get_transport_rebuilds_after_reconfigure() -> None:
    first = ledger._get_transport()
    config.configure(api_key="sk_other", base_url="https://api.test")
    second = ledger._get_transport()
    assert first is not second


# --- transport singleton thread safety ---------------------------------


def test_identical_reconfigure_does_not_rebuild_the_pool() -> None:
    """Configs compare by value: re-configure() with the same settings is a no-op.

    Under the old identity check this rebuilt the pool, because configure()
    returns a fresh object every call.
    """
    first = ledger._get_transport()
    config.configure(api_key="sk_test", base_url="https://api.test")
    assert ledger._get_transport() is first


def test_concurrent_first_call_builds_exactly_one_pool() -> None:
    """The check-then-act race: unsynchronized, N threads build N pools."""
    built: list[Transport] = []
    real_transport_cls = ledger.Transport

    def counting_transport(cfg: object) -> Transport:
        instance = real_transport_cls(cfg)  # type: ignore[arg-type]
        built.append(instance)
        return instance

    ledger.Transport = counting_transport  # type: ignore[assignment,misc]
    barrier = threading.Barrier(8)

    def worker() -> Transport:
        barrier.wait()  # maximize the overlap on the check-then-act
        return ledger._get_transport()

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [f.result() for f in [pool.submit(worker) for _ in range(8)]]
    finally:
        ledger.Transport = real_transport_cls  # type: ignore[misc]

    assert len(built) == 1
    assert all(r is results[0] for r in results)


def test_reconfigure_retires_the_old_pool_without_closing_it() -> None:
    """Closing inline would tear a request in flight on another thread."""
    first = ledger._get_transport()
    config.configure(api_key="sk_other", base_url="https://api.test")
    second = ledger._get_transport()

    assert second is not first
    assert first in ledger._retired_transports
    assert not first._client.is_closed  # still usable by an in-flight caller


# --- task() context across a thread hop --------------------------------


def test_task_context_is_lost_in_a_worker_thread_without_binding() -> None:
    """The bug bind_context() exists for — documented so it can't silently return."""
    with task(agent="research", entity="in"), ThreadPoolExecutor(1) as pool:
        seen = pool.submit(get_current_task).result()
    assert seen is None


def test_bind_context_carries_task_into_a_worker_thread() -> None:
    with (
        task(agent="research", entity="in", project="atlas"),
        ThreadPoolExecutor(1) as pool,
    ):
        seen = pool.submit(bind_context(get_current_task)).result()
    assert seen is not None
    assert (seen.agent, seen.entity, seen.project) == ("research", "in", "atlas")


def test_bound_callable_is_safe_to_run_concurrently() -> None:
    """Reusing one contextvars.Context object would raise here; snapshots don't."""
    with task(agent="research", entity="in"):
        bound = bind_context(get_current_task)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [f.result() for f in [pool.submit(bound) for _ in range(16)]]
    assert len(results) == 16
    assert all(r is not None and r.agent == "research" for r in results)


def test_bind_context_restores_the_workers_prior_state() -> None:
    with task(agent="outer", entity="in"):
        bound = bind_context(get_current_task)

    with task(agent="caller", entity="in"):
        assert bound() is not None
        after = get_current_task()
    assert after is not None
    assert after.agent == "caller"  # the wrapper reset what it set


def test_authorize_in_a_bound_worker_attributes_the_right_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the fan-out books against the parent's agent, not nothing."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["agent"])
        return _approved("auth_bound")

    _stub(monkeypatch, handler)

    def work() -> None:
        ledger.authorize(
            vendor="openai", amount="1.00", currency="USD", reason="fan-out"
        )

    with task(agent="research", entity="in"), ThreadPoolExecutor(3) as pool:
        for future in [pool.submit(bind_context(work)) for _ in range(3)]:
            future.result()

    assert seen == ["research"] * 3


# --- the SDK half of idempotency ---------------------------------------


def _approved(auth_id: str) -> httpx.Response:
    return httpx.Response(
        200, json={"authorization_id": auth_id, "status": "approved", "reason": None}
    )


def _keys_seen(
    monkeypatch: pytest.MonkeyPatch, sink: dict[str, list[str | None]]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        sink.setdefault(request.url.path, []).append(
            request.headers.get("Idempotency-Key")
        )
        if request.url.path == "/transactions/authorize":
            return _approved("auth_idem")
        return httpx.Response(
            200,
            json={
                "id": "tx_idem",
                "authorization_id": "auth_idem",
                "status": "booked",
                "vendor": "openai",
                "amount": 1.0,
                "currency": "USD",
            },
        )

    _stub(monkeypatch, handler)


def test_authorize_generates_an_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    ledger.authorize(
        vendor="openai",
        amount="1.00",
        currency="USD",
        reason="t",
        agent="a",
        entity="e",
    )
    (key,) = seen["/transactions/authorize"]
    assert key is not None and len(key) == 32


def test_authorize_uses_the_callers_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    ledger.authorize(
        vendor="openai",
        amount="1.00",
        currency="USD",
        reason="t",
        agent="a",
        entity="e",
        idempotency_key="caller-owned-key",
    )
    assert seen["/transactions/authorize"] == ["caller-owned-key"]


def test_each_authorize_call_gets_a_distinct_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two authorize() calls are two logical spends, not a retry of one."""
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    for _ in range(2):
        ledger.authorize(
            vendor="openai",
            amount="1.00",
            currency="USD",
            reason="t",
            agent="a",
            entity="e",
        )
    first, second = seen["/transactions/authorize"]
    assert first != second


def test_spend_forwards_the_idempotency_key_to_authorize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    ledger.spend(
        vendor="openai",
        amount="1.00",
        currency="USD",
        reason="t",
        agent="a",
        entity="e",
        idempotency_key="spend-key",
    )
    assert seen["/transactions/authorize"] == ["spend-key"]


def test_capture_sends_no_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """authorization_id already is capture's idempotency key — see capture()."""
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    ledger.capture(Verdict(authorization_id="auth_idem", status="approved"))
    assert seen["/transactions/capture"] == [None]


# --- operation() drives the key, with a payload guardrail -------------


def _spend_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "vendor": "openai",
        "amount": "1.00",
        "currency": "USD",
        "reason": "t",
        "agent": "a",
        "entity": "e",
    }
    return {**base, **overrides}


def test_operation_scope_supplies_the_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    with operation("order-42"):
        ledger.authorize(**_spend_kwargs())  # type: ignore[arg-type]
    assert seen["/transactions/authorize"] == ["order-42"]


def test_retrying_the_operation_block_reuses_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of `operation()`: the caller's own retry replays the original
    hold instead of minting a second one."""
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    for _ in range(3):
        with operation("order-43"):
            ledger.authorize(**_spend_kwargs())  # type: ignore[arg-type]
    assert seen["/transactions/authorize"] == ["order-43"] * 3


def test_explicit_key_outranks_the_operation_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    with operation("order-44"):
        ledger.authorize(**_spend_kwargs(idempotency_key="explicit"))  # type: ignore[arg-type]
    assert seen["/transactions/authorize"] == ["explicit"]


def test_auto_key_is_used_outside_any_operation_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    ledger.authorize(**_spend_kwargs())  # type: ignore[arg-type]
    (key,) = seen["/transactions/authorize"]
    assert key is not None and len(key) == 32


def test_spend_inherits_the_operation_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    with operation("order-45"):
        ledger.spend(**_spend_kwargs())  # type: ignore[arg-type]
    assert seen["/transactions/authorize"] == ["order-45"]


def test_repeated_identical_authorize_in_one_scope_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-process retry of the *same* spend inside the scope is the intended
    use, so it must not trip the guardrail."""
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    with operation("order-46"):
        ledger.authorize(**_spend_kwargs())  # type: ignore[arg-type]
        ledger.authorize(**_spend_kwargs())  # type: ignore[arg-type]
    assert seen["/transactions/authorize"] == ["order-46"] * 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vendor", "stripe"),
        ("amount", "2.00"),
        ("currency", "EUR"),
        ("agent", "other-agent"),
        ("entity", "other-entity"),
        ("category", "travel"),
        ("tax_category", "gst_5"),
        ("tax_inclusive", False),
        ("supply_type", "goods"),
    ],
)
def test_a_different_spend_in_one_scope_raises(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    with operation("order-47"):
        ledger.authorize(**_spend_kwargs())  # type: ignore[arg-type]
        with pytest.raises(IdempotencyConflict, match=field):
            ledger.authorize(**_spend_kwargs(**{field: value}))  # type: ignore[arg-type]
    # The second spend never reached the server.
    assert seen["/transactions/authorize"] == ["order-47"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reason", "a different explanation"),
        ("intent", "some intent"),
        ("project", "Q3"),
        ("model", "gpt-4o"),
        ("tokens", 512),
    ],
)
def test_descriptive_fields_do_not_trip_the_guardrail(
    monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    """Excluded deliberately: none of these can turn an unapproved spend into an
    approved one, and a guardrail that fires on cosmetic drift gets routed
    around by not using operation() at all."""
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    with operation("order-48"):
        ledger.authorize(**_spend_kwargs())  # type: ignore[arg-type]
        ledger.authorize(**_spend_kwargs(**{field: value}))  # type: ignore[arg-type]
    assert seen["/transactions/authorize"] == ["order-48"] * 2


def test_equal_amounts_written_differently_do_not_trip_the_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fingerprinted in python mode so amount stays a Decimal: 10 and "10.00"
    are the same money and must compare equal."""
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    with operation("order-49"):
        ledger.authorize(**_spend_kwargs(amount=10))  # type: ignore[arg-type]
        ledger.authorize(**_spend_kwargs(amount="10.00"))  # type: ignore[arg-type]
        ledger.authorize(**_spend_kwargs(amount=Decimal("10.000")))  # type: ignore[arg-type]
    assert seen["/transactions/authorize"] == ["order-49"] * 3


def test_a_new_spend_request_field_is_fingerprinted_by_default() -> None:
    """The exclusion set is an allowlist of *exemptions*, so a field added to
    SpendRequest later is covered unless someone deliberately exempts it."""
    request = SpendRequest(
        agent="a",
        entity="e",
        vendor="v",
        amount=Decimal("1"),
        currency="USD",
        reason="r",
    )
    expected = set(SpendRequest.model_fields) - ledger._FINGERPRINT_EXCLUDED
    assert set(ledger._operation_fingerprint(request)) == expected


@pytest.mark.parametrize("key", ["", "   ", "x" * 256])
def test_a_blank_or_oversized_explicit_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """A blank key would either skip dedupe silently or collide every
    blank-keyed request in the company against the first one."""
    seen: dict[str, list[str | None]] = {}
    _keys_seen(monkeypatch, seen)
    with pytest.raises(ConfigurationError):
        ledger.authorize(**_spend_kwargs(idempotency_key=key))  # type: ignore[arg-type]
    assert seen == {}


# --- void() releases an orphaned hold -----------------------------------


def _void_handler(
    calls: list[dict[str, object]], *, already: bool = False
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transactions/void"
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_v",
                "released_at": "2026-07-28T12:00:00Z",
                "release_reason": "vendor failed",
                "already_released": already,
            },
        )

    return handler


def test_void_releases_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    _stub(monkeypatch, _void_handler(calls))
    result = ledger.void(
        Verdict(authorization_id="auth_v", status="approved"),
        reason="vendor failed",
    )
    assert calls == [{"authorization_id": "auth_v", "reason": "vendor failed"}]
    assert result.authorization_id == "auth_v"
    assert result.already_released is False


def test_void_omits_an_absent_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    _stub(monkeypatch, _void_handler(calls))
    ledger.void(Verdict(authorization_id="auth_v", status="approved"))
    assert calls == [{"authorization_id": "auth_v"}]


def test_void_reports_an_already_released_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotent by design: void() is called from failure paths, so a retry must
    not become a second failure to handle."""
    _stub(monkeypatch, _void_handler([], already=True))
    result = ledger.void(Verdict(authorization_id="auth_v", status="approved"))
    assert result.already_released is True


def test_void_is_retried_on_a_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(500, text="down")
        return httpx.Response(
            200,
            json={
                "authorization_id": "auth_v",
                "released_at": "2026-07-28T12:00:00Z",
                "already_released": False,
            },
        )

    _stub(monkeypatch, handler)
    ledger.void(Verdict(authorization_id="auth_v", status="approved"))
    assert len(attempts) == 2  # retry_safe=True: the server dedupes a repeat
