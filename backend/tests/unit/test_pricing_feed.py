"""Server-side pricing feed: resolves date-ranged rates to today's flat prices."""

from __future__ import annotations

from datetime import date

import pytest

from lucnt_api.services.pricing_feed import load_feed_document, resolve_feed


def _sonnet(resolved: dict[str, object]) -> dict[str, str]:
    vendors = resolved["vendors"]
    assert isinstance(vendors, dict)
    return vendors["anthropic"]["models"]["claude-sonnet-5"]  # type: ignore[index]


def test_intro_rate_applies_inside_its_window() -> None:
    resolved = resolve_feed(date(2026, 7, 29))
    sonnet = _sonnet(resolved)
    assert sonnet["input"] == "2.00"
    assert sonnet["output"] == "10.00"


def test_list_rate_applies_after_the_window_lapses() -> None:
    resolved = resolve_feed(date(2026, 9, 1))
    sonnet = _sonnet(resolved)
    assert sonnet["input"] == "3.00"
    assert sonnet["output"] == "15.00"


def test_window_boundary_day_is_inclusive() -> None:
    sonnet = _sonnet(resolve_feed(date(2026, 8, 31)))
    assert sonnet["input"] == "2.00"


def test_models_without_windows_serve_their_flat_rate() -> None:
    resolved = resolve_feed(date(2026, 7, 29))
    vendors = resolved["vendors"]
    assert vendors["anthropic"]["models"]["claude-opus-5"]["input"] == "5.00"  # type: ignore[index]
    assert vendors["openai"]["models"]["gpt-4o"]["output"] == "10.00"  # type: ignore[index]


def test_resolved_feed_never_leaks_rate_windows_or_numbers() -> None:
    """Flat string prices only: no `rates` windows, no JSON numbers (floats)."""
    resolved = resolve_feed(date(2026, 7, 29))
    vendors = resolved["vendors"]
    assert isinstance(vendors, dict)
    for vendor in vendors.values():
        for entry in vendor["models"].values():
            assert "rates" not in entry
            assert isinstance(entry["input"], str)
            assert isinstance(entry["output"], str)


def test_the_shipped_document_is_valid() -> None:
    """A malformed feed document must fail loudly at load, not at serve time."""
    document = load_feed_document()
    assert document["vendors"]["anthropic"]["currency"] == "USD"


def test_a_numeric_price_in_the_document_is_rejected() -> None:
    with pytest.raises(ValueError, match="string"):
        resolve_feed(
            date(2026, 7, 29),
            document={
                "version": "x",
                "estimation": {},
                "vendors": {
                    "v": {
                        "currency": "USD",
                        "models": {"m": {"input": 3, "output": "1"}},
                    }
                },
            },
        )


def test_a_malformed_rate_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="until"):
        resolve_feed(
            date(2026, 7, 29),
            document={
                "version": "x",
                "estimation": {},
                "vendors": {
                    "v": {
                        "currency": "USD",
                        "models": {
                            "m": {
                                "input": "3",
                                "output": "15",
                                "rates": [
                                    {
                                        "input": "2",
                                        "output": "10",
                                        "until": "not-a-date",
                                    }
                                ],
                            }
                        },
                    }
                },
            },
        )
