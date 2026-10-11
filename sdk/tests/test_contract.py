"""A 2xx the SDK cannot read must raise `ContractError` (never retried), not leak."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest

from lucnt import config, ledger
from lucnt.client import Transport
from lucnt.exceptions import ContractError, LucntError, TransportError
from lucnt.models import Verdict


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


def _authorize() -> Verdict:
    return ledger.authorize(
        vendor="openai",
        amount="1.00",
        currency="USD",
        reason="test",
        agent="research",
        entity="in",
    )


def test_a_non_json_success_body_raises_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(
        monkeypatch,
        lambda request: httpx.Response(200, text="<html>gateway happy</html>"),
    )
    with pytest.raises(ContractError) as excinfo:
        _authorize()
    assert excinfo.value.path == "/transactions/authorize"
    assert "not JSON" in str(excinfo.value)


def test_an_html_success_body_is_truncated_in_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same policy as a 4xx body: never put a screenful of markup in a traceback."""
    _stub(monkeypatch, lambda request: httpx.Response(200, text="x" * 5_000))
    with pytest.raises(ContractError) as excinfo:
        _authorize()
    assert len(str(excinfo.value)) < 500


def test_a_json_array_success_body_raises_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, lambda request: httpx.Response(200, json=[1, 2, 3]))
    with pytest.raises(ContractError, match="not an object"):
        _authorize()


def test_a_drifted_verdict_shape_raises_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status` renamed server-side raises ContractError, not ValidationError."""
    _stub(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"authorization_id": "auth_1", "verdict_status": "approved"}
        ),
    )
    with pytest.raises(ContractError) as excinfo:
        _authorize()
    assert "Verdict" in str(excinfo.value)
    assert "newer than the SDK" in str(excinfo.value)


def test_an_unknown_verdict_status_raises_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status` is a closed Literal: a new value changes whether money may move."""
    _stub(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"authorization_id": "auth_1", "status": "quarantined"}
        ),
    )
    with pytest.raises(ContractError):
        _authorize()


def test_contract_error_is_a_lucnt_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """`except LucntError` covers response drift."""
    _stub(monkeypatch, lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(LucntError):
        _authorize()


def test_contract_error_is_not_a_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It must not be classified as retryable — the body will drift identically."""
    _stub(monkeypatch, lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(ContractError) as excinfo:
        _authorize()
    assert not isinstance(excinfo.value, TransportError)


def test_a_drifted_body_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, no_real_sleep: list[float]
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, text="not json")

    _stub(monkeypatch, handler)
    with pytest.raises(ContractError):
        _authorize()
    assert attempts == 1
    assert no_real_sleep == []


def test_a_missing_agents_key_raises_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing `agents` key raises ContractError, not KeyError."""
    _stub(monkeypatch, lambda request: httpx.Response(200, json={"items": []}))
    with pytest.raises(ContractError, match="no 'agents' list"):
        ledger.check_budget(agent="research")


def test_a_renamed_budget_field_raises_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "agents": [
                    {
                        "monthly_budget": "100.00",
                        "spent": "40.00",
                        # `remaining` renamed
                        "headroom": "60.00",
                        "pct_used": "40.00",
                        "status": "ok",
                    }
                ]
            },
        ),
    )
    with pytest.raises(ContractError, match="budget row"):
        ledger.check_budget(agent="research")


def test_a_non_object_budget_row_raises_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(
        monkeypatch,
        lambda request: httpx.Response(200, json={"agents": ["research"]}),
    )
    with pytest.raises(ContractError, match="not an object"):
        ledger.check_budget(agent="research")


def test_an_empty_agents_list_is_still_a_plain_lucnt_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No budget configured is a normal outcome, not a contract break."""
    _stub(monkeypatch, lambda request: httpx.Response(200, json={"agents": []}))
    with pytest.raises(LucntError, match="no budget found") as excinfo:
        ledger.check_budget(agent="research")
    assert not isinstance(excinfo.value, ContractError)


def test_a_drifted_transaction_shape_raises_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub(monkeypatch, lambda request: httpx.Response(200, json={"id": "tx_1"}))
    verdict = Verdict(authorization_id="auth_1", status="approved")
    with pytest.raises(ContractError, match="Transaction"):
        ledger.capture(verdict)


def test_a_4xx_still_raises_its_typed_api_error_not_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract checking runs on success bodies only; 4xx mapping is unchanged."""
    from lucnt.exceptions import InvalidRequestError

    _stub(
        monkeypatch,
        lambda request: httpx.Response(422, json={"detail": "bad currency"}),
    )
    with pytest.raises(InvalidRequestError):
        _authorize()
