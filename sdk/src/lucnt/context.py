"""Ambient task context: identity/intent set once via task(), inherited by calls."""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import ParamSpec, TypeVar

from lucnt.models import TaskContext

_P = ParamSpec("_P")
_R = TypeVar("_R")

_current_task: ContextVar[TaskContext | None] = ContextVar(
    "_current_task", default=None
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


def bind_context(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """Bind the current `task()` context to `fn` so it survives a thread hop (T5-8).

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

    Args:
        fn: The callable to run under the caller's current task context.

    Returns:
        A wrapper that applies the snapshot, runs `fn`, and restores the
        worker's prior state.
    """
    snapshot = _current_task.get()

    @functools.wraps(fn)
    def _run(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        token = _current_task.set(snapshot)
        try:
            return fn(*args, **kwargs)
        finally:
            _current_task.reset(token)

    return _run
