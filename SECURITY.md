# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report privately through
[GitHub Security Advisories](https://github.com/anushtup-nandy/lucnt/security/advisories/new),
which gives us a private channel to discuss and fix the issue before it becomes public.

Please include:

- what the issue is and where it lives (file, endpoint, or SDK call)
- how to reproduce it
- what an attacker gains — the impact is what drives our priority

We will acknowledge within **3 business days** and aim to give you a fix or a
concrete timeline within **14 days**. We are happy to credit you in the advisory
unless you would rather stay anonymous.

## Scope

LUCNT sits in the money path — it authorizes spend and writes the ledger — so we
are particularly interested in:

- **Authorization bypass** — anything that gets a spend past the policy engine, or
  turns a `blocked` / `human_hold` verdict into an approved one
- **Cross-tenant data access** — an API key or dashboard session reaching another
  company's or entity's agents, transactions, or books
- **Ledger integrity** — double-capture, unbalanced journal entries, replay of an
  authorization, or any path that mutates a committed transaction
- **Authentication and key handling** — API key forgery, privilege escalation
  between key scopes, JWT weaknesses
- **Injection** anywhere in the request path

Out of scope: findings that require a compromised host or database, denial of
service through sheer volume, missing hardening headers with no demonstrated
impact, and anything in an example, test fixture, or the local `docker compose`
development defaults (which are documented as insecure and never intended for
production).

## Supported versions

LUCNT is pre-1.0. Security fixes land on `main` and in the next SDK release; there
are no backported patch branches yet.

## Deployment note

Several defaults in this repository are **development-only** and will bite you if
shipped as-is:

- `SECRET_KEY` in `backend/.env.example` and `infra/docker-compose.yml` is a known
  public value. Outside `ENVIRONMENT=development` the app refuses to start with it.
- `ENVIRONMENT` has no default on purpose — an unset value must never be treated as
  development, because that would publish `/docs`, `/redoc` and `/openapi.json`.
- Nginx must set `set_real_ip_from` to **only** the CIDRs you control. The
  auth-failure rate limiter keys on client IP, so a too-broad trust range collapses
  every client into one bucket and becomes a lockout for all of them.
- Redis rate limiting **fails open** by design: an outage admits the request and
  logs a warning, because a cache blip must not take down `authorize()`. Alert on it.
