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
only; per-process overrides arrive as arguments (`meter(pricing=...)`).

**The server feed is the authority; the pack is the offline fallback (F-2).**
`load_active_pack()` fetches `GET /pricing/feed` — where date-ranged rates are
resolved server-side against *today*, so a lapsing introductory rate never
requires a client release — merges it over the packaged pack (feed wins per
model), and caches the result for `pricing_feed_ttl` seconds. Any failure falls
back to the pack with a warning; the money path never breaks on a feed outage.
Precedence end to end: `meter(pricing=...)` > feed > packaged pack.

**Money is encoded as strings and parsed to `Decimal` here, at the boundary.**
A JSON number would arrive as a `float` and reintroduce exactly the binary
precision error the ledger's Decimal discipline exists to prevent, so a
non-string price is rejected rather than coerced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib.resources import files
from typing import Final

from lucnt.exceptions import LucntError

_logger = logging.getLogger("lucnt.pricing")

_PACK_PACKAGE: Final = "lucnt.data"
_PACK_FILENAME: Final = "pricing.json"
_FEED_PATH: Final = "/pricing/feed"

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


# --------------------------------------------------------------------------- #
# The server-fetched feed (F-2)
# --------------------------------------------------------------------------- #

_feed_lock = threading.Lock()
# (monotonic timestamp of the attempt, parsed feed or None when it failed).
_feed_state: tuple[float, PricingPack | None] | None = None
_feed_warned = False


def _reset_feed_cache() -> None:
    """Clear the feed cache and warning latch. Test-only."""
    global _feed_state, _feed_warned
    with _feed_lock:
        _feed_state = None
        _feed_warned = False


def _fetch_feed() -> PricingPack:
    """Fetch and validate `GET /pricing/feed` through the pooled transport.

    Imported late: `ledger` is a heavier module than `pricing`, and importing
    it at module scope would create an import cycle through `adapters`.

    Raises:
        LucntError: The SDK is unconfigured, the request failed, or the body
            does not validate as a pricing pack.
    """
    from lucnt.ledger import _get_transport

    try:
        response = _get_transport().request("GET", _FEED_PATH)
        payload = json.loads(response.text)
    except LucntError:
        raise
    except Exception as exc:
        raise LucntError(f"pricing feed fetch failed: {exc}") from exc
    return _parse_pack(payload)


def _merge_feed(pack: PricingPack, feed: PricingPack) -> PricingPack:
    """Merge the feed over the packaged pack — feed wins, per model.

    Model-level so a feed that only carries corrections still leaves every
    pack-only model priced; a feed-only vendor is added whole. The estimation
    knobs come from the feed (it always carries them — `_parse_pack` requires
    the block), so the server can also tune estimate heuristics fleet-wide.
    """
    vendors: dict[str, VendorPricing] = dict(pack.vendors)
    for name, feed_vendor in feed.vendors.items():
        base = vendors.get(name)
        if base is None:
            vendors[name] = feed_vendor
        else:
            vendors[name] = VendorPricing(
                currency=feed_vendor.currency,
                models={**dict(base.models), **dict(feed_vendor.models)},
            )
    return PricingPack(
        version=feed.version,
        chars_per_token=feed.chars_per_token,
        default_output_tokens=feed.default_output_tokens,
        image_tokens=feed.image_tokens,
        document_page_tokens=feed.document_page_tokens,
        default_document_pages=feed.default_document_pages,
        vendors=vendors,
    )


def _feed_enabled() -> tuple[bool, float]:
    """Read `(enabled, ttl)` from the active config; disabled when unconfigured."""
    from lucnt.config import _active_config

    if _active_config is None:
        return (False, 0.0)
    return (_active_config.pricing_feed, _active_config.pricing_feed_ttl)


def load_active_pack() -> PricingPack:
    """The pricing every adapter prices against: feed-merged, pack-fallback (F-2).

    Fetches the feed at most once per `pricing_feed_ttl` (a failed fetch is
    cached for the same TTL, so a feed outage costs one connect error per TTL,
    not one per call). Inside a running event loop no blocking fetch is made —
    the cached feed or the pack is used; async applications that want the feed
    warm call `refresh_pricing_async()` at startup. Never raises on the feed's
    account: any failure degrades to the packaged pack with one warning per
    process.
    """
    global _feed_state, _feed_warned
    pack = load_pricing_pack()
    enabled, ttl = _feed_enabled()
    if not enabled:
        return pack
    with _feed_lock:
        state = _feed_state
        if state is not None and time.monotonic() - state[0] < ttl:
            feed = state[1]
            return _merge_feed(pack, feed) if feed is not None else pack
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # A blocking fetch would stall every coroutine in the process
            # (the T5-6 rule). Use what we have; do not poison the TTL cache.
            feed = state[1] if state is not None else None
            return _merge_feed(pack, feed) if feed is not None else pack
        try:
            feed = _fetch_feed()
        except Exception:
            feed = None
            if not _feed_warned:
                _feed_warned = True
                _logger.warning(
                    "lucnt: could not fetch the pricing feed from %s; using the "
                    "packaged pricing pack (version %s) until the next retry.",
                    _FEED_PATH,
                    pack.version,
                    exc_info=True,
                )
        _feed_state = (time.monotonic(), feed)
        return _merge_feed(pack, feed) if feed is not None else pack


def refresh_pricing() -> PricingPack:
    """Force-fetch the pricing feed now, replacing the cached copy.

    Unlike the implicit fetch inside `meter()`, an explicit refresh **raises**
    on failure — the caller asked for fresh prices and needs to know they did
    not arrive. Async applications call this (or the `_async` twin) at startup
    so adapters constructed inside a running loop find a warm feed.

    Returns:
        The feed-merged pack now in effect.

    Raises:
        LucntError: The SDK is unconfigured, the fetch failed, or the feed
            body is invalid.
    """
    global _feed_state
    feed = _fetch_feed()
    with _feed_lock:
        _feed_state = (time.monotonic(), feed)
    return _merge_feed(load_pricing_pack(), feed)


async def refresh_pricing_async() -> PricingPack:
    """Force-fetch the pricing feed without blocking the running loop.

    The fetch runs in a worker thread (`asyncio.to_thread`), so it is safe to
    await at application startup. Same semantics as `refresh_pricing()`.
    """
    return await asyncio.to_thread(refresh_pricing)


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
