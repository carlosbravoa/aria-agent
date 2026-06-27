"""
Agent native ReAct-loop tests with a mocked non-streaming client (no network,
no LLM):
- remember/learn tools persist to memory
- chat_collect vs chat_yield return shapes
- a tool call round-trips through the loop with parsed args
- multiple tool_calls in one turn each get a tool reply (mandatory list handling)
- the repeated-identical-call dedup guard terminates the loop
- content accompanying a delivering tool is surfaced; before a data tool it is not
"""

from __future__ import annotations

import pytest


def _agent():
    from aria.agent import Agent
    return Agent()


def test_remember_tool_saves_and_answers(minimal_env, native_client):
    a = _agent()
    a.client = native_client(
        {"tool_calls": [("remember", {"fact": "User's name is Carlos."})]},
        "Nice to meet you!",
    )
    out = a.chat_collect("my name is Carlos")
    assert "Nice to meet you!" in out
    assert "User's name is Carlos" in a.ws.load_memory()


def test_learn_tool_saves(minimal_env, native_client):
    a = _agent()
    a.client = native_client(
        {"tool_calls": [("learn", {"procedure": "Use Jira project ABC."})]},
        "Got it.",
    )
    out = a.chat_collect("note this")
    assert "Got it." in out
    assert "Use Jira project ABC" in (a.ws.load_operational_memory() or "")


def test_chat_collect_returns_text(minimal_env, native_client):
    a = _agent()
    a.client = native_client("The answer is 4.")
    assert a.chat_collect("2+2?") == "The answer is 4."


def test_chat_yield_returns_list(minimal_env, native_client):
    a = _agent()
    a.client = native_client("Hello there.")
    out = a.chat_yield("hi")
    assert isinstance(out, list)
    assert out == ["Hello there."]


def test_tool_call_roundtrip(minimal_env, native_client, monkeypatch):
    a = _agent()
    script = 'echo "{x}"\nfor i in 1 2; do echo $i; done'
    a.client = native_client(
        {"tool_calls": [("shell_run", {"action": "run", "script": script})]},
        "Done — ran your script.",
    )

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


def test_multiple_tool_calls_each_get_a_reply(minimal_env, native_client, monkeypatch):
    """Mandatory native invariant: when one turn returns N tool_calls, every
    tool_call_id must get its own `tool` message before the next request."""
    a = _agent()
    a.client = native_client(
        {"tool_calls": [
            ("web_fetch", {"url": "https://a.example"}),
            ("web_fetch", {"url": "https://b.example"}),
        ]},
        "Compared both pages.",
    )
    monkeypatch.setattr(a, "_execute_tool", lambda n, ar: f"content of {ar['url']}")
    out = a.chat_collect("compare a and b")
    assert out == "Compared both pages."

    tool_msgs = [m for m in a.history if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    asst_with_calls = [m for m in a.history
                       if m.get("role") == "assistant" and m.get("tool_calls")]
    ids = {tc["id"] for m in asst_with_calls for tc in m["tool_calls"]}
    replied = {m["tool_call_id"] for m in tool_msgs}
    assert ids == replied            # no orphaned call, no orphaned reply


def test_chat_yield_streams_responses_via_callback(minimal_env, native_client, monkeypatch):
    """response_cb fires for each user-facing response as produced, not batched."""
    a = _agent()
    a.client = native_client("Here is your answer.")
    streamed = []
    out = a.chat_yield("go", response_cb=streamed.append)
    assert streamed == ["Here is your answer."]      # streamed mid-turn
    assert out == ["Here is your answer."]            # and still returned


def test_chat_yield_emits_tool_activity(minimal_env, native_client, monkeypatch):
    """activity_cb gets a compact per-tool progress line (name + ✓/✗)."""
    a = _agent()
    a.client = native_client(
        {"tool_calls": [("shell_run", {"action": "run", "command": "ls"})]},
        "done",
    )
    monkeypatch.setattr(a, "_execute_tool", lambda n, ar: "file1\nfile2")
    activity = []
    a.chat_yield("list files", activity_cb=activity.append)
    assert any(d.startswith("shell_run ✓") for d in activity)


def test_loop_limit_delivers_message_to_channels(minimal_env, native_client, monkeypatch):
    """Hitting the loop limit must surface a message (it used to go to a
    discarded buffer, leaving channel users with a silent '(no response)')."""
    import aria.agent as agent_mod
    monkeypatch.setattr(agent_mod, "_MAX_LOOPS", 2)
    a = _agent()
    # Each turn returns a DISTINCT tool call so the dedup guard never trips and
    # the loop runs until the limit.
    seq = [{"tool_calls": [("shell_run", {"action": "run", "i": i})]} for i in range(5)]
    a.client = native_client(*seq)
    monkeypatch.setattr(a, "_execute_tool", lambda n, ar: "out")
    out = a.chat_yield("go")
    assert len(out) == 1
    assert "stopped after" in out[0].lower()


def test_repeated_identical_call_is_deduped(minimal_env, native_client, monkeypatch):
    a = _agent()
    # native_client repeats the last turn forever → identical tool call again
    a.client = native_client({"tool_calls": [("shell_run", {"action": "run"})]})

    calls = []
    monkeypatch.setattr(a, "_execute_tool", lambda n, ar: calls.append((n, ar)) or "out")

    out = a.chat_collect("go")
    # The identical second call must be blocked by the seen_calls guard, so the
    # tool executes exactly once and the loop terminates (no loop-limit spin).
    assert len(calls) == 1
    assert out  # returns some terminal message, doesn't hang


def test_pretool_content_before_data_tool_not_delivered(minimal_env, native_client, monkeypatch):
    """Content accompanying a DATA tool call is internal reasoning, never
    delivered — preserves channel ordering."""
    a = _agent()
    a.client = native_client(
        {"content": "Let me check that for you.",
         "tool_calls": [("shell_run", {"action": "run"})]},
        "Here are the files.",
    )
    monkeypatch.setattr(a, "_execute_tool", lambda n, ar: "a.py b.py")
    out = a.chat_collect("list files")
    assert "Let me check that for you" not in out
    assert out == "Here are the files."


def test_content_with_side_effect_tool_is_delivered(minimal_env, native_client, monkeypatch):
    """The answer written alongside a notify/send/schedule tool must reach the
    caller (supervisor/Telegram) — regression for the 'briefing sent but no
    content' bug."""
    a = _agent()
    assert "notify" in a._classify_side_effect_tools()
    briefing = "Briefing:\n- 9am standup\n- 2pm review"
    a.client = native_client(
        {"content": briefing, "tool_calls": [("notify", {"message": "sent"})]},
        "Briefing sent.",
    )
    monkeypatch.setattr(a, "_execute_tool", lambda n, ar: "[notify] Message sent.")
    out = a.chat_collect("send my briefing")
    assert "9am standup" in out          # the content survives, not just the wrap-up


def test_content_with_memory_tool_is_delivered(minimal_env, native_client):
    """Content accompanying a remember/learn call IS the user-facing answer."""
    a = _agent()
    a.client = native_client(
        {"content": "Nice to meet you, Carlos!",
         "tool_calls": [("remember", {"fact": "Name is Carlos."})]},
        "",
    )
    out = a.chat_collect("I'm Carlos")
    assert "Nice to meet you, Carlos!" in out


def test_multiline_tool_arg_executes(minimal_env, native_client, monkeypatch):
    """A notify call whose message has literal newlines must reach the tool
    intact (trivially true natively — args are real JSON, not hand-written)."""
    a = _agent()
    a.client = native_client(
        {"tool_calls": [("notify", {"message": "Briefing:\n- 9am standup\n- 2pm review"})]},
        "✅ Done.",
    )
    sent = []
    monkeypatch.setattr(a, "_execute_tool",
                        lambda n, ar: (sent.append((n, ar.get("message", ""))) or "[notify] Message sent."))
    a.chat_collect("send briefing")
    assert sent and sent[0][0] == "notify"
    assert "9am standup" in sent[0][1]      # full briefing actually reached notify


def _concurrency_probe():
    """Returns (fake_execute, state). state['max'] is the peak number of
    fake_execute calls running at once — 2 proves concurrency, 1 proves serial."""
    import threading
    import time
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fake(name, args):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return f"ok:{name}"

    return fake, state


def test_parallel_safe_batch_runs_concurrently(minimal_env, native_client, monkeypatch):
    """Two PARALLEL_SAFE calls (web_fetch) in one turn run at the same time."""
    a = _agent()
    a.client = native_client(
        {"tool_calls": [("web_fetch", {"url": "a"}), ("web_fetch", {"url": "b"})]},
        "done",
    )
    fake, state = _concurrency_probe()
    monkeypatch.setattr(a, "_execute_tool", fake)
    out = a.chat_collect("fetch both")
    assert out == "done"
    assert state["max"] == 2                       # both in flight at once
    assert len([m for m in a.history if m.get("role") == "tool"]) == 2


def test_mixed_batch_runs_sequentially(minimal_env, native_client, monkeypatch):
    """A batch with any non-PARALLEL_SAFE tool (shell_run) runs serially."""
    a = _agent()
    a.client = native_client(
        {"tool_calls": [("web_fetch", {"url": "a"}), ("shell_run", {"action": "run"})]},
        "done",
    )
    fake, state = _concurrency_probe()
    monkeypatch.setattr(a, "_execute_tool", fake)
    out = a.chat_collect("do both")
    assert out == "done"
    assert state["max"] == 1                        # never concurrent


class TestResumeDropsInterruptedExchange:
    """A session that breaks mid-task (error sentinel / loop-limit / closed
    mid-run) writes the user turn to the window but never an assistant reply.
    On resume that dangling user turn must NOT be seeded as a pending request,
    or the model hijacks the next message to resume the unfinished task."""

    def _seed_window(self, ws_path, turns):
        from aria.workspace import Workspace
        ws = Workspace(ws_path)
        ws.set_window_key("repl")
        for role, content in turns:
            ws.append_conversation_window(role, content, "Aria")

    def test_dangling_user_turn_dropped(self, minimal_env):
        from aria.agent import Agent
        self._seed_window(minimal_env, [
            ("user", "hello"),
            ("assistant", "hi there"),
            ("user", "do the long broken task"),   # interrupted — no reply
        ])
        a = Agent()
        contents = [m["content"] for m in a.history]
        assert "do the long broken task" not in contents
        assert contents == ["hello", "hi there"]
        assert a.history[-1]["role"] == "assistant"   # well-formed past context

    def test_clean_window_preserved(self, minimal_env):
        from aria.agent import Agent
        self._seed_window(minimal_env, [
            ("user", "hello"),
            ("assistant", "hi there"),
        ])
        a = Agent()
        contents = [m["content"] for m in a.history]
        assert contents == ["hello", "hi there"]   # nothing over-trimmed

    def test_user_only_window_seeds_empty(self, minimal_env):
        from aria.agent import Agent
        self._seed_window(minimal_env, [("user", "the only, interrupted message")])
        a = Agent()
        assert a.history == []   # no pending turn to resume
