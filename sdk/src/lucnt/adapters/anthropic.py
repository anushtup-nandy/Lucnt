"""Anthropic client adapter for `meter()` (Phase 3.5 / B).

Wraps `client.messages.create(...)`; reads `response.usage.input_tokens` /
`output_tokens` and `response.model`. Detection is by module so `lucnt` never
imports `anthropic`.

Pricing is USD per 1M tokens, current as of 2026-07 (verified against the
`claude-api` model catalog). Update the table when Anthropic changes prices or
ships new model ids — an unpriced model raises rather than booking a wrong cost.
Cache-discounted input tokens are billed at the standard input rate here (a
conservative MVP simplification; cache-aware pricing is a future refinement).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from lucnt.adapters.base import MeterAdapter, Usage, _int_attr
from lucnt.exceptions import LucntError

_PRICING: Mapping[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5": (Decimal("10"), Decimal("50")),
    "claude-opus-4-8": (Decimal("5"), Decimal("25")),
    "claude-opus-4-7": (Decimal("5"), Decimal("25")),
    "claude-opus-4-6": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5": (Decimal("3"), Decimal("15")),
    "claude-sonnet-4-6": (Decimal("3"), Decimal("15")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
}


class AnthropicAdapter(MeterAdapter):
    """Adapter for the Anthropic Python SDK's synchronous client."""

    vendor = "anthropic"
    method_path = ("messages", "create")
    pricing = _PRICING

    def matches(self, client: object) -> bool:
        """Match a synchronous `anthropic.Anthropic` client by module + class."""
        module = type(client).__module__.split(".", 1)[0]
        return module == "anthropic" and "Async" not in type(client).__qualname__

    def usage_from_response(self, response: object) -> Usage:
        """Read `.model` + `.usage.{input,output}_tokens` off a Messages response."""
        usage = getattr(response, "usage", None)
        model = getattr(response, "model", None)
        if usage is None or not isinstance(model, str):
            raise LucntError("anthropic: response has no usage/model to meter")
        return Usage(
            model=model,
            input_tokens=_int_attr(usage, "input_tokens"),
            output_tokens=_int_attr(usage, "output_tokens"),
        )
