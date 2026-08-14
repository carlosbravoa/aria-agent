"""Tests for mid-task continuity: the trim anchor guard (a long tool marathon
must never wipe the live turn and produce a system-only request — the 400
'At least one non-system message is required' bug), the active-plan context
injection that lets 'continue' resume after an interruption, and the same-tool
thrash nudge."""

import json

import pytest


def _pairs(n, fat=1200, start=0):
    """n assistant(tool_calls)+tool reply pairs with fat tool output."""
    msgs = []
    for i in range(start, start + n):
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "shell_run",
                                                  "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                     "content": "x" * fat})
    return msgs


# ── Trim anchor guard ─────────────────────────────────────────────────────────

def test_trim_marathon_turn_keeps_user_anchor(minimal_env, monkeypatch):
    """30+ tool iterations in ONE turn: the count trim used to pop the turn's
    only user message, after which the boundary sweep drained the ENTIRE
    history → next request was system-only → provider 400."""
    from aria import agent as agent_mod
    from aria.agent import Agent
    monkeypatch.setattr(agent_mod, "_MAX_HISTORY", 60)
    a = Agent()
    a.history = list(a._seed) + [
        {"role": "user", "content": "write a report on the telegram issue"},
    ] + _pairs(35)                      # 71 messages — over _MAX_HISTORY
    a._trim_history()
    real = a.history[len(a._seed):]
    assert real, "history must never be drained mid-turn"
    assert real[0]["role"] == "user"
    assert "report" in real[0]["content"]


def test_trim_token_budget_stops_at_anchor(minimal_env, monkeypatch):
    """Token pressure alone must not pop the live turn's user message either."""
    from aria import agent as agent_mod
    from aria.agent import Agent
    monkeypatch.setattr(agent_mod, "_CONTEXT_TOKENS", 500)   # tiny budget
    a = Agent()
    a.history = list(a._seed) + [
        {"role": "user", "content": "the live task"},
    ] + _pairs(10)
    a._trim_history()
    real = a.history[len(a._seed):]
    assert real and real[0]["role"] == "user"
    assert real[0]["content"] == "the live task"


def test_trim_megaturn_drops_old_groups_keeps_last_pair(minimal_env, monkeypatch):
    """When the anchor is already first and the turn itself blows the budget,
    whole old assistant+tool groups are dropped — never splitting a pair —
    and the final group survives intact."""
    from aria import agent as agent_mod
    from aria.agent import Agent
    monkeypatch.setattr(agent_mod, "_CONTEXT_TOKENS", 800)
    a = Agent()
    a.history = list(a._seed) + [
        {"role": "user", "content": "task"},
    ] + _pairs(12)
    a._trim_history()
    real = a.history[len(a._seed):]
    assert real[0]["role"] == "user"
    # pairing invariant: every tool reply's id was introduced by the assistant
    # message directly before it
    for i, m in enumerate(real):
        if m["role"] == "tool":
            intro = real[i - 1]
            ids = {tc["id"] for tc in intro.get("tool_calls") or []}
            assert m["tool_call_id"] in ids
    # the final pair (c11) must survive
    assert any(m.get("tool_call_id") == "c11" for m in real)
    # some earlier groups were dropped and the elision is marked
    assert not any(m.get("tool_call_id") == "c0" for m in real)
    assert any("trimmed" in (m.get("content") or "") for m in real
               if m["role"] == "assistant")


def test_trim_still_drops_old_turns_before_anchor(minimal_env, monkeypatch):
    """Anchor protection must not stop normal trimming of PREVIOUS exchanges."""
    from aria import agent as agent_mod
    from aria.agent import Agent
    monkeypatch.setattr(agent_mod, "_MAX_HISTORY", 4)
    a = Agent()
    a.history = list(a._seed) + [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "another old one"},
        {"role": "assistant", "content": "another answer"},
        {"role": "user", "content": "the live task"},
    ]
    a._trim_history()
    real = a.history[len(a._seed):]
    assert real[0]["role"] == "user"
    assert real[-1]["content"] == "the live task"
    assert not any(m.get("content") == "old question" for m in real)


# ── Active-plan context injection ─────────────────────────────────────────────

def test_plan_context_block_lifecycle(minimal_env):
    from aria.tools import plan
    assert plan.context_block() == ""                       # no plan yet
    plan.execute({"todos": [{"task": "grep the journal", "status": "done"},
                            {"task": "write the report", "status": "in_progress"}]})
    block = plan.context_block()
    assert "write the report" in block and "1/2 done" in block
    plan.execute({"todos": [{"task": "grep the journal", "status": "done"},
                            {"task": "write the report", "status": "done"}]})
    assert plan.context_block() == ""                       # all done → silent
    plan.execute({"action": "clear"})
    assert plan.context_block() == ""


def test_active_plan_reaches_the_wire(minimal_env, native_client):
    """Every model request carries the unfinished plan in its trailing context
    message, so the agent can re-orient after an error/compaction/restart."""
    from aria.agent import Agent
    from aria.tools import plan
    plan.execute({"todos": [{"task": "finish the incident report",
                             "status": "in_progress"}]})
    a = Agent()
    inner = native_client("ok")
    sent = {}
    real_create = inner.chat.completions.create

    def record(**kwargs):
        sent["messages"] = kwargs["messages"]
        return real_create(**kwargs)

    inner.chat.completions.create = record
    a.client = inner
    a.history = [{"role": "user", "content": "continue"}]
    a._call_model()
    tail = sent["messages"][-1]
    assert tail["role"] == "system"
    assert "Active Plan" in tail["content"]
    assert "finish the incident report" in tail["content"]


# ── Same-tool thrash nudge ────────────────────────────────────────────────────

def test_same_tool_nudge_fires_every_nth_call(minimal_env, native_client, monkeypatch):
    """The nudge must be visible to the MODEL on the request right after the
    Nth call. (Asserting on final history would miss it: once later iterations
    land, the trim pass compresses old fat tool messages — including the nudged
    one — which is fine, the model has already read it.)"""
    from aria import agent as agent_mod
    from aria.agent import Agent
    monkeypatch.setattr(agent_mod, "_SAME_TOOL_NUDGE_EVERY", 2)
    a = Agent()
    # Three near-identical (but not exact) calls to the same tool, then done.
    inner = native_client(
        {"content": None, "tool_calls": [("plan", {"action": "show"})]},
        {"content": None, "tool_calls": [("plan", {"action": "clear"})]},
        {"content": None, "tool_calls": [("plan", {"action": "show", "x": 1})]},
        "done",
    )
    requests = []
    real_create = inner.chat.completions.create

    def record(**kwargs):
        requests.append(kwargs["messages"])
        return real_create(**kwargs)

    inner.chat.completions.create = record
    a.client = inner
    a.chat_collect("hammer away")

    def tool_contents(msgs):
        return [m["content"] for m in msgs if m.get("role") == "tool"]

    # Request 3 (after 2 plan calls) carries the nudge; request 2 does not.
    assert any("call #2 to `plan`" in c for c in tool_contents(requests[2]))
    assert not any("call #" in c for c in tool_contents(requests[1]))
