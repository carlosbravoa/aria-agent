"""
Agent ReAct-loop tests with a mocked streaming client (no network, no LLM):
- REMEMBER/LEARN interception + stripping from the user-facing reply
- chat_collect vs chat_yield return shapes
- tool call via heredoc round-trips through the loop with parsed args
- the repeated-identical-call dedup guard terminates the loop
"""

from __future__ import annotations

import pytest


def _agent():
    from aria.agent import Agent
    return Agent()


def test_remember_saved_and_stripped(minimal_env, mock_client):
    a = _agent()
    a.client = mock_client("REMEMBER: User's name is Carlos.\nNice to meet you!")
    out = a.chat_collect("my name is Carlos")
    assert "REMEMBER:" not in out
    assert "Nice to meet you!" in out
    assert "User's name is Carlos" in a.ws.load_memory()


def test_learn_saved_and_stripped(minimal_env, mock_client):
    a = _agent()
    a.client = mock_client("LEARN: Use Jira project ABC.\nGot it.")
    out = a.chat_collect("note this")
    assert "LEARN:" not in out
    assert "Got it." in out
    assert "Use Jira project ABC" in (a.ws.load_operational_memory() or "")


def test_chat_collect_returns_text(minimal_env, mock_client):
    a = _agent()
    a.client = mock_client("The answer is 4.")
    assert a.chat_collect("2+2?") == "The answer is 4."


def test_chat_yield_returns_list(minimal_env, mock_client):
    a = _agent()
    a.client = mock_client("Hello there.")
    out = a.chat_yield("hi")
    assert isinstance(out, list)
    assert out == ["Hello there."]


def test_tool_call_heredoc_roundtrip(minimal_env, mock_client, monkeypatch):
    a = _agent()
    call = (
        'TOOL: shell_run\nINPUT: {"action": "run"}\n'
        'ARG script <<<\necho "{x}"\nfor i in 1 2; do echo $i; done\n>>>'
    )
    a.client = mock_client(call, "Done — ran your script.")

    captured = {}
    def fake_exec(name, args):
        captured["name"] = name
        captured["args"] = args
        return "1\n2"
    monkeypatch.setattr(a, "_execute_tool", fake_exec)

    out = a.chat_collect("run a script")
    assert captured["name"] == "shell_run"
    assert captured["args"]["action"] == "run"
    assert 'echo "{x}"' in captured["args"]["script"]   # braces/quotes intact
    assert "for i in 1 2" in captured["args"]["script"]
    assert out == "Done — ran your script."


def test_repeated_identical_call_is_deduped(minimal_env, mock_client, monkeypatch):
    a = _agent()
    # mock_client repeats the last (only) response forever → same tool call again
    call = 'TOOL: shell_run\nINPUT: {"action": "run"}\nARG script <<<\nls\n>>>'
    a.client = mock_client(call)

    calls = []
    monkeypatch.setattr(a, "_execute_tool", lambda n, ar: calls.append((n, ar)) or "out")

    out = a.chat_collect("go")
    # The identical second call must be blocked by the seen_calls guard, so the
    # tool executes exactly once and the loop terminates (no loop-limit spin).
    assert len(calls) == 1
    assert out  # returns some terminal message, doesn't hang


def test_pretool_preamble_not_in_response(minimal_env, mock_client, monkeypatch):
    """Text before a TOOL: call is internal reasoning, never delivered."""
    a = _agent()
    resp = (
        "Let me check that for you.\n"
        'TOOL: shell_run\nINPUT: {"action": "run"}\nARG script <<<\nls\n>>>'
    )
    a.client = mock_client(resp, "Here are the files.")
    monkeypatch.setattr(a, "_execute_tool", lambda n, ar: "a.py b.py")
    out = a.chat_collect("list files")
    assert "Let me check that for you" not in out
    assert out == "Here are the files."


def test_content_before_side_effect_tool_is_delivered(minimal_env, mock_client, monkeypatch):
    """The answer written BEFORE a notify/send/schedule tool must reach the
    caller (supervisor/Telegram) — regression for the 'briefing sent but no
    content' bug (side-effect pre-tool capture)."""
    a = _agent()
    assert "notify" in a._classify_side_effect_tools()
    briefing = "Briefing:\n- 9am standup\n- 2pm review"
    a.client = mock_client(f'{briefing}\nTOOL: notify\nINPUT: {{"message": "sent"}}',
                           "Briefing sent.")
    monkeypatch.setattr(a, "_execute_tool", lambda n, ar: "[notify] Message sent.")
    out = a.chat_collect("send my briefing")
    assert "9am standup" in out          # the content survives, not just the wrap-up


def test_preamble_before_data_tool_is_not_delivered(minimal_env, mock_client, monkeypatch):
    """Reasoning text before a DATA tool (web_fetch) stays internal — ordering
    fix preserved, the side-effect capture must not over-trigger."""
    a = _agent()
    a.client = mock_client('Let me check that.\nTOOL: web_fetch\nINPUT: {"url": "https://x.com"}',
                           "The page says hello.")
    monkeypatch.setattr(a, "_execute_tool", lambda n, ar: "hello")
    out = a.chat_collect("what does x say")
    assert "Let me check" not in out
    assert out == "The page says hello."


def test_multiline_notify_message_executes(minimal_env, mock_client, monkeypatch):
    """A notify call whose message has literal newlines must actually run — the
    multi-line briefing previously failed to parse and silently never sent."""
    a = _agent()
    call = ('TOOL: notify\nINPUT: {"message": "Briefing:\n- 9am standup\n- 2pm review"}\n'
            'RESULT: [notify] Message sent.\n✅ Done.')
    a.client = mock_client(call, "✅ Done.")
    sent = []
    monkeypatch.setattr(a, "_execute_tool",
                        lambda n, ar: (sent.append((n, ar.get("message", ""))) or "[notify] Message sent."))
    a.chat_collect("send briefing")
    assert sent and sent[0][0] == "notify"
    assert "9am standup" in sent[0][1]      # full briefing actually reached notify
