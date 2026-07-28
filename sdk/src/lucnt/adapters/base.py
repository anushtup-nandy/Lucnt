"""Base class for per-vendor meter adapters (Phase 3.5 / B).

An adapter teaches `meter()` one LLM client shape: how to detect it, which
nested method to wrap (`method_path`), how to read token usage off the
response, and how to price that usage into a Decimal USD cost. The SDK never
imports `openai`/`anthropic` — detection is by the client's module name and
usage is read duck-typed — so those stay optional dependencies of the
*customer's* app, not of `lucnt`.

Cost is inherently post-hoc for an LLM call, so the adapter also produces a
pre-call *estimate* (from the request's rough input size and `max_tokens`
ceiling) that `meter()` authorizes on before the call; the true cost is settled
at capture. The estimate is deliberately crude — it gates policy, it is not the
booked amount.
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
    load_pricing_pack,
    normalize_prices,
)

# Both stay compiled in, deliberately (T5-12c): `_MILLION` is the pricing unit
# itself, and `_FOUR_PLACES` matches the ledger's NUMERIC(18, 4) — making it
# configurable would let the client round money differently from the books.
_MILLION = Decimal(1_000_000)
_FOUR_PLACES = Decimal("0.0001")


# Content-block `type` discriminators across both vendors, for the pre-call
# estimate (T5-10). Kept compiled in rather than in the data pack: these are the
# vendors' wire vocabulary, and a customer overriding them could only break the
# walker — unlike a price or a model id, there is no per-customer variation to
# express. Anything unrecognised falls through to the media charge, so a vendor
# adding a block type degrades gracefully instead of counting base64 as prose.
_TEXT_BLOCK_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_BLOCK_TYPES = frozenset({"image", "image_url", "input_image"})
_DOCUMENT_BLOCK_TYPES = frozenset({"document", "file", "input_file"})


def _quantize(amount: Decimal) -> Decimal:
    """Round a cost to the ledger's 4-decimal precision (half-up)."""
    return amount.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)


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

    Subclasses set `vendor`, `method_path`, and (optionally)
    `output_token_keys`, and implement `matches` + `usage_from_response`.
    Prices and the billing currency come from the packaged data pack keyed by
    `vendor` (T5-12), not from a class attribute — so adding a model id is a
    data change, and a customer can override one without a release.
    """

    vendor: str
    # Dotted path to the metered method, e.g. ("messages", "create").
    method_path: tuple[str, ...]
    # Dotted path to a *separate* streaming helper that must also be metered
    # (T5-5). Anthropic's `messages.stream()` is its own method, so wrapping only
    # `messages.create` left every streamed call completely unmetered — spend with
    # no ledger row and no error. None when the vendor streams through
    # `method_path` with a `stream=True` kwarg instead.
    stream_method_path: tuple[str, ...] | None = None
    # Request kwargs that cap output length, in priority order.
    output_token_keys: tuple[str, ...] = ("max_tokens",)

    def __init__(
        self, *, pricing: Mapping[str, tuple[PriceInput, PriceInput]] | None = None
    ) -> None:
        """Resolve this vendor's prices from the pack, with optional overrides.

        Args:
            pricing: Model id → `(input, output)` price per 1M tokens, merged
                over the packaged table. Caller entries win, so a customer can
                both correct a stale price and add a model the pack lacks.

        Raises:
            LucntError: The pack has no entry for this adapter's vendor, or an
                override is malformed.
        """
        pack = load_pricing_pack()
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
        # The currency the vendor bills in, so `meter()` never hardcodes USD
        # for a vendor that doesn't use it (C-6).
        self.currency = vendor_pricing.currency
        merged: dict[str, ModelPrices] = dict(vendor_pricing.models)
        if pricing:
            merged.update(normalize_prices(pricing))
        self.pricing: Mapping[str, ModelPrices] = merged

    @classmethod
    def matches(cls, client: object) -> bool:
        """True if this adapter handles `client`, sync or async (by module).

        A classmethod so detection needs no instance — `resolve_adapter` can
        test every adapter before paying to construct the one that matches.

        Vendor knowledge — usage extraction, pricing, which method to wrap — is
        identical for a vendor's sync and async clients, so one adapter serves
        both and only `meter()`'s call wrapper differs (T5-6). See `is_async`.
        """
        raise NotImplementedError

    def is_async(self, client: object) -> bool:
        """True when `client` is this vendor's async client.

        Asks the completion method whether calling it yields a coroutine, which
        is a property of what the object *does*; the `Async...` class-name
        convention both vendors follow is only the fallback, for a client whose
        method cannot be resolved. Preferring the behavioural check means a
        vendor renaming its class does not silently route an async client
        through the blocking wrapper.
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
        """True when this request asks the vendor to stream (T5-5).

        Both vendors spell it `stream=True` on the completion call. A streamed
        response is an iterator, not a message, so `usage_from_response` cannot
        read it and `meter()` must consume the stream instead.
        """
        return kwargs.get("stream") is True

    def _snapshot_base(self, model: str) -> str | None:
        """Find the priced base id that `model` is a dated snapshot of (T5-2).

        Vendors publish a stable alias plus dated snapshots of it —
        `gpt-4o-2024-08-06`, `claude-haiku-4-5-20251001` — billed at the alias's
        rate. Only *those* resolve to the alias: the remainder after the base id
        must be `-` followed by digits. The longest such base wins, so
        `gpt-4o-mini-2024-07-18` prices as `gpt-4o-mini`, never as `gpt-4o`.

        Matching on any prefix would be the dangerous version of this fix: a
        genuinely new and pricier variant (`gpt-4o-turbo-ultra`) would silently
        bill at the cheaper base's rate and under-book real money. A suffix that
        isn't a snapshot returns None so `_prices` fails loudly instead.
        """
        matches = [
            base
            for base in self.pricing
            if model.startswith(f"{base}-")
            and model[len(base) + 1 :].split("-", 1)[0].isdigit()
        ]
        return max(matches, key=len) if matches else None

    def _prices(self, model: str, fallback_model: str | None = None) -> ModelPrices:
        """Resolve prices for `model`: exact id, then dated snapshot, then fallback.

        Args:
            model: The model id to price — normally the one the vendor reported.
            fallback_model: The id the caller *requested*, tried last. A vendor
                may answer with an id the pack has never seen while the
                requested alias is priced perfectly well.

        Returns:
            The `(input, output)` price per 1M tokens.

        Raises:
            LucntError: None of the three lookups resolved.
        """
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
            fallback_model: The requested model id, used when the id the vendor
                reported cannot be priced.

        Returns:
            The cost, quantized to the ledger's 4 decimal places.
        """
        input_price, output_price = self._prices(
            usage.model, fallback_model=fallback_model
        )
        raw = (
            usage.input_tokens * input_price + usage.output_tokens * output_price
        ) / _MILLION
        return _quantize(raw)

    def estimate_cost(self, kwargs: Mapping[str, Any]) -> Decimal:
        """Crude pre-call cost estimate for the authorize gate (not the booked amount).

        Input tokens are approximated from the serialized `messages`/`system`
        payload (~4 chars/token); output tokens from `max_tokens` (or a default).
        """
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
        """Estimate one content block, charging media as media (T5-10).

        The bug this replaces: the estimate used to be
        `len(str(messages)) // 4`, which counted a **base64 payload as text**. A
        1 MB image serializes to ~1.4M characters, so a single screenshot
        estimated at ~350,000 input tokens — enough to trip an amount-threshold
        policy or exhaust a budget on a call whose real usage is a few thousand
        tokens. The failure was in the expensive direction: `authorize()` blocks
        *before* the call, so an inflated estimate does not over-bill, it stops a
        legitimate spend outright, and the caller sees a policy block with no
        indication that an estimate was the cause.

        Media blocks therefore contribute a flat per-item figure from the pack
        and their payload is never measured. An unrecognised block with no text
        field is charged as media too: it is the bounded, roughly-right guess for
        the thing it most likely is, and it means a vendor adding a new media
        type degrades to a slightly-off estimate rather than back to counting
        base64.
        """
        if not isinstance(block, Mapping):
            # A non-dict block (e.g. a bare string in a content list).
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
            # Unknown wrapper that still carries plain text — count the text.
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
        """Estimate prompt tokens by walking the request structurally.

        Covers `system` (a string or, on Anthropic, a block list), every
        message's `content`, and the serialized `tools` — tool schemas are real
        input tokens and were previously ignored entirely, which under-estimated
        every tool-using agent.
        """
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
            # JSON schemas are plain text on the wire; measuring the serialized
            # form is the same heuristic applied to the same kind of content.
            tokens += self._text_tokens(str(tools))
        return max(1, tokens)

    def _estimate_output_tokens(self, kwargs: Mapping[str, Any]) -> int:
        for key in self.output_token_keys:
            value = kwargs.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return self._default_output_tokens
