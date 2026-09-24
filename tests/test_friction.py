"""Tests for friction detection — the three-layer 'something is broken'
surfacing: (1) in-turn broken-tool escalation after consecutive failures,
(2) the turn-end friction flag + friction log, (3) reflection's friction
diagnosis phase."""



# ── Workspace friction log ────────────────────────────────────────────────────

def test_friction_log_append_load_clear_and_cap(tmp_workspace):
    ws = tmp_workspace
    assert ws.load_friction_log() is None
    for i in range(55):
        ws.append_friction_log(f"[repl] calls=9 errors=5 worst=shell_run(5/9) e{i}")
    text = ws.load_friction_log()
    assert text is not None
    lines = [l for l in text.splitlines() if l.startswith("- ")]
    assert len(lines) == 50                      # capped
    assert lines[-1].endswith("e54")             # newest kept
    assert not any(l.endswith(" e4") for l in lines)   # oldest dropped
    ws.clear_friction_log()
    assert ws.load_friction_log() is None


# ── Layer 1: broken-tool escalation ───────────────────────────────────────────

def test_broken_tool_escalation_reaches_model(minimal_env, native_client):
    """Three consecutive failures of the same tool → the third result carries
    the 'consider the TOOL broken' escalation, visible on the next request."""
    from aria.agent import Agent
    a = Agent()
    inner = native_client(
        # unknown tool → "[tools] error: unknown tool 'nope'" (error-classified);
        # different args each time so the exact-repeat guard stays out of the way
        {"content": None, "tool_calls": [("nope", {"a": 1})]},
        {"content": None, "tool_calls": [("nope", {"a": 2})]},
        {"content": None, "tool_calls": [("nope", {"a": 3})]},
        "giving up",
    )
    requests = []
    real_create = inner.chat.completions.create

    def record(**kwargs):
        requests.append(kwargs["messages"])
        return real_create(**kwargs)

    inner.chat.completions.create = record
    a.client = inner
    a.chat_collect("do the thing")

    def tool_contents(msgs):
        return [m["content"] for m in msgs if m.get("role") == "tool"]

    # Request 4 (after 3 failures) carries the escalation; request 3 does not.
    assert any("3 failed `nope` calls in a row" in c
               for c in tool_contents(requests[3]))
    assert not any("calls in a row" in c for c in tool_contents(requests[2]))


def test_success_resets_consecutive_error_count(minimal_env, native_client,
                                                monkeypatch):
    """err, err, SUCCESS, err of the SAME tool — never 3 in a row, so no
    escalation. The unknown-tool result string is identical per call, so a
    stateful classifier stub drives the err/success sequence."""
    from aria import agent as agent_mod
    from aria.agent import Agent
    # err, err, success, err by tool-call index. _looks_like_error runs twice
    # per call (once in _execute_call for the ✓/✗ icon, once in the loop's
    # friction accounting), hence the //2.
    verdicts = [True, True, False, True]
    seen = {"n": 0}

    def fake(result):
        k = seen["n"] // 2
        seen["n"] += 1
        return verdicts[k] if k < len(verdicts) else False

    monkeypatch.setattr(agent_mod, "_looks_like_error", fake)
    a = Agent()
    a.client = native_client(
        {"content": None, "tool_calls": [("nope", {"a": 1})]},
        {"content": None, "tool_calls": [("nope", {"a": 2})]},
        {"content": None, "tool_calls": [("nope", {"a": 3})]},
        {"content": None, "tool_calls": [("nope", {"a": 4})]},
        "done",
    )
    a.chat_collect("go")
    assert not any("calls in a row" in (m.get("content") or "")
                   for m in a.history if m.get("role") == "tool")


def test_per_tool_error_counters_are_independent(minimal_env, native_client):
    """Failures of different tools must not pool into one escalation counter."""
    from aria.agent import Agent
    a = Agent()
    a.client = native_client(
        {"content": None, "tool_calls": [("nope", {"a": 1})]},
        {"content": None, "tool_calls": [("nada", {"a": 1})]},
        {"content": None, "tool_calls": [("nope", {"a": 2})]},
        "done",
    )
    a.chat_collect("go")
    assert not any("calls in a row" in (m.get("content") or "")
                   for m in a.history if m.get("role") == "tool")


# ── Layer 2: turn-end friction flag ───────────────────────────────────────────

def _fr(calls=0, errors=0, err_per_tool=None, calls_per_tool=None,
        repeats=0, hard_stop=False):
    return {"calls": calls, "errors": errors,
            "err_per_tool": err_per_tool or {}, "consec": {},
            "repeats": repeats, "hard_stop": hard_stop,
            "calls_per_tool": calls_per_tool or {}}


def test_flag_friction_high_error_turn(minimal_env):
    from aria.agent import Agent
    a = Agent()
    a._is_terminal = False
    a._flag_friction(_fr(calls=10, errors=6,
                         err_per_tool={"shell_run": 6},
                         calls_per_tool={"shell_run": 10}))
    assert any("High friction" in r for r in a._responses)
    assert any("shell_run: 6/10" in r for r in a._responses)
    logged = a.ws.load_friction_log()
    assert logged and "worst=shell_run(6/10)" in logged
    assert "[repl]" in logged


def test_flag_friction_quiet_turn_stays_silent(minimal_env):
    from aria.agent import Agent
    a = Agent()
    a._is_terminal = False
    a._flag_friction(_fr(calls=10, errors=1))         # 10% errors — fine
    a._flag_friction(_fr(calls=3, errors=3))          # errors but too few calls
    a._flag_friction(_fr(calls=0, errors=0, hard_stop=True))  # empty turn
    assert a._responses == []
    assert a.ws.load_friction_log() is None


def test_flag_friction_repeats_log_only(minimal_env):
    """Repeat-guard churn and hard stops are LOGGED for reflection but not
    re-announced — those turns already explain themselves to the user."""
    from aria.agent import Agent
    a = Agent()
    a._is_terminal = False
    a._flag_friction(_fr(calls=4, errors=0, repeats=2))
    a._flag_friction(_fr(calls=4, errors=1, hard_stop=True))
    assert a._responses == []
    logged = a.ws.load_friction_log()
    assert logged and "repeats=2" in logged and "hard_stop=yes" in logged


def test_flag_friction_disabled(minimal_env, monkeypatch):
    from aria import agent as agent_mod
    from aria.agent import Agent
    monkeypatch.setattr(agent_mod, "_FRICTION_MIN_CALLS", 0)
    a = Agent()
    a._is_terminal = False
    a._flag_friction(_fr(calls=20, errors=20,
                         err_per_tool={"x": 20}, calls_per_tool={"x": 20}))
    assert a._responses == []
    assert a.ws.load_friction_log() is None


def test_system_prompt_has_breakage_rule(minimal_env):
    from aria.agent import Agent
    assert "appears broken" in Agent().system_prompt


# ── Layer 3: reflection friction phase ────────────────────────────────────────

def test_friction_counts_and_hot(minimal_env):
    from aria import reflect
    text = ("# Friction Log\n"
            "- 2026-08-14 10:00 [repl] calls=9 errors=5 worst=shell_run(5/9) repeats=0 hard_stop=no\n"
            "- 2026-08-14 11:00 [repl] calls=8 errors=4 worst=shell_run(4/8) repeats=0 hard_stop=no\n"
            "- 2026-08-14 12:00 [telegram:1] calls=7 errors=3 worst=shell_run(3/7) repeats=1 hard_stop=no\n")
    assert reflect._friction_counts(text) == {"shell_run": 3}
    assert reflect._friction_is_hot(text) is True          # one tool at 3
    two = "\n".join(text.splitlines()[:3])
    assert reflect._friction_is_hot(two) is False          # only 2 events
    assert reflect._friction_is_hot("") is False


class _FakeLLM:
    """Minimal chat.completions.create stub returning a fixed reply."""
    def __init__(self, reply):
        class _Comp:
            def create(_s, **kwargs):
                msg = type("M", (), {"content": reply})
                choice = type("C", (), {"message": msg})
                return type("R", (), {"choices": [choice]})
        self.chat = type("Chat", (), {"completions": _Comp()})()


def test_phase_friction_flags_issue_and_consumes_log(minimal_env, tmp_workspace):
    from aria import reflect
    ws = tmp_workspace
    for i in range(3):
        ws.append_friction_log(f"[repl] calls=9 errors=5 worst=shell_run(5/9) "
                               f"repeats=0 hard_stop=no t{i}")
    status = reflect._phase_friction(
        ws, _FakeLLM("- shell_run: quoting breaks on nested quotes"),
        "test-model", notify=False)
    assert "systemic issue flagged" in status
    ops = ws.load_operational_memory()
    assert ops and "[suspected issue]" in ops and "quoting" in ops
    assert ws.load_friction_log() is None                  # consumed


def test_phase_friction_none_verdict_still_consumes(minimal_env, tmp_workspace):
    from aria import reflect
    ws = tmp_workspace
    for i in range(3):
        ws.append_friction_log(f"[repl] calls=9 errors=5 worst=web_fetch(5/9) "
                               f"repeats=0 hard_stop=no t{i}")
    status = reflect._phase_friction(ws, _FakeLLM("NONE"), "test-model",
                                     notify=False)
    assert "no systemic issue" in status
    assert not (ws.load_operational_memory() or "")
    assert ws.load_friction_log() is None


def test_phase_friction_cold_log_is_noop(minimal_env, tmp_workspace):
    from aria import reflect
    ws = tmp_workspace
    ws.append_friction_log("[repl] calls=9 errors=5 worst=git(5/9) "
                           "repeats=0 hard_stop=no")
    assert reflect._phase_friction(ws, _FakeLLM("x"), "m", notify=False) == ""
    assert ws.load_friction_log() is not None              # kept for later
