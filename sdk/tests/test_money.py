"""Money is exact at the SDK boundary: floats rejected, exact forms still work."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal

import httpx
import pytest

from lucnt import config, ledger
from lucnt.client import Transport
from lucnt.exceptions import ConfigurationError
from lucnt.models import Money, Verdict


@pytest.fixture(autouse=True)
def _configured() -> Iterator[None]:
    config.configure(api_key="sk_test", base_url="https://api.test")
    yield
    config._reset_config()


def _stub(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    transport = Transport(config.get_config(), transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ledger, "_get_transport", lambda: transport)


def _authorize(amount: object) -> Verdict:
    return ledger.authorize(
        vendor="openai",
        amount=amount,  # type: ignore[arg-type]
        currency="USD",
        reason="test",
        agent="research",
        entity="in",
    )


# --------------------------------------------------------------------------- #
# _as_decimal — the boundary itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (Decimal("1.83"), Decimal("1.83")),
        (Decimal("0"), Decimal("0")),
        ("1.83", Decimal("1.83")),
        ("0.0001", Decimal("0.0001")),
        ("-5.00", Decimal("-5.00")),
        (10, Decimal("10")),
        (0, Decimal("0")),
        (999_999, Decimal("999999")),
    ],
)
def test_exact_money_forms_are_accepted(raw: Money, expected: Decimal) -> None:
    assert ledger._as_decimal(raw) == expected


def test_float_is_rejected_with_the_fix_in_the_message() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        ledger._as_decimal(1.83)  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert "float is rejected" in message
    # An error that only says "no" costs the caller a docs trip; name the fix.
    assert "Decimal('1.83')" in message


def test_a_float_that_looks_exact_is_still_rejected() -> None:
    """`10.0` is refused too — the type is the problem, not the value."""
    with pytest.raises(ConfigurationError):
        ledger._as_decimal(10.0)  # type: ignore[arg-type]


def test_bool_is_rejected_even_though_it_is_an_int() -> None:
    """`amount=True` would otherwise book 1.00 of the currency."""
    with pytest.raises(ConfigurationError):
        ledger._as_decimal(True)  # type: ignore[arg-type]


@pytest.mark.parametrize("raw", [None, [], {}, object()])
def test_other_types_are_rejected(raw: object) -> None:
    with pytest.raises(ConfigurationError):
        ledger._as_decimal(raw)  # type: ignore[arg-type]


def test_an_unparseable_string_raises_configuration_error_not_invalid_operation() -> (
    None
):
    """A bad string raises inside the hierarchy, not `decimal.InvalidOperation`."""
    with pytest.raises(ConfigurationError, match="not a number"):
        ledger._as_decimal("twelve dollars")


def test_the_field_name_appears_in_the_message() -> None:
    with pytest.raises(ConfigurationError, match="amount_at_stake"):
        ledger._as_decimal(1.5, "amount_at_stake")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Every public money parameter enforces it
# --------------------------------------------------------------------------- #


def test_authorize_rejects_float_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []
    _stub(monkeypatch, lambda request: calls.append(request) or httpx.Response(200))

    with pytest.raises(ConfigurationError):
        _authorize(1.83)
    # Rejected before any request went out — no hold was minted.
    assert calls == []


def test_capture_rejects_float_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []
    _stub(monkeypatch, lambda request: calls.append(request) or httpx.Response(200))
    verdict = Verdict(authorization_id="auth_1", status="approved")

    with pytest.raises(ConfigurationError):
        ledger.capture(verdict, amount=1.83)  # type: ignore[arg-type]
    assert calls == []


def test_spend_rejects_float_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []
    _stub(monkeypatch, lambda request: calls.append(request) or httpx.Response(200))

    with pytest.raises(ConfigurationError):
        ledger.spend(
            vendor="openai",
            amount=1.83,  # type: ignore[arg-type]
            currency="USD",
            reason="test",
            agent="research",
            entity="in",
        )
    assert calls == []


def test_mark_failed_delivery_rejects_float_amount_at_stake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[httpx.Request] = []
    _stub(monkeypatch, lambda request: calls.append(request) or httpx.Response(200))

    with pytest.raises(ConfigurationError, match="amount_at_stake"):
        ledger.mark_failed_delivery(
            transaction_id="tx_1",
            reason="corrupt dataset",
            amount_at_stake=12.5,  # type: ignore[arg-type]
        )
    assert calls == []


def test_an_exact_string_amount_reaches_the_wire_unrounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrowing exists to protect this: four decimals survive intact."""
    import json

    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"authorization_id": "auth_1", "status": "approved", "reason": None},
        )

    _stub(monkeypatch, handler)
    _authorize("0.0001")
    assert Decimal(str(bodies[0]["amount"])) == Decimal("0.0001")
