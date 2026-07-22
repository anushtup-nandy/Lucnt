"""Jurisdiction-aware tax engine (backend/PLAN.md §5, Module 2).

Given an entity's tax regime and the transaction amount **in the entity's
functional currency**, compute the tax treatment. Pure and side-effect-free: it
returns a `TaxTreatment` value; persistence and journalling happen elsewhere.

MVP scope — India GST, Singapore GST, US sales tax:

- **Imported services** (paid in a foreign currency, i.e. a foreign vendor that
  bills no local tax) under IN/SG GST trigger the **reverse charge (RCM)**: the
  buyer self-assesses tax *on top of* the tax-exclusive amount, claims it back as
  input credit, and must issue a self-invoice.
- **Domestic services** carry GST *embedded* in the (tax-inclusive) amount; the
  engine extracts it and records the reclaimable input credit.
- **US sales tax**: no federal VAT and state sales/use-tax nexus is not evaluated
  in the MVP, so no tax is self-assessed (rate 0).

**Currency (PRODUCTION_PLAN T1-4).** Tax is computed on ``functional_amount`` —
the paid amount already converted to the entity's functional currency by
`services/fx.py` at capture — never on the raw foreign amount. Computing an INR
entity's RCM on a USD figure understated GST by the whole FX factor. Currency
plays **no** role in deciding import vs domestic (TE-1): that is the vendor
registry's place of supply. The *amount* the rate applies to is always functional.

**Place of supply (TE-1).** Import-vs-domestic and the reverse-charge decision key
off the vendor's ``country`` and ``vendor_registered_in`` (the countries it is
GST-registered in) — never its invoice currency, which was wrong in both
directions. The legal reverse-charge test is registration *in the buyer's
country*. When the vendor is unenriched (no ``country``), the engine does not
guess: it returns a conservative ``"UNCLASSIFIED"`` treatment with
``confidence="exception"`` (no ITC, no self-invoice, whole amount expensed) and
the money path opens a recon exception (TE-1b).

**Effective-dated rate (T1-12).** The statutory ``rate`` is *injected* by the
caller, resolved from the effective-dated `tax_rates` table by
`services/tax_rates.py` at the money-path boundary (the capture/authorize
router), keyed by the transaction date — so a rate change (SG GST 7%→8%→9%)
never back-applies to a historical supply. This keeps the engine pure and
SQLAlchemy-free; it applies whatever rate it is given.

All money math is `Decimal`; tax is carried at the ledger's 4dp scale,
ROUND_HALF_UP (T1-13).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Tax is carried at the ledger's 4dp money scale (T1-13); rounding to 2dp happens
# only at return/invoice aggregation, so token-scale micro-amounts don't vanish
# to zero (100k charges of ₹0.03 tax are ~₹457, not ₹0).
_FOUR_PLACES = Decimal("0.0001")
_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")

# The regimes the engine understands. Anything else is a misconfigured entity and
# must fail loud (T1-8), never silently book zero tax for months.
KNOWN_TAX_REGIMES: frozenset[str] = frozenset({"IN-GST", "SG-GST", "US-Sales-Tax"})

# ITC is blocked for these expense categories under CGST s.17(5) (T1-14): the GST
# becomes a cost, not a reclaimable credit. Keyed by regime (India only for MVP).
_BLOCKED_ITC_CATEGORIES: dict[str, frozenset[str]] = {
    "IN-GST": frozenset({"travel"}),
}
_BLOCKED_ITC_NOTE = " — input tax credit blocked under CGST s.17(5)"


class UnknownTaxRegimeError(Exception):
    """The entity's `tax_regime` is not one the engine understands (T1-8)."""

    def __init__(self, regime: str) -> None:
        """Record the offending regime string for the caller / logs."""
        self.regime = regime
        super().__init__(
            f"unknown tax regime {regime!r}; expected one of "
            f"{sorted(KNOWN_TAX_REGIMES)}"
        )


@dataclass(frozen=True)
class TaxComponent:
    """One GST head of a treatment — persisted as its own `TaxRecord` (T1-9).

    India intra-state splits one supply into CGST + SGST (two components);
    inter-state/import is a single IGST component; SG/US/the fallback path is a
    single unsplit ``GST``/``NONE`` component. ``Σ component.tax_amount`` equals
    the treatment's summary ``tax_amount`` (the split is exact), so the journal and
    GSTR-3B agree.
    """

    head: str  # CGST | SGST | IGST | GST | NONE
    rate: Decimal
    tax_amount: Decimal
    reclaim_amount: Decimal


@dataclass(frozen=True)
class TaxTreatment:
    """The tax verdict for one transaction — persisted as one or more `TaxRecord`s.

    The summary fields (``tax_type``/``rate``/``tax_amount``/``reclaim_amount`` …)
    are the aggregate over ``components`` and are unchanged from the pre-split
    contract; ``components`` carries the per-head breakdown (T1-9).
    """

    tax_type: str  # GST_RCM | GST | SALES_TAX | NONE
    jurisdiction: str
    rate: Decimal
    tax_amount: Decimal
    is_reclaimable: bool
    reclaim_amount: Decimal
    legal_reference: str
    reverse_charge: bool
    self_invoice_required: bool
    # The functional (entity) currency ``tax_amount``/``reclaim_amount`` are
    # denominated in — tax is always assessed in functional currency (T1-4).
    currency: str
    # The cash that leaves the wallet in functional currency (T1-10). Equal to the
    # paid amount for tax-inclusive and reverse-charge supplies; for a tax-exclusive
    # domestic supply it is ``functional + tax`` (GST charged on top by the vendor).
    gross_amount: Decimal
    # Per-head breakdown (T1-9); at least one component, summing to the totals.
    components: tuple[TaxComponent, ...]
    # ``"auto"`` when the treatment was determined with confidence; ``"exception"``
    # when place of supply could not be established (an unenriched vendor, TE-1b) —
    # the money path books it conservatively and opens a recon exception for
    # review, never a silent currency guess.
    confidence: str = "auto"


@dataclass(frozen=True)
class _RegimeRule:
    """Static metadata for one GST regime (jurisdiction + legal references).

    The *rate* is not here — it is effective-dated and injected per supply
    (T1-12); this holds only what is genuinely constant per regime.
    """

    jurisdiction: str
    legal_reference_imported: str
    legal_reference_domestic: str


_REGIMES: dict[str, _RegimeRule] = {
    "IN-GST": _RegimeRule(
        jurisdiction="India",
        legal_reference_imported=(
            "IGST Act 2017 s.5(3) — reverse charge on imported services; "
            "self-invoice per CGST Rules r.46"
        ),
        legal_reference_domestic=(
            "CGST/SGST Act 2017 — domestic supply; input tax credit per s.16"
        ),
    ),
    "SG-GST": _RegimeRule(
        jurisdiction="Singapore",
        legal_reference_imported=(
            "GST Act (Cap. 117A) s.14 — reverse charge on imported services"
        ),
        legal_reference_domestic=(
            "GST Act (Cap. 117A) — standard-rated domestic supply; input tax per s.19"
        ),
    ),
}


def _quantize(amount: Decimal) -> Decimal:
    """Round tax to the ledger's 4dp scale, half-up (T1-13)."""
    return amount.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)


def _is_itc_blocked(tax_regime: str, expense_category: str | None) -> bool:
    """Whether input tax credit is barred for this category/regime (T1-14)."""
    if expense_category is None:
        return False
    blocked = _BLOCKED_ITC_CATEGORIES.get(tax_regime, frozenset())
    return expense_category.strip().lower() in blocked


def _head_mode(
    *,
    tax_regime: str,
    imported: bool,
    place_of_supply_known: bool,
    vendor_state: str | None,
    entity_state: str | None,
) -> str:
    """The GST head split for a supply: ``"GST"`` | ``"IGST"`` | ``"CGST_SGST"`` (T1-9).

    Only India with a known place of supply splits. Everything else — SG, US, and
    the currency-fallback path — uses the unsplit ``"GST"`` head that books to the
    legacy 1400/2200 accounts (so an unenriched vendor is unchanged). Imports are
    always inter-state (IGST); a known-state intra-state domestic supply is
    CGST+SGST; a domestic supply whose state we can't pin stays unsplit ``"GST"``.
    """
    if not place_of_supply_known or tax_regime != "IN-GST":
        return "GST"
    if imported:
        return "IGST"
    if vendor_state is not None and entity_state is not None:
        same_state = vendor_state.strip().upper() == entity_state.strip().upper()
        return "CGST_SGST" if same_state else "IGST"
    return "GST"


def _components(
    head_mode: str, rate: Decimal, tax_amount: Decimal, reclaim_amount: Decimal
) -> tuple[TaxComponent, ...]:
    """Split a treatment's totals into per-head components (T1-9).

    CGST+SGST split the rate and amounts in half; the SGST leg takes the rounding
    remainder so ``Σ component == total`` exactly. IGST/GST is a single component.
    """
    if head_mode == "CGST_SGST":
        cgst_tax = _quantize(tax_amount / _TWO)
        cgst_reclaim = _quantize(reclaim_amount / _TWO)
        half_rate = rate / _TWO
        return (
            TaxComponent("CGST", half_rate, cgst_tax, cgst_reclaim),
            TaxComponent(
                "SGST", half_rate, tax_amount - cgst_tax, reclaim_amount - cgst_reclaim
            ),
        )
    head = "IGST" if head_mode == "IGST" else "GST"
    return (TaxComponent(head, rate, tax_amount, reclaim_amount),)


def compute_tax(
    *,
    tax_regime: str,
    functional_amount: Decimal,
    paid_currency: str,
    entity_currency: str,
    rate: Decimal,
    expense_category: str | None = None,
    tax_inclusive: bool = True,
    vendor_country: str | None = None,
    vendor_state: str | None = None,
    vendor_registered_in: tuple[str, ...] | None = None,
    entity_country: str | None = None,
    entity_state: str | None = None,
) -> TaxTreatment:
    """Compute the tax treatment for a captured spend.

    Args:
        tax_regime: The entity's regime (e.g. ``"IN-GST"``, ``"US-Sales-Tax"``).
        functional_amount: The transaction amount **in the entity's functional
            currency** (already FX-converted from the paid amount, T1-4).
        paid_currency: ISO code of what was actually paid — used to decide import
            vs domestic only when place of supply is unknown (the fallback).
        entity_currency: ISO code of the entity's functional currency.
        rate: The statutory rate as a fraction (0.18 = 18%), effective-dated and
            resolved by the caller from `services/tax_rates.py` (T1-12). Ignored
            for the US regime (no self-assessed tax in the MVP).
        expense_category: Optional caller-stated category; drives blocked input
            tax credit (T1-14) — e.g. IN travel spend cannot reclaim ITC.
        tax_inclusive: Whether a *domestic* amount includes GST (T1-10). ``True``
            (default) extracts embedded GST; ``False`` treats the amount as
            tax-exclusive and adds GST on top (``gross = amount + tax``). Ignored
            for reverse charge (always assessed on the tax-exclusive amount).
        vendor_country: The vendor's ISO country from the registry (T1-3). When
            given (with ``entity_country``), import-vs-domestic and the CGST/SGST/
            IGST head split are decided by *place of supply*, not currency. When
            ``None`` (an unenriched vendor) the engine falls back to the currency
            heuristic and a single unsplit ``GST`` head — today's behaviour.
        vendor_state: The vendor's state (India), for intra- vs inter-state.
        vendor_registered_in: The ISO countries the vendor is GST-registered in
            (TE-1). The reverse-charge test is registration *in the buyer's
            country*: a foreign vendor registered there (OIDAR) charges forward
            IGST (not RCM); a domestic India vendor absent from this list is
            reverse charge under CGST s.9(4). ``None``/empty ⇒ registered nowhere.
        entity_country: The recipient entity's ISO country.
        entity_state: The recipient entity's state (India).

    Returns:
        A `TaxTreatment` whose ``components`` carry the per-head split (T1-9);
        ``tax_amount`` is 0 for US. ``currency`` is always ``entity_currency``.

    Raises:
        UnknownTaxRegimeError: ``tax_regime`` is not in `KNOWN_TAX_REGIMES` (T1-8).
    """
    if tax_regime not in KNOWN_TAX_REGIMES:
        # Fail loud on a misconfigured regime rather than silently booking zero
        # tax for months (T1-8). Validating the regime at entity creation is the
        # proper guard (deferred with the entities CRUD endpoint); this is the
        # last line of defence at capture.
        raise UnknownTaxRegimeError(tax_regime)

    rule = _REGIMES.get(tax_regime)

    if rule is None:
        # US sales tax: no federal VAT, and state sales/use-tax nexus is not
        # evaluated in the MVP, so no tax is self-assessed (T1-15 — a labeled
        # gap surfaced per-transaction, not a silent zero).
        return TaxTreatment(
            tax_type="SALES_TAX",
            jurisdiction="United States",
            rate=_ZERO,
            tax_amount=_ZERO,
            is_reclaimable=False,
            reclaim_amount=_ZERO,
            legal_reference=(
                "US: no federal VAT; state sales/use-tax nexus not evaluated "
                "(MVP gap — PRODUCTION_PLAN T1-15)"
            ),
            reverse_charge=False,
            self_invoice_required=False,
            currency=entity_currency,
            gross_amount=functional_amount,
            components=(TaxComponent("NONE", _ZERO, _ZERO, _ZERO),),
        )

    # Place of supply must come from the registry — never currency (TE-1). Without
    # the vendor's country we cannot tell import from domestic; rather than guess
    # (wrong in both directions), book a conservative no-phantom treatment and flag
    # for review (TE-1b): claim no ITC, assess no reverse charge, issue no
    # self-invoice, expense the whole amount. The money path opens a recon
    # exception on ``confidence == "exception"`` so finance enriches the vendor.
    if vendor_country is None or entity_country is None:
        return TaxTreatment(
            tax_type="UNCLASSIFIED",
            jurisdiction=rule.jurisdiction,
            rate=_ZERO,
            tax_amount=_ZERO,
            is_reclaimable=False,
            reclaim_amount=_ZERO,
            legal_reference=(
                f"{rule.jurisdiction}: place of supply unknown — vendor unenriched; "
                "tax deferred pending classification (PRODUCTION_PLAN TE-1)"
            ),
            reverse_charge=False,
            self_invoice_required=False,
            currency=entity_currency,
            gross_amount=functional_amount,
            components=(TaxComponent("NONE", _ZERO, _ZERO, _ZERO),),
            confidence="exception",
        )

    # Import-vs-domestic and who-bears-the-tax are two independent axes, both from
    # the registry — never currency (TE-1):
    #   * ``imported``: is the supplier offshore relative to the buyer — drives the
    #     IGST vs CGST/SGST head split and the import legal reference.
    #   * ``registered_in_buyer_country``: is the vendor GST-registered *in the
    #     buyer's country* — the legal reverse-charge test.
    buyer = entity_country.strip().upper()
    imported = vendor_country.strip().upper() != buyer
    registered = {c.strip().upper() for c in (vendor_registered_in or ())}
    registered_in_buyer_country = buyer in registered

    blocked = _is_itc_blocked(tax_regime, expense_category)
    head_mode = _head_mode(
        tax_regime=tax_regime,
        imported=imported,
        place_of_supply_known=True,
        vendor_state=vendor_state,
        entity_state=entity_state,
    )

    # Reverse charge (the buyer self-assesses) when the vendor does *not* charge
    # forward GST in the buyer's country — but only for a cross-border supply or an
    # India domestic-unregistered supply (CGST s.9(4)). A registered foreign vendor
    # (OIDAR) charges forward IGST → not RCM. A non-India domestic supply (e.g.
    # Singapore) stays forward charge regardless of registration. Both RCM legs
    # self-assess on the tax-exclusive amount; a forward-charge supply has GST
    # embedded in (or added on top of) the amount and is extracted below.
    reverse_charge = (not registered_in_buyer_country) and (
        imported or tax_regime == "IN-GST"
    )

    if reverse_charge:
        # Self-assessed on the tax-exclusive amount; the wallet only pays the vendor
        # (the GST is a payable to the government, not cash to the vendor).
        tax = _quantize(functional_amount * rate)
        gross = functional_amount
        legal_reference = (
            rule.legal_reference_imported
            if imported
            else f"{rule.jurisdiction} reverse charge — unregistered supplier, "
            "CGST s.9(4)"
        )
        tax_type = "GST_RCM"
        self_invoice_required = True
    elif tax_inclusive:
        # Domestic tax-inclusive: GST is embedded in the paid amount, extracted
        # (T1-10). The wallet paid the gross-inclusive amount.
        tax = _quantize(functional_amount * rate / (_ONE + rate))
        gross = functional_amount
        legal_reference = rule.legal_reference_domestic
        tax_type = "GST"
        self_invoice_required = False
    else:
        # Domestic tax-exclusive: the vendor charged GST on top of the stated
        # amount (T1-10), so the buyer paid ``amount + tax`` — no phantom ITC
        # extracted from a net figure (the exact T1-10 bug).
        tax = _quantize(functional_amount * rate)
        gross = functional_amount + tax
        legal_reference = (
            rule.legal_reference_domestic + " — amount tax-exclusive, GST added on top"
        )
        tax_type = "GST"
        self_invoice_required = False

    # When ITC is blocked (T1-14) the output GST is still owed but not reclaimable.
    reclaim = _ZERO if blocked else tax
    if blocked:
        legal_reference += _BLOCKED_ITC_NOTE

    return TaxTreatment(
        tax_type=tax_type,
        jurisdiction=rule.jurisdiction,
        rate=rate,
        tax_amount=tax,
        is_reclaimable=not blocked,
        reclaim_amount=reclaim,
        legal_reference=legal_reference,
        reverse_charge=reverse_charge,
        self_invoice_required=self_invoice_required,
        currency=entity_currency,
        gross_amount=gross,
        components=_components(head_mode, rate, tax, reclaim),
    )
