"""Ambient context: `task()` sets identity/intent, `operation()` sets the retry scope.

Both are contextvars read by `authorize()` so a caller sets them once and every
call inside the block inherits them. `task()` answers *who is spending and why*;
`operation()` answers *which logical spend this is*, which is what makes the
caller's own retry loop safe.
"""

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

    There is no `Agent` class — the SDK attaches to the caller's own agent,
    identified here by a plain string handle.

    Args:
        agent: The agent handle this task's spend is attributed to.
        entity: The legal entity handle this task's spend is attributed to.
        intent: Free-text reason the task exists, e.g. "founder summit".
        project: Optional cost-center / project label.

    Yields:
        The active `TaskContext` for the duration of the `with` block.
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

    The key alone is not enough. The backend looks a repeated
    `Idempotency-Key` up and replays the original hold **without comparing the
    request body**, so if two different spends went out under one op id the
    second caller would receive a verdict describing the first one. `claim()`
    binds the scope to the first spend it sees and refuses any other.

    A scope survives a thread hop via `bind_context()`, so the *same* instance
    reaches every worker — which is exactly what makes a fan-out of differing
    spends under one op id detectable rather than silent.

    The lock around the check-and-set is **not** currently observable: on
    GIL CPython the window between reading `_fingerprint` and storing it was
    measured as unreachable (400 trials, 8 barrier-synchronized threads, a 1ns
    switch interval — removing the lock changed nothing). It is kept anyway
    because it is free and the guarantee is not: under free-threaded CPython
    there is no GIL to serialize those bytecodes, and a lost race here admits a
    second spend under the first one's key. No test asserts it, deliberately —
    a test that passes with the mechanism removed is worse than none.
    """

    __slots__ = ("_fingerprint", "_lock", "op_id")

    def __init__(self, op_id: str) -> None:
        """Start an unclaimed scope for `op_id`."""
        self.op_id = op_id
        self._lock = threading.Lock()
        self._fingerprint: dict[str, object] | None = None

    def claim(self, fingerprint: dict[str, object]) -> None:
        """Bind this scope to `fingerprint`, or raise if it already holds another.

        Args:
            fingerprint: The spend-identifying fields of the request about to
                go out under this scope's key.

        Raises:
            IdempotencyConflict: A materially different spend already claimed
                this scope.
        """
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

    `authorize()` auto-generates an idempotency key per call, which makes the
    SDK's own transport retries safe. It cannot help when the retry is
    yours: calling `spend()` again after a `TransportError` is a new logical
    spend, gets a fresh key, and mints a second hold — the double-book idempotency
    exists to prevent, failing silently. Naming the operation fixes that,
    because the key is now a property of the operation rather than the call:

        for attempt in range(3):
            try:
                with lucnt.operation(f"order-{order.id}"):
                    lucnt.spend(vendor="Acme", amount="49.00", currency="USD",
                                reason="seat upgrade")
                break
            except lucnt.TransportError:
                continue

    Composes with `task()` (they are independent contextvars) and survives a
    thread hop through `bind_context()`.

    **One scope is one logical spend.** A second, materially different spend
    inside the same scope raises `IdempotencyConflict` rather than returning the
    first spend's verdict — see `_Operation.claim`. The check is per scope
    *entry*, deliberately: re-entering `with lucnt.operation("order-7")` starts
    a fresh guardrail so the retry pattern above works, and the server's replay
    is what makes the repeat safe. A process-wide registry of every op id ever
    seen would both leak and break that pattern.

    Precedence, when more than one key is available: an explicit
    `idempotency_key=` argument wins, then this ambient scope, then the
    per-call auto-generated key.

    Args:
        op_id: Caller-chosen identifier for this logical spend, unique within
            your company. Use something you can regenerate on the retry path —
            an order id, a job id — not a fresh uuid per attempt.

    Yields:
        The normalized `op_id` now in effect.

    Raises:
        ConfigurationError: `op_id` is blank or longer than the server's
            255-character key column.
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
    """Bind the current ambient context to `fn` so it survives a thread hop.

    A worker thread starts with an empty context, so ambient identity set by
    `task()` does **not** reach code running in a `ThreadPoolExecutor`. Without
    this, a fan-out either fails with `ConfigurationError` (no agent resolvable)
    or silently books against whatever the worker can see. Wrap the callable
    *inside* the `task()` block — the snapshot is taken at bind time, not at
    call time:

        with lucnt.task(agent="researcher", entity="acme-us"):
            with ThreadPoolExecutor() as pool:
                results = list(pool.map(lucnt.bind_context(do_work), items))

    Snapshots the SDK's own ambient state by value rather than reusing a
    `contextvars.Context` object: a single `Context` cannot be entered
    concurrently, so a bound callable submitted to several workers at once
    would raise. Copying the value keeps the wrapper safe to call from any
    number of threads.

    Both `task()` and `operation()` are carried. For `operation()` the value is
    the scope object itself, so every worker shares one guardrail — a fan-out
    issuing *different* spends under a single op id raises `IdempotencyConflict`
    instead of silently collapsing them into the first one's hold.

    Args:
        fn: The callable to run under the caller's current ambient context.

    Returns:
        A wrapper that applies the snapshot, runs `fn`, and restores the
        worker's prior state.
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
