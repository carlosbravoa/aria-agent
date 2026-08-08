"""
Task-queue hardening tests: running/ reaper (crash recovery), per-task
execution timeout, retry backoff, and timezone-aware scheduling.

All offline: no real LLM, no network — the Agent used by supervisor._execute
is monkeypatched, and the queue lives in the minimal_env tmp workspace.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _queue(minimal_env, state):
    from aria.task import tasks_dir
    return tasks_dir() / state


def _age_running_file(path, seconds):
    """Rewrite a running task file's started_at to `seconds` ago."""
    from aria import task as taskmod
    d = json.loads(path.read_text(encoding="utf-8"))
    d["started_at"] = (taskmod._now_dt() - timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )
    path.write_text(json.dumps(d), encoding="utf-8")


def _delay_seconds(run_after):
    """Seconds from now until run_after (aware-safe)."""
    from aria import task as taskmod
    return (taskmod._parse_dt(run_after) - taskmod._now_dt()).total_seconds()


# ── running/ reaper (crash recovery) ──────────────────────────────────────────

def test_claim_stamps_started_at(minimal_env):
    from aria.task import Task, enqueue, claim
    t = Task(prompt="x")
    running = claim(enqueue(t), t)
    assert running is not None and running.parent.name == "running"
    stored = Task.from_text(running.read_text(encoding="utf-8"))
    assert stored.started_at != ""
    assert datetime.fromisoformat(stored.started_at).tzinfo is not None


def test_reaper_requeues_stale_task_with_retries_left(minimal_env, monkeypatch):
    from aria.task import Task, enqueue, claim, reap_running
    monkeypatch.setenv("ARIA_TASK_TIMEOUT", "900")
    t = Task(prompt="x", max_retries=2)
    running = claim(enqueue(t), t)
    _age_running_file(running, 2000)                    # lease long expired

    notes = reap_running()

    assert len(notes) == 1 and "requeued" in notes[0]
    assert not running.exists()
    pending = list(_queue(minimal_env, "pending").glob("*.task"))
    assert len(pending) == 1
    requeued = Task.from_text(pending[0].read_text(encoding="utf-8"))
    assert requeued.retries == 1
    assert requeued.started_at == ""                    # lease cleared
    assert _delay_seconds(requeued.run_after) > 0       # backoff applied


def test_reaper_fails_stale_task_without_retries(minimal_env, monkeypatch):
    from aria.task import Task, enqueue, claim, reap_running
    monkeypatch.setenv("ARIA_TASK_TIMEOUT", "900")
    t = Task(prompt="x", max_retries=0)
    running = claim(enqueue(t), t)
    _age_running_file(running, 2000)

    notes = reap_running()

    assert len(notes) == 1 and "failed" in notes[0]
    assert not running.exists()
    failed = list(_queue(minimal_env, "failed").glob("*.task"))
    assert len(failed) == 1
    d = json.loads(failed[0].read_text(encoding="utf-8"))
    assert "reaped" in d["error"] and "stale in running/" in d["error"]


def test_reaper_leaves_fresh_running_task_alone(minimal_env, monkeypatch):
    from aria.task import Task, enqueue, claim, reap_running
    monkeypatch.setenv("ARIA_TASK_TIMEOUT", "900")
    t = Task(prompt="x")
    running = claim(enqueue(t), t)                      # started_at = now

    assert reap_running() == []
    assert running.exists()


def test_reaper_falls_back_to_mtime_without_started_at(minimal_env, monkeypatch):
    from aria.task import Task, enqueue, claim, reap_running
    monkeypatch.setenv("ARIA_TASK_TIMEOUT", "900")
    t = Task(prompt="x", max_retries=1)
    running = claim(enqueue(t), t)
    # Simulate a pre-2.5 running file: no started_at stamp, old mtime.
    d = json.loads(running.read_text(encoding="utf-8"))
    d["started_at"] = ""
    running.write_text(json.dumps(d), encoding="utf-8")
    old = time.time() - 2000
    os.utime(running, (old, old))

    notes = reap_running()
    assert len(notes) == 1
    assert not running.exists()
    assert len(list(_queue(minimal_env, "pending").glob("*.task"))) == 1


def test_reaper_disabled_when_timeout_zero(minimal_env, monkeypatch):
    from aria.task import Task, enqueue, claim, reap_running
    monkeypatch.setenv("ARIA_TASK_TIMEOUT", "0")
    t = Task(prompt="x")
    running = claim(enqueue(t), t)
    _age_running_file(running, 100000)
    assert reap_running() == []
    assert running.exists()


# ── retry backoff ─────────────────────────────────────────────────────────────

def test_fail_sets_exponential_backoff(minimal_env, monkeypatch):
    from aria.task import Task, enqueue, claim, fail
    monkeypatch.setenv("ARIA_TASK_RETRY_BASE", "60")
    t = Task(prompt="x", max_retries=3)

    running = claim(enqueue(t), t)
    fail(running, t, "boom")                            # attempt 1 → base
    p1 = list(_queue(minimal_env, "pending").glob("*.task"))[0]
    t1 = Task.from_text(p1.read_text(encoding="utf-8"))
    assert t1.retries == 1
    assert 55 <= _delay_seconds(t1.run_after) <= 65

    running = claim(p1, t1)
    fail(running, t1, "boom")                           # attempt 2 → base*2
    p2 = list(_queue(minimal_env, "pending").glob("*.task"))[0]
    t2 = Task.from_text(p2.read_text(encoding="utf-8"))
    assert t2.retries == 2
    assert 115 <= _delay_seconds(t2.run_after) <= 125


def test_fail_backoff_is_capped(minimal_env, monkeypatch):
    from aria.task import Task, enqueue, claim, fail
    monkeypatch.setenv("ARIA_TASK_RETRY_BASE", "5000")
    monkeypatch.setenv("ARIA_TASK_RETRY_MAX", "100")
    t = Task(prompt="x", max_retries=1)
    running = claim(enqueue(t), t)
    fail(running, t, "boom")
    p = list(_queue(minimal_env, "pending").glob("*.task"))[0]
    t1 = Task.from_text(p.read_text(encoding="utf-8"))
    assert _delay_seconds(t1.run_after) <= 100


def test_pending_scan_skips_future_run_after(minimal_env):
    from aria import task as taskmod
    from aria.task import Task, enqueue, list_pending
    future = (taskmod._now_dt() + timedelta(hours=1)).isoformat(timespec="seconds")
    past   = (taskmod._now_dt() - timedelta(hours=1)).isoformat(timespec="seconds")
    enqueue(Task(prompt="later", run_after=future))
    enqueue(Task(prompt="due",   run_after=past))

    due = list_pending()
    assert [t.prompt for _, t in due] == ["due"]


# ── timezone-aware scheduling ─────────────────────────────────────────────────

def test_now_is_tz_aware_and_honors_aria_tz(minimal_env, monkeypatch):
    from aria import task as taskmod
    monkeypatch.setenv("ARIA_TZ", "Europe/Madrid")
    stamp = taskmod._now()
    dt = datetime.fromisoformat(stamp)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == datetime.now(ZoneInfo("Europe/Madrid")).utcoffset()


def test_tz_aware_roundtrip(minimal_env, monkeypatch):
    from aria import task as taskmod
    from aria.task import Task
    monkeypatch.setenv("ARIA_TZ", "Europe/Madrid")
    future = (taskmod._now_dt() + timedelta(minutes=5)).isoformat(timespec="seconds")
    t2 = Task.from_text(Task(prompt="x", run_after=future).to_text())
    assert t2.run_after == future                       # aware ISO survives the file
    assert not t2.is_due()
    past = (taskmod._now_dt() - timedelta(minutes=5)).isoformat(timespec="seconds")
    assert Task.from_text(Task(prompt="x", run_after=past).to_text()).is_due()


def test_legacy_naive_timestamps_parse_as_local(minimal_env):
    from aria.task import Task
    naive_future = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    naive_past   = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    assert not Task(prompt="x", run_after=naive_future).is_due()
    assert Task(prompt="x", run_after=naive_past).is_due()
    # Recurrence on a naive (legacy) base stays naive-format — old files keep
    # their shape and are still interpreted as local time everywhere.
    nxt = Task(prompt="x", recur="daily", run_after=naive_past).next_run_after()
    assert datetime.fromisoformat(nxt).tzinfo is None


def test_recur_on_aware_base_returns_aware(minimal_env, monkeypatch):
    from aria import task as taskmod
    from aria.task import Task
    monkeypatch.setenv("ARIA_TZ", "Europe/Madrid")
    base = (taskmod._now_dt() - timedelta(days=2)).isoformat(timespec="seconds")
    nxt = Task(prompt="x", recur="daily", run_after=base).next_run_after()
    parsed = datetime.fromisoformat(nxt)
    assert parsed.tzinfo is not None
    assert parsed > taskmod._now_dt()                   # advanced past now, no burst
    assert parsed <= taskmod._now_dt() + timedelta(days=1)


def test_bad_aria_tz_falls_back_to_local(minimal_env, monkeypatch):
    from aria import task as taskmod
    monkeypatch.setenv("ARIA_TZ", "Not/AZone")
    assert datetime.fromisoformat(taskmod._now()).tzinfo is not None


# ── killable per-task timeout (run_with_timeout) ──────────────────────────────
# These target functions are module-level (not closures/lambdas) so they can be
# pickled and run by the child process under fork, spawn AND forkserver — the
# default start method varies by platform / Python version, and forkserver/spawn
# do not inherit the parent's in-memory state, so we can't rely on a monkeypatched
# Agent crossing the process boundary. run_with_timeout is therefore exercised
# with these trivial targets, WITHOUT the LLM.

def _target_fast(x):
    return f"result:{x}"


def _target_slow(seconds):
    time.sleep(seconds)              # killed on timeout — never returns
    return "should have been killed"


def _target_raise(msg):
    raise ValueError(msg)


def test_run_with_timeout_returns_value_within_timeout(minimal_env):
    from aria.supervisor import run_with_timeout
    assert run_with_timeout(_target_fast, ("hi",), 10) == "result:hi"


def test_run_with_timeout_disabled_runs_inline(minimal_env):
    from aria.supervisor import run_with_timeout
    # timeout <= 0 disables the ceiling → run inline, unbounded (reaper parity).
    assert run_with_timeout(_target_fast, ("x",), 0) == "result:x"
    assert run_with_timeout(_target_fast, ("y",), None) == "result:y"


def test_run_with_timeout_kills_overrunning_process(minimal_env):
    from aria.supervisor import run_with_timeout
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="ARIA_TASK_TIMEOUT"):
        run_with_timeout(_target_slow, (30,), 1)     # would sleep 30s if abandoned
    # Proves the process was actually killed, not awaited: a mere abandon-and-
    # move-on can't return before the 30s sleep completes.
    assert time.monotonic() - t0 < 5


def test_run_with_timeout_marshals_worker_exception(minimal_env):
    from aria.supervisor import run_with_timeout
    with pytest.raises(ValueError, match="kaboom"):
        run_with_timeout(_target_raise, ("kaboom",), 10)


# ── _execute orchestration (parent side) ──────────────────────────────────────
# _execute wraps the prompt, forwards the ARIA_TASK_TIMEOUT ceiling to
# run_with_timeout, and re-raises whatever the worker surfaced. The real killing
# is covered above; here we stub run_with_timeout to test the wiring without a
# child process (the Agent/LLM can't be mocked across the process boundary).

def test_execute_wraps_prompt_forwards_timeout_and_returns(minimal_env, monkeypatch):
    import aria.supervisor as sup
    from aria.task import Task
    monkeypatch.setenv("ARIA_TASK_TIMEOUT", "123")
    captured = {}

    def _fake_rwt(target, args, timeout):
        captured["target"] = target
        captured["prompt"] = args[0]
        captured["timeout"] = timeout
        return "all done"

    monkeypatch.setattr(sup, "run_with_timeout", _fake_rwt)
    assert sup._execute(Task(prompt="do it", notify=False)) == "all done"
    assert captured["target"] is sup._run_agent_task
    assert captured["timeout"] == 123
    assert "do it" in captured["prompt"]
    assert "Do NOT call the notify tool" in captured["prompt"]


def test_execute_propagates_timeout(minimal_env, monkeypatch):
    import aria.supervisor as sup
    from aria.task import Task

    def _fake_rwt(target, args, timeout):
        raise TimeoutError(f"task exceeded ARIA_TASK_TIMEOUT ({timeout}s)")

    monkeypatch.setattr(sup, "run_with_timeout", _fake_rwt)
    with pytest.raises(TimeoutError, match="ARIA_TASK_TIMEOUT"):
        sup._execute(Task(prompt="x", notify=False))


def test_execute_propagates_worker_exception(minimal_env, monkeypatch):
    import aria.supervisor as sup
    from aria.task import Task

    def _fake_rwt(target, args, timeout):
        raise ValueError("model exploded")

    monkeypatch.setattr(sup, "run_with_timeout", _fake_rwt)
    with pytest.raises(ValueError, match="model exploded"):
        sup._execute(Task(prompt="x", notify=False))


# ── supervisor tick end-to-end (reap → skip-backoff → run due) ────────────────

def test_tick_reaps_then_runs_due_tasks(minimal_env, monkeypatch):
    import aria.supervisor as sup
    from aria.task import (Task, enqueue, claim, complete, fail,
                           list_pending)
    monkeypatch.setenv("ARIA_TASK_TIMEOUT", "900")
    monkeypatch.setattr(sup, "_execute", lambda task: f"ran {task.prompt}")

    # One stale orphan in running/ (has retries → requeued with future backoff)
    orphan = Task(prompt="orphan", max_retries=2)
    _age_running_file(claim(enqueue(orphan), orphan), 2000)
    # One due task in pending/
    enqueue(Task(prompt="due-now", notify=False))

    supervisor = sup.Supervisor.__new__(sup.Supervisor)  # skip signal handlers
    supervisor._tick(list_pending, claim, complete, fail)

    assert list(_queue(minimal_env, "running").glob("*.task")) == []
    done = list(_queue(minimal_env, "done").glob("*.task"))
    assert len(done) == 1
    assert json.loads(done[0].read_text(encoding="utf-8"))["prompt"] == "due-now"
    # The reaped orphan sits in pending/ with a future run_after → not executed
    pending = list(_queue(minimal_env, "pending").glob("*.task"))
    assert len(pending) == 1
    t = Task.from_text(pending[0].read_text(encoding="utf-8"))
    assert t.prompt == "orphan" and _delay_seconds(t.run_after) > 0
