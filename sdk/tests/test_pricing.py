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


# --- F-2: the server-fetched pricing feed --------------------------------------


import asyncio  # noqa: E402
import json  # noqa: E402
from collections.abc import Callable, Iterator  # noqa: E402

import httpx  # noqa: E402

from lucnt import config, ledger  # noqa: E402
from lucnt.client import Transport  # noqa: E402


@pytest.fixture()
def feed_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., list[str]]]:
    """Configured SDK + a controllable /pricing/feed endpoint.

    Yields an installer: call it with a handler (or feed payload) to stub the
    ledger transport; it returns the list of feed-request paths seen, for
    hit-count assertions. Resets the module-level feed cache around each test.
    """
    config.configure(api_key="sk_test", base_url="https://api.test")
    pricing._reset_feed_cache()

    def install(
        payload: object | None = None,
        *,
        status: int = 200,
        raise_error: bool = False,
    ) -> list[str]:
        hits: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            hits.append(request.url.path)
            if raise_error:
                raise httpx.ConnectError("feed down", request=request)
            return httpx.Response(status, json=payload)

        transport = Transport(
            config.get_config(), transport=httpx.MockTransport(handler)
        )
        monkeypatch.setattr(ledger, "_get_transport", lambda: transport)
        return hits

    yield install
    pricing._reset_feed_cache()
    config._reset_config()


def _feed_payload(opus_input: str = "7.00") -> dict[str, object]:
    return {
        "version": "feed-2026-07-29",
        "estimation": {
            "chars_per_token": 4,
            "default_output_tokens": 1024,
            "image_tokens": 4800,
            "document_page_tokens": 2000,
            "default_document_pages": 5,
        },
        "vendors": {
            "anthropic": {
                "currency": "USD",
                "models": {
                    "claude-opus-5": {"input": opus_input, "output": "35.00"},
                },
            },
        },
    }


def test_feed_prices_override_the_packaged_pack(
    feed_env: Callable[..., list[str]],
) -> None:
    """F-2: the server feed is authoritative; the wheel's pack is the offline
    fallback. A feed price wins, and pack-only models stay priced."""
    feed_env(_feed_payload())
    adapter = AnthropicAdapter()
    assert adapter.pricing["claude-opus-5"] == (Decimal("7.00"), Decimal("35.00"))
    # Pack-only model survives the merge:
    assert adapter.pricing["claude-haiku-4-5"] == (Decimal("1.00"), Decimal("5.00"))


def test_an_unreachable_feed_falls_back_to_the_pack(
    feed_env: Callable[..., list[str]], caplog: pytest.LogCaptureFixture
) -> None:
    feed_env(raise_error=True)
    with caplog.at_level("WARNING", logger="lucnt.pricing"):
        adapter = AnthropicAdapter()
    assert adapter.pricing["claude-opus-5"] == (Decimal("5.00"), Decimal("25.00"))
    assert "packaged pricing pack" in caplog.text


def test_an_invalid_feed_body_falls_back_to_the_pack(
    feed_env: Callable[..., list[str]], caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed feed must never break the money path — pack + warning."""
    feed_env({"not": "a pack"})
    with caplog.at_level("WARNING", logger="lucnt.pricing"):
        adapter = AnthropicAdapter()
    assert adapter.pricing["claude-opus-5"] == (Decimal("5.00"), Decimal("25.00"))


def test_the_feed_is_fetched_once_per_ttl(
    feed_env: Callable[..., list[str]],
) -> None:
    hits = feed_env(_feed_payload())
    AnthropicAdapter()
    AnthropicAdapter()
    OpenAIAdapter()
    assert hits.count("/pricing/feed") == 1


def test_a_failed_fetch_is_also_cached_for_the_ttl(
    feed_env: Callable[..., list[str]],
) -> None:
    """Feed-down must not add connect attempts to every meter() call.

    The first construction pays one fetch (with the transport's normal
    retries); constructions inside the TTL must add zero wire traffic.
    """
    hits = feed_env(raise_error=True)
    AnthropicAdapter()
    after_first = hits.count("/pricing/feed")
    assert after_first >= 1
    AnthropicAdapter()
    assert hits.count("/pricing/feed") == after_first


def test_pricing_feed_off_never_touches_the_network(
    feed_env: Callable[..., list[str]],
) -> None:
    hits = feed_env(_feed_payload())
    config.configure(
        api_key="sk_test", base_url="https://api.test", pricing_feed=False
    )
    adapter = AnthropicAdapter()
    assert hits == []
    assert adapter.pricing["claude-opus-5"] == (Decimal("5.00"), Decimal("25.00"))


def test_no_blocking_fetch_inside_a_running_event_loop(
    feed_env: Callable[..., list[str]],
) -> None:
    """Constructing an adapter inside a running loop must not do blocking I/O
    (the T5-6 rule); it uses the cache or the pack and moves on."""
    hits = feed_env(_feed_payload())

    async def main() -> AnthropicAdapter:
        return AnthropicAdapter()

    adapter = asyncio.run(main())
    assert hits == []
    assert adapter.pricing["claude-opus-5"] == (Decimal("5.00"), Decimal("25.00"))


def test_refresh_pricing_forces_a_new_fetch(
    feed_env: Callable[..., list[str]],
) -> None:
    hits = feed_env(_feed_payload())
    AnthropicAdapter()
    pack = pricing.refresh_pricing()
    assert hits.count("/pricing/feed") == 2
    assert pack.vendors["anthropic"].models["claude-opus-5"][0] == Decimal("7.00")


def test_refresh_pricing_raises_when_the_feed_is_down(
    feed_env: Callable[..., list[str]],
) -> None:
    """The implicit fetch degrades silently; an explicit refresh must not."""
    feed_env(raise_error=True)
    with pytest.raises(LucntError):
        pricing.refresh_pricing()


def test_refresh_pricing_async_runs_off_the_loop(
    feed_env: Callable[..., list[str]],
) -> None:
    hits = feed_env(_feed_payload())

    async def main() -> None:
        await pricing.refresh_pricing_async()

    asyncio.run(main())
    assert hits.count("/pricing/feed") == 1


def test_feed_exports_are_wired() -> None:
    import lucnt

    assert lucnt.refresh_pricing is pricing.refresh_pricing
    assert lucnt.refresh_pricing_async is pricing.refresh_pricing_async
    assert json is not None  # imported for handlers above
