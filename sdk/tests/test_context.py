"""Unit tests for lucnt.context: task() and operation()."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from lucnt.context import (
    bind_context,
    get_current_operation,
    get_current_task,
    operation,
    task,
)
from lucnt.exceptions import ConfigurationError, IdempotencyConflict


def test_get_current_task_is_none_outside_a_task_block() -> None:
    assert get_current_task() is None


def test_task_sets_ambient_context_inside_the_block() -> None:
    with task(
        agent="ops", entity="sg", intent="founder summit", project="Q3 offsite"
    ) as ctx:
        assert ctx.agent == "ops"
        assert ctx.entity == "sg"
        assert ctx.intent == "founder summit"
        assert ctx.project == "Q3 offsite"
        assert get_current_task() is ctx


def test_task_resets_context_on_exit() -> None:
    with task(agent="ops", entity="sg"):
        pass
    assert get_current_task() is None


def test_task_defaults_intent_and_project_to_none() -> None:
    with task(agent="ops", entity="sg") as ctx:
        assert ctx.intent is None
        assert ctx.project is None


def test_nested_tasks_restore_outer_context_on_exit() -> None:
    with task(agent="outer", entity="sg") as outer_ctx:
        with task(agent="inner", entity="in") as inner_ctx:
            assert get_current_task() is inner_ctx
        assert get_current_task() is outer_ctx


# --- operation() ambient idempotency scope ----------------------------


def test_get_current_operation_is_none_outside_an_operation_block() -> None:
    assert get_current_operation() is None


def test_operation_sets_and_resets_the_ambient_scope() -> None:
    with operation("order-1") as op_id:
        assert op_id == "order-1"
        scope = get_current_operation()
        assert scope is not None
        assert scope.op_id == "order-1"
    assert get_current_operation() is None


def test_operation_strips_surrounding_whitespace() -> None:
    with operation("  order-2\n") as op_id:
        assert op_id == "order-2"


@pytest.mark.parametrize("op_id", ["", "   ", "\t\n"])
def test_operation_rejects_a_blank_op_id(op_id: str) -> None:
    """Blank keys are refused: server-side they skip dedupe or collide company-wide."""
    with pytest.raises(ConfigurationError, match="must not be blank"), operation(op_id):
        pass


def test_operation_rejects_an_op_id_longer_than_the_server_column() -> None:
    too_long = "x" * 256
    with pytest.raises(ConfigurationError, match="maximum is 255"), operation(too_long):
        pass


def test_operation_accepts_an_op_id_exactly_at_the_limit() -> None:
    with operation("x" * 255) as op_id:
        assert len(op_id) == 255


def test_nested_operations_restore_the_outer_scope_on_exit() -> None:
    with operation("outer"):
        with operation("inner"):
            inner = get_current_operation()
            assert inner is not None and inner.op_id == "inner"
        outer = get_current_operation()
        assert outer is not None and outer.op_id == "outer"


def test_reentering_the_same_op_id_starts_a_fresh_guardrail() -> None:
    """Each scope entry is a new guardrail; the server replay makes repeats safe."""
    scopes = []
    for _ in range(2):
        with operation("order-3"):
            scope = get_current_operation()
            assert scope is not None
            scope.claim({"vendor": "acme"})
            scopes.append(scope)
    assert scopes[0] is not scopes[1]


def test_claim_is_idempotent_for_an_identical_fingerprint() -> None:
    with operation("order-4"):
        scope = get_current_operation()
        assert scope is not None
        scope.claim({"vendor": "acme", "amount": Decimal("10")})
        scope.claim({"vendor": "acme", "amount": Decimal("10")})


def test_claim_rejects_a_different_fingerprint_and_names_the_fields() -> None:
    with operation("order-5"):
        scope = get_current_operation()
        assert scope is not None
        scope.claim({"vendor": "acme", "amount": Decimal("10")})
        with pytest.raises(IdempotencyConflict) as excinfo:
            scope.claim({"vendor": "acme", "amount": Decimal("99")})
    message = str(excinfo.value)
    assert "amount" in message
    assert "vendor" not in message  # only the differing field is reported
    assert "one logical spend" in message


def test_operation_and_task_compose_independently() -> None:
    with task(agent="ops", entity="sg"), operation("order-6"):
        ctx = get_current_task()
        scope = get_current_operation()
        assert ctx is not None and ctx.agent == "ops"
        assert scope is not None and scope.op_id == "order-6"


def test_bind_context_carries_the_operation_scope_to_a_worker() -> None:
    with operation("order-7"):
        expected = get_current_operation()

        def read() -> object:
            return get_current_operation()

        bound = bind_context(read)

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(bound).result() is expected


def test_operation_scope_is_lost_in_a_worker_without_binding() -> None:
    """Documents the bug bind_context() fixes, so it cannot silently start passing."""
    with operation("order-8"), ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(get_current_operation).result() is None


def test_bound_workers_share_one_guardrail_across_threads() -> None:
    """A fan-out of *different* spends under one op id must be detected, not
    silently collapsed into the first one's hold."""
    with operation("order-9"):

        def claim(amount: int) -> None:
            scope = get_current_operation()
            assert scope is not None
            scope.claim({"vendor": "acme", "amount": amount})

        bound = bind_context(claim)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [pool.submit(bound, amount) for amount in range(4)]
        refused = [r for r in results if isinstance(r.exception(), IdempotencyConflict)]
    assert len(refused) == 3  # first claim wins, the other three are refused


def test_bind_context_restores_the_workers_prior_operation_state() -> None:
    with operation("outer-scope"):
        bound = bind_context(lambda: None)
    bound()
    assert get_current_operation() is None
