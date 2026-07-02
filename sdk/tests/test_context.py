"""Unit tests for lucnt.context.task()."""

from __future__ import annotations

from lucnt.context import get_current_task, task


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
