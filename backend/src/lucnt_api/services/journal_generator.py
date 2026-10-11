"""Journal generator: transaction + tax treatment -> balanced double-entry lines.

Pure; booked in functional currency. One shape covers every tax case:

    Cr Wallet(gross)
    Dr InputTaxCredit[head](reclaim)  per head
    Cr GSTPayable[head](tax)          per head, reverse charge only
    Dr Expense(gross + Σpayable − Σreclaim)
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

# Whole-token match ("roads" must not hit "ads"); plurals listed explicitly.
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
    """Pick the expense account: known category, else AI/travel/marketing/software."""
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
    """Produce balanced double-entry lines for a captured transaction (with an id).

    Raises:
        ValueError: if the generated journal does not balance (a bug).
    """
    currency: str = transaction.local_currency
    ledger = entity.code
    txn_id = transaction.id
    company_id = transaction.company_id
    entity_id = transaction.entity_id
    posting_date = transaction.transaction_date.date()

    def line(
        account: Account, *, debit: Decimal = _ZERO, credit: Decimal = _ZERO
    ) -> JournalEntry:
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
    # gross includes tax for a tax-exclusive domestic supply; expense is the residual.
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
    # Blocked ITC has reclaim 0, so no leg: the GST stays inside the expense.
    for comp in tax.components:
        if comp.reclaim_amount > _ZERO:
            lines.append(
                line(accounts[ITC_BY_HEAD[comp.head]], debit=comp.reclaim_amount)
            )
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
