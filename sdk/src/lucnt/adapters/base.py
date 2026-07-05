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

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from lucnt.exceptions import LucntError

_MILLION = Decimal(1_000_000)
_FOUR_PLACES = Decimal("0.0001")  # ledger precision is NUMERIC(18, 4)
_CHARS_PER_TOKEN = 4  # standard rough approximation for a pre-call estimate
_DEFAULT_OUTPUT_TOKENS = 1024  # assumed output when no max_tokens is given


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

    Subclasses set `vendor`, `method_path`, `pricing`, and (optionally)
    `output_token_keys`, and implement `matches` + `usage_from_response`.
    """

    vendor: str
    # Dotted path to the metered method, e.g. ("messages", "create").
    method_path: tuple[str, ...]
    # (input, output) USD price per 1M tokens, keyed by model id.
    pricing: Mapping[str, tuple[Decimal, Decimal]]
    # Request kwargs that cap output length, in priority order.
    output_token_keys: tuple[str, ...] = ("max_tokens",)

    def matches(self, client: object) -> bool:
        """True if this adapter handles `client` (by module; sync only)."""
        raise NotImplementedError

    def usage_from_response(self, response: object) -> Usage:
        """Extract model + token usage from a vendor response object."""
        raise NotImplementedError

    def _prices(self, model: str) -> tuple[Decimal, Decimal]:
        prices = self.pricing.get(model)
        if prices is None:
            raise LucntError(
                f"{self.vendor}: no pricing configured for model {model!r} "
                f"(known: {sorted(self.pricing)})"
            )
        return prices

    def cost(self, usage: Usage) -> Decimal:
        """Price actual usage into a Decimal USD cost (quantized to 4dp)."""
        input_price, output_price = self._prices(usage.model)
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

    def _estimate_input_tokens(self, kwargs: Mapping[str, Any]) -> int:
        chars = len(str(kwargs.get("messages", "")))
        chars += len(str(kwargs.get("system", "")))
        return max(1, chars // _CHARS_PER_TOKEN)

    def _estimate_output_tokens(self, kwargs: Mapping[str, Any]) -> int:
        for key in self.output_token_keys:
            value = kwargs.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return _DEFAULT_OUTPUT_TOKENS
