"""Vendor pricing: packaged data pack, merged under the server `/pricing/feed`.

Precedence: `meter(pricing=...)` > feed > packaged pack. Feed failures fall back
to the pack. Prices are JSON strings so they parse to `Decimal` exactly.
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

# No `float`: it cannot round-trip a price exactly.
PriceInput = Decimal | int | str


@dataclass(frozen=True)
class VendorPricing:
    """One vendor's billing currency and per-model token prices."""

    currency: str
    models: Mapping[str, ModelPrices]


@dataclass(frozen=True)
class PricingPack:
    """The pricing pack: pre-call estimate heuristics plus per-vendor pricing.

    `image_tokens` is set near the high-resolution ceiling so a hold never
    under-reserves; over-reservation is released at capture.
    """

    version: str
    chars_per_token: int
    default_output_tokens: int
    image_tokens: int
    document_page_tokens: int
    default_document_pages: int
    vendors: Mapping[str, VendorPricing]


def _price(raw: object, where: str) -> Decimal:
    """Parse one price string into a `Decimal`, rejecting anything else."""
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
    """Read one estimation knob; non-positive would zero the authorize estimate."""
    raw = estimation.get(key)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise LucntError(
            f"pricing pack estimation.{key} must be a positive integer, got {raw!r}"
        )
    return raw


@lru_cache(maxsize=1)
def load_pricing_pack() -> PricingPack:
    """Read and parse the pack shipped inside the installed package (cached).

    Raises:
        LucntError: The pack is missing or fails validation.
    """
    try:
        raw = files(_PACK_PACKAGE).joinpath(_PACK_FILENAME).read_text(encoding="utf-8")
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
# The server-fetched feed
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
    """Fetch and validate `GET /pricing/feed` (late import avoids a cycle)."""
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
    """Merge the feed over the pack per model; estimation knobs come from the feed."""
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
    """Return the feed-merged pack, falling back to the packaged pack.

    Fetches at most once per TTL (failures cached too). Never fetches inside a
    running event loop, and never raises on the feed's account.
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
            # A blocking fetch would stall the loop; don't poison the TTL cache.
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

    Unlike the implicit fetch, this raises on failure.

    Returns:
        The feed-merged pack now in effect.

    Raises:
        LucntError: SDK unconfigured, fetch failed, or invalid feed body.
    """
    global _feed_state
    feed = _fetch_feed()
    with _feed_lock:
        _feed_state = (time.monotonic(), feed)
    return _merge_feed(load_pricing_pack(), feed)


async def refresh_pricing_async() -> PricingPack:
    """Async `refresh_pricing()`; fetches in a worker thread."""
    return await asyncio.to_thread(refresh_pricing)


def normalize_prices(
    overrides: Mapping[str, tuple[PriceInput, PriceInput]],
) -> dict[str, ModelPrices]:
    """Convert `meter(pricing=...)` entries to `Decimal` pairs, rejecting `float`."""
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
