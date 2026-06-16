"""
Reflection tests. The no-new-sessions path runs the full reflect.run() body
(config + workspace + watermark) WITHOUT touching the LLM, so it executes the
exact code that was broken when the `def run(...)` line went missing.
"""

from __future__ import annotations


def test_run_with_no_sessions_returns_cleanly(minimal_env):
    from aria import reflect
    # Fresh workspace → no session logs → run() must short-circuit before any
    # OpenAI client construction and return a status string (not raise).
    result = reflect.run(notify=False)
    assert isinstance(result, str)
    assert "no new sessions" in result.lower()


def test_run_default_notify_arg(minimal_env):
    from aria import reflect
    # callers also invoke run() positionally / with default — must not require args
    assert isinstance(reflect.run(), str)
