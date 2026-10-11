"""Legal Entity ORM model — one per jurisdiction/subsidiary within a company."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from lucnt_api.database import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Entity(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A legal entity (subsidiary/jurisdiction) belonging to a company."""

    __tablename__ = "entities"
    __table_args__ = (
        # An entity code is the lookup key `authorize()` and every statement
        # resolve on. A duplicate makes `scalar_one_or_none()` raise
        # MultipleResultsFound — a 500 that takes the spend path down for that
        # entity. Mirrors uq_agents_company_slug; also indexes the lookup.
        Index("uq_entities_company_code", "company_id", "code", unique=True),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str] = mapped_column(String(255))
    code: Mapped[str] = mapped_column(String(50))
    full_name: Mapped[str] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3))
    # The entity's accounting standard (IFRS/Ind-AS/US-GAAP). Reserved for the
    # FX consolidation roadmap — it selects the translation method and the
    # CTA/gain-loss treatment when a group's per-entity books are translated to a
    # single presentation currency. Kept (not dropped) because that workstream
    # will consult it; consulted nowhere until then.
    accounting_std: Mapped[str] = mapped_column(String(50))
    erp: Mapped[str | None] = mapped_column(String(50), default=None)
    tax_regime: Mapped[str] = mapped_column(String(50))
    # Place of supply of the *recipient*. `country` (ISO-3166 alpha-2)
    # decides import vs domestic against the vendor's country; `gst_state` (Indian
    # state code) decides intra-state (CGST+SGST) vs inter-state (IGST) for India.
    # Nullable: back-filled from `tax_regime`; `gst_state` set per-entity later.
    country: Mapped[str | None] = mapped_column(String(2), default=None)
    gst_state: Mapped[str | None] = mapped_column(String(10), default=None)
    # The buyer's US state (2-letter, e.g. "TX") for US sales/use-tax lookup by
    # `US-{state}` jurisdiction. Distinct from `gst_state` (India CGST/SGST
    # split); an entity is in one country, so at most one is set. Nullable: a US
    # entity without it routes to the recon exception queue rather than a silent 0.
    us_state: Mapped[str | None] = mapped_column(String(2), default=None)
    # NUMERIC, never FLOAT -- FX rate is money math (Decimal, not float).
    to_usd_rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("1.0"))
    timezone: Mapped[str] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(default=True)
