"""Pydantic schemas for the vendors router.

The vendors registry carries place of supply — a vendor's country/state/registration
decide RCM and the CGST/SGST/IGST head split at capture. A finance admin enriches
auto-created vendors here; the SDK wire contract is untouched (``vendor`` stays a
free string on `SpendRequest`).
"""

from __future__ import annotations

import re
import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ISO_3166_ALPHA2 = re.compile(r"[A-Z]{2}")


def _normalise_country_code(value: str | None) -> str | None:
    """Upper-case and validate a single ISO-3166 alpha-2 code (two letters)."""
    if value is None:
        return None
    code = value.strip().upper()
    if not _ISO_3166_ALPHA2.fullmatch(code):
        raise ValueError("country must be a 2-letter ISO-3166 alpha-2 code")
    return code


def _normalise_country_list(value: list[str] | None) -> list[str] | None:
    """Upper-case/validate each ISO-3166 alpha-2 code, de-duplicated."""
    if value is None:
        return None
    seen: dict[str, None] = {}
    for item in value:
        code = item.strip().upper()
        if not _ISO_3166_ALPHA2.fullmatch(code):
            raise ValueError("gst_registered_in entries must be ISO-3166 alpha-2 codes")
        seen[code] = None
    return list(seen)


class VendorRead(BaseModel):
    """A vendor registry row."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    name: str
    country: str | None
    state: str | None
    gstin: str | None
    # The countries the vendor is GST-registered in; `is_registered` is a
    # derived convenience (true iff registered anywhere).
    gst_registered_in: list[str]
    is_registered: bool
    # Withholding-tax documentation.
    trc_on_file: bool
    trc_expiry: date | None
    form_10f_on_file: bool


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
    # The countries the vendor is GST-registered in. Empty ⇒ unregistered
    # everywhere; an India entry makes an India supply forward charge, its absence
    # reverse charge.
    gst_registered_in: list[str] = Field(default_factory=list)
    # Withholding-tax documentation: whether a TRC / Form 10F is on file and
    # the TRC's expiry, collected at onboarding to fix the s.195 withholding rate.
    trc_on_file: bool = False
    trc_expiry: date | None = None
    form_10f_on_file: bool = False

    @field_validator("country")
    @classmethod
    def _normalise_country(cls, value: str | None) -> str | None:
        """Upper-case and validate the ISO-3166 alpha-2 shape (two letters)."""
        return _normalise_country_code(value)

    @field_validator("gst_registered_in")
    @classmethod
    def _normalise_registered(cls, value: list[str]) -> list[str]:
        """Upper-case/validate each registration country code."""
        return _normalise_country_list(value) or []

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
    # Replaces the whole registration list when present; pass ``[]`` to mark
    # a vendor unregistered everywhere.
    gst_registered_in: list[str] | None = None
    # Withholding-tax documentation; only fields present are updated.
    trc_on_file: bool | None = None
    trc_expiry: date | None = None
    form_10f_on_file: bool | None = None

    @field_validator("country")
    @classmethod
    def _normalise_country(cls, value: str | None) -> str | None:
        return _normalise_country_code(value)

    @field_validator("gst_registered_in")
    @classmethod
    def _normalise_registered(cls, value: list[str] | None) -> list[str] | None:
        return _normalise_country_list(value)

    @field_validator("state")
    @classmethod
    def _normalise_state(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None
