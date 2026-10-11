"""Base class for per-vendor meter adapters.

Adapters price a crude pre-call estimate (the authorize gate) and the actual
post-call usage (the booked amount). Vendor SDKs are never imported.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from lucnt.exceptions import LucntError
from lucnt.pricing import (
    ModelPrices,
    PriceInput,
    load_active_pack,
    normalize_prices,
)

# Not configurable: must match the ledger's NUMERIC(18, 4) rounding.
_MILLION = Decimal(1_000_000)
_FOUR_PLACES = Decimal("0.0001")


# Unrecognised block types are charged as media, never as base64 text.
_TEXT_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_BLOCK_TYPES = frozenset({"image", "image_url", "input_image"})
_DOCUMENT_BLOCK_TYPES = frozenset({"document", "file", "input_file"})


def _quantize(amount: Decimal) -> Decimal:
    """Round a cost to 4dp half-up, flooring positive amounts at 0.0001.

    The backend rejects zero amounts, so a real sub-precision spend books the
    minimum unit rather than 0.
    """
    quantized = amount.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)
    if quantized <= 0 < amount:
        return _FOUR_PLACES
    return quantized


def _int_attr(obj: object, *names: str) -> int:
    """Read the first present integer attribute (duck-typed vendor response)."""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return int(value)
    raise LucntError(f"response is missing any of {names!r}")


@dataclass(frozen=True)
class Usage:
    """Normalized token usage extracted from a vendor response."""

    model: str
    input_tokens: int
    output_tokens: int


class MeterAdapter:
    """Base adapter: detection, method path, usage extraction, and pricing.

    Subclasses set `vendor` and `method_path`, and implement `matches` and
    `usage_from_response`. Prices come from the data pack keyed by `vendor`.
    """

    vendor: str
    method_path: tuple[str, ...]
    # A separate streaming method that must also be metered; None when the
    # vendor streams through `method_path` with `stream=True`.
    stream_method_path: tuple[str, ...] | None = None
    # Request kwargs that cap output length, in priority order.
    output_token_keys: tuple[str, ...] = ("max_tokens",)

    def __init__(
        self, *, pricing: Mapping[str, tuple[PriceInput, PriceInput]] | None = None
    ) -> None:
        """Resolve this vendor's prices from the pack, with optional overrides.

        Args:
            pricing: Model id -> `(input, output)` per 1M tokens; wins over the pack.

        Raises:
            LucntError: No pack entry for this vendor, or a malformed override.
        """
        pack = load_active_pack()
        vendor_pricing = pack.vendors.get(self.vendor)
        if vendor_pricing is None:
            raise LucntError(
                f"pricing pack has no entry for vendor {self.vendor!r} "
                f"(known: {sorted(pack.vendors)})"
            )
        self._chars_per_token = pack.chars_per_token
        self._default_output_tokens = pack.default_output_tokens
        self._image_tokens = pack.image_tokens
        self._document_page_tokens = pack.document_page_tokens
        self._default_document_pages = pack.default_document_pages
        self.currency = vendor_pricing.currency
        merged: dict[str, ModelPrices] = dict(vendor_pricing.models)
        if pricing:
            merged.update(normalize_prices(pricing))
        self.pricing: Mapping[str, ModelPrices] = merged

    @classmethod
    def matches(cls, client: object) -> bool:
        """True if this adapter handles `client`, sync or async (by module)."""
        raise NotImplementedError

    def is_async(self, client: object) -> bool:
        """True when `client` is this vendor's async client.

        Checks the method for a coroutine first; the `Async` class name is only
        a fallback, so a vendor rename cannot route async through sync.
        """
        method: object = client
        for name in self.method_path:
            method = getattr(method, name, None)
            if method is None:
                break
        if method is not None and inspect.iscoroutinefunction(method):
            return True
        return "Async" in type(client).__qualname__

    def usage_from_response(self, response: object) -> Usage:
        """Extract model + token usage from a vendor response object."""
        raise NotImplementedError

    @staticmethod
    def is_streaming_request(kwargs: Mapping[str, Any]) -> bool:
        """True when the request passes `stream=True`."""
        return kwargs.get("stream") is True

    def _snapshot_base(self, model: str) -> str | None:
        """Find the priced base id that `model` is a dated snapshot of.

        Only `<base>-<digits>...` matches (longest base wins); a plain prefix
        match would bill a pricier new variant at the cheaper base's rate.
        """
        matches = [
            base
            for base in self.pricing
            if model.startswith(f"{base}-")
            and model[len(base) + 1 :].split("-", 1)[0].isdigit()
        ]
        return max(matches, key=len) if matches else None

    def _prices(self, model: str, fallback_model: str | None = None) -> ModelPrices:
        """Resolve prices: exact id, then dated snapshot, then `fallback_model`."""
        for candidate in (model, self._snapshot_base(model)):
            if candidate is not None and candidate in self.pricing:
                return self.pricing[candidate]
        if fallback_model is not None and fallback_model != model:
            return self._prices(fallback_model)
        raise LucntError(
            f"{self.vendor}: no pricing configured for model {model!r} "
            f"(known: {sorted(self.pricing)}). Add it with "
            f"meter(client, pricing={{{model!r}: (input, output)}})."
        )

    def cost(self, usage: Usage, *, fallback_model: str | None = None) -> Decimal:
        """Price actual usage into a Decimal cost in `self.currency` (4dp).

        Args:
            usage: Token usage read off the vendor response.
            fallback_model: Requested model id, used if the reported one is unpriced.
        """
        input_price, output_price = self._prices(
            usage.model, fallback_model=fallback_model
        )
        raw = (
            usage.input_tokens * input_price + usage.output_tokens * output_price
        ) / _MILLION
        return _quantize(raw)

    def estimate_cost(self, kwargs: Mapping[str, Any]) -> Decimal:
        """Crude pre-call cost estimate for the authorize gate (not booked)."""
        model = kwargs.get("model")
        if not isinstance(model, str):
            raise LucntError(
                f"{self.vendor}: a string 'model' is required to meter a call"
            )
        input_price, output_price = self._prices(model)
        input_est = self._estimate_input_tokens(kwargs)
        output_est = self._estimate_output_tokens(kwargs)
        raw = (input_est * input_price + output_est * output_price) / _MILLION
        return _quantize(raw)

    def _text_tokens(self, text: str) -> int:
        """Approximate token count for a run of text."""
        return len(text) // self._chars_per_token

    def _block_tokens(self, block: object) -> int:
        """Estimate one content block, charging media as media.

        Media gets a flat per-item figure; measuring base64 as text would
        inflate the estimate enough to falsely block a legitimate spend.
        """
        if not isinstance(block, Mapping):
            return self._text_tokens(str(block))
        block_type = block.get("type")
        if block_type in _TEXT_BLOCK_TYPES:
            return self._text_tokens(str(block.get("text", "")))
        if block_type in _IMAGE_BLOCK_TYPES:
            return self._image_tokens
        if block_type in _DOCUMENT_BLOCK_TYPES:
            return self._document_page_tokens * self._default_document_pages
        text = block.get("text")
        if isinstance(text, str):
            return self._text_tokens(text)
        return self._image_tokens

    def _content_tokens(self, content: object) -> int:
        """Estimate a `content` value, which may be a string or a block list."""
        if isinstance(content, str):
            return self._text_tokens(content)
        if isinstance(content, (list, tuple)):
            return sum(self._block_tokens(block) for block in content)
        if content is None:
            return 0
        return self._text_tokens(str(content))

    def _estimate_input_tokens(self, kwargs: Mapping[str, Any]) -> int:
        """Estimate prompt tokens from `system`, message content, and `tools`."""
        tokens = self._content_tokens(kwargs.get("system"))
        messages = kwargs.get("messages")
        if isinstance(messages, (list, tuple)):
            for message in messages:
                if isinstance(message, Mapping):
                    tokens += self._content_tokens(message.get("content"))
                else:
                    tokens += self._text_tokens(str(message))
        elif messages is not None:
            tokens += self._text_tokens(str(messages))
        tools = kwargs.get("tools")
        if tools is not None:
            tokens += self._text_tokens(str(tools))
        return max(1, tokens)

    def _estimate_output_tokens(self, kwargs: Mapping[str, Any]) -> int:
        for key in self.output_token_keys:
            value = kwargs.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return self._default_output_tokens
