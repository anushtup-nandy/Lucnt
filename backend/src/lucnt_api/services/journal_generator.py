"""Journal generator (backend/PLAN.md §5, Module 2/4).

Maps a captured transaction + its tax treatment onto double-entry journal
lines against the company's Chart of Accounts. Pure: it builds and returns
unsaved `JournalEntry` rows; the caller adds them to the session.

All amounts are booked in the entity's **functional currency**
(``transaction.local_amount``, FX-converted at capture — PRODUCTION_PLAN T1-1),
never the raw paid amount, so a single entity's journal never mixes currencies.

The four cases, all of which balance by construction (Σ debit == Σ credit):

    imported (reverse charge):  Dr Expense(func) / Cr Wallet(func)
                                Dr InputTaxCredit(tax) / Cr GSTPayable(tax)
    domestic (reclaimable):     Dr Expense(func-tax) + Dr InputTaxCredit(tax)
                                                       / Cr Wallet(func)
    no reclaimable tax (US):    Dr Expense(func) / Cr Wallet(func)

A journal that does not balance is a bug, not a data condition — the generator
asserts and raises rather than persisting a broken ledger.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from lucnt_api.models import Account, Entity, JournalEntry, Transaction
from lucnt_api.services.chart_of_accounts import (
    EXPENSE_AI_COMPUTE_CODE,
    EXPENSE_MARKETING_CODE,
    EXPENSE_SOFTWARE_CODE,
    EXPENSE_TRAVEL_CODE,
    GST_PAYABLE_CODE,
    INPUT_TAX_CREDIT_CODE,
    WALLET_CODE,
)
from lucnt_api.services.tax_engine import TaxTreatment

_ZERO = Decimal("0")

_TRAVEL_KEYWORDS = (
    "airline", "airways", "booking", "expedia", "flight", "travel", "hotel"
)
_MARKETING_KEYWORDS = ("meta", "google ads", "canva", "ads", "marketing", "linkedin")


def _select_expense_code(
    vendor: str, model_used: str | None, tokens_consumed: int | None
) -> str:
    """Pick the expense account for a spend from its vendor / model signals.

    LLM/API spend (a model or token count is present) books to AI & Compute;
    otherwise a vendor-keyword heuristic routes to Travel or Marketing, falling
    back to Software & Subscriptions (the common case for agent SaaS commerce).
    """
    if model_used or tokens_consumed:
        return EXPENSE_AI_COMPUTE_CODE
    vendor_lc = vendor.lower()
    if any(keyword in vendor_lc for keyword in _TRAVEL_KEYWORDS):
        return EXPENSE_TRAVEL_CODE
    if any(keyword in vendor_lc for keyword in _MARKETING_KEYWORDS):
        return EXPENSE_MARKETING_CODE
    return EXPENSE_SOFTWARE_CODE


def _line(
    transaction_id: uuid.UUID,
    account: Account,
    ledger: str,
    currency: str,
    *,
    debit: Decimal = _ZERO,
    credit: Decimal = _ZERO,
) -> JournalEntry:
    """Build one unsaved journal line against `account`, in the functional currency."""
    return JournalEntry(
        transaction_id=transaction_id,
        account_name=account.account_name,
        account_code=account.account_code,
        debit=debit,
        credit=credit,
        ledger=ledger,
        currency=currency,
    )


def generate_journal(
    transaction: Transaction,
    entity: Entity,
    tax: TaxTreatment,
    accounts: dict[str, Account],
) -> list[JournalEntry]:
    """Produce balanced double-entry lines for a captured transaction.

    Args:
        transaction: The committed transaction (must already have an ``id``).
        entity: The transaction's legal entity (its ``code`` is the ledger).
        tax: The tax treatment from `tax_engine.compute_tax`.
        accounts: The company's Chart of Accounts, keyed by code
            (from `chart_of_accounts.ensure_default_accounts`).

    Returns:
        Unsaved `JournalEntry` rows whose debits equal their credits.

    Raises:
        ValueError: if the generated journal does not balance (a bug).
    """
    # All legs are in the entity's functional currency (T1-1); the tax amounts
    # from `tax_engine` are already functional too (T1-4), so the journal is
    # single-currency and balances.
    functional: Decimal = transaction.local_amount
    currency: str = transaction.local_currency
    ledger = entity.code
    txn_id = transaction.id

    expense_code = _select_expense_code(
        transaction.vendor, transaction.model_used, transaction.tokens_consumed
    )
    reclaim = tax.reclaim_amount if tax.is_reclaimable else _ZERO
    # For domestic GST the tax is embedded in the functional amount; the pure
    # expense is the net. For reverse charge / no-tax it is the full amount.
    expense_amount = functional if tax.reverse_charge else functional - reclaim

    lines = [
        _line(txn_id, accounts[expense_code], ledger, currency, debit=expense_amount),
        _line(txn_id, accounts[WALLET_CODE], ledger, currency, credit=functional),
    ]
    if reclaim > _ZERO:
        lines.append(
            _line(
                txn_id, accounts[INPUT_TAX_CREDIT_CODE], ledger, currency, debit=reclaim
            )
        )
    if tax.reverse_charge and tax.tax_amount > _ZERO:
        lines.append(
            _line(
                txn_id,
                accounts[GST_PAYABLE_CODE],
                ledger,
                currency,
                credit=tax.tax_amount,
            )
        )

    total_debit = sum((line.debit for line in lines), _ZERO)
    total_credit = sum((line.credit for line in lines), _ZERO)
    if total_debit != total_credit:
        raise ValueError(
            f"unbalanced journal for transaction {txn_id}: "
            f"debit {total_debit} != credit {total_credit}"
        )
    return lines
