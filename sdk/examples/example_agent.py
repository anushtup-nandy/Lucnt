"""E2E driver for LUCNT SDK Phase 1: a real agent doing governed spends.

Exercises the universal authorize -> capture primitive against both spend
shapes it must support identically — a real-world purchase (a flight) and
a generic vendor spend (LLM tokens) — and asserts the block path is
observable, not silent (sdk/PLAN.md §7, failure mode #2).

Requires a live backend at LUCNT_BASE_URL (see infra/PLAN.md). Until
backend Phase B/F land, this raises TransportError — expected, not a bug.
Not collected by pytest (testpaths = ["tests"]); run manually:
    python -m examples.example_agent
"""

from __future__ import annotations

import lucnt


def book_flight(vendor: str, amount: int, currency: str) -> None:
    """Stand-in for the caller's own purchase step — lucnt never moves money."""
    print(f"[example] booking {vendor} flight for {amount} {currency}")


def main() -> None:
    """Run the P1 acceptance flow: approve+capture a flight, block an oversized one."""
    lucnt.configure()

    with lucnt.task(
        agent="ops", entity="sg", intent="founder summit", project="Q3 offsite"
    ):
        verdict = lucnt.authorize(
            vendor="singapore_airlines",
            amount=1240,
            currency="SGD",
            reason="SIN->SFO, premium fare",
        )
        if not verdict.approved:
            raise AssertionError(
                f"expected approval, got {verdict.status}: {verdict.reason}"
            )
        book_flight("singapore_airlines", 1240, "SGD")
        tx = lucnt.capture(verdict, invoice_url="https://example.test/invoice/1")
        print(f"[example] captured flight transaction {tx.id}")

        blocked = lucnt.authorize(
            vendor="singapore_airlines",
            amount=999_999,
            currency="SGD",
            reason="deliberately over budget, proves the block path",
        )
        if blocked.approved:
            raise AssertionError("expected an oversized spend to be blocked")
        print(f"[example] blocked as expected: {blocked.status} - {blocked.reason}")

    result = lucnt.spend(
        agent="research",
        entity="in",
        vendor="openai",
        amount=1.83,
        reason="600pg contract, needs 128K",
        model="gpt-5",
        tokens=128_000,
    )
    if not isinstance(result, lucnt.Transaction):
        raise AssertionError(
            f"expected the generic vendor spend to be approved, got {result}"
        )
    print(f"[example] captured generic-vendor transaction {result.id}")


if __name__ == "__main__":
    main()
