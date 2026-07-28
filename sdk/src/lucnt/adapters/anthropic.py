"""Anthropic client adapter for `meter()` (Phase 3.5 / B).

Wraps `client.messages.create(...)`; reads `response.usage.input_tokens` /
`output_tokens` and `response.model`. Detection is by module so `lucnt` never
imports `anthropic`.

Prices live in the packaged data pack (`lucnt/data/pricing.json`), not here
(T5-12) — a new model id is a data change, and `meter(pricing=...)` overrides
one without waiting for a release. Cache-discounted input tokens are billed at
the standard input rate (a conservative simplification; cache-aware pricing is
a future refinement).
"""

from __future__ import annotations

from lucnt.adapters.base import MeterAdapter, Usage, _int_attr
from lucnt.exceptions import LucntError


class AnthropicAdapter(MeterAdapter):
    """Adapter for the Anthropic Python SDK's synchronous client."""

    vendor = "anthropic"
    method_path = ("messages", "create")
    # T5-5: `messages.stream()` is a separate method, so wrapping only `create`
    # let every streamed call through completely unmetered.
    stream_method_path = ("messages", "stream")

    @classmethod
    def matches(cls, client: object) -> bool:
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
