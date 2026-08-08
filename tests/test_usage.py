"""
Tests for persisted usage reporting (aria/usage.py) and the endpoint
system-role capability flag (LLM_SYSTEM_MESSAGES) that governs message layout.
"""

from __future__ import annotations


# ── usage aggregation ───────────────────────────────────────────────────────────

def test_format_report_empty(tmp_path):
    from aria import usage
    assert "No usage recorded" in usage.format_report(usage.load_usage(tmp_path / "none.jsonl"))


def test_load_usage_skips_malformed(tmp_path):
    from aria import usage
    p = tmp_path / "usage.jsonl"
    p.write_text(
        '{"model":"m1","channel":"repl","in":10,"out":5}\n'
        "not-json-garbage\n"
        '{"model":"m1","channel":"telegram:1","in":20,"out":7}\n'
    )
    recs = usage.load_usage(p)
    assert len(recs) == 2  # garbage line skipped


def test_summarize_totals_and_breakdowns():
    from aria import usage
    recs = [
        {"model": "m1", "channel": "repl", "in": 10, "out": 5},
        {"model": "m1", "channel": "telegram:1", "in": 20, "out": 7},
        {"model": "m2", "channel": "repl", "in": 3, "out": 1},
    ]
    s = usage.summarize(recs)
    assert s["calls"] == 3 and s["in"] == 33 and s["out"] == 13
    assert s["by_model"]["m1"]["in"] == 30 and s["by_model"]["m1"]["calls"] == 2
    assert s["by_channel"]["repl"]["calls"] == 2


def test_persist_usage_writes_jsonl(minimal_env, monkeypatch):
    """Agent._persist_usage appends a record readable by usage.load_usage."""
    from pathlib import Path
    from aria.agent import Agent
    from aria import usage

    a = Agent(terminal=False)
    a.model = "test-model"
    a._persist_usage(11, 4)
    recs = usage.load_usage(Path.home() / ".aria" / "usage.jsonl")
    assert recs and recs[-1]["in"] == 11 and recs[-1]["out"] == 4
    assert recs[-1]["model"] == "test-model"


# ── trailing system-message placement ────────────────────────────────────────────

def _capturing_client(native_client):
    inner = native_client("done")
    captured: dict = {}
    real = inner.chat.completions.create

    def _cap(**kw):
        captured["messages"] = kw["messages"]
        return real(**kw)

    inner.chat.completions.create = _cap
    return inner, captured


def test_system_supported_puts_context_in_trailing_system(minimal_env, native_client, monkeypatch):
    import aria.agent as ag
    a = ag.Agent(terminal=False)
    client, captured = _capturing_client(native_client)
    a.client = client
    a.history = [{"role": "user", "content": "hello"}]
    monkeypatch.setattr(ag, "_SUPPORTS_SYSTEM", True)
    a._call_model()
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["role"] == "system" and "Context" in msgs[-1]["content"]
    # the byte-stable prefix must not carry the volatile context
    assert "Context" not in msgs[0]["content"]


def test_system_unsupported_uses_leading_user_turn(minimal_env, native_client, monkeypatch):
    import aria.agent as ag
    a = ag.Agent(terminal=False)
    client, captured = _capturing_client(native_client)
    a.client = client
    a.history = [{"role": "user", "content": "hello"}]
    monkeypatch.setattr(ag, "_SUPPORTS_SYSTEM", False)
    a._call_model()
    msgs = captured["messages"]
    # no system role anywhere; prompt + context ride a leading user turn
    assert all(m["role"] != "system" for m in msgs)
    assert msgs[0]["role"] == "user" and "Context" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant"
