# lucnt

**Financial infrastructure that attaches to your existing agents.**

`lucnt` gives an AI agent's spending a gate and a ledger. It does not introduce an `Agent` class —
that would compete with whatever framework you already run. Identity is a plain string handle set
once, and every kind of agent-initiated spend (a flight, a SaaS seat, a batch of LLM tokens) flows
through the same two calls:

```
authorize()  →  can this happen?   ← runs BEFORE money moves, so it can block
capture()    →  it happened, book it
```

Everything else — double-entry journals, GST/VAT/sales-tax treatment, budgets, anomaly
detection — is derived server-side from what `capture()` commits.

Not yet on PyPI. Install from GitHub:

```bash
pip install "git+https://github.com/anushtup-nandy/lucnt.git#subdirectory=sdk"
```

Or from a clone: `pip install -e ./sdk`.

## Pointing the SDK at your backend

LUCNT is self-hosted: the SDK talks to a `lucnt` backend you run (see
[Running locally](https://github.com/anushtup-nandy/lucnt#running-locally)).
The base URL defaults to `http://localhost:8000`, which is where that backend
listens in development. For any other deployment, set it explicitly:

```bash
export LUCNT_API_KEY=lucnt_sk_...
export LUCNT_BASE_URL=https://lucnt.your-company.internal
```

or in code:

```python
lucnt.configure(api_key="lucnt_sk_...", base_url="https://lucnt.your-company.internal")
```

Plain `http://` is accepted only for `localhost`, `127.0.0.1` and `::1`; any
other host must be `https://`.

## Quickstart

```python
import lucnt

lucnt.configure(api_key="lucnt_sk_...")   # or set LUCNT_API_KEY

with lucnt.task(agent="travel-bot", entity="acme-in", intent="founder summit"):
    verdict = lucnt.authorize(
        vendor="United Airlines",
        amount="842.50",
        currency="USD",
        reason="SFO->EWR, 12 Aug",
    )

    if not verdict.approved:
        print(f"blocked: {verdict.reason}")      # policy said no — nothing was charged
    else:
        book_the_flight()                        # your code; the real purchase
        tx = lucnt.capture(verdict)
        print(tx.journal, tx.tax)                # balanced double-entry + tax treatment
```

`authorize()` returns a `Verdict` rather than raising, so you always check `.approved` before
spending. Pass `strict=True` to raise `PolicyBlocked` (or `BudgetExhausted`) instead:

```python
verdict = lucnt.authorize(..., strict=True)      # raises on a blocking verdict
```

When there is nothing to do between the gate and the purchase, `spend()` is the atomic pair:

```python
tx = lucnt.spend(vendor="AWS", amount="120.00", currency="USD", reason="nightly batch")
```

## Auto-instrumenting an LLM client

`meter()` wraps an OpenAI or Anthropic client and returns a transparent proxy. The completion call
is authorized on a pre-call estimate, then captured at the actual cost read from the response's
usage — no manual `authorize`/`capture` around every call.

```python
from openai import OpenAI

client = lucnt.meter(OpenAI())

with lucnt.task(agent="researcher", entity="acme-us"):
    resp = client.chat.completions.create(          # governed: blocks before spending
        model="gpt-4o",
        messages=[{"role": "user", "content": "summarize this filing"}],
    )
```

This is convenience over the primitive, not a replacement for it — `meter()` covers the LLM/API
subset, `authorize`/`capture` covers all spend.

**Turn off the vendor client's own retries.** A vendor SDK that retries internally does so *inside*
the wrapped call, so lucnt records one spend while the vendor may have served — and charged for —
more than one request. Construct it with `max_retries=0` and retry at the lucnt layer instead:

```python
client = lucnt.meter(OpenAI(max_retries=0))
```

## Async

Every function has an `_async` twin, and `meter()` accepts an async client:

```python
import asyncio, lucnt
from openai import AsyncOpenAI

async def main() -> None:
    lucnt.configure(api_key="sk_live_...")
    client = lucnt.meter(AsyncOpenAI(max_retries=0))

    with lucnt.task(agent="researcher", entity="acme-us"):
        # Metered, awaitable, and safe to run concurrently.
        await asyncio.gather(*(
            client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": q}],
            )
            for q in questions
        ))

        tx = await lucnt.spend_async(
            vendor="AWS", amount="120.00", currency="USD", reason="nightly batch",
        )

    await lucnt.aclose()          # optional: release sockets deterministically

asyncio.run(main())
```

`task()` and `operation()` need no async variant — they are contextvars, so they propagate across
`await` unchanged. Streaming works on both flavours (`async for` over `stream=True`, `async with`
over `messages.stream()`).

Calling a **synchronous** function from inside a running event loop raises `ConfigurationError`
naming the async twin, rather than blocking the loop for the request duration. To use the sync API
from async code deliberately, put it on a thread: `await asyncio.to_thread(lucnt.spend, ...)`.

## Letting an agent regulate itself

```python
budget = lucnt.check_budget(agent="researcher")
if budget.remaining < 50:
    switch_to_cheaper_model()

memory = lucnt.query(agent="researcher", question="am I on track this month?")
print(memory.headline)          # run-rate projection, cost-per-output, spend fingerprint
```

## Errors

All exceptions derive from `LucntError`:

| Exception | Meaning |
|---|---|
| `PolicyBlocked` | A policy rule refused the spend (`strict=True` only) |
| `BudgetExhausted` | Subclass of `PolicyBlocked` — the block was a budget cap |
| `AuthError` | Bad or missing API key |
| `ConflictError` | The request collided with resource state (e.g. voiding a captured hold) |
| `InvalidRequestError` | Malformed request (400/422) — never retry it |
| `NotFoundError` | No such agent, entity, authorization, or transaction |
| `RateLimitError` | Rate limited; carries `retry_after` |
| `IdempotencyConflict` | One `operation()` scope was used for two different spends |
| `ContractError` | The API returned a success body this SDK version cannot read |
| `ConfigurationError` | SDK used before `configure()`, an invalid base URL, inexact money, or a sync call from inside an event loop |
| `TransportError` | Network failure, timeout, or 5xx — after retries are exhausted |

Transient failures (network errors, timeouts, 5xx, 429) are retried automatically with jittered
exponential backoff. Retries are conservative by design: a request is only repeated when it is a
safe method or carries an idempotency key, so an unkeyed write is never sent twice.

### Retrying at your own layer

The SDK's automatic retries are transport-level. If *your* code retries, name the operation so the
retry replays the original hold instead of minting a second one:

```python
with lucnt.operation(f"order-{order.id}"):        # one scope = one logical spend
    lucnt.spend(vendor="Acme", amount="49.00", currency="USD", reason="seat upgrade")
```

## Status

Pre-1.0 and versioned accordingly — the public surface is stable in practice but not yet under a
semver 1.0 promise.

**Money must be exact.** `amount` accepts `Decimal`, `int`, or `str`. `float` is **rejected** — this
is a ledger, and `Decimal(1.83)` is not `Decimal("1.83")`. (Accepted in 0.1.x; removed since.)

Requires Python 3.10+.

## Documentation

Source, issues and changelog: **[github.com/anushtup-nandy/lucnt](https://github.com/anushtup-nandy/lucnt)**.

## License

Apache-2.0. See [LICENSE](LICENSE).
