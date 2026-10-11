"""Dev-only seed: a company, entities, mockup agents, persona logins, one API key.

Unblocks `sdk/examples/example_agent.py` end-to-end, which needs `agent=ops` /
`entity=sg` and `agent=research` / `entity=in` to exist. Agent identity/roster
matches the dashboard mockup's AGENTS array; `spent_ytd`/`spent_today` are
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
    Vendor,
)
from lucnt_api.models.user import User, UserRole
from lucnt_api.security import generate_api_key, hash_api_key, hash_password
from lucnt_api.services.chart_of_accounts import ensure_default_accounts
from lucnt_api.services.vendors import normalize_vendor_name

_COMPANY_SLUG = "acme-dev"

# (code, name, full_name, currency, accounting_std, tax_regime, timezone)
_ENTITIES = [
    (
        "sg",
        "LUCNT SG",
        "LUCNT Singapore Pte Ltd",
        "SGD",
        "SFRS",
        "SG-GST",
        "Asia/Singapore",
    ),
    (
        "in",
        "LUCNT IN",
        "LUCNT India Pvt Ltd",
        "INR",
        "Ind AS",
        "IN-GST",
        "Asia/Kolkata",
    ),
    (
        "us",
        "LUCNT US",
        "LUCNT US Inc",
        "USD",
        "US-GAAP",
        "US-Sales-Tax",
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
        slug="research",
        entity="in",
        glyph="◑",
        name="Research Agent",
        role="Vendor and market research",
        wallet_ref="Razorpay corporate",
        models_list=["GPT-5", "Claude", "Perplexity"],
        vendors_list=["OpenAI", "Anthropic", "Perplexity"],
        monthly_budget=Decimal("600000"),
        confidence_pct=94,
        is_live=True,
    ),
    _AgentSeed(
        slug="legal",
        entity="in",
        glyph="§",
        name="Legal Agent",
        role="Contract review and compliance",
        wallet_ref="Razorpay corporate",
        models_list=["Claude"],
        vendors_list=["Anthropic", "Westlaw"],
        monthly_budget=Decimal("60000"),
        confidence_pct=97,
        is_live=True,
    ),
    _AgentSeed(
        slug="marketing",
        entity="in",
        glyph="◆",
        name="Marketing Agent",
        role="Content and brand",
        wallet_ref="Razorpay corporate",
        models_list=["Claude", "Midjourney"],
        vendors_list=["Canva", "Meta", "Google"],
        monthly_budget=Decimal("200000"),
        confidence_pct=99,
        is_live=True,
    ),
    _AgentSeed(
        slug="coding",
        entity="us",
        glyph="⌘",
        name="Coding Agent",
        role="Engineering automation",
        wallet_ref="Stripe Issuing",
        models_list=["Claude", "GPT-5"],
        vendors_list=["Cursor", "Anthropic", "AWS"],
        monthly_budget=Decimal("5500"),
        confidence_pct=91,
        is_live=True,
    ),
    _AgentSeed(
        slug="sales",
        entity="us",
        glyph="▲",
        name="Sales Agent",
        role="Outbound and prospecting",
        wallet_ref="Stripe Issuing",
        models_list=["Claude"],
        vendors_list=["Clay", "Apollo"],
        monthly_budget=Decimal("3000"),
        confidence_pct=96,
        is_live=True,
    ),
    _AgentSeed(
        slug="support",
        entity="sg",
        glyph="◈",
        name="Support Agent",
        role="Customer support",
        wallet_ref="Airwallex",
        models_list=["Claude", "ElevenLabs"],
        vendors_list=["ElevenLabs", "Intercom"],
        monthly_budget=Decimal("2000"),
        confidence_pct=98,
        is_live=True,
    ),
    _AgentSeed(
        slug="ops",
        entity="sg",
        glyph="✦",
        name="Ops Agent",
        role="Operations and travel",
        wallet_ref="Airwallex",
        models_list=["Claude"],
        vendors_list=["Singapore Airlines", "Booking"],
        monthly_budget=Decimal("9000"),
        confidence_pct=86,
        is_live=True,
    ),
]


# (name, country, gst_registered_in) — the vendors `example_agent.py` spends to,
# enriched so place of supply (not currency) decides tax. Names match the
# example's vendor strings exactly (normalized lower/trim) so the capture path's
# resolve-or-create finds these rows instead of auto-creating unenriched ones —
# without this the demo's imported spends classify UNCLASSIFIED and its reverse-
# charge assertion fails. `openai`/`anthropic`/etc. are foreign (US), unregistered
# in the buyer's country -> IGST reverse charge; `singapore_airlines` is a domestic
# SG supply -> forward-charge GST. `coinbase` is policy-blocked before capture, so
# it never needs a row.
_VENDORS: list[tuple[str, str, list[str]]] = [
    ("singapore_airlines", "SG", ["SG"]),  # domestic SG -> forward-charge GST
    ("openai", "US", []),  # foreign, unregistered -> RCM (IGST)
    ("anthropic", "US", []),  # foreign -> RCM (the metered call)
    ("dataprovider", "US", []),  # foreign -> RCM
    ("flakyapi", "US", []),  # foreign -> RCM
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
                # Place of supply of the recipient: the entity's country is
                # its 2-letter code upper-cased (sg->SG, in->IN, us->US). Without it
                # every spend classifies UNCLASSIFIED — tax can't be decided.
                country=code.upper(),
                # US buyer state for sales/use-tax lookup. Delaware has no
                # state sales tax, so a US spend books cleanly (no use-tax accrual).
                us_state="DE" if code == "us" else None,
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


# --- Dashboard personas (P7.0) ---------------------------------------------
# One user per role in `UserRole`, so the dashboard persona switch demos
# against real server-side scoping rather than a UI toggle. `owner` names the
# agent slugs assigned to that user's `agents.owner_user_id` — all seven were
# NULL before this, which made `agent_scope="owned"` untestable.
#
# The password is DEV-ONLY and identical for all five, deliberately: it is
# printed by the seed, typed on stage, and the whole company lives in a
# throwaway database. `ENVIRONMENT != development` is not a supported target for
# this script.
_DEV_PASSWORD = "lucnt-demo-2026"

# (email local part, name, role, owned agent slugs)
_USERS: list[tuple[str, str, UserRole, tuple[str, ...]]] = [
    ("arvind", "Arvind Sharma", UserRole.CFO, ()),
    ("dev", "Priya Nair", UserRole.ENGINEER, ()),
    ("owner", "Rahul Menon", UserRole.AGENT_OWNER, ("research", "legal")),
    ("approver", "Sneha Iyer", UserRole.APPROVER, ()),
    ("auditor", "Vikram Rao", UserRole.AUDITOR, ()),
]


def _get_or_create_users(db: Session, company: Company) -> list[tuple[str, str]]:
    """Create the five persona logins and assign agent ownership.

    Returns:
        `(email, role)` pairs for the credentials block the seed prints.
    """
    credentials: list[tuple[str, str]] = []
    for local, name, role, owned in _USERS:
        email = f"{local}@acme.dev"
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            user = User(
                company_id=company.id,
                email=email,
                name=name,
                role=role,
                password_hash=hash_password(_DEV_PASSWORD),
            )
            db.add(user)
            db.flush()
        for slug in owned:
            agent = db.execute(
                select(Agent).where(Agent.company_id == company.id, Agent.slug == slug)
            ).scalar_one_or_none()
            if agent is not None:
                agent.owner_user_id = user.id
        credentials.append((email, role.value))
    db.flush()
    return credentials


def _get_or_create_vendors(db: Session, company: Company) -> None:
    """Seed the demo vendors with their place of supply."""
    for name, country, registered in _VENDORS:
        normalized = normalize_vendor_name(name)
        existing = db.execute(
            select(Vendor).where(
                Vendor.company_id == company.id,
                Vendor.normalized_name == normalized,
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        db.add(
            Vendor(
                company_id=company.id,
                name=name,
                normalized_name=normalized,
                country=country,
                gst_registered_in=registered,
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
        credentials = _get_or_create_users(db, company)
        _get_or_create_vendors(db, company)
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
    print(f"Vendors: {', '.join(name for name, *_ in _VENDORS)}")
    print(f"Policies: {', '.join(name for name, *_ in _POLICIES)}")
    print()
    print(f"LUCNT_API_KEY={plaintext}")
    print("(shown once -- only its hash is stored)")
    print()
    print("Dashboard logins (dev only):")
    for email, role in credentials:
        print(f"  {email:<20} {role:<12} password: {_DEV_PASSWORD}")


if __name__ == "__main__":
    main()
