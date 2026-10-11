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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucnt_api.models import Account, AccountCategory

# Account codes referenced by the journal generator. Named so callers never
# hard-code a magic string.
WALLET_CODE = "1010"
INPUT_TAX_CREDIT_CODE = "1400"
GST_PAYABLE_CODE = "2200"
# GST heads split by place of supply. India intra-state books CGST+SGST,
# inter-state/import books IGST; each head has its own ITC (asset) and payable
# (liability) account so GSTR-3B can be produced. The unsplit "GST" head keeps the
# legacy 1400/2200 accounts (SG, US, and the currency-fallback path).
CGST_ITC_CODE = "1410"
SGST_ITC_CODE = "1420"
IGST_ITC_CODE = "1430"
CGST_PAYABLE_CODE = "2210"
SGST_PAYABLE_CODE = "2220"
IGST_PAYABLE_CODE = "2230"
# The net GST owed to the government after a return-cycle settlement: the
# period's output GST less utilised input tax credit, cleared by a cash payment.
GST_SETTLEMENT_PAYABLE_CODE = "2300"
# US self-assessed use tax owed to a state: a no-nexus B2B buyer accrues use
# tax on a taxable-state purchase the vendor didn't charge. Never reclaimable (US
# sales/use tax is a cost, not a VAT input credit), so it has a payable but no ITC.
US_USE_TAX_PAYABLE_CODE = "2400"

# Map a tax head → its ITC (input credit, asset) and payable (liability) account.
ITC_BY_HEAD: dict[str, str] = {
    "GST": INPUT_TAX_CREDIT_CODE,
    "CGST": CGST_ITC_CODE,
    "SGST": SGST_ITC_CODE,
    "IGST": IGST_ITC_CODE,
}
GST_PAYABLE_BY_HEAD: dict[str, str] = {
    "GST": GST_PAYABLE_CODE,
    "CGST": CGST_PAYABLE_CODE,
    "SGST": SGST_PAYABLE_CODE,
    "IGST": IGST_PAYABLE_CODE,
    # US self-assessed use tax: a payable head with no ITC counterpart.
    "US_USE_TAX": US_USE_TAX_PAYABLE_CODE,
}
# Equity accounts. Contributed Capital receives wallet-funding entries;
# Retained Earnings is a real account that the balance sheet's
# undistributed-P&L line references instead of a fabricated code.
CONTRIBUTED_CAPITAL_CODE = "3000"
RETAINED_EARNINGS_CODE = "3900"
EXPENSE_SOFTWARE_CODE = "6010"
EXPENSE_AI_COMPUTE_CODE = "6020"
EXPENSE_TRAVEL_CODE = "6030"
EXPENSE_MARKETING_CODE = "6040"
EXPENSE_GENERAL_CODE = "6090"

# Caller-facing expense categories → account code. A caller may state the
# category explicitly (a fact about the purchase); otherwise the journal generator
# infers it from vendor/model signals. An unknown category falls back to inference.
CATEGORY_TO_ACCOUNT: dict[str, str] = {
    "software": EXPENSE_SOFTWARE_CODE,
    "ai_compute": EXPENSE_AI_COMPUTE_CODE,
    "travel": EXPENSE_TRAVEL_CODE,
    "marketing": EXPENSE_MARKETING_CODE,
    "general": EXPENSE_GENERAL_CODE,
}


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
    _AccountSpec(CGST_ITC_CODE, "Input Tax Credit — CGST", AccountCategory.ASSET),
    _AccountSpec(SGST_ITC_CODE, "Input Tax Credit — SGST", AccountCategory.ASSET),
    _AccountSpec(IGST_ITC_CODE, "Input Tax Credit — IGST", AccountCategory.ASSET),
    _AccountSpec(
        GST_PAYABLE_CODE, "GST Payable (Reverse Charge)", AccountCategory.LIABILITY
    ),
    _AccountSpec(CGST_PAYABLE_CODE, "GST Payable — CGST", AccountCategory.LIABILITY),
    _AccountSpec(SGST_PAYABLE_CODE, "GST Payable — SGST", AccountCategory.LIABILITY),
    _AccountSpec(IGST_PAYABLE_CODE, "GST Payable — IGST", AccountCategory.LIABILITY),
    _AccountSpec(
        GST_SETTLEMENT_PAYABLE_CODE,
        "GST Payable to Government (Net)",
        AccountCategory.LIABILITY,
    ),
    _AccountSpec(
        US_USE_TAX_PAYABLE_CODE, "US Use Tax Payable", AccountCategory.LIABILITY
    ),
    _AccountSpec(
        CONTRIBUTED_CAPITAL_CODE, "Contributed Capital", AccountCategory.EQUITY
    ),
    _AccountSpec(RETAINED_EARNINGS_CODE, "Retained Earnings", AccountCategory.EQUITY),
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

    **Concurrency.** Two first-ever captures can race to insert the same
    account code. Each insert runs in its own SAVEPOINT: if a concurrent capture
    won the `uq_accounts_company_code` unique constraint, the `IntegrityError`
    rolls back only that savepoint (not the whole capture transaction) and the
    committed row is adopted — so a capture never 500s *after* the vendor was paid.

    Returns:
        A mapping of ``account_code`` -> `Account` covering the full default set.
    """
    existing = {
        account.account_code: account
        for account in db.execute(
            select(Account).where(Account.company_id == company_id)
        ).scalars()
    }

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
        try:
            with db.begin_nested():
                db.add(account)
                db.flush()
            existing[spec.code] = account
        except IntegrityError:
            # A concurrent capture created it first — adopt the committed row.
            existing[spec.code] = db.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.account_code == spec.code,
                )
            ).scalar_one()
    return existing
