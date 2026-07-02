"""Ambient task context: identity/intent set once via task(), inherited by calls."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from lucnt.models import TaskContext

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
