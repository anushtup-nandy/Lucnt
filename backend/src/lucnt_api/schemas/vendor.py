"""Pydantic schemas for the vendors router (PRODUCTION_PLAN T1-3/T1-9).

The vendors registry carries place of supply — a vendor's country/state/registration
decide RCM and the CGST/SGST/IGST head split at capture. A finance admin enriches
auto-created vendors here; the SDK wire contract is untouched (``vendor`` stays a
free string on `SpendRequest`).
"""

from __future__ import annotations

import re
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ISO_3166_ALPHA2 = re.compile(r"[A-Z]{2}")


class VendorRead(BaseModel):
    """A vendor registry row."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    name: str
    country: str | None
    state: str | None
    gstin: str | None
    is_registered: bool


class VendorListResponse(BaseModel):
    """`GET /vendors`."""

    model_config = ConfigDict(frozen=True)

    items: list[VendorRead]
    total: int


class VendorCreate(BaseModel):
    """`POST /vendors` — register a vendor with its place of supply (admin)."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=255)
    country: str | None = Field(default=None, max_length=2)
    state: str | None = Field(default=None, max_length=10)
    gstin: str | None = Field(default=None, max_length=20)
    is_registered: bool = False

    @field_validator("country")
    @classmethod
    def _normalise_country(cls, value: str | None) -> str | None:
        """Upper-case and validate the ISO-3166 alpha-2 shape (two letters)."""
        if value is None:
            return None
        code = value.strip().upper()
        if not _ISO_3166_ALPHA2.fullmatch(code):
            raise ValueError("country must be a 2-letter ISO-3166 alpha-2 code")
        return code

    @field_validator("state")
    @classmethod
    def _normalise_state(cls, value: str | None) -> str | None:
        """Upper-case the state code (e.g. Indian state ``KA``)."""
        return value.strip().upper() if value else None


class VendorUpdate(BaseModel):
    """`PATCH /vendors/{id}` — enrich an (often auto-created) vendor (admin).

    Every field is optional; only the ones provided are updated.
    """

    model_config = ConfigDict(frozen=True)

    country: str | None = Field(default=None, max_length=2)
    state: str | None = Field(default=None, max_length=10)
    gstin: str | None = Field(default=None, max_length=20)
    is_registered: bool | None = None

    @field_validator("country")
    @classmethod
    def _normalise_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = value.strip().upper()
        if not _ISO_3166_ALPHA2.fullmatch(code):
            raise ValueError("country must be a 2-letter ISO-3166 alpha-2 code")
        return code

    @field_validator("state")
    @classmethod
    def _normalise_state(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None
