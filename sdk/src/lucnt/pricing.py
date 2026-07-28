"""Packaged pricing data pack — domain content, not operator configuration (T5-12).

Vendor token prices, the pre-call estimate heuristics, and each vendor's billing
currency live in a JSON file shipped inside the wheel rather than as module
constants in `adapters/*`. Two reasons: a new model id becomes a data change
instead of a code change (an unpriced model otherwise hard-blocks the customer's
own LLM calls — T5-10), and the table stops being source code that has to be
audited before the package goes public (T5-7d).

**No on-disk discovery, deliberately.** A library that hunts for a `lucnt.toml`
in the customer's working directory behaves differently depending on where their
process was started, and reading arbitrary files off their filesystem is a
security smell in a dependency. The pack is read from the installed package
only; per-process overrides arrive as arguments (`meter(pricing=...)`). Reading
configuration from a file stays the server's job, not a client library's, and a
server-fetched pricing feed is intended to replace this pack.

**Money is encoded as strings and parsed to `Decimal` here, at the boundary.**
A JSON number would arrive as a `float` and reintroduce exactly the binary
precision error the ledger's Decimal discipline exists to prevent, so a
non-string price is rejected rather than coerced.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib.resources import files
from typing import Final

from lucnt.exceptions import LucntError

_PACK_PACKAGE: Final = "lucnt.data"
_PACK_FILENAME: Final = "pricing.json"

# (input, output) price per 1M tokens, in the vendor's billing currency.
ModelPrices = tuple[Decimal, Decimal]

# Accepted for a caller-supplied override. `float` is absent on purpose: it is
# the one type that cannot round-trip a price exactly.
PriceInput = Decimal | int | str


@dataclass(frozen=True)
class VendorPricing:
    """One vendor's billing currency and per-model token prices."""

    currency: str
    models: Mapping[str, ModelPrices]


@dataclass(frozen=True)
class PricingPack:
    """The packaged pack: estimate heuristics plus per-vendor pricing.

    Attributes:
        version: The pack's release date, for support and drift diagnosis.
        chars_per_token: Rough characters-per-token ratio for the pre-call
            estimate. Estimate-only — never used to price actual usage.
        default_output_tokens: Assumed output length when the request sets no
            explicit cap.
        image_tokens: Tokens charged per image in the pre-call estimate (T5-10).
            A flat figure because the request cannot be priced properly without
            decoding the image: vendors bill roughly by pixel area, which is not
            knowable from a base64 payload without parsing it, and a client
            library has no business doing that on a money path. Set near the
            high-resolution ceiling so the hold never *under*-reserves — an
            over-reservation is released at capture, whereas an under-reservation
            lets a spend through a budget the policy engine believed had room.
        document_page_tokens: Tokens charged per PDF/document page in the
            estimate.
        default_document_pages: Assumed page count for an attached document.
            Page count is genuinely unknowable pre-call, so this is a stated
            assumption rather than a measurement.
        vendors: Per-vendor currency and model prices, keyed by vendor name.
    """

    version: str
    chars_per_token: int
    default_output_tokens: int
    image_tokens: int
    document_page_tokens: int
    default_document_pages: int
    vendors: Mapping[str, VendorPricing]


def _price(raw: object, where: str) -> Decimal:
    """Parse one price string into a `Decimal`, rejecting anything else.

    Args:
        raw: The value read from the pack.
        where: Dotted location, used in the error message.

    Returns:
        The parsed price.

    Raises:
        LucntError: The value is not a string, or is not a valid decimal.
    """
    if not isinstance(raw, str):
        raise LucntError(
            f"pricing pack {where} is {type(raw).__name__}, not a string: money "
            f"must be a string so it parses to Decimal exactly (a JSON number "
            f"would arrive as a float)."
        )
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise LucntError(f"pricing pack {where} is not a number: {raw!r}") from exc


def _parse_models(payload: object, vendor: str) -> dict[str, ModelPrices]:
    """Parse one vendor's `models` mapping into `Decimal` price pairs."""
    if not isinstance(payload, dict):
        raise LucntError(f"pricing pack vendors.{vendor}.models is not an object")
    models: dict[str, ModelPrices] = {}
    for model, entry in payload.items():
        if not isinstance(entry, dict):
            raise LucntError(
                f"pricing pack vendors.{vendor}.models.{model} is not an object"
            )
        models[model] = (
            _price(entry.get("input"), f"vendors.{vendor}.models.{model}.input"),
            _price(entry.get("output"), f"vendors.{vendor}.models.{model}.output"),
        )
    return models


def _parse_pack(payload: object) -> PricingPack:
    """Validate and convert the raw pack document into a `PricingPack`."""
    if not isinstance(payload, dict):
        raise LucntError("pricing pack is not a JSON object")
    estimation = payload.get("estimation")
    vendors_raw = payload.get("vendors")
    if not isinstance(estimation, dict) or not isinstance(vendors_raw, dict):
        raise LucntError("pricing pack is missing 'estimation' or 'vendors'")

    vendors: dict[str, VendorPricing] = {}
    for vendor, entry in vendors_raw.items():
        if not isinstance(entry, dict):
            raise LucntError(f"pricing pack vendors.{vendor} is not an object")
        currency = entry.get("currency")
        if not isinstance(currency, str) or not currency:
            raise LucntError(f"pricing pack vendors.{vendor}.currency is missing")
        vendors[vendor] = VendorPricing(
            currency=currency, models=_parse_models(entry.get("models"), vendor)
        )

    return PricingPack(
        version=str(payload.get("version", "unknown")),
        chars_per_token=_positive_int(estimation, "chars_per_token"),
        default_output_tokens=_positive_int(estimation, "default_output_tokens"),
        image_tokens=_positive_int(estimation, "image_tokens"),
        document_page_tokens=_positive_int(estimation, "document_page_tokens"),
        default_document_pages=_positive_int(estimation, "default_document_pages"),
        vendors=vendors,
    )


def _positive_int(estimation: dict[str, object], key: str) -> int:
    """Read one estimation knob, rejecting a missing or non-positive value.

    A zero or negative token count would silently produce a zero-cost estimate,
    so the authorize gate would approve an unbounded spend. Fail on the pack
    instead — a broken pack is a build problem, not something to guess around.
    """
    raw = estimation.get(key)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise LucntError(
            f"pricing pack estimation.{key} must be a positive integer, got {raw!r}"
        )
    return raw


@lru_cache(maxsize=1)
def load_pricing_pack() -> PricingPack:
    """Read and parse the pack shipped inside the installed package.

    Cached: the file is immutable for the life of the process, and `meter()`
    resolves an adapter on every call.

    Returns:
        The parsed `PricingPack`.

    Raises:
        LucntError: The pack is missing from the installed package (a broken
            build) or fails validation.
    """
    try:
        raw = (
            files(_PACK_PACKAGE).joinpath(_PACK_FILENAME).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise LucntError(
            f"pricing pack {_PACK_PACKAGE}/{_PACK_FILENAME} is missing from the "
            f"installed lucnt package — reinstall the package."
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LucntError(f"pricing pack is not valid JSON: {exc}") from exc
    return _parse_pack(payload)


def normalize_prices(
    overrides: Mapping[str, tuple[PriceInput, PriceInput]],
) -> dict[str, ModelPrices]:
    """Convert caller-supplied `meter(pricing=...)` entries to `Decimal` pairs.

    `float` is rejected rather than coerced: `Decimal(2.5)` is not
    `Decimal("2.5")`, and a price is the one input where that difference
    propagates into booked money. Pass `"2.50"` or `Decimal("2.50")`.

    Args:
        overrides: Model id → `(input, output)` price per 1M tokens.

    Returns:
        The same mapping with both prices as `Decimal`.

    Raises:
        LucntError: An entry is not a 2-tuple, or a price is a `float` or an
            otherwise unparseable value.
    """
    normalized: dict[str, ModelPrices] = {}
    for model, prices in overrides.items():
        if not isinstance(prices, tuple) or len(prices) != 2:
            raise LucntError(
                f"pricing override for {model!r} must be an (input, output) "
                f"2-tuple, got {prices!r}"
            )
        normalized[model] = (
            _override_price(prices[0], model, "input"),
            _override_price(prices[1], model, "output"),
        )
    return normalized


def _override_price(raw: object, model: str, field: str) -> Decimal:
    """Parse one caller-supplied price, rejecting `float`."""
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise LucntError(
            f"pricing override {model!r} {field} must be Decimal, int, or str — "
            f"got {type(raw).__name__}. float is rejected because it cannot "
            f"represent a price exactly."
        )
    try:
        return Decimal(str(raw))
    except InvalidOperation as exc:
        raise LucntError(
            f"pricing override {model!r} {field} is not a number: {raw!r}"
        ) from exc
