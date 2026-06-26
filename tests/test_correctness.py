"""
Correctness-bug fixes:
  B — imap: no `delete` action; `move` uses UID EXPUNGE (never bare expunge()).
  A — chat_yield: an empty turn returns the fallback, not a blank message.
"""

from __future__ import annotations


# ── B: imap delete removed + safe move ────────────────────────────────────────

class _FakeConn:
    def __init__(self, caps=("UIDPLUS",)):
        self.capabilities = caps
        self.calls = []

    def select(self, folder):
        self.calls.append(("select", folder)); return ("OK", [b"1"])

    def uid(self, cmd, *args):
        self.calls.append(("uid", cmd.lower(), args)); return ("OK", [b""])

    def expunge(self):
        self.calls.append(("expunge",)); return ("OK", [b""])


def test_imap_definition_has_no_delete():
    from aria.tools import imap
    assert "delete" not in imap.DEFINITION["parameters"]["properties"]["action"]["enum"]
    assert "delete" not in imap.DEFINITION["description"].split(".")[0].lower()


def test_imap_delete_action_is_rejected():
    from aria.tools import imap
    conn = _FakeConn()
    out = imap._dispatch(conn, "delete", {"uid": "5"})
    assert "Unknown action: delete" in out
    assert ("expunge",) not in conn.calls          # never reached the purge


def test_imap_move_uses_uid_expunge_not_bare_expunge():
    from aria.tools import imap
    conn = _FakeConn(caps=("IMAP4REV1", "UIDPLUS"))
    out = imap._dispatch(conn, "move", {"uid": "5", "destination": "Trash", "folder": "INBOX"})
    assert "moved to Trash" in out
    assert ("uid", "expunge", ("5",)) in conn.calls     # scoped to this message
    assert ("expunge",) not in conn.calls               # never the mailbox-wide purge


def test_imap_move_is_soft_without_uidplus():
    from aria.tools import imap
    conn = _FakeConn(caps=("IMAP4REV1",))               # no UIDPLUS
    out = imap._dispatch(conn, "move", {"uid": "5", "destination": "Trash", "folder": "INBOX"})
    assert "lacks" in out and "UIDPLUS" in out
    assert ("expunge",) not in conn.calls               # refuses to purge
    assert ("uid", "expunge", ("5",)) not in conn.calls


# ── A: chat_yield never emits a blank message ─────────────────────────────────

def test_chat_yield_empty_turn_returns_fallback(minimal_env, native_client, monkeypatch):
    from aria.agent import Agent
    a = Agent()
    # repeated identical tool call → dedup guard returns without a response,
    # so _responses is empty and _last_response is "".
    a.client = native_client({"tool_calls": [("shell_run", {"action": "run"})]})
    monkeypatch.setattr(a, "_execute_tool", lambda n, ar: "out")
    out = a.chat_yield("go")
    assert isinstance(out, list) and len(out) == 1
    assert out[0] != ""                                 # not a blank message
    assert "No response generated" in out[0]


# ── D: channel session-registry concurrency ──────────────────────────────────

class _FakeAgent:
    count = 0
    def __init__(self, window_key=None, terminal=None):
        _FakeAgent.count += 1
        self.window_key = window_key
    def chat_yield(self, text):
        return [f"reply:{text}"]
    def close(self):
        pass


def _patch_channel(monkeypatch):
    from aria import channel
    _FakeAgent.count = 0
    monkeypatch.setattr(channel, "Agent", _FakeAgent)
    monkeypatch.setattr(channel, "_IDLE_SECONDS", 10_000)  # don't fire mid-test
    with channel._registry_lock:
        channel._sessions.clear()
    return channel


def test_concurrent_handle_creates_one_session(minimal_env, monkeypatch):
    import threading
    channel = _patch_channel(monkeypatch)
    barrier = threading.Barrier(12)
    results = []

    def worker():
        barrier.wait()                       # maximise the race window
        results.append(channel.handle("telegram", "u1", "hi"))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads: t.start()
    for t in threads: t.join()

    assert _FakeAgent.count == 1, f"created {_FakeAgent.count} agents, expected 1"
    assert len(channel._sessions) == 1
    assert all(r == ["reply:hi"] for r in results)
    channel.shutdown()


def test_orphan_idle_does_not_evict_live_session(minimal_env, monkeypatch):
    channel = _patch_channel(monkeypatch)
    key = ("telegram", "u1")
    orphan = channel._Session("telegram", "u1")   # duplicate, NOT in registry
    live   = channel._Session("telegram", "u1")   # the registered one
    with channel._registry_lock:
        channel._sessions[key] = live

    orphan._on_idle(orphan._gen)                   # orphan's idle fires
    assert channel._sessions.get(key) is live      # must NOT evict the live one

    orphan.cancel(); live.cancel()
    channel.shutdown()


def test_idle_bails_when_activity_reset_timer(minimal_env, monkeypatch):
    channel = _patch_channel(monkeypatch)
    key = ("telegram", "u1")
    s = channel._Session("telegram", "u1")
    with channel._registry_lock:
        channel._sessions[key] = s

    old_gen = s._gen
    with s._lock:
        s._reset_timer()                           # simulate a newer message
    s._on_idle(old_gen)                            # stale timer fires
    assert channel._sessions.get(key) is s         # not evicted (gen mismatch)

    s.cancel()
    channel.shutdown()


# ── C: recurring tasks don't burst-catch-up after downtime ────────────────────

def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def test_recur_daily_advances_past_now_after_downtime():
    from datetime import datetime, timedelta
    from aria.task import Task
    past = _iso(datetime.now() - timedelta(days=3))      # supervisor was down 3 days
    nxt = datetime.fromisoformat(Task(prompt="x", recur="daily", run_after=past).next_run_after())
    assert nxt > datetime.now()                          # future, not in the past
    assert nxt <= datetime.now() + timedelta(days=1)     # next day, not a burst


def test_recur_minutes_advances_past_now():
    from datetime import datetime, timedelta
    from aria.task import Task
    past = _iso(datetime.now() - timedelta(hours=6))
    nxt = datetime.fromisoformat(Task(prompt="x", recur="60m", run_after=past).next_run_after())
    assert datetime.now() < nxt <= datetime.now() + timedelta(minutes=60)


def test_recur_future_base_unchanged():
    from datetime import datetime, timedelta
    from aria.task import Task
    soon = _iso(datetime.now() + timedelta(minutes=5))
    nxt = datetime.fromisoformat(Task(prompt="x", recur="daily", run_after=soon).next_run_after())
    assert nxt > datetime.now() + timedelta(hours=23)    # base+1d, normal case


def test_recur_zero_minutes_is_rejected():
    from aria.task import Task
    assert Task(prompt="x", recur="0m", run_after="").next_run_after() == ""


# ── E: _trim_history trims to a clean turn boundary ───────────────────────────

def test_trim_history_drops_leading_orphaned_tool_msg(minimal_env):
    from aria.agent import Agent
    a = Agent()
    # A leading `tool` message whose assistant `tool_calls` was trimmed away is
    # orphaned — the provider rejects it. Trim must drop to the first user turn.
    a.history = list(a._seed) + [
        {"role": "tool", "tool_call_id": "x", "content": "orphaned tool output"},
        {"role": "assistant", "content": "an answer"},
        {"role": "user", "content": "the real question"},
    ]
    a._trim_history()
    real = a.history[len(a._seed):]
    assert real and real[0]["role"] == "user"
    assert real[0]["content"] == "the real question"


def test_trim_history_drops_leading_assistant(minimal_env):
    from aria.agent import Agent
    a = Agent()
    a.history = list(a._seed) + [
        {"role": "assistant", "content": "resumed assistant turn"},
        {"role": "user", "content": "hi"},
    ]
    a._trim_history()
    real = a.history[len(a._seed):]
    assert real[0]["role"] == "user" and real[0]["content"] == "hi"


# ── F: reflection is serialised by a file lock ────────────────────────────────

def test_reflect_lock_blocks_second_acquire(minimal_env, tmp_workspace):
    from aria import reflect
    l1 = reflect._acquire_reflect_lock(tmp_workspace)
    assert l1                                   # acquired
    try:
        assert reflect._acquire_reflect_lock(tmp_workspace) is False   # blocked
    finally:
        reflect._release_reflect_lock(l1)
    l3 = reflect._acquire_reflect_lock(tmp_workspace)
    assert l3                                   # released → acquirable again
    reflect._release_reflect_lock(l3)


def test_reflect_run_skips_when_already_locked(minimal_env, tmp_workspace):
    from aria import reflect
    held = reflect._acquire_reflect_lock(tmp_workspace)   # same workspace root run() uses
    try:
        out = reflect.run()
        assert "already running" in out
    finally:
        reflect._release_reflect_lock(held)


# ── G: periodic schedule survives restarts ────────────────────────────────────

def test_periodic_job_persists_last_run_across_restart(minimal_env):
    from aria.supervisor import _PeriodicJob
    c1 = []
    j1 = _PeriodicJob("testjob", 3600, lambda: c1.append(1))
    j1.tick(5000.0)
    assert c1 == [1]                            # interval elapsed since epoch 0 → fired

    # simulate a process restart: a fresh instance loads the persisted timestamp
    c2 = []
    j2 = _PeriodicJob("testjob", 3600, lambda: c2.append(1))
    j2.tick(5100.0)
    assert c2 == []                             # 100s < 3600s → does NOT re-fire on restart
    j2.tick(9000.0)
    assert c2 == [1]                            # interval elapsed → fires
