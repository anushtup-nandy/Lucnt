"""Request-input bounds (T3-8).

The failure these prevent is specific: a schema looser than the column it feeds
lets oversized input pass validation and blow up in Postgres as a 500 `DataError`.
The caller gets an opaque server error for what is really a bad request, and the
traceback lands in the logs as if the service were broken.

So each test asserts the *rejection*, and the money-path ones assert the bound
equals the column width — a bound that drifted wider than its column would pass a
naive "is it bounded?" test while still 500ing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from lucnt_api.constants import (
    AGENT_SLUG_MAX,
    ENTITY_CODE_MAX,
    JOURNAL_LINES_MAX,
    MERCHANT_LIST_MAX,
    NOTES_MAX,
    POLICY_CONFIG_MAX_KEYS,
    POLICY_CONFIG_VALUE_MAX,
    REASON_MAX,
    VENDOR_MAX,
)
from lucnt_api.models.policy import PolicyRuleType
from lucnt_api.schemas.books import ManualJournalLineInput, ManualJournalRequest
from lucnt_api.schemas.policy import PolicyCreate, PolicyUpdate
from lucnt_api.schemas.recon import ReconResolveRequest
from lucnt_api.schemas.transaction import CaptureRequest, SpendRequest
from lucnt_api.services.policy_engine import MerchantListConfig


def _spend(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "agent": "ops",
        "entity": "sg",
        "vendor": "aws",
        "amount": Decimal("10"),
        "currency": "SGD",
        "reason": "compute",
    }
    body.update(overrides)
    return body


# --- The bounds must equal the columns they protect -------------------------

# (constant, the String(n) width of the column the field lands in)
_COLUMN_MIRRORS = (
    (AGENT_SLUG_MAX, 100),  # agents.slug
    (ENTITY_CODE_MAX, 50),  # entities.code
    (VENDOR_MAX, 255),  # transactions.vendor
)


@pytest.mark.parametrize(("bound", "column_width"), _COLUMN_MIRRORS)
def test_bound_matches_its_column_width(bound: int, column_width: int) -> None:
    """Wider than the column still 500s; narrower rejects legitimate input."""
    assert bound == column_width


# --- SpendRequest: the money path ------------------------------------------


@pytest.mark.parametrize(
    ("field", "bound"),
    [
        ("agent", AGENT_SLUG_MAX),
        ("entity", ENTITY_CODE_MAX),
        ("vendor", VENDOR_MAX),
        ("reason", REASON_MAX),
    ],
)
def test_spend_request_rejects_oversized_field(field: str, bound: int) -> None:
    with pytest.raises(ValidationError):
        SpendRequest(**_spend(**{field: "x" * (bound + 1)}))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "bound"),
    [
        ("agent", AGENT_SLUG_MAX),
        ("entity", ENTITY_CODE_MAX),
        ("vendor", VENDOR_MAX),
        ("reason", REASON_MAX),
    ],
)
def test_spend_request_accepts_field_at_the_bound(field: str, bound: int) -> None:
    """Exactly-at-the-limit must pass — an off-by-one here rejects valid input."""
    request = SpendRequest(**_spend(**{field: "x" * bound}))  # type: ignore[arg-type]
    assert len(getattr(request, field)) == bound


@pytest.mark.parametrize("field", ["agent", "entity", "vendor", "reason"])
def test_spend_request_rejects_empty_required_text(field: str) -> None:
    """An empty slug/vendor is not a valid lookup key; it must not reach the DB."""
    with pytest.raises(ValidationError):
        SpendRequest(**_spend(**{field: ""}))  # type: ignore[arg-type]


def test_spend_request_still_normalises_currency() -> None:
    """Regression guard: adding bounds must not disturb the T1-5 currency rules."""
    assert SpendRequest(**_spend(currency="sgd")).currency == "SGD"
    with pytest.raises(ValidationError):
        SpendRequest(**_spend(currency="SGDD"))


def test_capture_request_bounds_invoice_url() -> None:
    """Bounded at URL_MAX, mirroring the column it lands in since F-12."""
    with pytest.raises(ValidationError):
        CaptureRequest(
            authorization_id="0" * 8 + "-0000-0000-0000-" + "0" * 12,  # type: ignore[arg-type]
            invoice_url="https://x/" + "y" * 4096,
        )


# --- Policy config ---------------------------------------------------------


def _policy(config: dict[str, object]) -> PolicyCreate:
    return PolicyCreate(name="cap", rule_type=PolicyRuleType.BUDGET_CAP, config=config)


def test_policy_config_rejects_too_many_keys() -> None:
    with pytest.raises(ValidationError):
        _policy({f"k{i}": 1 for i in range(POLICY_CONFIG_MAX_KEYS + 1)})


def test_policy_config_rejects_oversized_value() -> None:
    with pytest.raises(ValidationError):
        _policy({"limit": "x" * (POLICY_CONFIG_VALUE_MAX + 1)})


def test_policy_config_accepts_a_normal_rule_body() -> None:
    """The bound must not disturb a real config — this is the common case."""
    assert _policy({"period": "daily", "limit": "100"}).config["limit"] == "100"


def test_policy_update_config_is_bounded_too() -> None:
    """PATCH is the same write path; bounding only POST would leave a hole."""
    with pytest.raises(ValidationError):
        PolicyUpdate(config={f"k{i}": 1 for i in range(POLICY_CONFIG_MAX_KEYS + 1)})


def test_policy_response_config_is_deliberately_unbounded() -> None:
    """Reads must not inherit the input cap, or a stored row 500s on serialization."""
    from lucnt_api.schemas.policy import PolicyResponse

    field = PolicyResponse.model_fields["config"]
    assert field.metadata == [], "an input bound leaked onto the response schema"


# --- Merchant list: scanned per authorize() --------------------------------


def test_merchant_list_is_bounded() -> None:
    with pytest.raises(ValidationError):
        MerchantListConfig(
            mode="block", vendors=[f"v{i}" for i in range(MERCHANT_LIST_MAX + 1)]
        )


def test_merchant_list_rejects_an_oversized_entry() -> None:
    """Each entry is compared against a String(255) column."""
    with pytest.raises(ValidationError):
        MerchantListConfig(mode="block", vendors=["x" * (VENDOR_MAX + 1)])


def test_merchant_list_accepts_a_normal_list() -> None:
    assert len(MerchantListConfig(mode="allow", vendors=["aws", "openai"]).vendors) == 2


# --- Free text and collections elsewhere -----------------------------------


def test_recon_resolve_bounds_fix_notes() -> None:
    with pytest.raises(ValidationError):
        ReconResolveRequest(action="resolve", fix_notes="x" * (NOTES_MAX + 1))


def test_manual_journal_bounds_line_count() -> None:
    """An unbounded journal is an unbounded transaction and an unbounded response."""
    line = {"account_code": "1000", "debit": Decimal("1")}
    with pytest.raises(ValidationError):
        ManualJournalRequest(
            entity="sg",
            memo="bulk",
            lines=[
                ManualJournalLineInput(**line) for _ in range(JOURNAL_LINES_MAX + 1)
            ],  # type: ignore[arg-type]
        )


def test_manual_journal_bounds_entity_code() -> None:
    with pytest.raises(ValidationError):
        ManualJournalRequest(
            entity="x" * (ENTITY_CODE_MAX + 1),
            memo="adj",
            lines=[
                ManualJournalLineInput(account_code="1000", debit=Decimal("1")),
                ManualJournalLineInput(account_code="4000", credit=Decimal("1")),
            ],
        )
