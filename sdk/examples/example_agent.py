"""E2E driver for LUCNT SDK: a real agent doing governed spends.

Exercises the universal authorize -> capture primitive against both spend
shapes it must support identically — a real-world purchase (a flight) and
a generic vendor spend (LLM tokens) — and asserts the block path is
observable, not silent (sdk/PLAN.md §7, failure mode #2).

Phase 2 (Books) additions: every captured transaction now carries a balanced
double-entry `journal` and a jurisdiction `tax` treatment. A domestic spend
(SGD by the SG entity) is standard GST; an imported spend (USD by the IN
entity) is reverse charge with a self-invoice.

Phase 3 (Policy engine + budgets) additions: a policy block is enforced at
authorize (the seeded "No crypto exchanges" merchant rule), surfaced as a typed
`PolicyBlocked` under `strict=True`; `check_budget()` reads an agent's live
utilisation (self-regulation).

Phase 3.5 (meter) addition: `meter()` wraps a customer's LLM client so each
completion call is authorized before and captured after — with the real
cost/model/tokens read from usage. A stub client stands in here so the demo
needs no API key.

Phase 4 (recovery / reconciliation) addition: the agent double-charges a vendor
(an accidental retry) — a later `POST /recon/scan` on the control side flags the
second as a duplicate — and reports a paid-but-undelivered task via the SDK
`mark_failed_delivery()` hook, which opens a `failed_delivery` recovery exception.

Requires a live backend at LUCNT_BASE_URL (see infra/PLAN.md) seeded via
`scripts/seed_dev.py`. Not collected by pytest (testpaths = ["tests"]); run
manually:
    python -m examples.example_agent
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import lucnt


class _StubUsage:
    """Token usage, shaped like an Anthropic `response.usage`."""

    input_tokens = 12_000
    output_tokens = 3_000


class _StubMessages:
    """Stands in for `client.messages` — records nothing, returns a fixed usage."""

    def create(self, **kwargs: object) -> object:
        """Mimic `messages.create`, returning a response with model + usage."""
        print(f"[example]   (stub LLM call for model {kwargs.get('model')})")
        return SimpleNamespace(model=kwargs["model"], usage=_StubUsage())


class _StubAnthropicClient:
    """A tiny stand-in for `anthropic.Anthropic()` so the demo needs no API key."""

    messages = _StubMessages()


# `meter()` detects the vendor by the client's module; spoof it so the stub is
# treated as an Anthropic client. Real callers pass `OpenAI()` / `Anthropic()`.
_StubAnthropicClient.__module__ = "anthropic"


def _assert_booked(tx: lucnt.Transaction) -> None:
    """Assert a captured transaction carries a balanced journal and a tax record."""
    if tx.tax is None:
        raise AssertionError(f"transaction {tx.id} has no tax treatment")
    if not tx.journal:
        raise AssertionError(f"transaction {tx.id} has no journal lines")
    total_debit = sum((line.debit for line in tx.journal), Decimal("0"))
    total_credit = sum((line.credit for line in tx.journal), Decimal("0"))
    if total_debit != total_credit:
        raise AssertionError(
            f"transaction {tx.id} journal does not balance: "
            f"debit {total_debit} != credit {total_credit}"
        )


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
        _assert_booked(tx)
        assert tx.tax is not None  # narrowed by _assert_booked; re-assert for mypy
        if tx.tax.reverse_charge:
            raise AssertionError("a domestic SGD spend must not be reverse charge")
        print(
            f"[example] captured flight {tx.id}: {tx.tax.tax_type} "
            f"{tx.tax.tax_amount}, {len(tx.journal)} journal lines"
        )

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
    _assert_booked(result)
    assert result.tax is not None  # narrowed by _assert_booked; re-assert for mypy
    if not result.tax.reverse_charge:
        raise AssertionError("an imported USD spend by the IN entity must be RCM")
    if not result.tax.self_invoice_generated:
        raise AssertionError("a reverse-charge spend must flag a self-invoice")
    print(
        f"[example] captured generic-vendor {result.id}: {result.tax.tax_type} "
        f"{result.tax.tax_amount} (reverse charge), {len(result.journal)} journal lines"
    )

    # ── Phase 3: policy engine + budgets ─────────────────────────────────
    # The seeded "No crypto exchanges" policy blocks coinbase for every agent.
    # strict=True turns the block into a typed PolicyBlocked instead of a
    # falsy verdict — a blocked spend can never be silently ignored.
    try:
        lucnt.spend(
            agent="ops",
            entity="sg",
            vendor="coinbase",
            amount=100,
            currency="SGD",
            reason="buy crypto — must be policy-blocked",
            strict=True,
        )
    except lucnt.PolicyBlocked as exc:
        print(
            f"[example] policy blocked coinbase as expected: "
            f"{exc.code} - {exc.reason}"
        )
    else:
        raise AssertionError("expected the crypto spend to be policy-blocked")

    # check_budget: an agent reading its own live budget to self-regulate (M8).
    status = lucnt.check_budget(agent="research")
    if status.remaining < 0:
        raise AssertionError("remaining budget should not be negative here")
    print(
        f"[example] research budget: spent {status.spent}/{status.monthly_budget} "
        f"({status.pct_used}% used, status={status.status})"
    )

    # ── Phase 3.5: meter() auto-instrumentation ──────────────────────────
    # Wrap the (stub) LLM client once; the completion call is now authorized
    # before and captured after, with real cost/model/tokens from usage.
    metered = lucnt.meter(_StubAnthropicClient(), agent="research", entity="in")
    metered.messages.create(
        model="claude-opus-4-8",
        messages=[{"role": "user", "content": "summarize the filing"}],
        max_tokens=4096,
    )
    after = lucnt.check_budget(agent="research")
    if after.spent <= status.spent:
        raise AssertionError("metered call should have increased research spend")
    print(
        f"[example] metered LLM call captured; research spend {status.spent} "
        f"-> {after.spent}"
    )

    # ── Phase 4: recovery / reconciliation ───────────────────────────────
    # (a) An accidental double-charge: the research agent pays the same vendor
    #     the same amount twice within the window. A recon scan on the control
    #     side (POST /recon/scan) flags the second as a duplicate — proven live
    #     against the endpoints, not via the SDK (scan is a dashboard action).
    for attempt in ("first", "accidental retry"):
        dup = lucnt.spend(
            agent="research",
            entity="in",
            vendor="dataprovider",
            amount=5,
            currency="USD",
            reason=f"dataset licence ({attempt})",
        )
        if not isinstance(dup, lucnt.Transaction):
            raise AssertionError(f"expected the dataprovider spend to book, got {dup}")
    print("[example] booked two identical dataprovider spends (a double-charge)")

    # (b) A failed delivery the ledger cannot detect on its own: the agent paid
    #     but the downstream task errored. mark_failed_delivery() is the SDK
    #     recovery hook — it opens a failed_delivery exception with a drafted
    #     refund request.
    paid = lucnt.spend(
        agent="research",
        entity="in",
        vendor="flakyapi",
        amount=12,
        currency="USD",
        reason="premium data pull",
    )
    if not isinstance(paid, lucnt.Transaction):
        raise AssertionError(f"expected the flakyapi spend to book, got {paid}")
    recon_exc = lucnt.mark_failed_delivery(
        transaction=paid, reason="vendor returned a 500; no data delivered"
    )
    if recon_exc.exception_type != "failed_delivery":
        raise AssertionError(
            f"expected failed_delivery, got {recon_exc.exception_type}"
        )
    plan = recon_exc.evidence.get("recovery_plan", {})
    print(
        f"[example] flagged failed delivery {recon_exc.id}: {recon_exc.status}, "
        f"${recon_exc.amount_at_stake} at stake, recovery={plan.get('method')}"
    )


if __name__ == "__main__":
    main()
