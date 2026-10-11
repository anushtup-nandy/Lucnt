"""Account ORM model: one line of a company's Chart of Accounts."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class AccountCategory(enum.StrEnum):
    """The five standard account categories. Determines the normal balance side."""

    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"

    @property
    def normal_balance(self) -> str:
        """The side (`debit`/`credit`) on which this category normally increases."""
        debit_side = (AccountCategory.ASSET, AccountCategory.EXPENSE)
        return "debit" if self in debit_side else "credit"


class Account(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One account in a company's Chart of Accounts (double-entry bookkeeping)."""

    __tablename__ = "accounts"
    __table_args__ = (
        # The journal generator resolves accounts by code.
        UniqueConstraint("company_id", "account_code", name="uq_accounts_company_code"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    account_name: Mapped[str] = mapped_column(String(255))
    account_code: Mapped[str] = mapped_column(String(20))
    category: Mapped[AccountCategory] = mapped_column(
        Enum(
            AccountCategory,
            name="account_category",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    # Derived from `category` at creation, stored for cheap trial-balance math.
    normal_balance: Mapped[str] = mapped_column(String(6))
    is_active: Mapped[bool] = mapped_column(default=True)
