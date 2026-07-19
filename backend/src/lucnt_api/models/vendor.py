"""Vendor ORM model — the place-of-supply registry (PRODUCTION_PLAN T1-3/T1-9).

A company-scoped record of who an agent spends with. Its purpose is **place of
supply**: the vendor's ``country``/``state``/``is_registered`` decide whether a
spend is an import (reverse charge → IGST) or a domestic supply, and for India
whether domestic GST splits into CGST+SGST (intra-state) or IGST (inter-state).
Deciding this from *currency* was wrong in both directions (T1-3); the registry
is the correct signal.

A spend to an unknown vendor auto-creates an **unregistered** row with no
place-of-supply data; the tax engine then falls back to the currency heuristic
(labelled) until a finance admin enriches it via `PATCH /vendors/{id}`. The
caller still passes ``vendor`` as a free string — no frozen-wire change — so this
is "state the fact, system judges" moved to the company-config layer. The same
registry seeds M3 invoice matching and recon vendor grouping later.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
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
    # Display name as first seen; `normalized_name` (lower/trimmed) is the lookup key
    # so "OpenAI"/"openai " resolve to one vendor.
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255))
    # Place of supply — all nullable: an auto-created vendor is unenriched until a
    # finance admin fills these, and the tax engine falls back until then.
    country: Mapped[str | None] = mapped_column(String(2), default=None)  # ISO-3166-2
    state: Mapped[str | None] = mapped_column(String(10), default=None)  # e.g. IN state
    gstin: Mapped[str | None] = mapped_column(String(20), default=None)
    # Whether the vendor is GST-registered (charges forward GST). An unregistered
    # domestic vendor can flip a supply to reverse charge under CGST s.9(4).
    is_registered: Mapped[bool] = mapped_column(default=False)
