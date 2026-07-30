"""Journal generator (backend/PLAN.md §5, Module 2/4).

Maps a captured transaction + its tax treatment onto double-entry journal
lines against the company's Chart of Accounts. Pure: it builds and returns
unsaved `JournalEntry` rows; the caller adds them to the session.

All amounts are booked in the entity's **functional currency** (FX-converted at
capture — PRODUCTION_PLAN T1-1), never the raw paid amount, so a single entity's
journal never mixes currencies.

One **unified** double-entry that balances by construction (Σ debit == Σ credit)
covers every case — reverse charge, domestic inclusive/exclusive (T1-10), the
CGST/SGST/IGST head split (T1-9), blocked ITC (T1-14) and US no-tax:

    Cr Wallet(gross)                              # cash out (T1-10 gross_amount)
    Dr InputTaxCredit[head](reclaim)  per head    # capitalised input credit (T1-9)
    Cr GSTPayable[head](tax)          per head     # reverse-charge output GST only
    Dr Expense(gross + Σpayable − Σreclaim)        # the residual that balances it

A journal that does not balance is a bug, not a data condition — the generator
asserts and raises rather than persisting a broken ledger.
"""

from __future__ import annotations

import re
from decimal import Decimal

from lucnt_api.models import Account, Entity, JournalEntry, Transaction
from lucnt_api.services.chart_of_accounts import (
    CATEGORY_TO_ACCOUNT,
    EXPENSE_AI_COMPUTE_CODE,
    EXPENSE_MARKETING_CODE,
    EXPENSE_SOFTWARE_CODE,
    EXPENSE_TRAVEL_CODE,
    GST_PAYABLE_BY_HEAD,
    ITC_BY_HEAD,
    WALLET_CODE,
)
from lucnt_api.services.tax_engine import TaxTreatment

_ZERO = Decimal("0")

# Whole-token vendor keywords (T1-19): matched against the vendor's tokenised
# words, so "roads" no longer matches "ads" and "metabase" no longer matches
# "meta". Plurals are listed explicitly (word-boundary alone misses "airlines").
_TRAVEL_KEYWORDS = frozenset(
    {
        "airline",
        "airlines",
        "airways",
        "flight",
        "flights",
        "travel",
        "hotel",
        "hotels",
        "booking",
        "expedia",
    }
)
_MARKETING_KEYWORDS = frozenset({"ads", "meta", "canva", "marketing", "linkedin"})


def _select_expense_code(
    vendor: str,
    model_used: str | None,
    tokens_consumed: int | None,
    category: str | None,
) -> str:
    """Pick the expense account for a spend from caller category / vendor signals.

    An explicit, known caller `category` wins (T1-19); otherwise LLM/API spend (a
    model or token count) books to AI & Compute, and a **whole-token** vendor
    match routes to Travel or Marketing, falling back to Software & Subscriptions.
    """
    if category is not None:
        code = CATEGORY_TO_ACCOUNT.get(category.strip().lower())
        if code is not None:
            return code
    if model_used or tokens_consumed:
        return EXPENSE_AI_COMPUTE_CODE
    tokens = set(re.split(r"[^a-z0-9]+", vendor.lower()))
    if tokens & _TRAVEL_KEYWORDS:
        return EXPENSE_TRAVEL_CODE
    if tokens & _MARKETING_KEYWORDS:
        return EXPENSE_MARKETING_CODE
    return EXPENSE_SOFTWARE_CODE


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
    currency: str = transaction.local_currency
    ledger = entity.code
    txn_id = transaction.id
    company_id = transaction.company_id
    entity_id = transaction.entity_id
    posting_date = transaction.transaction_date.date()

    def line(
        account: Account, *, debit: Decimal = _ZERO, credit: Decimal = _ZERO
    ) -> JournalEntry:
        """One unsaved, self-sufficient line in the functional currency (T1-21)."""
        return JournalEntry(
            transaction_id=txn_id,
            company_id=company_id,
            entity_id=entity_id,
            posting_date=posting_date,
            account_name=account.account_name,
            account_code=account.account_code,
            debit=debit,
            credit=credit,
            ledger=ledger,
            currency=currency,
        )

    expense_code = _select_expense_code(
        transaction.vendor,
        transaction.model_used,
        transaction.tokens_consumed,
        transaction.category,
    )
    # Unified double-entry (T1-9/T1-10). The wallet pays the gross cash-out
    # (``tax.gross_amount`` — functional for inclusive/RCM, functional+tax for a
    # tax-exclusive domestic supply); the expense is the residual after per-head
    # input tax credit is capitalised and any reverse-charge output GST is booked
    # as a payable. Balances by construction: expense = gross + Σpayable − Σreclaim.
    gross = tax.gross_amount
    total_payable = (
        sum((c.tax_amount for c in tax.components), _ZERO)
        if tax.reverse_charge
        else _ZERO
    )
    total_reclaim = sum((c.reclaim_amount for c in tax.components), _ZERO)
    expense_amount = gross + total_payable - total_reclaim

    lines = [
        line(accounts[expense_code], debit=expense_amount),
        line(accounts[WALLET_CODE], credit=gross),
    ]
    # Per-head input tax credit (T1-9): one Dr ITC[head] leg per component with a
    # reclaimable amount. A blocked component carries reclaim 0, so no leg — the GST
    # stays inside the expense. India intra-state books two legs (CGST + SGST).
    for comp in tax.components:
        if comp.reclaim_amount > _ZERO:
            lines.append(
                line(accounts[ITC_BY_HEAD[comp.head]], debit=comp.reclaim_amount)
            )
    # Per-head GST payable (reverse charge only): one Cr GST-Payable[head] per
    # component that self-assesses output tax.
    if tax.reverse_charge:
        for comp in tax.components:
            if comp.tax_amount > _ZERO:
                lines.append(
                    line(
                        accounts[GST_PAYABLE_BY_HEAD[comp.head]],
                        credit=comp.tax_amount,
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
