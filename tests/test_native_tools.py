"""
Native tool-engine unit tests (no network, no LLM):
- the remember/learn tools persist to the right memory files
- _looks_like_error classifies tool result strings for the ✓/✗ activity icon
- _arg_preview produces a compact, truncated single-line preview
- _wire_schemas strips internal registry keys before they reach the provider
- _assistant_msg serializes a reply into a well-formed wire dict
"""

from __future__ import annotations

import pytest


def test_remember_tool_writes_core_memory(minimal_env):
    from aria.tools import remember
    out = remember.execute({"fact": "User prefers metric units."})
    assert "Saved" in out
    from aria.workspace import Workspace
    assert "metric units" in Workspace(minimal_env).load_memory()


def test_learn_tool_writes_operational_memory(minimal_env):
    from aria.tools import learn
    out = learn.execute({"procedure": "Deploy with make release."})
    assert "Saved" in out
    from aria.workspace import Workspace
    assert "make release" in (Workspace(minimal_env).load_operational_memory() or "")


def test_remember_tool_rejects_empty(minimal_env):
    from aria.tools import remember
    assert "No fact" in remember.execute({"fact": "   "})


@pytest.mark.parametrize("result, is_error", [
    ("[notify] Message sent.", False),
    ("[remember] Saved to core memory: x", False),
    ("[notify error] boom", True),
    ("[shell_run] error: nonzero exit", True),
    ("[agent] Could not parse arguments for x: y", True),
    ("Here is a normal answer.", False),
    ("", False),
])
def test_looks_like_error(result, is_error):
    from aria.agent import _looks_like_error
    assert _looks_like_error(result) is is_error


def test_arg_preview_truncates_and_flattens():
    from aria.agent import Agent
    long = {"script": "for i in 1 2 3; do echo a-very-long-line-of-code $i; done\nmore"}
    p = Agent._arg_preview(long)
    assert "\n" not in p
    assert len(p) <= 51            # 50 chars + ellipsis
    assert Agent._arg_preview({}) == ""


def test_parallel_safe_flag_surfaced(minimal_env):
    from aria import tools
    schemas = {t["function"]["name"]: t for t in tools.load_all()}
    # read/stateless tools opt in; everything else defaults False
    assert schemas["web_fetch"]["parallel_safe"] is True
    assert schemas["shell_run"]["parallel_safe"] is False
    assert schemas["remember"]["parallel_safe"] is False


def test_wire_schemas_strips_internal_keys(minimal_env):
    from aria.agent import Agent
    a = Agent()
    wire = a._wire_schemas()
    assert wire, "no tools wired"
    for t in wire:
        assert set(t.keys()) == {"type", "function"}     # no `_module`, no extras
        assert t["type"] == "function"
        assert "name" in t["function"]


def test_assistant_msg_shapes_tool_calls():
    from aria.agent import Agent

    class _Fn:
        def __init__(self, n, a): self.name = n; self.arguments = a

    class _TC:
        def __init__(self, i, n, a): self.id = i; self.function = _Fn(n, a)

    class _Msg:
        content = "thinking"

    tcs = [_TC("call_1", "web_fetch", '{"url": "x"}')]
    msg = Agent._assistant_msg(_Msg(), tcs)
    assert msg["role"] == "assistant"
    assert msg["content"] == "thinking"
    assert msg["tool_calls"][0]["id"] == "call_1"
    assert msg["tool_calls"][0]["type"] == "function"
    assert msg["tool_calls"][0]["function"]["name"] == "web_fetch"

    # No tool calls → plain content message, no tool_calls key.
    plain = Agent._assistant_msg(_Msg(), [])
    assert "tool_calls" not in plain
