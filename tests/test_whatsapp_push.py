"""
4.10 — WhatsApp push delivery.

The Python bridge answers POST /message at once; commands and approval answers
come back in the HTTP response, everything else runs on a per-sender FIFO
worker whose replies are pushed (one WhatsApp message each) to the Node
bridge's push listener. Also: inbound/outbound media, and compatibility of the
immediate response with an OLD deployed bridge.js.

Real sockets on 127.0.0.1 (ephemeral ports): the bridge's HTTP handler and a
fake Node push listener. The agent is faked at host.handle_message.
"""

from __future__ import annotations

import base64
import http.server
import json
import re
import shutil
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from aria.channels import host
from aria.channels.whatsapp import bridge, notify

REPO = Path(__file__).resolve().parent.parent
SENDER = "34600111222"
OTHER = "34600333444"
SECRET = "s3cret"


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _PushListener:
    """Stands in for bridge.js's push listener: records every /send body."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.status = 200
        self.ack: dict = {"ok": True}
        self.cond = threading.Condition()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body["_secret"] = self.headers.get("X-Aria-Secret")
                with outer.cond:
                    outer.bodies.append(body)
                    outer.cond.notify_all()
                out = json.dumps(outer.ack if outer.status == 200 else
                                 {"error": outer.ack.get("error", "x")}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def texts(self, to: str | None = None) -> list[str]:
        return [b.get("text") for b in self.bodies if to is None or b["to"] == to]

    def wait(self, n: int, timeout: float = 5.0) -> list[dict]:
        with self.cond:
            self.cond.wait_for(lambda: len(self.bodies) >= n, timeout=timeout)
            return list(self.bodies)


@pytest.fixture
def wa(minimal_env, monkeypatch):
    """Bridge HTTP server + fake push listener, both on ephemeral ports."""
    push = _PushListener()
    monkeypatch.setenv("ARIA_WA_SECRET", SECRET)
    monkeypatch.setenv("WHATSAPP_ALLOWED", f"{SENDER},{OTHER}")
    monkeypatch.setenv("ARIA_WA_PUSH_PORT", str(push.port))
    monkeypatch.setattr(bridge, "_queues", bridge._SenderQueues(idle=0.3))
    monkeypatch.setattr(bridge, "_PUSH_BACKOFF", (0.0, 0.0))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), bridge._Handler)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    ns = type("WA", (), {})()
    ns.port = server.server_address[1]
    ns.push = push
    ns.ws = minimal_env
    yield ns
    server.shutdown()
    push.server.shutdown()


def post(port: int, payload: dict, *, new_bridge: bool = True,
         secret: str = SECRET) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "X-Aria-Secret": secret}
    if new_bridge:
        headers["X-Aria-Bridge"] = "2"
    req = urllib.request.Request(f"http://127.0.0.1:{port}/message",
                                 data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class _Turns:
    """Fake host.handle_message: records calls; per-text gates block a turn."""

    def __init__(self, replies=("one", "two")):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []
        self.gates: dict[str, threading.Event] = {}
        self.active: dict[str, int] = {}
        self.max_active: dict[str, int] = {}
        self.done = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, channel, user_id, text, response_cb=None, activity_cb=None):
        assert channel == "whatsapp"
        with self._lock:
            self.calls.append((user_id, text))
            self.active[user_id] = self.active.get(user_id, 0) + 1
            self.max_active[user_id] = max(self.max_active.get(user_id, 0),
                                           self.active[user_id])
        gate = self.gates.get(text)
        if gate is not None:
            assert gate.wait(5), "gate never opened"
        else:
            time.sleep(0.02)
        out = [f"{r} ({text})" for r in self.replies]
        for r in out:
            if response_cb:
                response_cb(r)
        with self._lock:
            self.active[user_id] -= 1
        return out


# ── Immediate response + push delivery ───────────────────────────────────────

def test_message_is_acknowledged_before_the_turn_finishes(wa, monkeypatch):
    turns = _Turns(replies=("Aria: first", "second", "Status: done"))
    gate = turns.gates["hi"] = threading.Event()
    monkeypatch.setattr(host, "handle_message", turns)

    t0 = time.monotonic()
    code, body = post(wa.port, {"from": SENDER, "text": "hi"})
    assert (code, body) == (200, {"queued": True})
    assert time.monotonic() - t0 < 2          # did not wait for the blocked turn
    assert wa.push.bodies == []

    gate.set()
    bodies = wa.push.wait(3)
    # Each response is its own WhatsApp message, agent-name prefix stripped
    # (only the leading "Aria: " — never a legitimate colon).
    assert [b["text"] for b in bodies] == ["first (hi)", "second (hi)", "Status: done (hi)"]
    assert all(b["to"] == SENDER and b["_secret"] == SECRET for b in bodies)


def test_turn_replies_are_not_recorded_as_proactive(wa, monkeypatch):
    fed = []
    monkeypatch.setattr(notify, "_record_feed", lambda t: fed.append(t))
    monkeypatch.setattr(host, "handle_message", _Turns(replies=("x",)))
    post(wa.port, {"from": SENDER, "text": "hi"})
    wa.push.wait(1)
    assert fed == []


def test_non_streamed_responses_fall_back_to_the_returned_list(wa, monkeypatch):
    monkeypatch.setattr(host, "handle_message",
                        lambda *a, **k: ["Aria: a", "", "b"])       # never calls response_cb
    post(wa.port, {"from": SENDER, "text": "hi"})
    assert [b["text"] for b in wa.push.wait(2)] == ["a", "b"]


def test_crashing_turn_tells_the_user(wa, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(host, "handle_message", boom)
    post(wa.port, {"from": SENDER, "text": "hi"})
    assert wa.push.wait(1)[0]["text"] == "Sorry, something went wrong: kaput"


def test_per_sender_order_and_parallel_senders(wa, monkeypatch):
    turns = _Turns(replies=("r",))
    gate = turns.gates["a1"] = threading.Event()
    monkeypatch.setattr(host, "handle_message", turns)

    for text in ("a1", "a2", "a3"):
        assert post(wa.port, {"from": SENDER, "text": text}) == (200, {"queued": True})
    assert post(wa.port, {"from": OTHER, "text": "b1"}) == (200, {"queued": True})

    # OTHER is not stuck behind SENDER's blocked turn.
    bodies = wa.push.wait(1)
    assert [b["to"] for b in bodies] == [OTHER]
    assert [c for c in turns.calls if c[0] == SENDER] == [(SENDER, "a1")]

    gate.set()
    wa.push.wait(4)
    assert wa.push.texts(SENDER) == ["r (a1)", "r (a2)", "r (a3)"]
    assert turns.max_active[SENDER] == 1        # one turn at a time per sender


def test_idle_worker_exits_and_restarts(wa, monkeypatch):
    monkeypatch.setattr(host, "handle_message", _Turns(replies=("r",)))
    post(wa.port, {"from": SENDER, "text": "one"})
    wa.push.wait(1)
    deadline = time.monotonic() + 3
    while bridge._queues.active() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert bridge._queues.active() == []
    post(wa.port, {"from": SENDER, "text": "two"})
    wa.push.wait(2)
    assert wa.push.texts() == ["r (one)", "r (two)"]


def test_push_retries_a_briefly_unready_bridge(monkeypatch):
    calls = []

    def flaky(text, to=None, record=True):
        calls.append(text)
        if len(calls) < 2:
            raise RuntimeError("WhatsApp push error 503")
    monkeypatch.setattr(notify, "send", flaky)
    monkeypatch.setattr(bridge, "_PUSH_BACKOFF", (0.0, 0.0))
    assert bridge._push(SENDER, "x") is True and calls == ["x", "x"]


# ── Synchronous commands / approvals (never queued behind a turn) ────────────

class _Agent:
    name = "Aria"

    def __init__(self):
        self.stopped = False

    def request_stop(self):
        self.stopped = True
        return True


def test_stop_is_answered_while_a_turn_runs(wa, monkeypatch):
    turns = _Turns(replies=("r",))
    gate = turns.gates["long job"] = threading.Event()
    monkeypatch.setattr(host, "handle_message", turns)
    agent = _Agent()
    monkeypatch.setattr(host, "get_agent", lambda c, u: agent)

    post(wa.port, {"from": SENDER, "text": "long job"})
    code, body = post(wa.port, {"from": SENDER, "text": "/stop"})
    assert code == 200 and body["reply"].startswith("⏹")
    assert agent.stopped
    assert turns.calls == [(SENDER, "long job")]          # /stop never queued
    gate.set()
    wa.push.wait(1)


def test_approval_answer_is_answered_while_the_turn_waits(wa, monkeypatch):
    from aria import approval
    turns = _Turns(replies=("r",))
    gate = turns.gates["delete it"] = threading.Event()
    monkeypatch.setattr(host, "handle_message", turns)
    approval._write(approval._dir() / "1234.json",
                    {"code": "1234", "channel": "whatsapp", "to": SENDER,
                     "summary": "delete x", "status": "pending", "created": time.time()})

    post(wa.port, {"from": SENDER, "text": "delete it"})
    code, body = post(wa.port, {"from": SENDER, "text": "yes 1234"})
    assert (code, body) == (200, {"reply": "✅ Approved."})
    assert approval._read("1234")["status"] == "approved"
    assert turns.calls == [(SENDER, "delete it")]
    gate.set()
    wa.push.wait(1)


def test_unknown_slash_text_is_a_normal_message(wa, monkeypatch):
    turns = _Turns(replies=("r",))
    monkeypatch.setattr(host, "handle_message", turns)
    assert post(wa.port, {"from": SENDER, "text": "/nope"}) == (200, {"queued": True})
    wa.push.wait(1)
    assert turns.calls == [(SENDER, "/nope")]


# ── Security is unchanged (fail closed) ──────────────────────────────────────

def test_rejects_bad_secret_and_unknown_sender(wa, monkeypatch):
    turns = _Turns()
    monkeypatch.setattr(host, "handle_message", turns)
    assert post(wa.port, {"from": SENDER, "text": "hi"}, secret="nope")[0] == 403
    assert post(wa.port, {"from": "999", "text": "hi"})[0] == 403
    assert post(wa.port, {"from": SENDER, "text": ""})[0] == 400
    time.sleep(0.1)
    assert turns.calls == [] and wa.push.bodies == []


# ── Inbound media ────────────────────────────────────────────────────────────

def _media(data: bytes, **kw) -> dict:
    return {"mimetype": "application/pdf", "filename": "../../evil.pdf",
            "data": base64.b64encode(data).decode(), "kind": "document", **kw}


def test_inbound_media_is_saved_and_described(wa, monkeypatch):
    turns = _Turns(replies=("got it",))
    monkeypatch.setattr(host, "handle_message", turns)

    code, body = post(wa.port, {"from": SENDER, "text": "please summarise",
                                "media": _media(b"%PDF-1.4 hello")})
    assert (code, body) == (200, {"queued": True})
    wa.push.wait(1)
    (_, text), = turns.calls
    saved = Path(re.search(r"saved_to: (.+)", text).group(1))
    inbox = wa.ws / "inbox" / "whatsapp" / SENDER
    assert saved.parent == inbox and saved.name.endswith("_evil.pdf")   # no traversal
    assert saved.read_bytes() == b"%PDF-1.4 hello"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert stat.S_IMODE(inbox.stat().st_mode) == 0o700
    assert "[The user sent a file via whatsapp]" in text
    assert "type: application/pdf (document)" in text
    assert text.endswith("please summarise")


def test_inbound_voice_note_without_caption(wa, monkeypatch):
    turns = _Turns(replies=("r",))
    monkeypatch.setattr(host, "handle_message", turns)
    media = _media(b"OggS...", mimetype="audio/ogg; codecs=opus", filename="", kind="ptt")
    assert post(wa.port, {"from": SENDER, "text": "", "media": media})[0] == 200
    wa.push.wait(1)
    (_, text), = turns.calls
    assert "(voice)" in text and "transcription is not enabled" in text
    assert re.search(r"filename: whatsapp_ptt\.(oga|ogg|opus)", text)
    assert "No message accompanied the file" in text


def test_inbound_media_over_the_cap_is_refused_politely(wa, monkeypatch):
    turns = _Turns()
    monkeypatch.setattr(host, "handle_message", turns)
    monkeypatch.setenv("ARIA_WA_MAX_MB", "0.001")           # ~1 KB
    code, body = post(wa.port, {"from": SENDER, "text": "", "media": _media(b"x" * 4096)})
    assert code == 200 and "only accept files up to" in body["reply"]
    time.sleep(0.1)
    assert turns.calls == []


def test_inbound_media_bad_base64_is_400(wa, monkeypatch):
    monkeypatch.setattr(host, "handle_message", _Turns())
    bad = {"mimetype": "image/png", "data": "***not base64***", "kind": "image"}
    assert post(wa.port, {"from": SENDER, "text": "", "media": bad})[0] == 400


# ── Outbound files (send_file) ───────────────────────────────────────────────

def test_send_file_posts_base64_media(wa, tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF data")
    wa.push.ack = {"ok": True, "media": True}
    assert notify.send_file(f, caption=" here ", to=SENDER) == "report.pdf"
    (body,) = wa.push.bodies
    assert body["to"] == SENDER and body["caption"] == "here"
    assert "text" not in body                         # see the outdated-bridge tests
    assert body["media"]["mimetype"] == "application/pdf"
    assert body["media"]["filename"] == "report.pdf"
    assert base64.b64decode(body["media"]["data"]) == b"%PDF data"


def test_plugin_send_file_routes_to_notify(monkeypatch, tmp_path):
    from aria.channels.whatsapp import PLUGIN
    got = []
    monkeypatch.setattr(notify, "send_file",
                        lambda p, caption="", to=None: got.append((p, caption, to)) or "n")
    assert PLUGIN.supports_files
    assert PLUGIN.send_file(tmp_path / "x", "c", to="1") == "n"
    assert got == [(tmp_path / "x", "c", "1")]


def test_send_file_detects_an_outdated_bridge_that_ignored_the_media(wa, tmp_path):
    """An old bridge.js answers {"ok": true} without doing anything with
    `media` (and would only do that if there were a `text`)."""
    f = tmp_path / "a.txt"
    f.write_text("x")
    wa.push.ack = {"ok": True}
    with pytest.raises(RuntimeError, match="outdated"):
        notify.send_file(f, to=SENDER)


def test_send_file_maps_old_bridge_400_to_outdated(wa, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    wa.push.status, wa.push.ack = 400, {"error": "missing 'to' or 'text'"}
    with pytest.raises(RuntimeError, match="outdated"):
        notify.send_file(f, caption="with caption", to=SENDER)


@pytest.mark.parametrize("setup, match", [
    (lambda p: None, "File not found"),
    (lambda p: p.write_bytes(b""), "empty"),
    (lambda p: p.write_bytes(b"x" * 4096), "ARIA_WA_MAX_MB"),
])
def test_send_file_errors(wa, tmp_path, monkeypatch, setup, match):
    monkeypatch.setenv("ARIA_WA_MAX_MB", "0.001")
    f = tmp_path / "f.bin"
    setup(f)
    with pytest.raises(RuntimeError, match=match):
        notify.send_file(f, to=SENDER)
    assert wa.push.bodies == []


def test_send_file_unreachable_bridge(minimal_env, tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_WA_SECRET", SECRET)
    monkeypatch.setenv("ARIA_WA_PUSH_PORT", "1")
    f = tmp_path / "a.txt"
    f.write_text("x")
    with pytest.raises(RuntimeError, match="unreachable"):
        notify.send_file(f, to=SENDER)


# ── Compatibility with an OLD deployed bridge.js ─────────────────────────────
#
# Pre-4.10 bridge.js (still running until the update redeploys it and restarts
# the Node unit) does:  if (parsed.reply) resolve(parsed.reply)
#                       else reject(new Error(parsed.error || "Empty reply..."))
# and on reject sends "⚠️ Something went wrong". It sends no X-Aria-Bridge
# header, so Python answers it with a non-empty acknowledgement; the real
# replies arrive through the push listener that bridge already runs.

_OLD_CALL_BRIDGE = r"""
const http = require("http");
const PORT = parseInt(process.argv[2]);
const SECRET = process.argv[3];
const TIMEOUT_MS = 5000;
function callBridge(from, text) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ from, text });
    const options = {
      hostname: "127.0.0.1", port: PORT, path: "/message", method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
        ...(SECRET ? { "X-Aria-Secret": SECRET } : {}),
      },
    };
    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          const parsed = JSON.parse(data);
          if (parsed.reply) resolve(parsed.reply);
          else reject(new Error(parsed.error || "Empty reply from bridge"));
        } catch {
          reject(new Error(`Invalid JSON from bridge: ${data}`));
        }
      });
    });
    req.on("error", reject);
    req.setTimeout(TIMEOUT_MS, () => { req.destroy(); reject(new Error("timed out")); });
    req.write(body);
    req.end();
  });
}
callBridge(process.argv[4], process.argv[5])
  .then((r) => console.log(JSON.stringify({ reply: r })))
  .catch((e) => console.log(JSON.stringify({ error: e.message })));
"""


def _old_bridge_would_error(body: dict) -> bool:
    """The old bridge.js's decision, in Python: falsy reply → error message."""
    return not body.get("reply")


def test_legacy_bridge_gets_a_non_empty_ack_and_push_replies(wa, monkeypatch):
    monkeypatch.setattr(host, "handle_message", _Turns(replies=("real answer",)))
    code, body = post(wa.port, {"from": SENDER, "text": "hi"}, new_bridge=False)
    assert code == 200 and body == {"reply": bridge._LEGACY_ACK}
    assert not _old_bridge_would_error(body)
    assert wa.push.wait(1)[0]["text"] == "real answer (hi)"


def test_new_bridge_response_would_break_an_old_bridge_so_it_is_never_sent_to_one():
    # Guard the reasoning: the {"queued": true} shape IS an error for old JS,
    # which is exactly why it is only used when X-Aria-Bridge is present.
    assert _old_bridge_would_error({"queued": True})
    assert bridge._LEGACY_ACK.strip()


def test_commands_reply_in_the_http_response_for_both_bridges(wa, monkeypatch):
    agent = _Agent()
    monkeypatch.setattr(host, "get_agent", lambda c, u: agent)
    for new in (True, False):
        code, body = post(wa.port, {"from": SENDER, "text": "/stop"}, new_bridge=new)
        assert code == 200 and body["reply"] and not _old_bridge_would_error(body)


_node = shutil.which("node") or shutil.which("nodejs")


def _run_node(tmp_path: Path, script: str, *args: str) -> dict:
    js = tmp_path / "call_bridge.js"
    js.write_text(script)
    assert _node is not None
    out = subprocess.run([_node, str(js), *args], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(_node is None, reason="node not installed")
def test_old_bridge_js_really_sends_the_ack_not_an_error(wa, monkeypatch, tmp_path):
    monkeypatch.setattr(host, "handle_message", _Turns(replies=("real answer",)))
    res = _run_node(tmp_path, _OLD_CALL_BRIDGE, str(wa.port), SECRET, SENDER, "hi")
    assert res == {"reply": bridge._LEGACY_ACK}
    assert wa.push.wait(1)[0]["text"] == "real answer (hi)"


@pytest.mark.skipif(_node is None, reason="node not installed")
def test_current_bridge_js_callbridge_against_the_python_bridge(wa, monkeypatch, tmp_path):
    """Run the real callBridge() from whatsapp/bridge.js against the bridge."""
    monkeypatch.setattr(host, "handle_message", _Turns(replies=("r",)))
    agent = _Agent()
    monkeypatch.setattr(host, "get_agent", lambda c, u: agent)
    src = (REPO / "whatsapp" / "bridge.js").read_text()
    fn = src[src.index("function callBridge("):src.index("// ── Push listener")]
    script = (
        'const http = require("http");\n'
        "const PORT = parseInt(process.argv[2]); const SECRET = process.argv[3];\n"
        'const TIMEOUT_MS = 5000; const BRIDGE_VERSION = "2";\n'
        + fn +
        "\ncallBridge(JSON.parse(process.argv[4]))"
        ".then((r) => console.log(JSON.stringify(r)))"
        ".catch((e) => console.log(JSON.stringify({ rejected: e.message })));\n"
    )
    queued = _run_node(tmp_path, script, str(wa.port), SECRET, json.dumps({"from": SENDER, "text": "hi"}))
    assert queued == {"queued": True}
    stop = _run_node(tmp_path, script, str(wa.port), SECRET, json.dumps({"from": SENDER, "text": "/stop"}))
    assert stop["reply"].startswith("⏹")
    bad = _run_node(tmp_path, script, str(wa.port), "wrong", json.dumps({"from": SENDER, "text": "hi"}))
    assert bad == {"rejected": "forbidden"}
    wa.push.wait(1)


@pytest.mark.skipif(_node is None, reason="node not installed")
def test_bridge_js_syntax():
    subprocess.run([_node, "--check", str(REPO / "whatsapp" / "bridge.js")], check=True)


def test_bridge_js_keeps_security_and_protocol_markers():
    src = (REPO / "whatsapp" / "bridge.js").read_text()
    assert "crypto.timingSafeEqual" in src
    assert "if (!ALLOWED.includes(sender))" in src               # fail-closed allowlist
    assert 'reply(403, { error: "bridge not configured: set ARIA_WA_SECRET" })' in src
    assert '"X-Aria-Bridge": BRIDGE_VERSION' in src
    assert "downloadMedia()" in src and "new MessageMedia(" in src
    assert "ARIA_WA_MAX_MB" in src
