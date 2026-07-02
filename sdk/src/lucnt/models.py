"""Typed data structures shared across the lucnt SDK."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


class LucntConfig(BaseModel):
    """Resolved SDK configuration: API key, base URL, and transport timeout."""

    model_config = ConfigDict(frozen=True)

    api_key: str
    base_url: str
    timeout: float


class TaskContext(BaseModel):
    """Ambient identity/intent set once via `task()`, inherited by calls inside it."""

    model_config = ConfigDict(frozen=True)

    agent: str
    entity: str
    intent: str | None = None
    project: str | None = None


class SpendRequest(BaseModel):
    """Request body for `POST /transactions/authorize` — the frozen P1 wire contract.

    `amount` is `Decimal`, never `float`: this is a ledger, and float
    rounding error compounds across authorize -> capture -> journal ->
    reconciliation.
    """

    model_config = ConfigDict(frozen=True)

    agent: str
    entity: str
    vendor: str
    amount: Decimal
    currency: str | None = None
    reason: str
    intent: str | None = None
    project: str | None = None
    model: str | None = None
    tokens: int | None = None


class Verdict(BaseModel):
    """Policy verdict from `POST /transactions/authorize`, before any spend happens."""

    model_config = ConfigDict(frozen=True)

    authorization_id: str
    status: Literal["approved", "blocked", "review", "human_hold"]
    reason: str | None = None

    @property
    def approved(self) -> bool:
        """True only when `status == "approved"`; every other status blocks it."""
        return self.status == "approved"


class Transaction(BaseModel):
    """A committed spend, returned by `POST /transactions/capture`."""

    model_config = ConfigDict(frozen=True)

    id: str
    authorization_id: str
    status: str
    vendor: str
    amount: Decimal
    currency: str | None = None

    @property
    def approved(self) -> bool:
        """Always True: a Transaction only exists once capture has succeeded."""
        return True
