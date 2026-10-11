"""Default Chart of Accounts and idempotent provisioning (single source of truth)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucnt_api.models import Account, AccountCategory

WALLET_CODE = "1010"
INPUT_TAX_CREDIT_CODE = "1400"
GST_PAYABLE_CODE = "2200"
# Per-head ITC/payable accounts so GSTR-3B can be produced. India intra-state is
# CGST+SGST, inter-state/import IGST; the unsplit "GST" head uses 1400/2200.
CGST_ITC_CODE = "1410"
SGST_ITC_CODE = "1420"
IGST_ITC_CODE = "1430"
CGST_PAYABLE_CODE = "2210"
SGST_PAYABLE_CODE = "2220"
IGST_PAYABLE_CODE = "2230"
# Net GST owed after a return-cycle settlement (output GST less utilised ITC).
GST_SETTLEMENT_PAYABLE_CODE = "2300"
# US self-assessed use tax: a cost, never reclaimable, so payable but no ITC.
US_USE_TAX_PAYABLE_CODE = "2400"

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
    "US_USE_TAX": US_USE_TAX_PAYABLE_CODE,
}
CONTRIBUTED_CAPITAL_CODE = "3000"
RETAINED_EARNINGS_CODE = "3900"
EXPENSE_SOFTWARE_CODE = "6010"
EXPENSE_AI_COMPUTE_CODE = "6020"
EXPENSE_TRAVEL_CODE = "6030"
EXPENSE_MARKETING_CODE = "6040"
EXPENSE_GENERAL_CODE = "6090"

# An unknown caller category falls back to vendor/model inference.
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
    """Get-or-create the default accounts for a company; return them by code.

    Flushes but does not commit. Each insert is savepoint-isolated so a concurrent
    first capture never 500s after the vendor was paid.
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
            # Lost the race to a concurrent capture: adopt the committed row.
            existing[spec.code] = db.execute(
                select(Account).where(
                    Account.company_id == company_id,
                    Account.account_code == spec.code,
                )
            ).scalar_one()
    return existing
