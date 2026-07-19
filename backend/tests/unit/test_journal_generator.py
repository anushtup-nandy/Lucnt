"""Unit tests for the pure journal generator (no DB).

Models are instantiated in memory (never added to a session), so these assert
the accounting logic in isolation. The key invariant under test: every
generated journal balances (Σ debit == Σ credit).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from lucnt_api.models import (
    Account,
    AccountCategory,
    Entity,
    Transaction,
    TransactionSource,
)
from lucnt_api.services.chart_of_accounts import (
    EXPENSE_AI_COMPUTE_CODE,
    EXPENSE_MARKETING_CODE,
    EXPENSE_SOFTWARE_CODE,
    EXPENSE_TRAVEL_CODE,
    GST_PAYABLE_CODE,
    INPUT_TAX_CREDIT_CODE,
    WALLET_CODE,
)
from lucnt_api.services.journal_generator import generate_journal
from lucnt_api.services.tax_engine import compute_tax

_ALL_CODES = (
    WALLET_CODE,
    INPUT_TAX_CREDIT_CODE,
    GST_PAYABLE_CODE,
    EXPENSE_SOFTWARE_CODE,
    EXPENSE_AI_COMPUTE_CODE,
    EXPENSE_TRAVEL_CODE,
    EXPENSE_MARKETING_CODE,
)


def _accounts() -> dict[str, Account]:
    """A minimal in-memory Chart of Accounts keyed by code."""
    return {
        code: Account(
            account_code=code,
            account_name=f"acct-{code}",
            category=AccountCategory.EXPENSE,
            normal_balance="debit",
        )
        for code in _ALL_CODES
    }


def _txn(
    *,
    paid_amount: Decimal,
    paid_currency: str,
    vendor: str = "acme",
    model_used: str | None = None,
    tokens_consumed: int | None = None,
    category: str | None = None,
) -> Transaction:
    """An in-memory captured transaction with an explicit id (not flushed)."""
    return Transaction(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        entity_id=uuid.uuid4(),
        source=TransactionSource.SDK,
        vendor=vendor,
        paid_amount=paid_amount,
        paid_currency=paid_currency,
        local_amount=paid_amount,
        local_currency=paid_currency,
        category=category,
        model_used=model_used,
        tokens_consumed=tokens_consumed,
    )


def _debit(lines: list, code: str) -> Decimal:
    matches = (line.debit for line in lines if line.account_code == code)
    return sum(matches, Decimal("0"))


def _credit(lines: list, code: str) -> Decimal:
    matches = (line.credit for line in lines if line.account_code == code)
    return sum(matches, Decimal("0"))


def _assert_balances(lines: list) -> None:
    total_debit = sum((line.debit for line in lines), Decimal("0"))
    total_credit = sum((line.credit for line in lines), Decimal("0"))
    assert total_debit == total_credit


def test_imported_reverse_charge_journal() -> None:
    """RCM: expense=paid, wallet credited paid, ITC debit == GST payable credit."""
    entity = Entity(code="in", currency="INR")
    txn = _txn(paid_amount=Decimal("100"), paid_currency="USD", model_used="gpt-5")
    tax = compute_tax(
        tax_regime="IN-GST",
        functional_amount=txn.local_amount,
        paid_currency=txn.paid_currency,
        entity_currency=entity.currency,
    )
    lines = generate_journal(txn, entity, tax, _accounts())

    _assert_balances(lines)
    assert len(lines) == 4
    # model present -> AI & Compute expense of the full paid amount
    assert _debit(lines, EXPENSE_AI_COMPUTE_CODE) == Decimal("100")
    assert _credit(lines, WALLET_CODE) == Decimal("100")
    assert _debit(lines, INPUT_TAX_CREDIT_CODE) == Decimal("18.00")
    assert _credit(lines, GST_PAYABLE_CODE) == Decimal("18.00")


def test_domestic_gst_journal_nets_expense() -> None:
    """Domestic: expense = paid - embedded tax; ITC debit; no GST payable leg."""
    entity = Entity(code="in", currency="INR")
    txn = _txn(paid_amount=Decimal("118"), paid_currency="INR")
    tax = compute_tax(
        tax_regime="IN-GST",
        functional_amount=txn.local_amount,
        paid_currency=txn.paid_currency,
        entity_currency=entity.currency,
    )
    lines = generate_journal(txn, entity, tax, _accounts())

    _assert_balances(lines)
    assert len(lines) == 3
    assert _debit(lines, EXPENSE_SOFTWARE_CODE) == Decimal("100.00")  # 118 - 18
    assert _debit(lines, INPUT_TAX_CREDIT_CODE) == Decimal("18.00")
    assert _credit(lines, WALLET_CODE) == Decimal("118")
    assert _credit(lines, GST_PAYABLE_CODE) == Decimal("0")


def test_us_no_tax_journal_is_two_lines() -> None:
    """US: a plain Dr Expense / Cr Wallet pair, no tax legs."""
    entity = Entity(code="us", currency="USD")
    txn = _txn(paid_amount=Decimal("42.50"), paid_currency="USD")
    tax = compute_tax(
        tax_regime="US-Sales-Tax",
        functional_amount=txn.local_amount,
        paid_currency=txn.paid_currency,
        entity_currency=entity.currency,
    )
    lines = generate_journal(txn, entity, tax, _accounts())

    _assert_balances(lines)
    assert len(lines) == 2
    assert _debit(lines, EXPENSE_SOFTWARE_CODE) == Decimal("42.50")
    assert _credit(lines, WALLET_CODE) == Decimal("42.50")


@pytest.mark.parametrize(
    ("vendor", "model", "tokens", "expected_code"),
    [
        ("Singapore Airlines", None, None, EXPENSE_TRAVEL_CODE),
        ("Meta Ads", None, None, EXPENSE_MARKETING_CODE),
        ("OpenAI", "gpt-5", 1000, EXPENSE_AI_COMPUTE_CODE),
        ("SomeSaaS", None, None, EXPENSE_SOFTWARE_CODE),
    ],
)
def test_expense_account_selection(
    vendor: str, model: str | None, tokens: int | None, expected_code: str
) -> None:
    """The expense account is chosen from vendor/model signals."""
    entity = Entity(code="us", currency="USD")
    txn = _txn(
        paid_amount=Decimal("10"),
        paid_currency="USD",
        vendor=vendor,
        model_used=model,
        tokens_consumed=tokens,
    )
    tax = compute_tax(
        tax_regime="US-Sales-Tax",
        functional_amount=txn.local_amount,
        paid_currency=txn.paid_currency,
        entity_currency=entity.currency,
    )
    lines = generate_journal(txn, entity, tax, _accounts())
    assert _debit(lines, expected_code) == Decimal("10")


def test_caller_category_overrides_vendor_inference() -> None:
    """An explicit, known caller category wins over vendor inference (T1-19)."""
    entity = Entity(code="us", currency="USD")
    txn = _txn(
        paid_amount=Decimal("10"), paid_currency="USD",
        vendor="mystery vendor", category="travel",
    )
    tax = compute_tax(
        tax_regime="US-Sales-Tax", functional_amount=txn.local_amount,
        paid_currency=txn.paid_currency, entity_currency=entity.currency,
    )
    lines = generate_journal(txn, entity, tax, _accounts())
    assert _debit(lines, EXPENSE_TRAVEL_CODE) == Decimal("10")


@pytest.mark.parametrize(
    ("vendor", "expected_code"),
    [
        ("Metabase", EXPENSE_SOFTWARE_CODE),  # not Marketing via substring "meta"
        ("Roads Logistics", EXPENSE_SOFTWARE_CODE),  # not Marketing via "ads"
        ("Singapore Airlines", EXPENSE_TRAVEL_CODE),  # plural token matches
    ],
)
def test_whole_token_vendor_matching(vendor: str, expected_code: str) -> None:
    """Vendor classification matches whole tokens, not substrings (T1-19)."""
    entity = Entity(code="us", currency="USD")
    txn = _txn(paid_amount=Decimal("10"), paid_currency="USD", vendor=vendor)
    tax = compute_tax(
        tax_regime="US-Sales-Tax", functional_amount=txn.local_amount,
        paid_currency=txn.paid_currency, entity_currency=entity.currency,
    )
    lines = generate_journal(txn, entity, tax, _accounts())
    assert _debit(lines, expected_code) == Decimal("10")


def test_blocked_itc_rcm_books_tax_into_expense() -> None:
    """Blocked-ITC RCM (IN travel): no input-credit leg; GST is a cost (T1-14)."""
    entity = Entity(code="in", currency="INR")
    txn = _txn(
        paid_amount=Decimal("100"), paid_currency="USD",
        vendor="grand hotel", category="travel",
    )
    tax = compute_tax(
        tax_regime="IN-GST", functional_amount=txn.local_amount,
        paid_currency=txn.paid_currency, entity_currency=entity.currency,
        expense_category="travel",
    )
    lines = generate_journal(txn, entity, tax, _accounts())
    _assert_balances(lines)
    assert len(lines) == 3  # expense, wallet, GST payable — no ITC leg
    assert _debit(lines, EXPENSE_TRAVEL_CODE) == Decimal("118.0000")  # 100 + 18 GST
    assert _credit(lines, WALLET_CODE) == Decimal("100")
    assert _credit(lines, GST_PAYABLE_CODE) == Decimal("18.0000")
    assert _debit(lines, INPUT_TAX_CREDIT_CODE) == Decimal("0")


def test_ledger_is_entity_code() -> None:
    """Every line is tagged with the entity's code as its ledger."""
    entity = Entity(code="sg", currency="SGD")
    txn = _txn(paid_amount=Decimal("100"), paid_currency="USD")
    tax = compute_tax(
        tax_regime="SG-GST",
        functional_amount=txn.local_amount,
        paid_currency=txn.paid_currency,
        entity_currency=entity.currency,
    )
    lines = generate_journal(txn, entity, tax, _accounts())
    assert all(line.ledger == "sg" for line in lines)
