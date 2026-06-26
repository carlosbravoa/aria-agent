"""Tests for the plan/todo tool (roadmap #1)."""

from aria.tools import plan


def test_plan_set_and_render(minimal_env):
    out = plan.execute({"action": "set", "todos": [
        {"task": "Read config", "status": "done"},
        {"task": "Refactor", "status": "in_progress"},
        {"task": "Run tests", "status": "pending"},
    ]})
    assert "Plan — 1/3 done" in out
    assert "☑ Read config" in out
    assert "◐ Refactor" in out
    assert "☐ Run tests" in out


def test_plan_set_implied_by_todos(minimal_env):
    # no action given, but todos present → treated as 'set'
    out = plan.execute({"todos": [{"task": "Step one"}]})
    assert "☐ Step one" in out          # default status is pending


def test_plan_show_after_set(minimal_env):
    plan.execute({"action": "set", "todos": [{"task": "A", "status": "done"}]})
    assert "☑ A" in plan.execute({"action": "show"})


def test_plan_show_when_empty(minimal_env):
    assert "No plan yet" in plan.execute({"action": "show"})


def test_plan_clear(minimal_env):
    plan.execute({"action": "set", "todos": [{"task": "A"}]})
    assert "Cleared" in plan.execute({"action": "clear"})
    assert "No plan yet" in plan.execute({"action": "show"})


def test_plan_rejects_empty_set(minimal_env):
    assert "required" in plan.execute({"action": "set", "todos": []})


def test_plan_normalizes_bad_status(minimal_env):
    out = plan.execute({"action": "set", "todos": [{"task": "X", "status": "weird"}]})
    assert "☐ X" in out                 # unknown status falls back to pending
