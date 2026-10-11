"""Transactions router: the authorize→capture spine, void, and reversal."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lucnt_api import metrics
from lucnt_api.config import settings
from lucnt_api.dependencies import (
    AuthContext,
    get_auth_context,
    get_db,
    require_admin,
    require_live,
    require_nav,
)
from lucnt_api.models import (
    Agent,
    AgentStatus,
    Authorization,
    Company,
    Entity,
    JournalEntry,
    Policy,
    PolicyScopeType,
    ReconciliationException,
    ReconExceptionType,
    TaxRate,
    TaxRecord,
    Transaction,
    TransactionSource,
    TransactionStatus,
    Vendor,
    VerdictStatus,
    WithholdingRecord,
)
from lucnt_api.pagination import Page, count_total, paginate
from lucnt_api.schemas.transaction import (
    CaptureRequest,
    JournalLine,
    SpendRequest,
    TaxInfo,
    TransactionDetail,
    TransactionFull,
    TransactionListResponse,
    Verdict,
    VoidRequest,
    VoidResult,
)
from lucnt_api.schemas.transaction import Transaction as TransactionSchema
from lucnt_api.services.audit import record as record_audit
from lucnt_api.services.chart_of_accounts import ensure_default_accounts
from lucnt_api.services.fx import FxRateUnavailableError, convert
from lucnt_api.services.journal_generator import generate_journal
from lucnt_api.services.lookups import entity_or_404
from lucnt_api.services.permissions import scope_agents
from lucnt_api.services.policy_engine import SpendContext, evaluate_spend
from lucnt_api.services.self_invoice import build_self_invoice
from lucnt_api.services.spend_window import (
    committed_spend,
    now_in_entity_tz,
    period_starts,
    recent_activity_count,
    reserved_spend,
)
from lucnt_api.services.tax_engine import TaxTreatment, compute_tax
from lucnt_api.services.tax_rates import (
    STANDARD_CATEGORY,
    US_REGIME,
    TaxRateUnavailableError,
    resolve_rate_row,
    resolve_us_rule,
)
from lucnt_api.services.vendors import (
    normalize_vendor_name,
    resolve_or_create_vendor,
)
from lucnt_api.services.withholding import WithholdingOutcome, determine_withholding

router = APIRouter(
    prefix="/transactions",
    tags=["transactions"],
    dependencies=[Depends(require_nav("transactions"))],
)

# Most-restrictive-wins ordering; mirrors the policy engine's.
_VERDICT_SEVERITY: dict[VerdictStatus, int] = {
    VerdictStatus.APPROVED: 0,
    VerdictStatus.REVIEW: 1,
    VerdictStatus.HUMAN_HOLD: 2,
    VerdictStatus.BLOCKED: 3,
}


def _resolve_agent(
    db: Session, company: Company, slug: str, *, for_update: bool = False
) -> Agent:
    """Look up an agent by slug; `for_update` locks it for atomic check-and-reserve."""
    stmt = select(Agent).where(Agent.company_id == company.id, Agent.slug == slug)
    if for_update:
        stmt = stmt.with_for_update()
    agent = db.execute(stmt).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown agent: {slug}")
    return agent


def _fetch_active_policies(db: Session, company: Company, agent: Agent) -> list[Policy]:
    """Active policies applicable to this agent: company-wide + agent-scoped (M5)."""
    stmt = select(Policy).where(
        Policy.company_id == company.id,
        Policy.active.is_(True),
        or_(
            Policy.scope_type == PolicyScopeType.COMPANY,
            and_(
                Policy.scope_type == PolicyScopeType.AGENT,
                Policy.scope_id == agent.id,
            ),
        ),
    )
    return list(db.execute(stmt).scalars().all())


def _verdict_from(authorization: Authorization) -> Verdict:
    """Reconstruct the wire verdict from a persisted hold (idempotent replay)."""
    # Sole verdict construction point, so the one place to count them.
    metrics.authorize_verdicts_total.labels(verdict=authorization.status.value).inc()
    return Verdict(
        authorization_id=authorization.id,
        status=authorization.status.value,
        code=authorization.verdict_code or "ok",
        reason=authorization.reason_text,
    )


def _resolve_tax_rate_row(
    db: Session, regime: str, when: datetime, tax_category: str | None
) -> TaxRate:
    """Effective-dated rate row for a regime/slab; fails closed (422) if missing.

    Keyed by transaction date so a rate change never back-applies.
    """
    try:
        return resolve_rate_row(
            db,
            regime=regime,
            category=tax_category or STANDARD_CATEGORY,
            on_date=when.date(),
        )
    except TaxRateUnavailableError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _compute_tax(
    db: Session,
    company: Company,
    entity: Entity,
    *,
    functional_amount: Decimal,
    paid_currency: str,
    vendor_name: str,
    category: str | None,
    tax_category: str | None,
    tax_inclusive: bool,
    supply_type: str,
    when: datetime,
) -> tuple[TaxTreatment, uuid.UUID | None, str | None]:
    """Resolve rate + vendor place-of-supply and run the pure tax engine.

    Returns ``(treatment, applied_rule_id, applied_rule_source)``; the rule fields
    are ``None`` when no rate was applied (e.g. unconfigured US state).
    """
    vendor = resolve_or_create_vendor(db, company.id, vendor_name)

    us_rule_type: str | None = None
    us_jurisdiction: str | None = None
    applied_rule_id: uuid.UUID | None = None
    applied_rule_source: str | None = None
    if entity.tax_regime == US_REGIME:
        rule = (
            resolve_us_rule(
                db,
                us_state=entity.us_state,
                on_date=when.date(),
                category=tax_category or STANDARD_CATEGORY,
            )
            if entity.us_state
            else None
        )
        if rule is not None:
            us_rule_type = rule.rule_type
            us_jurisdiction = rule.jurisdiction
            rate = rule.rate
            applied_rule_id = rule.rule_id
            applied_rule_source = rule.source
        else:
            # Engine routes this to the recon queue rather than a silent zero.
            rate = Decimal("0")
    else:
        row = _resolve_tax_rate_row(db, entity.tax_regime, when, tax_category)
        rate = Decimal(row.rate)
        applied_rule_id = row.id
        applied_rule_source = row.source

    tax = compute_tax(
        tax_regime=entity.tax_regime,
        functional_amount=functional_amount,
        paid_currency=paid_currency,
        entity_currency=entity.currency,
        rate=rate,
        expense_category=category,
        tax_inclusive=tax_inclusive,
        vendor_country=vendor.country,
        vendor_state=vendor.state,
        vendor_registered_in=tuple(vendor.gst_registered_in),
        entity_country=entity.country,
        entity_state=entity.gst_state,
        us_rule_type=us_rule_type,
        us_jurisdiction=us_jurisdiction,
        supply_type=supply_type,
    )
    return tax, applied_rule_id, applied_rule_source


def _withholding_outcome(
    db: Session,
    company: Company,
    entity: Entity,
    vendor_name: str,
    functional_amount: Decimal,
    on_date: date,
) -> WithholdingOutcome:
    """India s.195 withholding outcome for a payment.

    Read-only vendor lookup: never creates a vendor row at authorize.
    """
    normalized = normalize_vendor_name(vendor_name)
    vendor = db.execute(
        select(Vendor).where(
            Vendor.company_id == company.id, Vendor.normalized_name == normalized
        )
    ).scalar_one_or_none()
    return determine_withholding(
        buyer_country=entity.country,
        vendor_country=vendor.country if vendor else None,
        functional_amount=functional_amount,
        on_date=on_date,
        trc_on_file=vendor.trc_on_file if vendor else False,
        trc_expiry=vendor.trc_expiry if vendor else None,
        form_10f_on_file=vendor.form_10f_on_file if vendor else False,
        treaty_rate=settings.withholding_treaty_rate,
        domestic_rate=settings.withholding_domestic_rate,
        cb_threshold=settings.withholding_15cb_threshold,
    )


def _persist_tax_records(
    db: Session,
    transaction_id: uuid.UUID,
    tax: TaxTreatment,
    applied_rule_id: uuid.UUID | None = None,
    applied_rule_source: str | None = None,
) -> None:
    """Persist one `TaxRecord` per tax-head component (e.g. CGST + SGST)."""
    for comp in tax.components:
        db.add(
            TaxRecord(
                transaction_id=transaction_id,
                tax_type=tax.tax_type,
                tax_head=comp.head,
                jurisdiction=tax.jurisdiction,
                rate=comp.rate,
                tax_amount=comp.tax_amount,
                is_reclaimable=tax.is_reclaimable,
                reclaim_amount=comp.reclaim_amount,
                currency=tax.currency,
                legal_reference=tax.legal_reference,
                self_invoice_generated=tax.self_invoice_required,
                applied_rule_id=applied_rule_id,
                applied_rule_source=applied_rule_source,
            )
        )


def _flag_unclassified_tax(
    db: Session, company: Company, transaction: Transaction, tax: TaxTreatment
) -> None:
    """Open a recon exception when tax could not be classified.

    ``amount_at_stake`` is 0: nothing is recoverable, so it must not inflate the inbox.
    """
    if tax.tax_type == "UNCLASSIFIED":
        reason = "place_of_supply_unknown"
        detail = (
            "vendor has no country/registration on file; tax booked conservatively "
            "(no ITC, no self-invoice) pending enrichment"
        )
        fix_notes = (
            f"Enrich vendor {transaction.vendor!r} (country + GST registration) "
            "via PATCH /vendors/{id}, then reconcile this spend's tax."
        )
    elif tax.tax_type == "CUSTOMS":
        reason = "goods_customs_review"
        detail = tax.legal_reference
        fix_notes = (
            f"Imported goods — handle customs duty / border tax manually: "
            f"{tax.legal_reference}"
        )
    else:
        reason = "us_tax_review"
        detail = tax.legal_reference
        fix_notes = (
            f"Confirm the US sales/use-tax treatment for this spend: "
            f"{tax.legal_reference}"
        )
    db.add(
        ReconciliationException(
            company_id=company.id,
            transaction_id=transaction.id,
            exception_type=ReconExceptionType.TAX_UNCLASSIFIED,
            amount_at_stake=Decimal("0"),
            currency=transaction.paid_currency,
            evidence_json={
                "reason": reason,
                "vendor": transaction.vendor,
                "detail": detail,
            },
            fix_notes=fix_notes,
        )
    )


def _capture_response(
    transaction: Transaction, tax: TaxTreatment, journal_lines: list[JournalEntry]
) -> TransactionSchema:
    """Assemble the capture wire response from a transaction + its tax/journal."""
    return TransactionSchema(
        id=transaction.id,
        authorization_id=transaction.authorization_id,
        status=transaction.status.value,
        vendor=transaction.vendor,
        amount=transaction.paid_amount,
        currency=transaction.paid_currency,
        estimated=transaction.estimated,
        invoice_url=transaction.invoice_url,
        journal=[
            JournalLine(
                account_name=line.account_name,
                account_code=line.account_code,
                debit=line.debit,
                credit=line.credit,
                ledger=line.ledger,
                currency=line.currency,
            )
            for line in journal_lines
        ],
        tax=TaxInfo(
            tax_type=tax.tax_type,
            jurisdiction=tax.jurisdiction,
            rate=tax.rate,
            tax_amount=tax.tax_amount,
            is_reclaimable=tax.is_reclaimable,
            reclaim_amount=tax.reclaim_amount,
            currency=tax.currency,
            legal_reference=tax.legal_reference,
            reverse_charge=tax.reverse_charge,
            self_invoice_generated=tax.self_invoice_required,
        ),
    )


def _replay_capture(
    db: Session, company: Company, authorization_id: uuid.UUID
) -> TransactionSchema:
    """Idempotent capture retry: return the first capture, never double-book.

    Tax and journal are pure functions of the row, so they are recomputed.
    """
    existing = db.execute(
        select(Transaction).where(
            Transaction.authorization_id == authorization_id,
            Transaction.company_id == company.id,
        )
    ).scalar_one_or_none()
    if existing is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Capture conflict")
    entity = db.get(Entity, existing.entity_id)
    if entity is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "transaction references a missing entity",
        )
    tax, _, _ = _compute_tax(
        db,
        company,
        entity,
        functional_amount=existing.local_amount,
        paid_currency=existing.paid_currency,
        vendor_name=existing.vendor,
        category=existing.category,
        tax_category=existing.tax_category,
        tax_inclusive=existing.tax_inclusive,
        supply_type=existing.supply_type,
        when=existing.transaction_date,
    )
    accounts = ensure_default_accounts(db, company.id)
    journal_lines = generate_journal(existing, entity, tax, accounts)
    return _capture_response(existing, tax, journal_lines)


def _controls_block(agent: Agent, entity: Entity) -> tuple[str, str] | None:
    """`(code, reason)` if agent/entity state forbids any spend, else None."""
    if agent.status != AgentStatus.ACTIVE:
        return "agent_inactive", f"agent is {agent.status.value}, not active"
    if not agent.is_live:
        return "agent_inactive", "agent is not live"
    if not entity.is_active:
        return "entity_inactive", f"entity {entity.code!r} is inactive"
    return None


def _persist_authorization(
    db: Session,
    company: Company,
    agent: Agent,
    entity: Entity,
    request: SpendRequest,
    *,
    verdict_status: VerdictStatus,
    code: str,
    reason: str | None,
    policy_id: uuid.UUID | None,
    idempotency_key: str | None,
    exchange_rate: Decimal | None = None,
    local_amount: Decimal | None = None,
    local_currency: str | None = None,
) -> Verdict:
    """Persist the hold and return its verdict, idempotency-race-safe.

    FX fields are None for a controls-blocked hold, which never reserves budget.
    """
    authorization = Authorization(
        expires_at=datetime.now(UTC)
        + timedelta(hours=settings.authorization_ttl_hours),
        company_id=company.id,
        agent_id=agent.id,
        entity_id=entity.id,
        vendor=request.vendor,
        amount=request.amount,
        currency=request.currency,
        exchange_rate=exchange_rate,
        local_amount=local_amount,
        local_currency=local_currency,
        reason=request.reason,
        intent=request.intent,
        project=request.project,
        category=request.category,
        tax_category=request.tax_category,
        tax_inclusive=request.tax_inclusive,
        supply_type=request.supply_type,
        model_used=request.model,
        tokens_consumed=request.tokens,
        status=verdict_status,
        policy_id=policy_id,
        reason_text=reason,
        verdict_code=code,
        idempotency_key=idempotency_key,
    )
    db.add(authorization)
    try:
        db.commit()
    except IntegrityError:
        # Lost the idempotency-key race: return the winning hold.
        db.rollback()
        if idempotency_key is None:
            raise
        winner = db.execute(
            select(Authorization).where(
                Authorization.company_id == company.id,
                Authorization.idempotency_key == idempotency_key,
            )
        ).scalar_one()
        return _verdict_from(winner)
    db.refresh(authorization)
    return _verdict_from(authorization)


@router.post("/authorize", response_model=Verdict)
def authorize(
    request: SpendRequest,
    company: Company = Depends(require_live),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Verdict:
    """Evaluate the policy engine on a spend before any money moves (M5)."""
    # Replay: a second hold for a retried spend() could be captured twice.
    if idempotency_key is not None:
        prior = db.execute(
            select(Authorization).where(
                Authorization.company_id == company.id,
                Authorization.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if prior is not None:
            return _verdict_from(prior)

    # Row lock serializes concurrent authorizes so each sees prior holds.
    agent = _resolve_agent(db, company, request.agent, for_update=True)
    entity = entity_or_404(db, company, request.entity)

    controls = _controls_block(agent, entity)
    if controls is not None:
        code, reason = controls
        return _persist_authorization(
            db,
            company,
            agent,
            entity,
            request,
            verdict_status=VerdictStatus.BLOCKED,
            code=code,
            reason=reason,
            policy_id=None,
            idempotency_key=idempotency_key,
        )

    policies = _fetch_active_policies(db, company, agent)
    now = now_in_entity_tz(entity)
    month_start, day_start = period_starts(now)

    # Budget compares in functional currency; fail closed on unknown currency.
    currency = request.currency or entity.currency
    try:
        exchange_rate, functional_amount = convert(
            db,
            amount=request.amount,
            paid_currency=currency,
            functional_currency=entity.currency,
            on_date=now.date(),
        )
    except FxRateUnavailableError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # Committed (ledger) + reserved (open holds); spent_* columns are display-only.
    reserved = reserved_spend(db, agent.id)
    spent_month = committed_spend(db, agent.id, month_start) + reserved
    spent_today = committed_spend(db, agent.id, day_start) + reserved

    def recent_count(window: timedelta) -> int:
        return recent_activity_count(db, agent.id, now - window)

    ctx = SpendContext(
        amount=functional_amount,
        currency=currency,
        vendor=request.vendor,
        now=now,
        agent_monthly_budget=agent.monthly_budget,
        agent_spent=spent_month,
        agent_spent_today=spent_today,
        recent_count=recent_count,
    )
    decision = evaluate_spend(policies, ctx)

    for policy in policies:
        if policy.id in decision.fired_policy_ids:
            policy.hit_count += 1
        if policy.id in decision.blocking_policy_ids:
            policy.block_count += 1

    policy_id = next(
        iter(decision.blocking_policy_ids or decision.fired_policy_ids), None
    )

    # s.195 gate: banks refuse a non-resident remittance lacking TRC/Form 10F
    # docs above the 15CB threshold, so hold it before money moves.
    final_status: VerdictStatus = decision.status
    final_code: str = decision.code.value
    final_reason: str | None = decision.reason
    if entity.country == "IN":
        wh = _withholding_outcome(
            db, company, entity, request.vendor, functional_amount, now.date()
        )
        if (
            wh.applies
            and wh.blocked
            and _VERDICT_SEVERITY[VerdictStatus.HUMAN_HOLD]
            > _VERDICT_SEVERITY[final_status]
        ):
            final_status = VerdictStatus.HUMAN_HOLD
            final_code = "withholding_docs_required"
            final_reason = wh.reason

    return _persist_authorization(
        db,
        company,
        agent,
        entity,
        request,
        verdict_status=final_status,
        code=final_code,
        reason=final_reason,
        policy_id=policy_id,
        idempotency_key=idempotency_key,
        exchange_rate=exchange_rate,
        local_amount=functional_amount,
        local_currency=entity.currency,
    )


@router.post("/capture", response_model=TransactionSchema)
def capture(
    request: CaptureRequest,
    company: Company = Depends(require_live),
    db: Session = Depends(get_db),
) -> TransactionSchema:
    """Commit an approved authorization to the ledger."""
    if request.source != "sdk":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"source={request.source!r} capture lands in Phase 6.5 (card/wallet rail)",
        )

    authorization = db.execute(
        select(Authorization).where(
            Authorization.id == request.authorization_id,
            Authorization.company_id == company.id,
        )
    ).scalar_one_or_none()
    if authorization is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown authorization")
    if authorization.status != VerdictStatus.APPROVED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Cannot capture a {authorization.status.value} authorization",
        )
    # `expires_at` is deliberately not checked: a late real spend must still book.
    if authorization.released_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Authorization was voided at {authorization.released_at.isoformat()} "
            f"({authorization.release_reason or 'no reason given'}) and cannot be "
            f"captured; re-authorize the spend instead.",
        )

    # Lock the agent row before the insert: the FK's KEY SHARE upgraded by the
    # counter UPDATE deadlocks against a concurrent authorize() FOR UPDATE.
    agent = db.get(Agent, authorization.agent_id, with_for_update=True)
    if agent is None:  # FK-guaranteed; explicit guard, not an assert
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "authorization references a missing agent",
        )
    entity = db.get(Entity, authorization.entity_id)
    if entity is None:  # FK-guaranteed; explicit guard, not an assert
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "authorization references a missing entity",
        )
    currency = authorization.currency or entity.currency

    # Settlement may correct the estimate, capped at auth x settlement_buffer:
    # the verdict and reservation covered only the authorized amount.
    paid_amount = request.amount if request.amount is not None else authorization.amount
    if request.amount is not None:
        max_settlement = authorization.amount * settings.settlement_buffer
        if request.amount > max_settlement:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"settlement {request.amount} exceeds authorized "
                f"{authorization.amount} by more than the allowed "
                f"{settings.settlement_buffer}x buffer; re-authorize the spend",
            )
    model_used = (
        request.model if request.model is not None else authorization.model_used
    )
    tokens_consumed = (
        request.tokens if request.tokens is not None else authorization.tokens_consumed
    )

    capture_date = datetime.now(UTC)
    # Fail closed on a missing FX rate; booking at 1.0 corrupts tax and budget.
    try:
        exchange_rate, local_amount = convert(
            db,
            amount=paid_amount,
            paid_currency=currency,
            functional_currency=entity.currency,
            on_date=capture_date.date(),
        )
    except FxRateUnavailableError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    transaction = Transaction(
        company_id=company.id,
        agent_id=authorization.agent_id,
        entity_id=authorization.entity_id,
        authorization_id=authorization.id,
        source=TransactionSource.SDK,
        vendor=authorization.vendor,
        item_description=authorization.reason,
        paid_amount=paid_amount,
        paid_currency=currency,
        exchange_rate=exchange_rate,
        local_amount=local_amount,
        local_currency=entity.currency,
        project=authorization.project,
        intent=authorization.intent,
        category=authorization.category,
        tax_category=authorization.tax_category,
        tax_inclusive=authorization.tax_inclusive,
        supply_type=authorization.supply_type,
        model_used=model_used,
        tokens_consumed=tokens_consumed,
        estimated=request.estimated,
        invoice_url=request.invoice_url,
        status=TransactionStatus.COMPLETED,
        approval_method="auto",
        reason_text=authorization.reason_text,
        transaction_date=capture_date,
    )
    db.add(transaction)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _replay_capture(db, company, request.authorization_id)

    # Atomic SQL increment: a Python read-modify-write loses concurrent updates.
    db.execute(
        update(Agent)
        .where(Agent.id == agent.id)
        .values(
            spent_ytd=Agent.spent_ytd + transaction.local_amount,
            spent_today=Agent.spent_today + transaction.local_amount,
        )
        .execution_options(synchronize_session=False)
    )

    # Tax + journal book in the capture's DB transaction, keyed off the row.
    tax, applied_rule_id, applied_rule_source = _compute_tax(
        db,
        company,
        entity,
        functional_amount=transaction.local_amount,
        paid_currency=transaction.paid_currency,
        vendor_name=transaction.vendor,
        category=transaction.category,
        tax_category=transaction.tax_category,
        tax_inclusive=transaction.tax_inclusive,
        supply_type=transaction.supply_type,
        when=capture_date,
    )
    _persist_tax_records(db, transaction.id, tax, applied_rule_id, applied_rule_source)
    # Reverse charge requires a buyer-issued self-invoice (r.46).
    if tax.self_invoice_required:
        db.add(build_self_invoice(transaction, entity, tax))
    if tax.confidence == "exception":
        _flag_unclassified_tax(db, company, transaction, tax)
    # Record only; any withholding block was already enforced at authorize.
    withholding = _withholding_outcome(
        db,
        company,
        entity,
        transaction.vendor,
        transaction.local_amount,
        capture_date.date(),
    )
    if withholding.applies:
        db.add(
            WithholdingRecord(
                transaction_id=transaction.id,
                rate_bps=int(withholding.rate * 10000),
                amount=withholding.amount,
                currency=entity.currency,
                treaty_rate_applied=withholding.treaty_rate_applied,
                legal_reference=withholding.reason,
            )
        )
    accounts = ensure_default_accounts(db, company.id)
    journal_lines = generate_journal(transaction, entity, tax, accounts)
    db.add_all(journal_lines)

    # Build before commit; expire_on_commit would reload every attribute.
    result = _capture_response(transaction, tax, journal_lines)
    db.commit()
    return result


@router.post("/void", response_model=VoidResult)
def void(
    request: VoidRequest,
    company: Company = Depends(require_live),
    db: Session = Depends(get_db),
) -> VoidResult:
    """Release an approved hold whose spend never happened.

    Idempotent. A captured hold cannot be voided; reverse the transaction instead.
    """
    authorization = db.execute(
        select(Authorization).where(
            Authorization.id == request.authorization_id,
            Authorization.company_id == company.id,
        )
    ).scalar_one_or_none()
    if authorization is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown authorization")

    if authorization.released_at is not None:
        return VoidResult(
            authorization_id=authorization.id,
            released_at=authorization.released_at,
            release_reason=authorization.release_reason,
            already_released=True,
        )

    captured = db.execute(
        select(Transaction.id).where(
            Transaction.authorization_id == authorization.id,
            Transaction.company_id == company.id,
        )
    ).scalar_one_or_none()
    if captured is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Authorization is already captured and cannot be voided; reverse the "
            f"transaction instead (transaction_id={captured}).",
        )

    authorization.released_at = datetime.now(UTC)
    authorization.release_reason = request.reason
    db.commit()
    db.refresh(authorization)
    # Narrows the nullable column for the type checker.
    if authorization.released_at is None:  # pragma: no cover - defensive
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "void did not persist"
        )
    return VoidResult(
        authorization_id=authorization.id,
        released_at=authorization.released_at,
        release_reason=authorization.release_reason,
    )


@router.post("/{transaction_id}/reverse", response_model=TransactionSchema)
def reverse_transaction(
    transaction_id: uuid.UUID,
    company: Company = Depends(require_admin),
    db: Session = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
) -> TransactionSchema:
    """Reverse a committed transaction with a contra entry, freeing its budget.

    Admin-only and audited. Reversing a reversal or a reversed row is a 409.
    """
    original = db.execute(
        select(Transaction).where(
            Transaction.id == transaction_id,
            Transaction.company_id == company.id,
        )
    ).scalar_one_or_none()
    if original is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown transaction")
    if original.reverses_transaction_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot reverse a reversal")
    if original.is_reversed:
        raise HTTPException(status.HTTP_409_CONFLICT, "Transaction already reversed")

    entity = db.get(Entity, original.entity_id)
    if entity is None:  # FK-guaranteed; explicit guard, not an assert
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "transaction references a missing entity",
        )

    original_lines = list(
        db.execute(
            select(JournalEntry).where(JournalEntry.transaction_id == original.id)
        ).scalars()
    )

    # Same lock ordering as capture(): agent row before the insert (deadlock).
    db.execute(select(Agent.id).where(Agent.id == original.agent_id).with_for_update())

    now = datetime.now(UTC)
    reversal = Transaction(
        company_id=company.id,
        agent_id=original.agent_id,
        entity_id=original.entity_id,
        reverses_transaction_id=original.id,
        source=original.source,
        vendor=original.vendor,
        item_description=f"Reversal of {original.id}",
        paid_amount=original.paid_amount,
        paid_currency=original.paid_currency,
        exchange_rate=original.exchange_rate,
        local_amount=original.local_amount,
        local_currency=original.local_currency,
        project=original.project,
        intent=original.intent,
        category=original.category,
        tax_category=original.tax_category,
        tax_inclusive=original.tax_inclusive,
        supply_type=original.supply_type,
        model_used=original.model_used,
        tokens_consumed=original.tokens_consumed,
        status=TransactionStatus.COMPLETED,
        approval_method="reversal",
        reason_text=f"reversal of transaction {original.id}",
        transaction_date=now,
    )
    db.add(reversal)
    db.flush()

    # Contra journal: swap debit/credit on each original line.
    contra_lines = [
        JournalEntry(
            transaction_id=reversal.id,
            company_id=reversal.company_id,
            entity_id=reversal.entity_id,
            posting_date=now.date(),
            account_name=line.account_name,
            account_code=line.account_code,
            debit=line.credit,
            credit=line.debit,
            ledger=line.ledger,
            currency=line.currency,
        )
        for line in original_lines
    ]
    db.add_all(contra_lines)

    # Use the original supply date so the contra negates the tax actually booked.
    tax, applied_rule_id, applied_rule_source = _compute_tax(
        db,
        company,
        entity,
        functional_amount=reversal.local_amount,
        paid_currency=reversal.paid_currency,
        vendor_name=reversal.vendor,
        category=reversal.category,
        tax_category=reversal.tax_category,
        tax_inclusive=reversal.tax_inclusive,
        supply_type=reversal.supply_type,
        when=original.transaction_date,
    )
    contra_components = tuple(
        replace(c, tax_amount=-c.tax_amount, reclaim_amount=-c.reclaim_amount)
        for c in tax.components
    )
    contra_tax = replace(
        tax,
        tax_amount=-tax.tax_amount,
        reclaim_amount=-tax.reclaim_amount,
        self_invoice_required=False,
        legal_reference=f"Reversal — {tax.legal_reference}",
        components=contra_components,
    )
    _persist_tax_records(
        db, reversal.id, contra_tax, applied_rule_id, applied_rule_source
    )

    original.is_reversed = True

    db.execute(
        update(Agent)
        .where(Agent.id == original.agent_id)
        .values(
            spent_ytd=Agent.spent_ytd - reversal.local_amount,
            spent_today=Agent.spent_today - reversal.local_amount,
        )
        .execution_options(synchronize_session=False)
    )

    record_audit(
        db,
        ctx,
        action="transaction.reverse",
        entity_type="transaction",
        entity_id=reversal.id,
        before={"transaction_id": str(original.id), "is_reversed": False},
        after={"reversal_id": str(reversal.id), "is_reversed": True},
    )

    result = _capture_response(reversal, contra_tax, contra_lines)
    db.commit()
    return result


@router.get("", response_model=TransactionListResponse)
def list_transactions(
    ctx: AuthContext = Depends(require_nav("transactions")),
    db: Session = Depends(get_db),
    agent: str | None = Query(default=None, description="Filter by agent slug"),
    vendor: str | None = Query(default=None, description="Filter by vendor"),
    entity: str | None = Query(default=None, description="Filter by entity code"),
    estimated: bool | None = Query(
        default=None,
        description="Filter to estimate-booked (true) or settled (false) rows",
    ),
    reversed_only: bool | None = Query(
        default=None,
        alias="reversed",
        description="Filter to reversed (true) or live (false) rows",
    ),
    page: Page = Depends(paginate()),
) -> TransactionListResponse:
    """List committed transactions for the current company, filterable and paginated."""
    company = ctx.company
    stmt = select(Transaction).where(Transaction.company_id == company.id)
    if ctx.perms is not None and ctx.perms.agent_scope == "owned":
        stmt = scope_agents(
            stmt.join(Agent, Agent.id == Transaction.agent_id),
            ctx.perms,
            ctx.user.id if ctx.user else None,
        )
    if agent is not None:
        agent_row = _resolve_agent(db, company, agent)
        stmt = stmt.where(Transaction.agent_id == agent_row.id)
    if vendor is not None:
        stmt = stmt.where(Transaction.vendor == vendor)
    if entity is not None:
        entity_row = entity_or_404(db, company, entity)
        stmt = stmt.where(Transaction.entity_id == entity_row.id)
    if estimated is not None:
        stmt = stmt.where(Transaction.estimated == estimated)
    if reversed_only is not None:
        stmt = stmt.where(Transaction.is_reversed == reversed_only)

    total = count_total(db, stmt)
    rows = (
        db.execute(
            stmt.order_by(Transaction.created_at.desc())
            .offset(page.offset)
            .limit(page.limit)
        )
        .scalars()
        .all()
    )
    return TransactionListResponse(
        items=[TransactionDetail.model_validate(row) for row in rows], total=total
    )


@router.get("/{transaction_id}", response_model=TransactionFull)
def get_transaction(
    transaction_id: uuid.UUID,
    ctx: AuthContext = Depends(require_nav("transactions")),
    db: Session = Depends(get_db),
) -> TransactionFull:
    """Full detail for one transaction: the row, its tax treatment, its journal."""
    stmt = select(Transaction).where(
        Transaction.id == transaction_id, Transaction.company_id == ctx.company.id
    )
    if ctx.perms is not None and ctx.perms.agent_scope == "owned":
        stmt = scope_agents(
            stmt.join(Agent, Agent.id == Transaction.agent_id),
            ctx.perms,
            ctx.user.id if ctx.user else None,
        )
    transaction = db.execute(stmt).scalar_one_or_none()
    if transaction is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown transaction")

    lines = (
        db.execute(
            select(JournalEntry)
            .where(JournalEntry.transaction_id == transaction.id)
            # Debits before credits, then by account code.
            .order_by(JournalEntry.credit, JournalEntry.account_code)
        )
        .scalars()
        .all()
    )
    tax_record = (
        db.execute(select(TaxRecord).where(TaxRecord.transaction_id == transaction.id))
        .scalars()
        .first()
    )

    return TransactionFull(
        **TransactionDetail.model_validate(transaction).model_dump(),
        tax=(
            TaxInfo(
                tax_type=tax_record.tax_type,
                jurisdiction=tax_record.jurisdiction,
                rate=tax_record.rate,
                tax_amount=tax_record.tax_amount,
                is_reclaimable=tax_record.is_reclaimable,
                reclaim_amount=tax_record.reclaim_amount,
                currency=tax_record.currency,
                legal_reference=tax_record.legal_reference,
                reverse_charge=tax_record.tax_type.upper().startswith("RCM"),
                self_invoice_generated=tax_record.self_invoice_generated,
            )
            if tax_record is not None
            else None
        ),
        journal=[
            JournalLine(
                account_name=line.account_name,
                account_code=line.account_code,
                debit=line.debit,
                credit=line.credit,
                ledger=line.ledger,
                currency=line.currency,
            )
            for line in lines
        ],
    )
