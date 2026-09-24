"""
Regression tests for recurring tasks multiplying / running more than once.

Root causes covered:
  - a running task re-scheduling itself (recurring create inside a task);
  - non-idempotent `schedule create`;
  - cancel not stopping a series (new id per occurrence, or cancel mid-run);
  - retries shifting the schedule / exhausted retries ending the series;
  - a global plan leaking "schedule X" steps into supervisor runs;
  - duplicates already sitting in pending/ from older versions.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest


def _files(state):
    from aria.task import tasks_dir
    d = tasks_dir() / state
    return sorted(d.glob("*.task")) if d.exists() else []


def _load(p):
    from aria.task import Task
    return Task.from_text(p.read_text(encoding="utf-8"))


def _future(hours=1):
    from aria.task import _now_dt
    return (_now_dt() + timedelta(hours=hours)).isoformat(timespec="seconds")


def _past(minutes=5):
    from aria.task import _now_dt
    return (_now_dt() - timedelta(minutes=minutes)).isoformat(timespec="seconds")


# ── schedule create guards ────────────────────────────────────────────────────

def test_create_is_idempotent(minimal_env):
    from aria.tools import schedule
    args = {"prompt": "Summarise HN", "recur": "daily", "run_after": _future()}
    first = schedule.execute(args)
    second = schedule.execute({**args, "prompt": "  summarise   hn "})
    assert "queued" in first
    assert "Already scheduled" in second
    assert len(_files("pending")) == 1


def test_recurring_create_refused_inside_a_task(minimal_env, monkeypatch):
    from aria.tools import schedule
    monkeypatch.setenv("ARIA_TASK_ID", "abc12345")
    out = schedule.execute({"prompt": "Summarise HN", "recur": "daily"})
    assert "schedule error" in out
    assert _files("pending") == []
    # a one-shot follow-up is still allowed
    assert "queued" in schedule.execute({"prompt": "follow up", "run_after": _future()})


@pytest.mark.parametrize("args", [
    {"prompt": "x", "recur": "hourly"},
    {"prompt": "x", "recur": "0m"},
    {"prompt": "x", "run_after": "tomorrow 8am"},
])
def test_invalid_recur_or_run_after_rejected(minimal_env, args):
    from aria.tools import schedule
    assert "schedule error" in schedule.execute(args)
    assert _files("pending") == []


# ── series lifecycle ──────────────────────────────────────────────────────────

def _run_once(result="ok"):
    """One supervisor iteration without the agent: claim + complete."""
    from aria.task import list_pending, claim, complete
    for path, task in list_pending():
        rp = claim(path, task)
        if rp:
            complete(rp, task, result)


def test_requeue_keeps_series_id(minimal_env):
    from aria.task import Task, enqueue
    t = Task(prompt="p", recur="daily", run_after=_past())
    enqueue(t)
    _run_once()
    [nxt] = _files("pending")
    n = _load(nxt)
    assert n.series_id == t.task_id
    assert n.task_id != t.task_id
    assert n.scheduled_for == n.run_after


def test_cancel_with_old_occurrence_id_stops_series(minimal_env):
    from aria.task import Task, enqueue
    from aria.tools import schedule
    t = Task(prompt="p", recur="daily", run_after=_past())
    enqueue(t)
    _run_once()                                  # now a NEW occurrence is pending
    out = schedule.execute({"action": "cancel", "task_id": t.task_id})
    assert "not found" not in out
    assert _files("pending") == []


def test_cancel_while_running_is_not_requeued(minimal_env):
    from aria.task import Task, enqueue, list_pending, claim, complete, cancel
    enqueue(Task(prompt="p", recur="daily", run_after=_past()))
    [(path, task)] = list_pending()
    rp = claim(path, task)
    assert cancel(task.task_id) == [task.task_id]   # user cancels mid-run
    complete(rp, task, "done")
    assert _files("pending") == []
    assert len(_files("cancelled")) == 1


def test_retry_does_not_shift_the_schedule(minimal_env, monkeypatch):
    from aria.task import Task, enqueue, list_pending, claim, fail, complete, _parse_dt
    monkeypatch.setenv("ARIA_TASK_RETRY_BASE", "0")
    slot = _past(minutes=2)
    enqueue(Task(prompt="p", recur="daily", run_after=slot))
    [(path, task)] = list_pending()
    fail(claim(path, task), task, "boom")          # retry → run_after moves
    [(path, task)] = list_pending()
    assert task.scheduled_for == slot
    complete(claim(path, task), task, "ok")
    [nxt] = _files("pending")
    assert _parse_dt(_load(nxt).run_after) == _parse_dt(slot) + timedelta(days=1)


def test_exhausted_retries_keep_the_series_alive(minimal_env):
    from aria.task import Task, enqueue, list_pending, claim, fail
    enqueue(Task(prompt="p", recur="daily", run_after=_past(), max_retries=0))
    [(path, task)] = list_pending()
    fail(claim(path, task), task, "boom")
    assert len(_files("failed")) == 1
    [nxt] = _files("pending")
    n = _load(nxt)
    assert n.retries == 0 and n.series_id == task.series_id


def test_requeue_never_forks_a_series(minimal_env):
    from aria.task import Task, enqueue, _requeue_series
    t = Task(prompt="p", recur="daily", run_after=_past())
    _requeue_series(t)
    _requeue_series(t)                             # e.g. reaper + owner both finish
    assert len(_files("pending")) == 1


def test_dedupe_pending_collapses_existing_duplicates(minimal_env):
    from aria.task import Task, enqueue, dedupe_pending
    slot = _future(1)
    enqueue(Task(prompt="Summarise HN", recur="daily", run_after=slot))
    enqueue(Task(prompt="summarise hn", recur="daily", run_after=slot))
    enqueue(Task(prompt="Summarise HN", recur="weekly", run_after=_future(3)))
    enqueue(Task(prompt="one-shot", run_after=_future(1)))
    notes = dedupe_pending()
    assert len(notes) == 1
    assert len(_files("pending")) == 3
    assert len(_files("cancelled")) == 1


def test_legacy_task_without_series_fields_loads(minimal_env):
    from aria.task import Task
    t = Task.from_text(json.dumps({"prompt": "p", "id": "deadbeef", "recur": "daily"}))
    assert t.series_id == "deadbeef"
    assert t.scheduled_for == ""


# ── supervisor task context ───────────────────────────────────────────────────

def test_task_context_marks_and_restores(minimal_env, monkeypatch):
    import os
    from aria import supervisor

    seen = {}

    class _FakeAgent:
        def __init__(self, **kw): pass
        def chat_collect(self, prompt):
            seen["id"] = os.environ.get("ARIA_TASK_ID")
            return "ok"
        def close(self): pass

    import aria.agent
    monkeypatch.setattr(aria.agent, "Agent", _FakeAgent)
    monkeypatch.delenv("ARIA_TASK_ID", raising=False)
    assert supervisor._run_agent_task("hi", "t1") == "ok"
    assert seen["id"] == "t1"
    assert "ARIA_TASK_ID" not in os.environ


def test_wrapper_forbids_rescheduling(minimal_env, monkeypatch):
    from aria import supervisor
    from aria.task import Task
    captured = {}

    def fake_run(target, args, timeout):
        captured["prompt"] = args[0]
        return ""

    monkeypatch.setattr(supervisor, "run_with_timeout", fake_run)
    supervisor._execute(Task(prompt="x", notify=False))
    assert "Do NOT call the schedule tool" in captured["prompt"]


# ── plan scoping ──────────────────────────────────────────────────────────────

def test_plan_is_scoped_per_conversation(minimal_env):
    from aria.tools import plan
    token = plan.set_scope("telegram:42")
    try:
        plan.execute({"todos": [{"task": "schedule daily digest"}]})
    finally:
        plan.reset_scope(token)
    assert "schedule daily digest" in plan.context_block("telegram:42")
    assert plan.context_block("supervisor") == ""
    assert plan.context_block("repl") == ""


def test_agent_tool_calls_use_their_own_plan_scope(minimal_env):
    from aria.agent import Agent
    from aria.tools import plan
    a = Agent(window_key="telegram:7", terminal=False)
    a._execute_tool("plan", {"todos": [{"task": "step one"}]})
    assert "step one" in plan.context_block("telegram:7")
    assert plan.context_block() == ""           # default (repl) scope untouched
    a.clear_session()
    assert plan.context_block("telegram:7") == ""


# ── per-conversation model profile ────────────────────────────────────────────

def test_profile_state_is_per_conversation(minimal_env, monkeypatch, tmp_path):
    import aria.agent as agent_mod
    monkeypatch.setattr(agent_mod, "_PROFILE_STATE", tmp_path / ".last_profile")
    monkeypatch.setenv("LLM_PROFILE1_NAME", "fast")
    monkeypatch.setenv("LLM_PROFILE1_MODEL", "fast-model")

    wa = agent_mod.Agent(window_key="whatsapp:1", terminal=False)
    wa.switch_profile("fast")
    assert not (tmp_path / ".last_profile").exists()     # REPL unaffected

    assert agent_mod.Agent(window_key="whatsapp:1", terminal=False).model == "fast-model"
    assert agent_mod.Agent(window_key="repl", terminal=False).model == "test-model"
    sup = agent_mod.Agent(window_key="supervisor", terminal=False)
    sup.switch_profile("fast")                           # never persisted
    assert agent_mod.Agent(window_key="supervisor", terminal=False).model == "test-model"

    # the REPL's choice is the fallback for conversations without their own
    agent_mod.Agent(window_key="repl", terminal=False).switch_profile("fast")
    assert agent_mod.Agent(window_key="supervisor", terminal=False).model == "fast-model"
    assert agent_mod.Agent(window_key="telegram:9", terminal=False).model == "fast-model"
    assert agent_mod.Agent(window_key="cli", terminal=False).model == "fast-model"


# ── /compact failure is reported, not faked ───────────────────────────────────

def test_compact_reports_llm_failure(minimal_env):
    from aria.agent import Agent
    a = Agent(window_key="repl", terminal=False)
    a.history = [{"role": "user", "content": "hello"},
                 {"role": "assistant", "content": "hi there"}]

    class _Boom:
        def create(self, **kw): raise RuntimeError("LLM down")

    a.client = type("C", (), {"chat": type("Ch", (), {"completions": _Boom()})()})()
    out = a.compact()
    assert out.startswith("[compact failed]")
    assert len(a.history) == 2 and a.history[0]["content"] == "hello"


def test_same_prompt_different_times_are_not_duplicates(minimal_env):
    """'take pills' daily at 08:00 AND 20:00 are two real jobs."""
    from aria.task import Task, enqueue, dedupe_pending, _now_dt
    from aria.tools import schedule
    base = (_now_dt() + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    morning, evening = base.isoformat(), base.replace(hour=20).isoformat()
    assert "queued" in schedule.execute({"prompt": "take pills", "recur": "daily", "run_after": morning})
    assert "queued" in schedule.execute({"prompt": "take pills", "recur": "daily", "run_after": evening})
    assert "Already" in schedule.execute({"prompt": "take pills", "recur": "daily", "run_after": morning})
    enqueue(Task(prompt="take pills", recur="daily", run_after=evening))   # a legacy dup
    assert len(dedupe_pending()) == 1
    assert len(_files("pending")) == 2
