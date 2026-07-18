"""Dev-only seed: a company, its entities, and mockup agents, plus one API key.

Unblocks `sdk/examples/example_agent.py` end-to-end, which needs `agent=ops` /
`entity=sg` and `agent=research` / `entity=in` to exist. Agent identity/roster
matches `docs/lucnt-v9.html`'s AGENTS array; `spent_ytd`/`spent_today` are
seeded at 0 rather than copied from the mockup, whose numbers are a demo
snapshot (e.g. "research" shown budget-exhausted) rather than a sane starting
state for a fresh install. Idempotent on company/entities/agents (matched by
slug/code); always issues one fresh API key.

Prints the plaintext key exactly once -- only its hash is ever persisted.

    conda run -n lucnt-backend python scripts/seed_dev.py
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from lucnt_api.database import SessionLocal
from lucnt_api.models import (
    Agent,
    ApiKey,
    ApiKeyMode,
    Company,
    Entity,
    Policy,
    PolicyRuleType,
    PolicyScopeType,
)
from lucnt_api.security import generate_api_key, hash_api_key
from lucnt_api.services.chart_of_accounts import ensure_default_accounts

_COMPANY_SLUG = "acme-dev"

# (code, name, full_name, currency, accounting_std, tax_regime, timezone)
_ENTITIES = [
    (
        "sg", "LUCNT SG", "LUCNT Singapore Pte Ltd", "SGD", "SFRS", "SG-GST",
        "Asia/Singapore",
    ),
    (
        "in", "LUCNT IN", "LUCNT India Pvt Ltd", "INR", "Ind AS", "IN-GST",
        "Asia/Kolkata",
    ),
    (
        "us", "LUCNT US", "LUCNT US Inc", "USD", "US-GAAP", "US-Sales-Tax",
        "America/Los_Angeles",
    ),
]


@dataclass(frozen=True)
class _AgentSeed:
    """One row of mockup-matching agent seed data."""

    slug: str
    entity: str
    glyph: str
    name: str
    role: str
    wallet_ref: str
    models_list: list[str]
    vendors_list: list[str]
    monthly_budget: Decimal
    confidence_pct: int
    is_live: bool


_AGENTS = [
    _AgentSeed(
        slug="research", entity="in", glyph="◑", name="Research Agent",
        role="Vendor and market research", wallet_ref="Razorpay corporate",
        models_list=["GPT-5", "Claude", "Perplexity"],
        vendors_list=["OpenAI", "Anthropic", "Perplexity"],
        monthly_budget=Decimal("600000"), confidence_pct=94, is_live=True,
    ),
    _AgentSeed(
        slug="legal", entity="in", glyph="§", name="Legal Agent",
        role="Contract review and compliance", wallet_ref="Razorpay corporate",
        models_list=["Claude"], vendors_list=["Anthropic", "Westlaw"],
        monthly_budget=Decimal("60000"), confidence_pct=97, is_live=True,
    ),
    _AgentSeed(
        slug="marketing", entity="in", glyph="◆", name="Marketing Agent",
        role="Content and brand", wallet_ref="Razorpay corporate",
        models_list=["Claude", "Midjourney"], vendors_list=["Canva", "Meta", "Google"],
        monthly_budget=Decimal("200000"), confidence_pct=99, is_live=False,
    ),
    _AgentSeed(
        slug="coding", entity="us", glyph="⌘", name="Coding Agent",
        role="Engineering automation", wallet_ref="Stripe Issuing",
        models_list=["Claude", "GPT-5"], vendors_list=["Cursor", "Anthropic", "AWS"],
        monthly_budget=Decimal("5500"), confidence_pct=91, is_live=True,
    ),
    _AgentSeed(
        slug="sales", entity="us", glyph="▲", name="Sales Agent",
        role="Outbound and prospecting", wallet_ref="Stripe Issuing",
        models_list=["Claude"], vendors_list=["Clay", "Apollo"],
        monthly_budget=Decimal("3000"), confidence_pct=96, is_live=True,
    ),
    _AgentSeed(
        slug="support", entity="sg", glyph="◈", name="Support Agent",
        role="Customer support", wallet_ref="Airwallex",
        models_list=["Claude", "ElevenLabs"], vendors_list=["ElevenLabs", "Intercom"],
        monthly_budget=Decimal("2000"), confidence_pct=98, is_live=True,
    ),
    _AgentSeed(
        slug="ops", entity="sg", glyph="✦", name="Ops Agent",
        role="Operations and travel", wallet_ref="Airwallex",
        models_list=["Claude"], vendors_list=["Singapore Airlines", "Booking"],
        monthly_budget=Decimal("9000"), confidence_pct=86, is_live=False,
    ),
]


def _get_or_create_company(db: Session) -> Company:
    company = db.execute(
        select(Company).where(Company.slug == _COMPANY_SLUG)
    ).scalar_one_or_none()
    if company is not None:
        return company
    company = Company(name="Acme Inc (dev)", slug=_COMPANY_SLUG)
    db.add(company)
    db.flush()
    return company


def _get_or_create_entities(db: Session, company: Company) -> dict[str, Entity]:
    entities: dict[str, Entity] = {}
    for code, name, full_name, currency, std, regime, tz in _ENTITIES:
        entity = db.execute(
            select(Entity).where(Entity.company_id == company.id, Entity.code == code)
        ).scalar_one_or_none()
        if entity is None:
            entity = Entity(
                company_id=company.id,
                name=name,
                code=code,
                full_name=full_name,
                currency=currency,
                accounting_std=std,
                tax_regime=regime,
                timezone=tz,
            )
            db.add(entity)
            db.flush()
        entities[code] = entity
    return entities


def _get_or_create_agents(
    db: Session, company: Company, entities: dict[str, Entity]
) -> None:
    for seed in _AGENTS:
        existing = db.execute(
            select(Agent).where(Agent.company_id == company.id, Agent.slug == seed.slug)
        ).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(
            Agent(
                company_id=company.id,
                entity_id=entities[seed.entity].id,
                name=seed.name,
                slug=seed.slug,
                glyph=seed.glyph,
                role=seed.role,
                wallet_ref=seed.wallet_ref,
                models_list=seed.models_list,
                vendors_list=seed.vendors_list,
                monthly_budget=seed.monthly_budget,
                confidence_pct=seed.confidence_pct,
                is_live=seed.is_live,
            )
        )
    db.flush()


# (name, rule_type, config, description) — company-wide defaults. Chosen NOT to
# block the example_agent's normal spends: crypto exchanges nobody uses, and a
# review threshold well above the demo amounts.
_POLICIES: list[tuple[str, PolicyRuleType, dict[str, object], str]] = [
    (
        "No crypto exchanges",
        PolicyRuleType.MERCHANT_LIST,
        {"mode": "block", "vendors": ["coinbase", "binance"]},
        "Agent spend on crypto exchanges is blocked outright.",
    ),
    (
        "Review large spends",
        PolicyRuleType.AMOUNT_THRESHOLD,
        {"max_amount": "100000", "action": "review"},
        "Single spends over 100,000 are flagged for human review.",
    ),
]


def _get_or_create_policies(db: Session, company: Company) -> None:
    for name, rule_type, config, description in _POLICIES:
        existing = db.execute(
            select(Policy).where(Policy.company_id == company.id, Policy.name == name)
        ).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(
            Policy(
                company_id=company.id,
                name=name,
                rule_type=rule_type,
                config_json=config,
                scope_type=PolicyScopeType.COMPANY,
                description=description,
            )
        )
    db.flush()


def _issue_api_key(db: Session, company: Company) -> str:
    plaintext = generate_api_key(ApiKeyMode.LIVE)
    db.add(
        ApiKey(
            company_id=company.id,
            key_prefix=plaintext[:12],
            key_hash=hash_api_key(plaintext),
            name="dev seed key",
            mode=ApiKeyMode.LIVE,
        )
    )
    db.flush()
    return plaintext


def main() -> None:
    """Seed a dev company/entities/agents and print one fresh live API key."""
    db = SessionLocal()
    try:
        company = _get_or_create_company(db)
        entities = _get_or_create_entities(db, company)
        _get_or_create_agents(db, company, entities)
        _get_or_create_policies(db, company)
        # Default Chart of Accounts so the Books layer has accounts to post to
        # (capture self-heals this too, but seed it explicitly for a clean start).
        ensure_default_accounts(db, company.id)
        plaintext = _issue_api_key(db, company)
        db.commit()
        company_name, company_slug = company.name, company.slug
    finally:
        db.close()

    print(f"Company: {company_name} ({company_slug})")
    print(f"Entities: {', '.join(sorted(entities))}")
    print(f"Agents: {', '.join(seed.slug for seed in _AGENTS)}")
    print(f"Policies: {', '.join(name for name, *_ in _POLICIES)}")
    print()
    print(f"LUCNT_API_KEY={plaintext}")
    print("(shown once -- only its hash is stored)")


if __name__ == "__main__":
    main()
