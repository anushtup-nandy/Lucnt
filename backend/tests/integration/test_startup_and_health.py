"""Health split, startup readiness, request context and metrics.

The readiness check is exercised directly here rather than through the app
lifespan, because `conftest` disables the lifespan check for the suite (it would
otherwise assert about the module-level engine, which no test uses).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from lucnt_api import metrics
from lucnt_api.database import get_db
from lucnt_api.main import app
from lucnt_api.middleware import REQUEST_ID_HEADER
from lucnt_api.startup import verify_ready_or_raise


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


# --- Liveness / readiness -------------------------------------------


def test_liveness_never_touches_a_dependency(client: TestClient) -> None:
    """A DB outage must not make every container fail its liveness probe."""
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_legacy_health_alias_still_works(client: TestClient) -> None:
    """Existing docs and probes curl `/health`; keep it answering."""
    assert client.get("/health").json() == {"status": "ok"}


def test_readiness_reports_ready_against_a_migrated_database(
    client: TestClient,
) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"] == "up"
    # The suite runs against a migrated schema, so the head comparison must have
    # actually happened -- `None` here would mean the scripts weren't found and
    # the check silently degraded to a bare ping.
    assert body["schema_at_head"] is True


def test_readiness_is_503_when_the_database_is_unreachable(
    client: TestClient,
) -> None:
    """The point of the health split: an LB must stop routing to a broken backend."""
    dead = create_engine(
        "postgresql://nobody:nobody@127.0.0.1:1/nonexistent",
        connect_args={"connect_timeout": 1},
    )
    dead_session = sessionmaker(bind=dead)()
    app.dependency_overrides[get_db] = lambda: dead_session
    try:
        response = client.get("/health/ready")
    finally:
        app.dependency_overrides.pop(get_db, None)
        dead_session.close()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["database"] == "down"
    # The DSN must not leak to an unauthenticated prober.
    assert body["detail"] == "database unreachable"


def test_startup_check_raises_when_the_database_is_unreachable() -> None:
    """The lifespan half: refuse to finish starting rather than serve broken."""
    dead = create_engine(
        "postgresql://nobody:nobody@127.0.0.1:1/nonexistent",
        connect_args={"connect_timeout": 1},
    )
    with (
        sessionmaker(bind=dead)() as session,
        pytest.raises(RuntimeError, match="startup readiness check failed"),
    ):
        verify_ready_or_raise(session)


def test_startup_check_passes_against_the_real_test_database(
    resolved_engine: Engine,
) -> None:
    """The lifespan's check, run against the database the suite actually uses."""
    with sessionmaker(bind=resolved_engine)() as session:
        report = verify_ready_or_raise(session)
    assert report.ready is True
    assert report.current_revision == report.expected_head


# --- Request context + access logging --------------------------------


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.headers[REQUEST_ID_HEADER]


def test_a_sane_caller_supplied_request_id_is_reused(client: TestClient) -> None:
    """So a caller can correlate its own trace with the server's log lines."""
    response = client.get("/health/live", headers={REQUEST_ID_HEADER: "abc-123"})
    assert response.headers[REQUEST_ID_HEADER] == "abc-123"


def test_an_oversized_request_id_is_replaced_not_truncated(
    client: TestClient,
) -> None:
    """An unbounded header is a log-injection and cardinality vector."""
    response = client.get("/health/live", headers={REQUEST_ID_HEADER: "x" * 500})
    returned = response.headers[REQUEST_ID_HEADER]
    assert returned != "x" * 500
    assert len(returned) == 32


# --- Metrics ---------------------------------------------------------


def test_metrics_label_by_route_template_not_by_path(client: TestClient) -> None:
    """A UUID in a metric label mints a series per entity and kills the scrape."""
    missing_id = "00000000-0000-0000-0000-000000000000"
    client.get(f"/agents/{missing_id}")
    body = metrics.render().decode()
    assert "/agents/{agent_id}" in body
    assert missing_id not in body


def test_unmatched_paths_do_not_name_their_own_series(client: TestClient) -> None:
    client.get("/definitely-not-a-route")
    body = metrics.render().decode()
    assert "__unmatched__" in body
    assert "definitely-not-a-route" not in body


def test_metrics_endpoint_serves_the_prometheus_exposition_format(
    client: TestClient,
) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "lucnt_http_requests_total" in response.text


def test_metrics_endpoint_does_not_measure_itself(client: TestClient) -> None:
    """Otherwise every scrape inflates the counters it is scraping."""
    client.get("/metrics")
    body = metrics.render().decode()
    assert 'route="/metrics"' not in body
    assert 'route="/health/live"' not in body
