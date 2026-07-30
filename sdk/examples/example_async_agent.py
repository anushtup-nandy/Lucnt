"""E2E driver for the lucnt SDK's async surface and hold release (T5-6, T5-4).

`example_agent.py` covers the synchronous primitive. This covers what it cannot:
the `_async` twins against **real sockets** rather than `httpx.MockTransport`, and
`void()` — the hold-release path, which the unit suite exercises only against a
mock and which is the one operation whose whole purpose is to undo a reservation
the ledger has already made.

Why a live run matters here specifically: a mock transport answers whatever the
test says it answers. It cannot catch a header the server rejects, a body field
the server ignores, a concurrency limit the pool trips, or an `AsyncClient`
misconfigured such that it never actually multiplexes. Those are exactly the
failures an async client is prone to.

Requires a live backend at LUCNT_BASE_URL seeded via `scripts/seed_dev.py`.
Not collected by pytest (testpaths = ["tests"]); run manually:

    LUCNT_API_KEY=... LUCNT_BASE_URL=http://127.0.0.1:8000 \
        python -m examples.example_async_agent

Every beat asserts. A silent pass is not a pass.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import lucnt

AGENT = "research"
ENTITY = "in"


async def beat_authorize_capture() -> None:
    """The primitive, awaited: a hold and then a settled capture."""
    verdict = await lucnt.authorize_async(
        agent=AGENT,
        entity=ENTITY,
        vendor="openai",
        amount="2.50",
        currency="USD",
        reason="async smoke: embeddings batch",
    )
    assert verdict.approved, f"expected approval, got {verdict.status}"
    tx = await lucnt.capture_async(
        verdict, amount="2.4100", tokens=90_000, model="gpt-4o"
    )
    assert isinstance(tx, lucnt.Transaction)
    # Settled at the actual cost, not the authorized estimate.
    assert tx.amount == Decimal("2.4100"), tx.amount
    assert tx.journal, "capture returned no journal lines"
    assert tx.tax is not None, "capture returned no tax treatment"
    print(
        f"[async] captured {tx.id}: {tx.tax.tax_type} {tx.tax.tax_amount}, "
        f"{len(tx.journal)} journal lines"
    )


async def beat_void_releases_the_hold() -> None:
    """T5-4 against the real backend: an unspent hold must stop reserving budget.

    The bug being closed is silent and permanent: `reserved_spend` counts
    approved-but-uncaptured holds, so a vendor error after `authorize()` shrinks
    what the agent may spend for good.

    **Observed through the policy gate, not through `check_budget()`.** Those are
    deliberately different numbers: `check_budget().remaining` is
    `monthly_budget − captured spend`, a live SQL sum over the ledger, while the
    authorize gate *additionally* subtracts reserved holds. So the only way to
    see a hold reserve budget is to ask the gate — take a hold, watch a second
    spend that would fit without it get refused, release the hold, and watch the
    same spend succeed.

    (That the two numbers differ is worth knowing as a caller: an agent
    self-regulating on `check_budget()` sees headroom the gate may not grant.
    Pre-existing behaviour, not introduced here.)
    """
    # The SG ops agent has the smallest seeded budget, so two spends can bracket
    # its headroom while each stays under the review threshold. Sizes are derived
    # from the live figure rather than hardcoded, so a reseed cannot silently
    # turn this beat into a tautology.
    ops_agent, ops_entity = "ops", "sg"
    before = await lucnt.check_budget_async(agent=ops_agent)
    each = (before.remaining * Decimal("0.6")).quantize(Decimal("1"))
    assert each > 0, f"ops has no headroom to test with (remaining {before.remaining})"
    assert each < Decimal("100000"), (
        f"each spend ({each}) must stay under the seeded review threshold, or the "
        f"gate would answer 'review' rather than approved/blocked"
    )

    hold = await lucnt.authorize_async(
        agent=ops_agent,
        entity=ops_entity,
        vendor="dataprovider",
        amount=str(each),
        currency="SGD",
        reason="async smoke: hold to be released",
    )
    assert hold.approved, f"the first hold should fit: {hold.status} {hold.reason}"

    # Second spend of the same size: fits the *booked* budget, must not fit once
    # the first hold is counted.
    blocked = await lucnt.authorize_async(
        agent=ops_agent,
        entity=ops_entity,
        vendor="dataprovider",
        amount=str(each),
        currency="SGD",
        reason="async smoke: should be refused while the hold stands",
    )
    assert not blocked.approved, (
        "a second spend was approved while an equal hold was outstanding — "
        "reserved_spend is not being counted by the authorize gate"
    )
    print(f"[async] hold of {each} SGD reserved budget: second spend {blocked.code}")

    result = await lucnt.void_async(hold, reason="async smoke: vendor never called")
    assert result.authorization_id == hold.authorization_id
    assert not result.already_released

    # Same spend again, now that the hold is released.
    reapproved = await lucnt.authorize_async(
        agent=ops_agent,
        entity=ops_entity,
        vendor="dataprovider",
        amount=str(each),
        currency="SGD",
        reason="async smoke: should fit again once the hold is released",
    )
    assert reapproved.approved, (
        f"void did not free the reservation: {reapproved.status} {reapproved.reason}"
    )
    print("[async] void released it: the same spend is approved again")
    await lucnt.void_async(reapproved, reason="async smoke cleanup")

    # Idempotent: a second void is a no-op, because it is called from failure
    # paths where a second failure is the last thing the caller needs.
    repeat = await lucnt.void_async(hold, reason="async smoke: repeat")
    assert repeat.already_released, (
        "a repeated void was not reported as already released"
    )
    print("[async] repeated void reported already_released=True")


async def beat_capturing_a_voided_hold_is_refused() -> None:
    """The asymmetry: an explicit void means the money never moved."""
    verdict = await lucnt.authorize_async(
        agent=AGENT,
        entity=ENTITY,
        vendor="openai",
        amount="1.00",
        currency="USD",
        reason="async smoke: void then capture",
    )
    await lucnt.void_async(verdict, reason="async smoke")
    try:
        await lucnt.capture_async(verdict)
    except lucnt.ConflictError as exc:
        print(f"[async] capturing a voided hold refused as expected: {exc.status_code}")
    else:
        raise AssertionError("capturing a voided hold should have raised ConflictError")


async def beat_concurrent_spends() -> None:
    """Real concurrency over one pool — the reason the async path exists.

    Also the beat a mock cannot fake: these share an `httpx.AsyncClient`, so if
    the pool serialized requests the elapsed time would scale with the count.
    """
    started = time.monotonic()
    results = await asyncio.gather(
        *(
            lucnt.spend_async(
                agent=AGENT,
                entity=ENTITY,
                vendor="anthropic",
                amount="0.75",
                currency="USD",
                reason=f"async smoke: parallel task {n}",
            )
            for n in range(8)
        )
    )
    elapsed = time.monotonic() - started
    assert all(isinstance(r, lucnt.Transaction) for r in results), results
    ids = {r.id for r in results}  # type: ignore[union-attr]
    assert len(ids) == 8, f"expected 8 distinct transactions, got {len(ids)}"
    print(f"[async] 8 concurrent spends booked as 8 distinct txns in {elapsed:.2f}s")


async def beat_operation_replays_instead_of_double_booking() -> None:
    """T5-11 against the real dedupe: one op id, two attempts, one hold.

    This is the beat that most needs a live server. The guardrail is client-side,
    but the *replay* is the backend looking up a repeated `Idempotency-Key` — a
    mock cannot tell us whether the header we send is the one it dedupes on.
    """
    op_id = f"async-smoke-{int(time.time())}"
    first_id: str | None = None
    for attempt in range(2):
        with lucnt.operation(op_id):
            verdict = await lucnt.authorize_async(
                agent=AGENT,
                entity=ENTITY,
                vendor="openai",
                amount="3.00",
                currency="USD",
                reason="async smoke: idempotent hold",
            )
        if attempt == 0:
            first_id = verdict.authorization_id
        else:
            assert verdict.authorization_id == first_id, (
                f"a repeated operation() minted a second hold: "
                f"{first_id} != {verdict.authorization_id}"
            )
    print(f"[async] operation({op_id!r}) replayed one hold across two attempts")
    await lucnt.void_async(
        lucnt.Verdict(authorization_id=str(first_id), status="approved"),
        reason="async smoke cleanup",
    )


async def beat_a_different_spend_under_one_op_id_is_refused() -> None:
    """The guardrail: the server would replay a verdict describing the other spend."""
    op_id = f"async-guard-{int(time.time())}"
    try:
        with lucnt.operation(op_id):
            await lucnt.authorize_async(
                agent=AGENT,
                entity=ENTITY,
                vendor="openai",
                amount="1.00",
                currency="USD",
                reason="first",
            )
            await lucnt.authorize_async(
                agent=AGENT,
                entity=ENTITY,
                vendor="openai",
                amount="99.00",
                currency="USD",
                reason="second",
            )
    except lucnt.IdempotencyConflict:
        print("[async] a second, different spend under one op id was refused")
    else:
        raise AssertionError("expected IdempotencyConflict")


async def beat_reads() -> None:
    """The self-regulation reads, awaited."""
    budget = await lucnt.check_budget_async(agent=AGENT)
    assert budget.agent == AGENT
    memory = await lucnt.query_async(agent=AGENT, question="am I on track this month?")
    assert memory.headline, "query returned an empty headline"
    assert memory.budget.monthly_budget > 0
    print(f"[async] budget {budget.spent}/{budget.monthly_budget} ({budget.status})")
    print(f"[async] memory: {memory.headline}")


async def beat_a_sync_call_refuses_to_block_the_loop() -> None:
    """The guard, live: this would otherwise stall every coroutine in the process."""
    try:
        lucnt.check_budget(agent=AGENT)
    except lucnt.ConfigurationError as exc:
        assert "check_budget_async" in str(exc), exc
        print("[async] a sync call from inside the loop was refused, naming its twin")
    else:
        raise AssertionError("a sync call inside a running loop should have raised")


async def beat_a_policy_block_still_blocks() -> None:
    """The gate works identically on the async path — blocked before money moves."""
    try:
        await lucnt.spend_async(
            agent=AGENT,
            entity=ENTITY,
            vendor="coinbase",
            amount="10.00",
            currency="USD",
            reason="async smoke: blocklisted vendor",
            strict=True,
        )
    except lucnt.PolicyBlocked as exc:
        assert exc.code == "merchant_blocked", exc.code
        print(f"[async] policy blocked coinbase as expected: {exc.code}")
    else:
        raise AssertionError("expected PolicyBlocked for a blocklisted vendor")


async def main() -> None:
    """Run every beat in order, always closing the async pool on the way out."""
    lucnt.configure()  # reads LUCNT_API_KEY / LUCNT_BASE_URL
    try:
        await beat_authorize_capture()
        await beat_void_releases_the_hold()
        await beat_capturing_a_voided_hold_is_refused()
        await beat_concurrent_spends()
        await beat_operation_replays_instead_of_double_booking()
        await beat_a_different_spend_under_one_op_id_is_refused()
        await beat_reads()
        await beat_a_sync_call_refuses_to_block_the_loop()
        await beat_a_policy_block_still_blocks()
    finally:
        await lucnt.aclose()
    print("[async] all beats held")


if __name__ == "__main__":
    asyncio.run(main())
