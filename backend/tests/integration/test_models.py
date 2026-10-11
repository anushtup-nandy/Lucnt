"""ORM models against the migrated schema."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucnt_api.models import Company, Entity, User, UserRole


def _make_company(db_session: Session, slug: str = "acme") -> Company:
    company = Company(name="Acme Inc", slug=slug)
    db_session.add(company)
    db_session.flush()
    return company


def test_company_persists_with_defaults(db_session: Session) -> None:
    company = _make_company(db_session)
    assert company.id is not None
    assert company.plan == "free"
    assert company.settings == {}
    assert company.created_at is not None
    assert company.updated_at is not None


def test_entity_defaults_to_usd_rate_as_decimal(db_session: Session) -> None:
    company = _make_company(db_session)
    entity = Entity(
        company_id=company.id,
        name="LUCNT SG",
        code="SG",
        full_name="LUCNT Singapore Pte Ltd",
        currency="SGD",
        accounting_std="SFRS",
        tax_regime="SG-GST",
        timezone="Asia/Singapore",
    )
    db_session.add(entity)
    db_session.flush()
    assert entity.to_usd_rate == Decimal("1.0")
    assert isinstance(entity.to_usd_rate, Decimal)


def test_user_role_round_trips_as_lowercase_enum(db_session: Session) -> None:
    company = _make_company(db_session)
    user = User(
        company_id=company.id,
        email="ops@acme.test",
        name="Ops Lead",
        role=UserRole.AGENT_OWNER,
        password_hash="not-a-real-hash",
    )
    db_session.add(user)
    db_session.flush()
    db_session.refresh(user)
    assert user.role == UserRole.AGENT_OWNER
    assert user.role.value == "agent_owner"


def test_duplicate_email_violates_unique_constraint(db_session: Session) -> None:
    company = _make_company(db_session)
    db_session.add(
        User(
            company_id=company.id,
            email="dup@acme.test",
            name="First",
            role=UserRole.ENGINEER,
            password_hash="hash1",
        )
    )
    db_session.flush()
    db_session.add(
        User(
            company_id=company.id,
            email="dup@acme.test",
            name="Second",
            role=UserRole.ENGINEER,
            password_hash="hash2",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_invalid_role_value_is_rejected(db_session: Session) -> None:
    company = _make_company(db_session)
    db_session.add(
        User(
            company_id=company.id,
            email="bad-role@acme.test",
            name="Bad Role",
            role="not_a_real_role",  # type: ignore[arg-type]
            password_hash="hash",
        )
    )
    with pytest.raises(Exception):  # noqa: B017 -- exact SQLAlchemy exception type TBD
        db_session.flush()
