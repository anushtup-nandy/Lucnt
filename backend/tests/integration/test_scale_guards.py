"""Scale, uniqueness and pool guards (PRODUCTION_PLAN T3-1, T3-4, T3-5, T3-6).

Three classes of guard, none of which any prior test covered:

* **T3-5/T3-6 — uniqueness at the DB.** A duplicate entity code makes every
  ``authorize()`` for that entity raise ``MultipleResultsFound`` (a 500 that
  takes the spend path down); a duplicate ``transaction_hash`` lets a redelivered
  card webhook double-book a spend. Both must be rejected by the schema itself,
  for *any* writer, and both must stay scoped per-company.
* **T3-1 — the hot-path indexes exist.** Asserted against ``pg_indexes`` so a
  future migration (or a stale model ``__table_args__``) cannot silently drop the
  indexes that keep ``authorize()`` off a sequential scan. ``alembic check``
  catches model/DB drift; this catches the DB losing them outright.
* **T3-4 — the pool is configured.** A bare ``create_engine()`` has no liveness
  check and no server-side statement cap.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucnt_api.database import engine
from lucnt_api.models import Agent, Company, Entity, Transaction, TransactionSource


def _company(db_session: Session, name: str = "Acme") -> Company:
    company = Company(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    db_session.flush()
    return company


def _entity(db_session: Session, company: Company, code: str) -> Entity:
    entity = Entity(
        company_id=company.id,
        name=f"LUCNT {code.upper()}",
        code=code,
        full_name=f"LUCNT {code.upper()} Pte Ltd",
        currency="SGD",
        accounting_std="SFRS",
        tax_regime="SG-GST",
        timezone="Asia/Singapore",
    )
    db_session.add(entity)
    db_session.flush()
    return entity


def _txn(
    db_session: Session,
    company: Company,
    entity: Entity,
    agent: Agent,
    txn_hash: str | None,
) -> Transaction:
    txn = Transaction(
        company_id=company.id,
        agent_id=agent.id,
        entity_id=entity.id,
        source=TransactionSource.CARD,
        transaction_hash=txn_hash,
        vendor="stripe",
        paid_amount=Decimal("10"),
        paid_currency="SGD",
        local_amount=Decimal("10"),
        local_currency="SGD",
        transaction_date=datetime.now(timezone.utc),
    )
    db_session.add(txn)
    db_session.flush()
    return txn


def _agent(db_session: Session, company: Company, entity: Entity) -> Agent:
    agent = Agent(
        company_id=company.id,
        entity_id=entity.id,
        name="Ops Agent",
        slug=f"ops-{uuid.uuid4().hex[:8]}",
        glyph="x",
        role="Operations",
        monthly_budget=Decimal("9000"),
    )
    db_session.add(agent)
    db_session.flush()
    return agent


# --- T3-5: entities (company_id, code) is unique ---


def test_db_rejects_duplicate_entity_code_in_one_company(db_session: Session) -> None:
    """Two entities sharing a code break authorize()'s scalar_one_or_none()."""
    company = _company(db_session)
    _entity(db_session, company, "sg")
    with pytest.raises(IntegrityError), db_session.begin_nested():
        _entity(db_session, company, "sg")


def test_same_entity_code_allowed_across_companies(db_session: Session) -> None:
    """The guard is per-tenant: two customers may both call an entity "sg"."""
    first = _entity(db_session, _company(db_session, "Acme"), "sg")
    second = _entity(db_session, _company(db_session, "Globex"), "sg")
    assert first.code == second.code
    assert first.company_id != second.company_id


# --- T3-6: transactions (company_id, transaction_hash) is unique when set ---


def test_db_rejects_duplicate_transaction_hash(db_session: Session) -> None:
    """A redelivered card webhook must not double-book the same spend."""
    company = _company(db_session)
    entity = _entity(db_session, company, "sg")
    agent = _agent(db_session, company, entity)
    _txn(db_session, company, entity, agent, "sha256:abc")
    with pytest.raises(IntegrityError), db_session.begin_nested():
        _txn(db_session, company, entity, agent, "sha256:abc")


def test_null_transaction_hashes_are_unconstrained(db_session: Session) -> None:
    """The index is partial: source=sdk rows always leave the hash null."""
    company = _company(db_session)
    entity = _entity(db_session, company, "sg")
    agent = _agent(db_session, company, entity)
    first = _txn(db_session, company, entity, agent, None)
    second = _txn(db_session, company, entity, agent, None)
    assert first.id != second.id


def test_same_transaction_hash_allowed_across_companies(db_session: Session) -> None:
    """Dedup is per-tenant — two customers' webhooks can hash identically."""
    hashes = []
    for name in ("Acme", "Globex"):
        company = _company(db_session, name)
        entity = _entity(db_session, company, "sg")
        agent = _agent(db_session, company, entity)
        hashes.append(_txn(db_session, company, entity, agent, "sha256:same").id)
    assert hashes[0] != hashes[1]


# --- T3-1: the hot-path indexes are present in the live schema ---


# The subset whose absence is a latency bug on the blocking money path, not just
# a slow dashboard: two scans of `transactions` and two of `authorizations` per
# authorize(), plus the per-call policy fetch.
_MONEY_PATH_INDEXES = (
    "ix_transactions_agent_id_transaction_date",
    "ix_authorizations_agent_id_status_created_at",
    "ix_policies_company_id_active",
    "ix_journal_entries_transaction_id",
    "ix_tax_records_transaction_id",
    "uq_entities_company_code",
    "uq_transactions_company_transaction_hash",
)


@pytest.mark.parametrize("index_name", _MONEY_PATH_INDEXES)
def test_hot_path_index_exists(db_session: Session, index_name: str) -> None:
    found = db_session.execute(
        text(
            "SELECT 1 FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexname = :name"
        ),
        {"name": index_name},
    ).scalar_one_or_none()
    assert found == 1, f"missing index {index_name} — authorize() will seq-scan"


def test_no_invalid_indexes(db_session: Session) -> None:
    """A failed CONCURRENTLY build leaves an INVALID index that enforces nothing."""
    invalid = db_session.execute(
        text("SELECT count(*) FROM pg_index WHERE NOT indisvalid")
    ).scalar_one()
    assert invalid == 0


# --- T3-4: the engine pool is explicitly configured ---


def test_engine_has_pre_ping_and_statement_timeout() -> None:
    """Without these, an idle-reaped connection 500s and a runaway query pins one."""
    assert engine.pool._pre_ping is True
    with engine.connect() as conn:
        timeout = conn.execute(text("SHOW statement_timeout")).scalar_one()
    assert timeout not in ("0", 0), "no server-side statement cap is configured"
