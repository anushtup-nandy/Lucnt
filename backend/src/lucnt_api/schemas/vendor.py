"""Vendors schemas. Place of supply decides RCM and the CGST/SGST/IGST split."""

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
    gst_registered_in: list[str]
    is_registered: bool
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
    # An India entry makes an India supply forward charge; absent => RCM.
    gst_registered_in: list[str] = Field(default_factory=list)
    # TRC / Form 10F fix the s.195 withholding rate.
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
    """`PATCH /vendors/{id}` (admin); only provided fields change."""

    model_config = ConfigDict(frozen=True)

    country: str | None = Field(default=None, max_length=2)
    state: str | None = Field(default=None, max_length=10)
    gstin: str | None = Field(default=None, max_length=20)
    # Replaces the whole list; ``[]`` marks the vendor unregistered.
    gst_registered_in: list[str] | None = None
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
