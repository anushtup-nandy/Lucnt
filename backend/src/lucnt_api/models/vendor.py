"""Vendor ORM model: the company's place-of-supply registry.

Vendor country/state/registration (not currency) decide import vs domestic tax.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Vendor(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A supplier an agent spends with, carrying its place-of-supply attributes."""

    __tablename__ = "vendors"
    __table_args__ = (
        # One row per (company, normalized name); resolve-or-create dedupes on it.
        UniqueConstraint(
            "company_id", "normalized_name", name="uq_vendors_company_normalized_name"
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    # `normalized_name` (lower/trimmed) is the lookup key: "OpenAI" == "openai ".
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255))
    # Nullable: auto-created vendors are unenriched; the tax engine falls back.
    country: Mapped[str | None] = mapped_column(String(2), default=None)  # ISO-3166-2
    state: Mapped[str | None] = mapped_column(String(10), default=None)  # e.g. IN state
    gstin: Mapped[str | None] = mapped_column(String(20), default=None)
    # Reverse-charge test is registration in the *buyer's* country, hence a list:
    # OIDAR-registered foreign vendor -> forward IGST; unregistered domestic
    # vendor -> RCM under CGST s.9(4).
    gst_registered_in: Mapped[list[str]] = mapped_column(
        ARRAY(String(2)), default=list, server_default="{}"
    )
    # India s.195: valid TRC + Form 10F let the payer apply the lower DTAA rate.
    trc_on_file: Mapped[bool] = mapped_column(default=False)
    trc_expiry: Mapped[date | None] = mapped_column(Date, default=None)
    form_10f_on_file: Mapped[bool] = mapped_column(default=False)

    @property
    def is_registered(self) -> bool:
        """Whether the vendor is GST-registered anywhere (derived, for display)."""
        return bool(self.gst_registered_in)
