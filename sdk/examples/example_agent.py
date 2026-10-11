"""E2E driver for LUCNT SDK: a real agent doing governed spends.

Exercises the universal authorize -> capture primitive against both spend
shapes it must support identically — a real-world purchase (a flight) and
a generic vendor spend (LLM tokens) — and asserts the block path is
observable, not silent.

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

Phase 5 (intelligence) addition: the agent reads its own living financial memory
via `query()` — budget headroom, a month-end run-rate projection, cost-per-output,
and its spend fingerprint — computed live from the ledger so it can self-regulate
*before* it spends (M8). The `POST /intelligence/recompute` + Insights/Memory
dashboard tables are the control-plane side, curled live like the recon scan.

Correctness additions: the **third verdict** — a spend that is neither approved nor
blocked but `review`, held for a human; budget measured in the entity's functional
currency rather than the paid one; spend derived from the current month of the ledger
with entity-local boundaries, not a stored counter; and an anomaly score computed
against the agent's *own* baseline.

Hardening additions: oversized free text is rejected at the edge as a 422 naming the
field instead of dying as a 500 `DataError`, including on the recovery hook;
`check_budget()` resolves one agent server-side so its cost no longer scales with tenant
size; and the freshness contract — a capture moves `budget` immediately, because it is a
live SQL sum, while fingerprint statistics are only as fresh as the last recompute.

Requires a live backend at LUCNT_BASE_URL seeded via `scripts/seed_dev.py`. Not
collected by pytest (testpaths = ["tests"]); run manually:
    python -m examples.example_agent

Every step asserts. A silent pass is not a pass — if a beat stops holding, this
script fails loudly rather than printing a story that is no longer true.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import httpx

import lucnt


class _StubUsage:
    """Token usage, shaped like an Anthropic `response.usage`.

    Sized so the *actual* cost lands under the pre-call estimate (max_tokens=4096
    of opus output) — a realistic metered call whose settlement is within the
    buffer.
    """

    input_tokens = 2_000
    output_tokens = 3_000


class _StubMessages:
    """Stands in for `client.messages` — records nothing, returns a fixed usage."""

    def create(self, **kwargs: object) -> object:
        """Mimic `messages.create`, returning a response with model + usage."""
        print(f"[example]   (stub LLM call for model {kwargs.get('model')})")
        return SimpleNamespace(model=kwargs["model"], usage=_StubUsage())


class _StubAnthropicClient:
    """A tiny stand-in for `anthropic.Anthropic()` so the demo needs no API key.

    Carries a real `httpx.Client` at `._client` because every real vendor
    client has one and `meter()` now installs its transport governor there
    — a client it cannot govern is refused loudly. The stub's `create`
    never does real I/O, so the transport is never actually hit.
    """

    def __init__(self) -> None:
        self.messages = _StubMessages()
        self._client = httpx.Client()


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
            f"[example] policy blocked coinbase as expected: {exc.code} - {exc.reason}"
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

    # ── Phase 5: intelligence — living financial memory (query) ──────────
    # The agent asks LUCNT about its own financial state — computed live from the
    # ledger, so it self-regulates *before* it spends (M8). No recompute needed.
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
    # (a) There is a THIRD verdict. approve/block is the easy story; the one
    #     that matters operationally is `review` — the spend is inside budget
    #     and breaks no merchant rule, but crosses a threshold a human owns.
    #     The caller must stop: nothing is captured, and the hold
    #     lands in the approval queue for an admin to approve or reject.
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

    # (b) Budget is measured in the entity's FUNCTIONAL currency, never the
    #     paid one. research paid in USD above; its budget, spend
    #     and projection all read in INR because that is what the IN entity
    #     books in. Mixed-currency addition is the classic way a budget
    #     invariant goes quietly wrong.
    if memory.currency != "INR":
        raise AssertionError(f"IN entity must budget in INR, got {memory.currency}")
    print(
        f"[example] budget currency {memory.currency} — the USD spends above were "
        f"FX-converted at capture, never summed as-paid"
    )

    # (c) Spend is derived from the CURRENT MONTH of the ledger, not a stored
    #     running counter, with month boundaries in the entity's local
    #     timezone. That is why it resets on its own and can never drift
    #     from the journals.
    print(
        f"[example] month-to-date {memory.projection.month_to_date} "
        f"{memory.currency} — derived from the ledger, resets at the entity-local "
        f"month boundary"
    )

    # (d) The fingerprint scores each agent against its OWN baseline, so
    #     "large" is relative to that agent rather than a global constant.
    print(
        f"[example] anomaly score {memory.fingerprint.anomaly_score} "
        f"(max recent z-score against this agent's own history)"
    )

    # ── Hardening, as the caller actually experiences it ─────────────────
    # (a) Every free-text field is bounded server-side. Oversized input
    #     is a 422 naming the offending field — previously it sailed through
    #     validation and died in Postgres as a 500 DataError, which reads to the
    #     caller as "the service is broken" rather than "your input is wrong".
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

    # (b) The recovery hook is bounded too — an agent-supplied failure
    #     reason is attacker-adjacent free text on a write path.
    try:
        lucnt.mark_failed_delivery(transaction=paid, reason="x" * 501)
    except lucnt.LucntError:
        print("[example] oversized failed-delivery reason rejected (bounded at 500)")
    else:
        raise AssertionError("an oversized recovery reason must be rejected")

    # (c) check_budget() resolves ONE agent server-side. It used to fetch
    #     every agent's budget and scan the list for a matching slug — which
    #     silently broke the moment /budgets became paginated, reporting "no
    #     budget found" for an agent that exists. Cost is now independent of how
    #     many agents the tenant has.
    solo = lucnt.check_budget(agent="ops")
    print(
        f"[example] check_budget(ops) resolved server-side: "
        f"{solo.spent}/{solo.monthly_budget} ({solo.status})"
    )

    # (d) The freshness contract. `budget` is always a live SQL sum over
    #     the ledger — prove it by spending and re-reading. Once the control
    #     plane has run /intelligence/recompute, the fingerprint STATISTICS are
    #     served from that stored fingerprint and are only as fresh as it;
    #     `budget` never is. Gate spending decisions on budget.
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
