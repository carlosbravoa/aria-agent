"""The Vicus channel's Python side, against a fake sidecar speaking the same
stdio protocol (tests/fixtures/fake_vicus_bridge.py): routing, the allow-list,
group mentions, instant commands and approval answers, attachments, acks,
pushes from other processes through the unix socket, fatal handling, and the
installer notes."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

FAKE = Path(__file__).parent / "fixtures" / "fake_vicus_bridge.py"


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def vicus(minimal_env, tmp_path, monkeypatch):
    import shutil
    import tempfile
    from aria.channels.vicus import runner
    # a private, SHORT runtime dir: never the real one (a running Aria's socket
    # lives there), and unix socket paths are capped at ~108 bytes
    rundir = tempfile.mkdtemp(prefix="av", dir="/tmp")
    monkeypatch.setenv("XDG_RUNTIME_DIR", rundir)
    log = tmp_path / "commands.jsonl"
    log.write_text("")
    monkeypatch.setattr(runner, "_bridge_argv", lambda: [sys.executable, str(FAKE)])
    monkeypatch.setenv("FAKE_VICUS_LOG", str(log))
    monkeypatch.setenv("VICUS_ALLOWED", "carlos@example.org, ana@example.org")
    monkeypatch.setenv("VICUS_EMAIL", "aria@example.org")
    monkeypatch.setenv("AGENT_NAME", "Aria")
    monkeypatch.delenv("VICUS_GROUP_REPLIES", raising=False)
    turns: list[tuple[str, str]] = []

    def fake_handle(channel, conv, text, response_cb=None, activity_cb=None):
        turns.append((conv, text))
        if response_cb:
            response_cb(f"reply to {text}")
        return [f"reply to {text}"]

    from aria.channels import host
    monkeypatch.setattr(host, "handle_message", fake_handle)

    state = {"stop": None, "thread": None, "error": None}

    def start(messages=(), **env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("FAKE_VICUS_MESSAGES", json.dumps(list(messages)))
        stop = threading.Event()

        def run():
            try:
                runner.serve(stop, service=False)
            except Exception as exc:          # attached mode surfaces errors
                state["error"] = exc

        t = threading.Thread(target=run, daemon=True)
        t.start()
        state.update(stop=stop, thread=t)
        return stop

    def commands():
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    yield type("V", (), {"start": staticmethod(start), "turns": turns,
                         "commands": staticmethod(commands), "state": state,
                         "runner": runner, "tmp": tmp_path})
    if state["stop"] is not None:
        state["stop"].set()
        state["thread"].join(5)
    shutil.rmtree(rundir, ignore_errors=True)


def _msg(seq, text, sender="carlos@example.org", conv="c1", members=2, **extra):
    return {"id": f"{conv}:{seq}", "convId": conv, "seq": seq, "from": sender,
            "text": text, "createdAt": 0, "members": members, "attachment": None, **extra}


def test_one_to_one_message_runs_a_turn_replies_and_acks(vicus):
    vicus.start([_msg(1, "hola")])
    assert _wait(lambda: any(c["type"] == "ack" for c in vicus.commands()))
    assert vicus.turns == [("c1", "hola")]
    sends = [c for c in vicus.commands() if c["type"] == "send"]
    assert [(s["convId"], s["text"]) for s in sends] == [("c1", "reply to hola")]
    acks = [c["id"] for c in vicus.commands() if c["type"] == "ack"]
    assert acks == ["c1:1"]
    # the ack comes AFTER the reply — durability: never acknowledge unhandled work
    kinds = [c["type"] for c in vicus.commands()]
    assert kinds.index("ack") > kinds.index("send")


def test_unknown_sender_is_acked_and_ignored(vicus):
    vicus.start([_msg(1, "hi", sender="stranger@evil.org")])
    assert _wait(lambda: any(c["type"] == "ack" for c in vicus.commands()))
    assert vicus.turns == [] and not [c for c in vicus.commands() if c["type"] == "send"]


def test_groups_answer_only_when_mentioned(vicus):
    vicus.start([_msg(1, "lunch?", members=3),
                 _msg(2, "@Aria what's the weather", members=3),
                 _msg(3, "aria: remind me", members=3)])
    assert _wait(lambda: len([c for c in vicus.commands() if c["type"] == "ack"]) == 3)
    assert vicus.turns == [("c1", "what's the weather"), ("c1", "remind me")]


def test_group_mode_all(vicus):
    vicus.start([_msg(1, "lunch?", members=3)], VICUS_GROUP_REPLIES="all")
    assert _wait(lambda: vicus.turns == [("c1", "lunch?")])


def test_instant_commands_skip_the_queue(vicus, monkeypatch):
    """Regression: instant replies used to run on the stdout reader thread and
    deadlock waiting for their own send result."""
    from aria.channels import host
    ran = []
    monkeypatch.setattr(host, "run_command", lambda ch, conv, text: ran.append(text) or "⏹ Stopping…")
    vicus.start([_msg(1, "/stop")])
    assert _wait(lambda: any(c["type"] == "ack" for c in vicus.commands()))
    assert ran == ["/stop"] and vicus.turns == []
    assert [c["text"] for c in vicus.commands() if c["type"] == "send"] == ["⏹ Stopping…"]


def test_approval_answers_are_instant(vicus, monkeypatch):
    from aria.channels import host
    monkeypatch.setattr(host, "answer_approval",
                        lambda ch, conv, text: "✅ Approved." if text == "yes 1234" else None)
    vicus.start([_msg(1, "yes 1234")])
    assert _wait(lambda: any(c["type"] == "ack" for c in vicus.commands()))
    assert vicus.turns == []
    assert [c["text"] for c in vicus.commands() if c["type"] == "send"] == ["✅ Approved."]


def test_attachment_moves_into_the_inbox(vicus, minimal_env):
    f = vicus.tmp / "incoming.jpg"
    f.write_bytes(b"\xff\xd8jpeg")
    vicus.start([_msg(1, "look", attachment={"path": str(f), "name": "cat.jpg",
                                              "mime": "image/jpeg", "size": 6})])
    assert _wait(lambda: vicus.turns)
    conv, text = vicus.turns[0]
    assert not f.exists()
    saved = list((minimal_env / "inbox" / "vicus").rglob("*cat*.jpg"))
    assert saved and saved[0].stat().st_mode & 0o777 == 0o600
    assert "look" in text and str(saved[0]) in text


def test_push_from_another_process_goes_through_the_socket(vicus):
    vicus.start()
    assert _wait(lambda: vicus.runner.socket_path().exists())
    # another process has no bridge of its own: simulate by talking to the socket
    ok = vicus.runner._socket_request({"type": "send", "convId": "c9", "text": "done!"})
    assert ok["ok"]
    ok = vicus.runner._socket_request({"type": "notify", "accounts": ["carlos@example.org"],
                                       "text": "report"})
    assert ok["ok"]
    kinds = [(c["type"], c.get("convId") or c.get("accounts")) for c in vicus.commands()]
    assert ("send", "c9") in kinds and ("notify", ["carlos@example.org"]) in kinds
    bad = vicus.runner._socket_request({"type": "shutdown"})
    assert not bad["ok"]                              # only send/sendfile/notify pass


def test_plugin_send_uses_the_running_bridge(vicus):
    from aria.channels.vicus import PLUGIN
    vicus.start()
    assert _wait(lambda: vicus.runner._active is not None and vicus.runner._active.ready.is_set())
    PLUGIN.send("hi there", to="c5")
    PLUGIN.send("broadcast")                          # → notify the allowed accounts
    cmds = vicus.commands()
    assert {"type": "send", "convId": "c5", "text": "hi there"}.items() <= next(
        c for c in cmds if c["type"] == "send").items()
    notify = next(c for c in cmds if c["type"] == "notify")
    assert notify["accounts"] == ["ana@example.org", "carlos@example.org"]


def test_fatal_is_raised_in_attached_mode(vicus):
    vicus.start(FAKE_VICUS_FATAL="token has no tenant claims — not part of any group yet")
    assert _wait(lambda: vicus.state["error"] is not None)
    assert "not part of any group" in str(vicus.state["error"])


def test_send_without_a_running_channel_explains(minimal_env):
    from aria.channels.vicus import runner
    with pytest.raises(RuntimeError, match="isn't running"):
        runner.request_send("x", "c1")


def test_mentions():
    import os
    from aria.channels.vicus import runner
    os.environ["AGENT_NAME"] = "Aria"
    os.environ["VICUS_EMAIL"] = "aria@example.org"
    assert runner.mention_stripped("@Aria hola") == "hola"
    assert runner.mention_stripped("Aria, hola") == "hola"
    assert runner.mention_stripped("hey @aria@example.org ping") == "hey ping"
    assert runner.mention_stripped("Mariana says hi") is None
    assert runner.mention_stripped("I asked aria yesterday") is None


def test_installer_notes(minimal_env, monkeypatch, tmp_path):
    from aria.channels.vicus import deploy
    monkeypatch.delenv("VICUS_SOURCE_DIR", raising=False)
    notes = deploy.install(dry_run=True)
    texts = " | ".join(t for _, t in notes)
    assert "VICUS_SOURCE_DIR is not set" in texts
    checkout = tmp_path / "vicus-src"
    (checkout / "packages/client/dist").mkdir(parents=True)
    (checkout / "packages/client/dist/index.js").write_text("")
    monkeypatch.setenv("VICUS_SOURCE_DIR", str(checkout))
    texts = " | ".join(t for _, t in deploy.checkout_notes())
    assert "client not built" not in texts and "wasm-pack build --target nodejs" in texts


def test_attachment_errors_are_reported_not_turned_into_turns(vicus):
    vicus.start([_msg(1, "big one", attachment={"path": None, "name": "movie.mp4",
                                                 "mime": "video/mp4", "size": 99,
                                                 "error": "over the 25 MiB limit"})])
    assert _wait(lambda: any(c["type"] == "ack" for c in vicus.commands()))
    assert vicus.turns == []
    assert [c["text"] for c in vicus.commands() if c["type"] == "send"] == [
        "I couldn't open that file: over the 25 MiB limit"]


def test_the_real_bridge_protocol_tests_pass():
    """The Node half's own suite (vicus/test), when node is available."""
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    root = Path(__file__).parents[1]
    r = subprocess.run([node, "--test", "vicus/test/"], cwd=root, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]


def test_service_stops_gracefully_on_sigterm(vicus, monkeypatch, tmp_path):
    """systemd's stop signal ends the service through its cleanup path: the
    sidecar is told to shut down (it saves its state) and the socket goes."""
    import os
    import signal
    import subprocess
    src = str(Path(__file__).parents[1] / "src")
    code = (
        f"import sys; sys.path.insert(0, {src!r})\n"
        "from aria.channels.vicus import runner\n"
        f"runner._bridge_argv = lambda: [sys.executable, {str(FAKE)!r}]\n"
        "from aria.channels.vicus import PLUGIN\n"
        "PLUGIN.run()\n")
    env = dict(os.environ)
    proc = subprocess.Popen([sys.executable, "-c", code], env=env)
    sock = vicus.runner.socket_path()
    assert _wait(lambda: sock.exists(), 15)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(15) == 0
    assert not sock.exists()
    assert any(c["type"] == "shutdown" for c in vicus.commands())
