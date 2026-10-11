"""Anthropic client adapter for `meter()`.

Cache-discounted input tokens are billed at the standard input rate
(conservative).
"""

from __future__ import annotations

from lucnt.adapters.base import MeterAdapter, Usage, _int_attr
from lucnt.exceptions import LucntError


class AnthropicAdapter(MeterAdapter):
    """Adapter for the Anthropic Python SDK's synchronous client."""

    vendor = "anthropic"
    method_path = ("messages", "create")
    # Separate method; without it streamed calls would go unmetered.
    stream_method_path = ("messages", "stream")

    @classmethod
    def matches(cls, client: object) -> bool:
        """Match any `anthropic` client — `Anthropic` or `AsyncAnthropic`."""
        return type(client).__module__.split(".", 1)[0] == "anthropic"

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
