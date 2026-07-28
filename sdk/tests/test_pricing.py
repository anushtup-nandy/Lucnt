"""Unit tests for the packaged pricing data pack (T5-12)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from lucnt import pricing
from lucnt.adapters import resolve_adapter
from lucnt.adapters.anthropic import AnthropicAdapter
from lucnt.adapters.base import Usage
from lucnt.adapters.openai import OpenAIAdapter
from lucnt.exceptions import LucntError


def test_pack_loads_from_the_installed_package() -> None:
    pack = pricing.load_pricing_pack()
    assert pack.version
    assert pack.chars_per_token > 0
    assert pack.default_output_tokens > 0
    assert {"anthropic", "openai"} <= set(pack.vendors)


def test_every_packaged_price_is_a_decimal() -> None:
    """A JSON number would load as a float and reintroduce the binary-precision
    error the ledger's Decimal discipline exists to prevent."""
    pack = pricing.load_pricing_pack()
    for vendor in pack.vendors.values():
        for prices in vendor.models.values():
            assert all(isinstance(price, Decimal) for price in prices)


def test_pack_prices_the_current_default_opus_model() -> None:
    """Regression guard for the live bug this pack fixed: claude-opus-5 was
    absent from the hardcoded table, so metering it raised."""
    models = pricing.load_pricing_pack().vendors["anthropic"].models
    assert models["claude-opus-5"] == (Decimal("5.00"), Decimal("25.00"))


def test_every_vendor_declares_a_billing_currency() -> None:
    pack = pricing.load_pricing_pack()
    for vendor in pack.vendors.values():
        assert vendor.currency


# --- loader boundary: money must be strings ----------------------------------


def _pack_doc(models: dict[str, object]) -> dict[str, object]:
    return {
        "estimation": {"chars_per_token": 4, "default_output_tokens": 8},
        "vendors": {"acme": {"currency": "USD", "models": models}},
    }


def test_a_json_number_price_is_rejected_not_coerced() -> None:
    with pytest.raises(LucntError, match="must be a string"):
        pricing._parse_pack(_pack_doc({"m": {"input": 2.5, "output": "10"}}))


def test_an_unparseable_price_string_is_rejected() -> None:
    with pytest.raises(LucntError, match="not a number"):
        pricing._parse_pack(_pack_doc({"m": {"input": "cheap", "output": "10"}}))


def test_a_vendor_without_a_currency_is_rejected() -> None:
    with pytest.raises(LucntError, match="currency is missing"):
        pricing._parse_pack(
            {
                "estimation": {"chars_per_token": 4, "default_output_tokens": 8},
                "vendors": {"acme": {"models": {}}},
            }
        )


def test_a_pack_missing_its_top_level_sections_is_rejected() -> None:
    with pytest.raises(LucntError, match="missing 'estimation' or 'vendors'"):
        pricing._parse_pack({"version": "1"})


# --- meter(pricing=...) overrides --------------------------------------------


@pytest.mark.parametrize("value", ["3.00", 3, Decimal("3.00")])
def test_override_accepts_decimal_int_and_str(value: object) -> None:
    normalized = pricing.normalize_prices({"m": (value, "15")})  # type: ignore[dict-item]
    assert normalized["m"] == (Decimal("3"), Decimal("15"))


def test_override_rejects_float() -> None:
    """float is the one type that cannot round-trip a price exactly."""
    with pytest.raises(LucntError, match="float is rejected"):
        pricing.normalize_prices({"m": (3.0, "15")})  # type: ignore[dict-item]


def test_override_rejects_a_non_pair() -> None:
    with pytest.raises(LucntError, match="2-tuple"):
        pricing.normalize_prices({"m": "3.00"})  # type: ignore[dict-item]


def test_override_adds_a_model_the_pack_does_not_know() -> None:
    adapter = AnthropicAdapter(pricing={"claude-unreleased-9": ("7", "35")})
    cost = adapter.cost(
        Usage(model="claude-unreleased-9", input_tokens=1_000_000, output_tokens=0)
    )
    assert cost == Decimal("7.0000")


def test_override_wins_over_the_packaged_price() -> None:
    """The negotiated-rate case: a customer corrects a price without a release."""
    adapter = AnthropicAdapter(pricing={"claude-opus-5": ("1", "2")})
    cost = adapter.cost(
        Usage(model="claude-opus-5", input_tokens=1_000_000, output_tokens=0)
    )
    assert cost == Decimal("1.0000")


def test_unrelated_packaged_models_survive_an_override() -> None:
    adapter = AnthropicAdapter(pricing={"claude-opus-5": ("1", "2")})
    assert adapter.pricing["claude-haiku-4-5"] == (Decimal("1.00"), Decimal("5.00"))


def test_overrides_do_not_leak_into_the_cached_pack() -> None:
    """The pack is lru_cached and shared; an override must not mutate it for the
    next adapter built in the same process."""
    AnthropicAdapter(pricing={"claude-opus-5": ("1", "2")})
    assert AnthropicAdapter().pricing["claude-opus-5"] == (
        Decimal("5.00"),
        Decimal("25.00"),
    )


# --- adapters read the pack --------------------------------------------------


def test_adapters_expose_the_vendor_billing_currency() -> None:
    """meter() forwards this to authorize() instead of hardcoding USD (C-6)."""
    assert AnthropicAdapter().currency == "USD"
    assert OpenAIAdapter().currency == "USD"


def test_estimate_uses_the_packs_heuristics() -> None:
    pack = pricing.load_pricing_pack()
    adapter = AnthropicAdapter()
    input_price, output_price = adapter.pricing["claude-opus-5"]
    estimate = adapter.estimate_cost({"model": "claude-opus-5", "messages": ""})
    # Input estimate floors at 1 token for an empty payload; output falls back
    # to the pack's default_output_tokens.
    expected = (
        input_price * 1 + output_price * pack.default_output_tokens
    ) / Decimal(1_000_000)
    assert estimate == expected.quantize(Decimal("0.0001"))


def test_matches_is_usable_without_constructing_an_adapter() -> None:
    """Detection is a classmethod so resolve_adapter() doesn't load the pricing
    pack once per candidate vendor."""
    assert AnthropicAdapter.matches(object()) is False
    assert OpenAIAdapter.matches(object()) is False


def test_resolve_adapter_threads_pricing_overrides_through() -> None:
    class FakeAnthropic:
        pass

    FakeAnthropic.__module__ = "anthropic"
    adapter = resolve_adapter(FakeAnthropic(), pricing={"m": ("9", "9")})
    assert adapter is not None
    assert adapter.pricing["m"] == (Decimal("9"), Decimal("9"))


def test_resolve_adapter_returns_none_for_an_unknown_client() -> None:
    assert resolve_adapter(object()) is None
