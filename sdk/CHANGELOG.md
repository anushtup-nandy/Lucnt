# Changelog

All notable changes to the `lucnt` Python SDK are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — unreleased

First public release.

### Added

- **`authorize()` → `capture()`** — the core primitive. `authorize()` runs the
  policy engine *before* money moves and returns a `Verdict`
  (`approved` / `blocked` / `review` / `human_hold`); `capture()` commits the real
  spend to the ledger. Spend-type agnostic: a flight, a SaaS seat and a batch of
  tokens all take the same call.
- **`spend()`** — atomic `authorize` + `capture` for callers that don't need a gap
  between the two.
- **`task()`** — a context manager that sets agent, entity and intent once for
  every call made inside it. `bind_context()` for non-`with` call sites.
- **`meter(client)`** — auto-instruments an OpenAI or Anthropic client so token
  spend is booked without hand-writing the calls. Sync and async clients, including
  streaming helpers. `metered_http_client()` covers LangChain and anything else
  that accepts an httpx client.
- **`check_budget()`** — returns `reserved` and `available` so an agent can
  self-regulate, and **`query()`** for reading its own spend history.
- **Full async surface** — `authorize_async()`, `capture_async()`,
  `check_budget_async()` and friends mirror every synchronous call.
- **Typed exception hierarchy** rooted at `LucntError`: `PolicyBlocked`,
  `BudgetExhausted`, `IdempotencyConflict`, `AuthError`, `RateLimitError`,
  `TransportError`, `ContractError` and others.
- **`Money` as `Decimal` throughout.** No float ever touches an amount.
- **`py.typed`** — the package ships its own type information.
- Python 3.10 through 3.13; tested against 3.10 and 3.12 in CI.

[Unreleased]: https://github.com/anushtup-nandy/lucnt/compare/main...HEAD
