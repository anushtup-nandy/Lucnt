"""Ambient context: `task()` sets identity/intent, `operation()` the retry scope."""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import ParamSpec, TypeVar

from lucnt.client import check_idempotency_key
from lucnt.exceptions import IdempotencyConflict
from lucnt.models import TaskContext

_P = ParamSpec("_P")
_R = TypeVar("_R")

_current_task: ContextVar[TaskContext | None] = ContextVar(
    "_current_task", default=None
)
_current_operation: ContextVar[_Operation | None] = ContextVar(
    "_current_operation", default=None
)


@contextmanager
def task(
    *, agent: str, entity: str, intent: str | None = None, project: str | None = None
) -> Iterator[TaskContext]:
    """Set the ambient agent/entity/intent/project context for calls inside the block.

        with lucnt.task(agent="researcher", entity="acme-us", intent="summit"):
            lucnt.spend(vendor="Acme", amount="49.00", currency="USD", reason="x")

    Args:
        agent: Agent handle the spend is attributed to.
        entity: Legal entity handle the spend is attributed to.
        intent: Free-text reason the task exists.
        project: Optional cost-center / project label.

    Yields:
        The active `TaskContext`.
    """
    ctx = TaskContext(agent=agent, entity=entity, intent=intent, project=project)
    token = _current_task.set(ctx)
    try:
        yield ctx
    finally:
        _current_task.reset(token)


def get_current_task() -> TaskContext | None:
    """Return the ambient `TaskContext`, or `None` outside any `task()` block."""
    return _current_task.get()


class _Operation:
    """One logical spend's idempotency key, plus the payload guardrail on it.

    The server replays a repeated key without comparing bodies, so `claim()`
    binds the scope to its first spend and refuses any other. The lock is for
    free-threaded CPython, where the check-and-set could otherwise race.
    """

    __slots__ = ("_fingerprint", "_lock", "op_id")

    def __init__(self, op_id: str) -> None:
        """Start an unclaimed scope for `op_id`."""
        self.op_id = op_id
        self._lock = threading.Lock()
        self._fingerprint: dict[str, object] | None = None

    def claim(self, fingerprint: dict[str, object]) -> None:
        """Bind this scope to `fingerprint`, or raise `IdempotencyConflict`."""
        with self._lock:
            if self._fingerprint is None:
                self._fingerprint = fingerprint
                return
            if self._fingerprint == fingerprint:
                return
            diff = ", ".join(
                f"{field}: {self._fingerprint.get(field)!r} != "
                f"{fingerprint.get(field)!r}"
                for field in sorted(self._fingerprint.keys() | fingerprint.keys())
                if self._fingerprint.get(field) != fingerprint.get(field)
            )
        raise IdempotencyConflict(
            f"operation({self.op_id!r}) is already bound to a different spend "
            f"({diff}). One operation() scope is one logical spend — the server "
            f"replays the first hold for a repeated key regardless of payload, so "
            f"a verdict returned here would describe the other spend. Open a "
            f"separate operation() scope per spend, or pass an explicit "
            f"idempotency_key= to opt out."
        )


@contextmanager
def operation(op_id: str) -> Iterator[str]:
    """Name one logical spend so *your* retry of it replays instead of re-holding.

    Without it, re-calling `spend()` after a `TransportError` mints a fresh key
    and a second hold. One scope is one spend; a different spend inside it
    raises `IdempotencyConflict`. Key precedence: explicit `idempotency_key=`,
    then this scope, then an auto-generated key.

    Args:
        op_id: Stable id you can regenerate on retry (e.g. an order id).

    Yields:
        The normalized `op_id` now in effect.

    Raises:
        ConfigurationError: `op_id` is blank or longer than 255 characters.
    """
    cleaned = check_idempotency_key(op_id, "operation() op_id")
    token = _current_operation.set(_Operation(cleaned))
    try:
        yield cleaned
    finally:
        _current_operation.reset(token)


def get_current_operation() -> _Operation | None:
    """Return the ambient operation scope, or `None` outside any `operation()`."""
    return _current_operation.get()


def bind_context(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """Bind the current `task()`/`operation()` context to `fn` for a thread hop.

    Worker threads start with an empty context. Snapshot is taken at bind time;
    values are copied (not a `Context`) so the wrapper is safe across threads.

        with lucnt.task(agent="researcher", entity="acme-us"):
            with ThreadPoolExecutor() as pool:
                results = list(pool.map(lucnt.bind_context(do_work), items))

    Args:
        fn: The callable to run under the caller's current ambient context.

    Returns:
        A wrapper that applies the snapshot, runs `fn`, then restores state.
    """
    task_snapshot = _current_task.get()
    operation_snapshot = _current_operation.get()

    @functools.wraps(fn)
    def _run(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        task_token = _current_task.set(task_snapshot)
        operation_token = _current_operation.set(operation_snapshot)
        try:
            return fn(*args, **kwargs)
        finally:
            _current_operation.reset(operation_token)
            _current_task.reset(task_token)

    return _run
