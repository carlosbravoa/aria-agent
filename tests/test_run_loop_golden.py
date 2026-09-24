"""
Golden-trace characterization of Agent._run_loop.

Each scenario scripts the model and the tools, runs one turn, and records
everything observable: every request's messages (minus the per-minute context
block), the delivered responses, the conversation window and the friction log.
The trace must match tests/golden/run_loop.json exactly, so a refactor of the
loop can't silently change behaviour.

Regenerate ONLY for an intended behaviour change:
    ARIA_REGEN_GOLDEN=1 pytest tests/test_run_loop_golden.py
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden" / "run_loop.json"


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _TC:
    def __init__(self, id_, name, args):
        self.id, self.type = id_, "function"
        self.function = _Fn(name, json.dumps(args, sort_keys=True))


class _Msg:
    def __init__(self, content, tcs):
        self.content, self.tool_calls = content, tcs or None


class _Client:
    """`script(i)` returns the i-th model turn: a str (final answer) or
    (content, [(name, args), ...])."""

    def __init__(self, script):
        self.script, self.requests, self._i = script, [], 0
        self._ids = itertools.count(1)
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kw):
        self.requests.append([dict(m) for m in kw["messages"]])
        turn = self.script(self._i)
        self._i += 1
        if isinstance(turn, str):
            content, calls = turn, []
        else:
            content, calls = turn
        tcs = [_TC(f"call_{next(self._ids)}", n, a) for n, a in calls]
        return type("R", (), {"choices": [type("C", (), {"message": _Msg(content, tcs)})()],
                              "usage": None})()


def _scrub(messages):
    out = []
    for m in messages:
        if m.get("role") == "system":
            continue                      # system prompt + per-minute context
        m = {k: v for k, v in m.items() if k in ("role", "content", "tool_call_id", "tool_calls")}
        if m.get("tool_calls"):
            m["tool_calls"] = [
                {"id": tc["id"], "name": tc["function"]["name"],
                 "arguments": tc["function"]["arguments"]}
                for tc in m["tool_calls"]]
        out.append(m)
    return out


def _tools_ok(name, args):
    return f"{name} result for {json.dumps(args, sort_keys=True)}"


def _tools_fail(name, args):
    return f"[{name} error] boom {args.get('n', '')}"


SCENARIOS = {
    "final_answer": (lambda i: "Hello!", _tools_ok, {}),
    "data_tool_preamble_hidden": (
        lambda i: [("Let me check.", [("shell_run", {"command": "ls"})]), "Two files."][min(i, 1)],
        _tools_ok, {}),
    "deliver_tool_content_delivered": (
        lambda i: [("Here is your briefing.", [("notify", {"message": "b"})]), "Sent."][min(i, 1)],
        _tools_ok, {}),
    "identical_call_hard_stop": (
        lambda i: (None, [("shell_run", {"command": "ls"})]), _tools_ok, {}),
    "loop_limit": (
        lambda i: (None, [("shell_run", {"command": f"echo {i}"})]), _tools_ok,
        {"_MAX_LOOPS": 4, "_SAME_TOOL_NUDGE_EVERY": 0}),
    "thrash_nudge": (
        lambda i: (None, [("shell_run", {"command": f"echo {i}"})]) if i < 4 else "done",
        _tools_ok, {"_SAME_TOOL_NUDGE_EVERY": 2}),
    "broken_tool_and_friction": (
        lambda i: (None, [("shell_run", {"n": i})]) if i < 5 else "gave up",
        _tools_fail, {"_TOOL_BROKEN_AFTER": 2, "_FRICTION_MIN_CALLS": 3,
                      "_SAME_TOOL_NUDGE_EVERY": 0}),
    "parallel_batch": (
        lambda i: [(None, [("web_fetch", {"url": "https://a.example"}),
                           ("web_fetch", {"url": "https://b.example"})]), "Both fetched."][min(i, 1)],
        _tools_ok, {}),
    "mixed_batch_sequential": (
        lambda i: [(None, [("web_fetch", {"url": "https://a.example"}),
                           ("shell_run", {"command": "ls"})]), "ok"][min(i, 1)],
        _tools_ok, {}),
    "repeat_then_recover": (
        lambda i: [(None, [("shell_run", {"command": "ls"})]),
                   (None, [("shell_run", {"command": "ls"})]),
                   "recovered"][min(i, 2)],
        _tools_ok, {}),
    "empty_final_answer": (lambda i: "", _tools_ok, {}),
}


def _run(name, minimal_env, monkeypatch):
    from aria import agent as agent_mod
    script, tool_fn, consts = SCENARIOS[name]
    for k, v in consts.items():
        monkeypatch.setattr(agent_mod, k, v)
    a = agent_mod.Agent(window_key="golden", terminal=False)
    client = _Client(script)
    a.client = client
    a._execute_tool = tool_fn
    out = a.chat_yield("please do the thing")
    window = a.ws._window_path()
    friction = minimal_env / "memory" / "friction_log.md"
    return {
        "responses": out,
        "requests": [_scrub(r) for r in client.requests],
        "window": window.read_text(encoding="utf-8") if window.exists() else "",
        "friction": [ln.split("] ", 1)[-1] for ln in
                     (friction.read_text(encoding="utf-8").splitlines() if friction.exists() else [])
                     if "calls=" in ln],
    }


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_run_loop_matches_golden(name, minimal_env, monkeypatch):
    trace = json.loads(json.dumps(_run(name, minimal_env, monkeypatch)))
    golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if os.environ.get("ARIA_REGEN_GOLDEN"):
        golden[name] = trace
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
        return
    assert name in golden, "no golden trace — run with ARIA_REGEN_GOLDEN=1"
    assert trace == golden[name]
