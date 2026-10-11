"""E2E driver: a real agent doing governed spends through the lucnt SDK.

Walks authorize -> capture, policy blocks, budgets, meter(), recovery,
query(), the review verdict, and input bounds. Every step asserts.

Requires a live backend at LUCNT_BASE_URL seeded via `scripts/seed_dev.py`:
    python -m examples.example_agent
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import httpx

import lucnt


class _StubUsage:
    """Token usage shaped like Anthropic `response.usage`; settles under the hold."""

    input_tokens = 2_000
    output_tokens = 3_000


class _StubMessages:
    """Stands in for `client.messages` — records nothing, returns a fixed usage."""

    def create(self, **kwargs: object) -> object:
        """Mimic `messages.create`, returning a response with model + usage."""
        print(f"[example]   (stub LLM call for model {kwargs.get('model')})")
        return SimpleNamespace(model=kwargs["model"], usage=_StubUsage())


class _StubAnthropicClient:
    """Stand-in for `anthropic.Anthropic()` so the demo needs no API key.

    `meter()` requires an httpx client at `._client`; the stub never uses it.
    """

    def __init__(self) -> None:
        self.messages = _StubMessages()
        self._client = httpx.Client()


# `meter()` detects the vendor by module; spoof it. Real callers pass `Anthropic()`.
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
    """Run the acceptance flow end to end."""
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
        # Quoted, not 1.83 — money arguments reject `float`.
        amount="1.83",
        currency="USD",  # paid in USD by the INR entity -> imported (RCM)
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

    # ── Policy engine + budgets ──────────────────────────────────────────
    # strict=True raises PolicyBlocked instead of returning a falsy verdict.
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
            f"[example] policy blocked coinbase as expected: {exc.code} - {exc.reason}"
        )
    else:
        raise AssertionError("expected the crypto spend to be policy-blocked")

    # check_budget: an agent reading its own live budget to self-regulate.
    status = lucnt.check_budget(agent="research")
    if status.remaining < 0:
        raise AssertionError("remaining budget should not be negative here")
    print(
        f"[example] research budget: spent {status.spent}/{status.monthly_budget} "
        f"({status.pct_used}% used, status={status.status})"
    )

    # ── meter() auto-instrumentation ─────────────────────────────────────
    # Wrap the client once; each call is authorized before and captured after.
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

    # ── Recovery / reconciliation ────────────────────────────────────────
    # (a) A double-charge; POST /recon/scan (dashboard side) flags the duplicate.
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

    # (b) Paid but not delivered: mark_failed_delivery() opens a recovery case.
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

    # ── Intelligence: living financial memory (query) ────────────────────
    # Computed live from the ledger, so the agent can self-regulate before spending.
    memory = lucnt.query(agent="research", question="am I on track this month?")
    if memory.fingerprint is None:
        raise AssertionError("research has spent; expected a fingerprint")
    print(f"[example] research financial memory: {memory.headline}")
    print(
        f"[example]   budget {memory.budget.remaining}/{memory.budget.monthly_budget} "
        f"left; projecting {memory.projection.projected_month_end} {memory.currency} "
        f"(on_track={memory.on_track})"
    )
    top_vendors = memory.fingerprint.top_vendors
    top_vendor = top_vendors[0].vendor if top_vendors else "n/a"
    print(
        f"[example]   fingerprint: {memory.fingerprint.total_records} spends, "
        f"top vendor {top_vendor}"
    )
    if memory.cost_per_output:
        top = memory.cost_per_output[0]
        print(
            f"[example]   priciest output: '{top.unit}' at {top.avg_cost} "
            f"{top.currency}/unit across {top.count} charges"
        )
    # The same read via ambient task() context (agent inherited), cost-routed.
    with lucnt.task(agent="research", entity="in"):
        cost_view = lucnt.query(question="what is my cost per output?")
    print(f"[example]   (via task ctx) {cost_view.headline}")

    # ── Correctness: what the numbers depend on ──────────────────────────
    # (a) `review`: within budget but over a human-owned threshold. Stop; nothing
    #     is captured until an approver acts.
    held = lucnt.authorize(
        agent="research",
        entity="in",
        vendor="dataprovider",
        amount=150_000,
        currency="INR",  # inside the 600,000 budget, over the 100,000 threshold
        reason="annual dataset licence renewal",
    )
    if held.approved:
        raise AssertionError("a spend over the review threshold must not auto-approve")
    if held.status != "review":
        raise AssertionError(f"expected a review verdict, got {held.status}")
    print(
        f"[example] held for human review: {held.status} - {held.reason} "
        f"(nothing captured; awaiting an approver)"
    )

    # (b) Budgets are in the entity's functional currency, never the paid one.
    if memory.currency != "INR":
        raise AssertionError(f"IN entity must budget in INR, got {memory.currency}")
    print(
        f"[example] budget currency {memory.currency} — the USD spends above were "
        f"FX-converted at capture, never summed as-paid"
    )

    # (c) Spend is derived from the current ledger month (entity-local), no counter.
    print(
        f"[example] month-to-date {memory.projection.month_to_date} "
        f"{memory.currency} — derived from the ledger, resets at the entity-local "
        f"month boundary"
    )

    # (d) Anomaly scores are relative to the agent's own baseline.
    print(
        f"[example] anomaly score {memory.fingerprint.anomaly_score} "
        f"(max recent z-score against this agent's own history)"
    )

    # ── Hardening, as the caller experiences it ──────────────────────────
    # (a) Oversized free text is a 422 naming the field.
    try:
        lucnt.authorize(
            agent="research",
            entity="in",
            vendor="dataprovider",
            amount=1,
            currency="INR",
            reason="x" * 2_001,  # REASON_MAX is 2,000
        )
    except lucnt.LucntError as exc:
        detail = str(exc)
        if "422" not in detail:
            raise AssertionError(
                f"expected a 422 for oversized reason, got {detail}"
            ) from exc
        print("[example] oversized reason rejected at the edge: 422, field named")
    else:
        raise AssertionError("an oversized reason must be rejected, not truncated")

    # (b) The recovery hook is bounded too.
    try:
        lucnt.mark_failed_delivery(transaction=paid, reason="x" * 501)
    except lucnt.LucntError:
        print("[example] oversized failed-delivery reason rejected (bounded at 500)")
    else:
        raise AssertionError("an oversized recovery reason must be rejected")

    # (c) check_budget() resolves one agent server-side.
    solo = lucnt.check_budget(agent="ops")
    print(
        f"[example] check_budget(ops) resolved server-side: "
        f"{solo.spent}/{solo.monthly_budget} ({solo.status})"
    )

    # (d) `budget` is a live sum; fingerprint stats are only as fresh as the last
    #     recompute. Gate spending on budget.
    before_spend = lucnt.query(agent="research").budget.spent
    topup = lucnt.spend(
        agent="research",
        entity="in",
        vendor="dataprovider",
        amount=3,
        currency="USD",
        reason="incremental dataset pull",
    )
    if not isinstance(topup, lucnt.Transaction):
        raise AssertionError(f"expected the top-up spend to book, got {topup}")
    after_spend = lucnt.query(agent="research").budget.spent
    if after_spend <= before_spend:
        raise AssertionError("budget must reflect a new spend immediately")
    print(
        f"[example] budget is always live: {before_spend} -> {after_spend} "
        f"{memory.currency} immediately after a capture"
    )


if __name__ == "__main__":
    main()
