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
    assert "\n" not in p            # only the first line, flattened
    assert len(p) <= 101            # 100 chars + ellipsis
    assert Agent._arg_preview({}) == ""


def test_arg_preview_prefers_salient_field():
    """The header should show WHAT is being run, not generic key=value noise."""
    from aria.agent import Agent
    assert Agent._arg_preview({"action": "run", "command": "pytest -q"}) == "pytest -q"
    assert Agent._arg_preview({"url": "https://x.com", "raw": False}) == "https://x.com"
    # falls back to flattened preview when no salient key is present
    assert "foo=bar" in Agent._arg_preview({"foo": "bar", "baz": "qux"})


def test_call_signature_normalizes_argument_order():
    """The repeat guard must catch a re-serialized call with reordered keys —
    otherwise a 'duplicate' Jira create slips through and runs twice."""
    from aria.agent import Agent
    from types import SimpleNamespace
    def tc(name, args):
        return SimpleNamespace(function=SimpleNamespace(name=name, arguments=args))
    a = tc("jira", '{"action":"create","summary":"X"}')
    b = tc("jira", '{"summary": "X", "action": "create"}')   # reordered + spaces
    assert Agent._call_signature([a]) == Agent._call_signature([b])
    c = tc("jira", '{"action":"create","summary":"Y"}')       # genuinely different
    assert Agent._call_signature([a]) != Agent._call_signature([c])


# ── REPL conveniences: spinner verbs, /retry, /compact, !shell, /copy ─────────

def test_spinner_label_is_action_aware(minimal_env):
    from aria.agent import Agent
    a = Agent()
    assert a._spinner_label("shell_run", {"command": "pytest"}, "pytest").startswith("[dim]⚙ Running")
    assert "Fetching" in a._spinner_label("web_fetch", {}, "example.com")
    assert "Reading" in a._spinner_label("file_access", {"action": "read"}, "x.py")
    assert "Editing" in a._spinner_label("file_access", {"action": "patch"}, "x.py")


def test_retry_last_rewinds_to_last_user(minimal_env):
    from aria.agent import Agent
    a = Agent()
    a.history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ans1"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "ans2"},
    ]
    txt = a.retry_last()
    assert txt == "second"
    assert a.history == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ans1"},
    ]
    # empty history → nothing to retry
    a.history = []
    assert a.retry_last() is None


def test_compact_replaces_history_with_summary(minimal_env, native_client):
    from aria.agent import Agent
    a = Agent()
    a.client = native_client("- discussed X\n- decided Y")
    a.history = [
        {"role": "user", "content": "let's talk about X"},
        {"role": "assistant", "content": "sure, X is ..."},
    ]
    summary = a.compact()
    assert "decided Y" in summary
    assert len(a.history) == 2                       # collapsed to summary scaffold
    assert a.history[0]["role"] == "user"
    assert "Summary of earlier conversation" in a.history[0]["content"]


def test_compact_noop_on_short_history(minimal_env):
    from aria.agent import Agent
    a = Agent()
    a.history = [{"role": "user", "content": "hi"}]
    assert a.compact().startswith("[compact]")


def test_copy_to_clipboard_returns_false_without_tool(monkeypatch):
    from aria import main as M
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert M._copy_to_clipboard("hello") is False


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


def _frag(index, id=None, name=None, args=None):
    """Build a fake streamed delta.tool_calls fragment."""
    from types import SimpleNamespace
    fn = SimpleNamespace(name=name, arguments=args) if (name or args) else None
    return SimpleNamespace(index=index, id=id, function=fn)


def test_streamed_tool_call_assembles_from_fragments():
    """Phase 3: id/name arrive once; arguments stream in pieces across deltas."""
    import json
    from aria.agent import Agent
    frags = {}
    Agent._accumulate_tool_frags(frags, [_frag(0, id="call_x", name="web_fetch", args='{"url"')])
    Agent._accumulate_tool_frags(frags, [_frag(0, args=': "https://a"}')])
    msg = Agent._assemble_streamed(["Here you go."], frags)
    assert msg.content == "Here you go."
    assert len(msg.tool_calls) == 1
    tc = msg.tool_calls[0]
    assert tc.id == "call_x"
    assert tc.function.name == "web_fetch"
    assert json.loads(tc.function.arguments) == {"url": "https://a"}


def test_streamed_content_only_has_no_tool_calls():
    from aria.agent import Agent
    msg = Agent._assemble_streamed(["a", "b", "c"], {})
    assert msg.content == "abc"
    assert msg.tool_calls is None


def test_streamed_multiple_calls_kept_in_index_order():
    from aria.agent import Agent
    frags = {}
    Agent._accumulate_tool_frags(frags, [_frag(1, id="b", name="t2", args="{}")])
    Agent._accumulate_tool_frags(frags, [_frag(0, id="a", name="t1", args="{}")])
    msg = Agent._assemble_streamed([], frags)
    assert [tc.id for tc in msg.tool_calls] == ["a", "b"]   # sorted by index


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


# ── REPL UX: token usage, soft-interrupt cleanup, @file mentions ──────────────

def test_record_usage_accumulates(minimal_env):
    from types import SimpleNamespace
    from aria.agent import Agent
    a = Agent()
    a._record_usage(SimpleNamespace(prompt_tokens=10, completion_tokens=4))
    a._record_usage(SimpleNamespace(prompt_tokens=5,  completion_tokens=1))
    a._record_usage(None)  # endpoints without usage leave the count flat
    assert a._session_tokens == {"in": 15, "out": 5}


def test_finalize_interrupt_drops_dangling_tool_calls(minimal_env):
    """A trailing assistant tool_calls msg with no tool replies would break the
    next request — finalize must strip it so the session stays usable."""
    from aria.agent import Agent
    a = Agent()
    a._is_terminal = False
    a.history = [{"role": "user", "content": "hi"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}]
    a._finalize_interrupt()
    assert a.history[-1]["role"] == "user"   # empty dangling assistant dropped


def test_finalize_interrupt_cleans_partial_tool_batch(minimal_env):
    from aria.agent import Agent
    a = Agent()
    a._is_terminal = False
    a.history = [{"role": "user", "content": "hi"},
                 {"role": "assistant", "content": "working",
                  "tool_calls": [{"id": "1"}, {"id": "2"}]},
                 {"role": "tool", "tool_call_id": "1", "content": "x"}]
    a._finalize_interrupt()
    # the stray tool reply is dropped and the unsatisfied tool_calls stripped,
    # but the assistant's content is preserved (it isn't empty).
    assert a.history[-1]["role"] == "assistant"
    assert "tool_calls" not in a.history[-1]
    assert a.history[-1]["content"] == "working"


def test_expand_mentions_attaches_and_flags(tmp_path):
    from aria import main as M
    p = tmp_path / "note.txt"
    p.write_text("hello world contents")
    out = M._expand_mentions(f"summarize @{p} please")
    assert "--- Attached files ---" in out
    assert "hello world contents" in out
    # missing files are flagged, not silently dropped
    assert "no such file" in M._expand_mentions("@/no/such/file.txt")
    # prose without a mention is returned unchanged
    assert M._expand_mentions("just a question") == "just a question"


def test_make_diff_reports_changes():
    from aria.agent import Agent
    assert Agent._make_diff("same", "same") is None          # unchanged → no diff
    lines, total = Agent._make_diff("a\nb\nc", "a\nB\nc")
    joined = "\n".join(lines)
    assert "-b" in joined and "+B" in joined
    assert total == len(lines)


def test_make_diff_caps_lines():
    from aria.agent import Agent
    old = "\n".join(str(i) for i in range(200))
    new = "\n".join(str(i) + "x" for i in range(200))
    lines, total = Agent._make_diff(old, new, max_lines=10)
    assert len(lines) == 10 and total > 10   # capped for display, total preserved


def test_file_edit_target_only_for_terminal_mutations(minimal_env):
    from aria.agent import Agent
    a = Agent()
    a._is_terminal = True
    assert a._file_edit_target("file_access", {"action": "write", "path": "~/x"}) is not None
    assert a._file_edit_target("file_access", {"action": "read", "path": "~/x"}) is None
    assert a._file_edit_target("shell_run", {"action": "write", "path": "~/x"}) is None
    a._is_terminal = False
    assert a._file_edit_target("file_access", {"action": "write", "path": "~/x"}) is None
