"""Chart of Accounts — the default account set and idempotent provisioning.

A company's books need a small set of accounts before any journal can post.
`ensure_default_accounts` is idempotent (get-or-create by code), so capture
works for any company — seeded or created via the API — without a separate
setup step. It is the single source of truth for the default CoA, reused by
`scripts/seed_dev.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.models import Account, AccountCategory

# Account codes referenced by the journal generator. Named so callers never
# hard-code a magic string (CLAUDE.md §2.3: no magic numbers).
WALLET_CODE = "1010"
INPUT_TAX_CREDIT_CODE = "1400"
GST_PAYABLE_CODE = "2200"
EXPENSE_SOFTWARE_CODE = "6010"
EXPENSE_AI_COMPUTE_CODE = "6020"
EXPENSE_TRAVEL_CODE = "6030"
EXPENSE_MARKETING_CODE = "6040"
EXPENSE_GENERAL_CODE = "6090"


@dataclass(frozen=True)
class _AccountSpec:
    """One line of the default Chart of Accounts template."""

    code: str
    name: str
    category: AccountCategory


_DEFAULT_ACCOUNTS: tuple[_AccountSpec, ...] = (
    _AccountSpec(WALLET_CODE, "Cash & Wallet", AccountCategory.ASSET),
    _AccountSpec(
        INPUT_TAX_CREDIT_CODE,
        "Input Tax Credit (GST Receivable)",
        AccountCategory.ASSET,
    ),
    _AccountSpec(
        GST_PAYABLE_CODE, "GST Payable (Reverse Charge)", AccountCategory.LIABILITY
    ),
    _AccountSpec(
        EXPENSE_SOFTWARE_CODE, "Software & Subscriptions", AccountCategory.EXPENSE
    ),
    _AccountSpec(EXPENSE_AI_COMPUTE_CODE, "AI & Compute", AccountCategory.EXPENSE),
    _AccountSpec(EXPENSE_TRAVEL_CODE, "Travel", AccountCategory.EXPENSE),
    _AccountSpec(
        EXPENSE_MARKETING_CODE, "Marketing & Advertising", AccountCategory.EXPENSE
    ),
    _AccountSpec(
        EXPENSE_GENERAL_CODE, "General & Administrative", AccountCategory.EXPENSE
    ),
)


def ensure_default_accounts(db: Session, company_id: uuid.UUID) -> dict[str, Account]:
    """Ensure the default Chart of Accounts exists for a company; return it by code.

    Creates any missing default accounts (idempotent — safe to call on every
    capture). Flushes so newly created accounts have ids, but does not commit;
    the caller owns the transaction.

    Returns:
        A mapping of ``account_code`` -> `Account` covering the full default set.
    """
    existing = {
        account.account_code: account
        for account in db.execute(
            select(Account).where(Account.company_id == company_id)
        ).scalars()
    }

    created = False
    for spec in _DEFAULT_ACCOUNTS:
        if spec.code in existing:
            continue
        account = Account(
            company_id=company_id,
            account_name=spec.name,
            account_code=spec.code,
            category=spec.category,
            normal_balance=spec.category.normal_balance,
        )
        db.add(account)
        existing[spec.code] = account
        created = True

    if created:
        db.flush()
    return existing
