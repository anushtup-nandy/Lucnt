"""Journal generator (backend/PLAN.md §5, Module 2/4).

Maps a captured transaction + its tax treatment onto double-entry journal
lines against the company's Chart of Accounts. Pure: it builds and returns
unsaved `JournalEntry` rows; the caller adds them to the session.

The four cases, all of which balance by construction (Σ debit == Σ credit):

    imported (reverse charge):  Dr Expense(paid) / Cr Wallet(paid)
                                Dr InputTaxCredit(tax) / Cr GSTPayable(tax)
    domestic (reclaimable):     Dr Expense(paid-tax) + Dr InputTaxCredit(tax)
                                                       / Cr Wallet(paid)
    no reclaimable tax (US):    Dr Expense(paid) / Cr Wallet(paid)

A journal that does not balance is a bug, not a data condition — the generator
asserts and raises rather than persisting a broken ledger.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from lucnt.models import Account, Entity, JournalEntry, Transaction
from lucnt.services.chart_of_accounts import (
    EXPENSE_AI_COMPUTE_CODE,
    EXPENSE_MARKETING_CODE,
    EXPENSE_SOFTWARE_CODE,
    EXPENSE_TRAVEL_CODE,
    GST_PAYABLE_CODE,
    INPUT_TAX_CREDIT_CODE,
    WALLET_CODE,
)
from lucnt.services.tax_engine import TaxTreatment

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
    *,
    debit: Decimal = _ZERO,
    credit: Decimal = _ZERO,
) -> JournalEntry:
    """Build one unsaved journal line against `account`."""
    return JournalEntry(
        transaction_id=transaction_id,
        account_name=account.account_name,
        account_code=account.account_code,
        debit=debit,
        credit=credit,
        ledger=ledger,
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
    paid: Decimal = transaction.paid_amount
    ledger = entity.code
    txn_id = transaction.id

    expense_code = _select_expense_code(
        transaction.vendor, transaction.model_used, transaction.tokens_consumed
    )
    reclaim = tax.reclaim_amount if tax.is_reclaimable else _ZERO
    # For domestic GST the tax is embedded in `paid`; the pure expense is the
    # net. For reverse charge / no-tax the expense is the full paid amount.
    expense_amount = paid if tax.reverse_charge else paid - reclaim

    lines = [
        _line(txn_id, accounts[expense_code], ledger, debit=expense_amount),
        _line(txn_id, accounts[WALLET_CODE], ledger, credit=paid),
    ]
    if reclaim > _ZERO:
        lines.append(
            _line(txn_id, accounts[INPUT_TAX_CREDIT_CODE], ledger, debit=reclaim)
        )
    if tax.reverse_charge and tax.tax_amount > _ZERO:
        lines.append(
            _line(txn_id, accounts[GST_PAYABLE_CODE], ledger, credit=tax.tax_amount)
        )

    total_debit = sum((line.debit for line in lines), _ZERO)
    total_credit = sum((line.credit for line in lines), _ZERO)
    if total_debit != total_credit:
        raise ValueError(
            f"unbalanced journal for transaction {txn_id}: "
            f"debit {total_debit} != credit {total_credit}"
        )
    return lines
