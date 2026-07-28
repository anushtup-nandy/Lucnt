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

```bash
pip install lucnt
```

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
| `ConfigurationError` | SDK used before `configure()`, or an invalid base URL |
| `TransportError` | Network failure reaching the API |

## Status

Pre-1.0 and versioned accordingly — the public surface is stable in practice but not yet under a
semver 1.0 promise. Known gaps being closed before 1.0.0: the transport is **synchronous only**
(`meter()` does not yet accept `AsyncOpenAI`), streaming responses are not metered, and there are
no automatic retries. Money amounts accept `str`/`int`/`Decimal`/`float` today; **`float` will be
rejected in 1.0.0** — pass `Decimal` or `str` for money.

Requires Python 3.10+.

## Documentation

Full reference at **[docs.lucnt.dev](https://docs.lucnt.dev)**.

## License

Apache-2.0. See [LICENSE](LICENSE).
