"""Load test for the authorize→capture money path.

Usage (backend running, `scripts/seed_dev.py` applied):

    export LUCNT_API_KEY=lucnt_live_...   # printed by seed_dev.py
    # burst: realistic concurrency
    locust -f backend/loadtest/locustfile.py --headless -u 50 -r 10 -t 90s \
        --host http://127.0.0.1:8000
    # soak: long enough to surface pool exhaustion (pool 10 + overflow 10/worker)
    locust -f backend/loadtest/locustfile.py --headless -u 20 -r 5 -t 300s \
        --host http://127.0.0.1:8000

"""

from __future__ import annotations

import os
import uuid

from locust import HttpUser, between, task

_AGENT = os.environ.get("LOADTEST_AGENT", "research")
_ENTITY = os.environ.get("LOADTEST_ENTITY", "in")
_CURRENCY = os.environ.get("LOADTEST_CURRENCY", "INR")


def _headers() -> dict[str, str]:
    key = os.environ.get("LUCNT_API_KEY")
    if not key:
        raise RuntimeError("set LUCNT_API_KEY (printed by scripts/seed_dev.py)")
    return {"Authorization": f"Bearer {key}"}


class LucntAgent(HttpUser):
    """One synthetic agent doing governed spends."""

    wait_time = between(0.05, 0.2)

    def on_start(self) -> None:
        self.client.headers.update(_headers())

    @task(10)
    def authorize_then_capture(self) -> None:
        """Authorize then capture; a blocked verdict is valid, not a failure."""
        verdict = self.client.post(
            "/transactions/authorize",
            json={
                "agent": _AGENT,
                "entity": _ENTITY,
                "vendor": "loadtest",
                "amount": "1.50",
                "currency": _CURRENCY,
                "reason": "load test spend",
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
            name="/transactions/authorize",
        )
        if verdict.status_code != 200:
            return  # non-200 is recorded as a failure by locust already
        body = verdict.json()
        if body.get("status") != "approved":
            return  # blocked by policy under load — valid gate behaviour
        self.client.post(
            "/transactions/capture",
            json={"authorization_id": body["authorization_id"], "source": "sdk"},
            name="/transactions/capture",
        )

    @task(2)
    def authorize_blocked(self) -> None:
        """An over-budget spend: exercises the same gate, always blocked."""
        with self.client.post(
            "/transactions/authorize",
            json={
                "agent": _AGENT,
                "entity": _ENTITY,
                "vendor": "loadtest",
                "amount": "999999999",
                "currency": _CURRENCY,
                "reason": "deliberately over budget",
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
            name="/transactions/authorize [blocked]",
            catch_response=True,
        ) as response:
            if response.status_code == 200 and response.json().get("status") in {
                "blocked",
                "review",
                "human_hold",
            }:
                response.success()
            else:
                response.failure(
                    f"expected a blocking verdict, got {response.status_code}"
                )

    @task(1)
    def read_transactions(self) -> None:
        self.client.get(
            "/transactions?limit=20",
            name="/transactions [list]",
        )
