"""
aria/channels/vicus/runner.py — Runs the Vicus sidecar and routes its messages.

  serve(stop)   spawn `node <bridge.mjs>`, restart it with backoff if it dies,
                route every message event through the Aria host, and listen on
                a unix socket so other processes (notify, the supervisor) can
                push through the ONE running device. A Vicus device must have a
                single live connection and a single copy of its MLS state, so
                nothing else may start a second bridge.

Stdio protocol (JSON lines; see vicus/bridge.mjs):
  bridge → us   ready · message · result · fatal
  us → bridge   ack · send · sendfile · notify · shutdown

A message is acknowledged only after Aria has handled it: the bridge keeps it
in its persisted inbox until then and re-emits it after a restart, so a crash
between decrypting and answering never loses a message.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from aria.channels.vicus import state_dir

log = logging.getLogger(__name__)

CHANNEL = "vicus"
_RESTART_MIN, _RESTART_MAX = 5.0, 300.0
_REQUEST_TIMEOUT = 180.0
_IDLE_WORKER_SEC = 60.0

_active: _Bridge | None = None          # the bridge running in THIS process
_active_lock = threading.Lock()


# ── Settings ──────────────────────────────────────────────────────────────────

def allowed() -> set[str]:
    from aria.channel_util import parse_allowed
    return {a.lower() for a in parse_allowed("VICUS_ALLOWED")}


def group_mode() -> str:
    return "all" if os.environ.get("VICUS_GROUP_REPLIES", "").strip().lower() == "all" else "mention"


def display_name() -> str:
    return (os.environ.get("VICUS_DISPLAY_NAME") or os.environ.get("AGENT_NAME") or "Aria").strip()


def socket_path() -> Path:
    """The push socket: $XDG_RUNTIME_DIR (per-user, 0700, short) when set,
    else ~/.aria/run — falling back to a private dir under the temp dir when
    that path is too long for a unix socket (~108 bytes)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    candidates = ([Path(xdg) / "aria"] if xdg else []) + [Path.home() / ".aria" / "run"]
    import tempfile
    candidates.append(Path(tempfile.gettempdir()) / f"aria-{os.getuid()}")
    for d in candidates:
        path = d / "vicus.sock"
        if len(str(path).encode()) < 100:
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            return path
    raise RuntimeError("no usable path for the Vicus push socket")


def bridge_script() -> Path | None:
    """The deployed sidecar (aria-install copies it), else the repo copy."""
    deployed = Path.home() / ".aria" / "vicus-bridge" / "bridge.mjs"
    if deployed.exists():
        return deployed
    from aria.channels.vicus import deploy
    src = deploy.source_dir()
    return src / "bridge.mjs" if src else None


# ── Mentions (group conversations) ────────────────────────────────────────────

def mention_stripped(text: str) -> str | None:
    """The message with the mention removed if it addresses Aria, else None.
    Matches "@Name …", "Name: …" / "Name, …" at the start, or the bot's email."""
    name = re.escape(display_name())
    email = re.escape(os.environ.get("VICUS_EMAIL", "").strip().lower() or "\x00")
    # the email first: "@aria@example.org" also starts with "@aria"
    patterns = [rf"@?{email}[:,]?\s*", rf"@{name}\b[:,]?\s*", rf"^\s*{name}\s*[:,]\s*"]
    for pat in patterns:
        if re.search(pat, text, re.I):
            return re.sub(pat, "", text, count=1, flags=re.I).strip() or text.strip()
    return None


# ── The sidecar process ───────────────────────────────────────────────────────

class _Fatal(Exception):
    pass


def _bridge_argv() -> list[str]:
    """The command that runs the sidecar (tests substitute a fake)."""
    script = bridge_script()
    node = shutil.which("node")
    if script is None or not script.exists():
        raise _Fatal("the Vicus bridge (vicus/bridge.mjs) isn't deployed — run aria-install")
    if node is None:
        raise _Fatal("node not found — the Vicus channel needs Node.js 20+")
    return [node, str(script)]


class _Bridge:
    """One `node bridge.mjs` child and its stdio protocol."""

    def __init__(self, on_message) -> None:
        argv = _bridge_argv()
        env = dict(os.environ)
        env.setdefault("VICUS_STATE_DIR", str(state_dir()))
        env.setdefault("VICUS_DISPLAY_NAME", display_name())
        self._on_message = on_message
        self._write_lock = threading.Lock()
        self._results: dict[str, tuple[threading.Event, dict]] = {}
        self.ready = threading.Event()
        self.fatal: str | None = None
        self.info: dict = {}
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
            cwd=str(Path(argv[-1]).parent))

    def start(self) -> None:
        """Begin reading events. Separate from __init__ so the caller can
        register the bridge first: the sidecar re-emits its saved inbox at
        once, and replying/acking those needs the bridge to be findable."""
        self._events: queue.Queue = queue.Queue()
        threading.Thread(target=self._dispatch, daemon=True, name="vicus-dispatch").start()
        threading.Thread(target=self._read_stdout, daemon=True, name="vicus-stdout").start()
        threading.Thread(target=self._read_stderr, daemon=True, name="vicus-stderr").start()

    def _dispatch(self) -> None:
        """Message events are handled HERE, never on the stdout reader: an
        instant reply (/stop, an approval answer) sends a request and waits
        for its result — which only the reader can deliver. One thread keeps
        the arrival order."""
        while True:
            ev = self._events.get()
            if ev is None:
                return
            try:
                self._on_message(ev)
            except Exception:
                log.exception("vicus: handling message %s failed", ev.get("id"))

    # stdout: events
    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                log.warning("vicus bridge: non-JSON output ignored")
                continue
            kind = ev.get("type")
            if kind == "ready":
                self.info = ev
                self.ready.set()
                log.info("Vicus ready as %s (device %s)", ev.get("account"), ev.get("deviceId"))
            elif kind == "message":
                self._events.put(ev)
            elif kind == "result":
                waiter = self._results.pop(str(ev.get("reqId")), None)
                if waiter is not None:
                    waiter[1].update(ev)
                    waiter[0].set()
            elif kind == "fatal":
                self.fatal = str(ev.get("error") or "unknown error")
                log.error("Vicus bridge: %s", self.fatal)
                self.ready.set()
        # the process ended: stop the dispatcher, release anyone waiting
        self._events.put(None)
        for event, result in list(self._results.values()):
            result.update(ok=False, error="the Vicus bridge stopped")
            event.set()
        self._results.clear()

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            if line.strip():
                log.info("vicus bridge: %s", line.rstrip())

    # stdin: commands
    def _write(self, obj: dict) -> None:
        with self._write_lock:
            if self.proc.stdin is None or self.proc.poll() is not None:
                raise RuntimeError("the Vicus bridge isn't running")
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    def request(self, obj: dict, timeout: float = _REQUEST_TIMEOUT) -> dict:
        req = str(uuid.uuid4())
        event = threading.Event()
        result: dict[str, Any] = {}
        self._results[req] = (event, result)
        self._write({**obj, "reqId": req})
        if not event.wait(timeout):
            self._results.pop(req, None)
            raise RuntimeError("the Vicus bridge didn't answer in time")
        return result

    def ack(self, msg_id: str) -> None:
        try:
            self._write({"type": "ack", "id": msg_id})
        except RuntimeError:
            pass              # it re-emits the message on its next start

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self, timeout: float = 10.0) -> None:
        try:
            self._write({"type": "shutdown"})
            if self.proc.stdin:
                self.proc.stdin.close()
        except (RuntimeError, OSError):
            pass
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ── Routing ───────────────────────────────────────────────────────────────────

class _Router:
    """Turns message events into Aria turns: one FIFO worker per conversation
    (ordered within a conversation, parallel across them); approval answers
    and shared commands are answered at once so they never wait behind the
    turn they concern."""

    def __init__(self, send, ack) -> None:
        self._send = send            # (convId, text) -> None
        self._ack = ack              # (msg id) -> None
        self._queues: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()

    def on_message(self, ev: dict) -> None:
        msg_id = str(ev.get("id") or "")
        conv = str(ev.get("convId") or "")
        sender = str(ev.get("from") or "").lower()
        text = str(ev.get("text") or "")
        if not conv or sender not in allowed():
            if conv:
                log.info("vicus: ignoring a message from %s (not in VICUS_ALLOWED)", sender)
            self._ack(msg_id)
            return
        if int(ev.get("members") or 2) > 2 and group_mode() == "mention":
            stripped = mention_stripped(text)
            if stripped is None and not ev.get("attachment"):
                self._ack(msg_id)
                return
            text = stripped if stripped is not None else text
        if ev.get("attachment"):
            try:
                text = _attachment_text(conv, ev["attachment"], text)
            except Exception as exc:
                log.error("vicus: attachment for %s failed: %s", msg_id, exc)
                self._reply(conv, f"I couldn't open that file: {exc}")
                self._ack(msg_id)
                return
        else:
            from aria.channels import host
            instant = host.answer_approval(CHANNEL, conv, text)
            if instant is None:
                from aria.channels import commands
                parsed = commands.parse(text)
                if parsed is not None and parsed[0] in commands.INSTANT:
                    instant = host.run_command(CHANNEL, conv, text)
            if instant is not None:
                self._reply(conv, instant)
                self._ack(msg_id)
                return
        self._enqueue(conv, (msg_id, text))

    def _reply(self, conv: str, text: str) -> None:
        try:
            self._send(conv, text)
        except Exception as exc:
            log.error("vicus: reply to %s failed: %s", conv, exc)

    def _enqueue(self, conv: str, item: tuple[str, str]) -> None:
        with self._lock:
            q = self._queues.get(conv)
            if q is None:
                q = self._queues[conv] = queue.Queue()
                threading.Thread(target=self._work, args=(conv, q), daemon=True,
                                 name=f"vicus-{conv}").start()
            q.put(item)

    def _work(self, conv: str, q: queue.Queue) -> None:
        from aria.channels import host
        while True:
            try:
                msg_id, text = q.get(timeout=_IDLE_WORKER_SEC)
            except queue.Empty:
                with self._lock:
                    if q.empty():
                        self._queues.pop(conv, None)
                        return
                continue
            streamed: list[str] = []

            def on_response(reply: str, _streamed=streamed) -> None:
                _streamed.append(reply)
                self._reply(conv, reply)

            try:
                replies = host.handle_message(CHANNEL, conv, text, response_cb=on_response)
            except Exception as exc:
                log.exception("vicus: turn in %s failed", conv)
                replies = [f"Sorry, something went wrong: {exc}"]
            if not streamed:
                for r in replies:
                    if r.strip():
                        self._reply(conv, r)
            self._ack(msg_id)


def _attachment_text(conv: str, a: dict, caption: str) -> str:
    """Move a decrypted attachment into the inbox; return the turn text."""
    from aria import attachments
    if a.get("error"):                 # too big, or the download kept failing
        raise RuntimeError(str(a["error"]))
    src = Path(str(a.get("path") or ""))
    if not src.is_file():
        raise RuntimeError("the decrypted file is missing")
    mime = str(a.get("mime") or "application/octet-stream").split(";")[0].strip()
    name = str(a.get("name") or src.name)
    kind = next((k for prefix, k in (("image/", "photo"), ("audio/", "audio"),
                                     ("video/", "video")) if mime.startswith(prefix)),
                "document")
    dest = attachments.destination(CHANNEL, conv, name)
    shutil.move(str(src), dest)
    os.chmod(dest, 0o600)
    attachments.finalize(dest)
    return attachments.describe(dest, channel=CHANNEL, kind=kind, original_name=name,
                                mime=mime, size=dest.stat().st_size, caption=caption)


# ── Push socket (other processes → the running device) ────────────────────────

def _serve_socket(bridge_ref: dict, stop: threading.Event) -> None:
    path = socket_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(path))
    except OSError as exc:
        log.error("Vicus push socket %s unavailable: %s — pushes from other "
                  "processes won't reach Vicus", path, exc)
        srv.close()
        return
    os.chmod(path, 0o600)
    mine = path.stat().st_ino
    srv.listen(8)
    srv.settimeout(0.5)
    try:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            threading.Thread(target=_handle_socket_client, args=(conn, bridge_ref),
                             daemon=True).start()
    finally:
        srv.close()
        try:
            if path.stat().st_ino == mine:   # never remove a successor's socket
                path.unlink()
        except FileNotFoundError:
            pass


def _handle_socket_client(conn: socket.socket, bridge_ref: dict) -> None:
    with conn:
        try:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            req = json.loads(data.decode() or "{}")
            # A push can land while the bridge is (re)starting: give it a moment.
            deadline = time.monotonic() + 30
            bridge = bridge_ref.get("bridge")
            while (bridge is None or not bridge.ready.is_set()) and time.monotonic() < deadline:
                time.sleep(0.1)
                bridge = bridge_ref.get("bridge")
            if bridge is None or not bridge.alive() or bridge.fatal:
                result = {"ok": False, "error": "the Vicus bridge isn't running"}
            elif req.get("type") not in ("send", "sendfile", "notify"):
                result = {"ok": False, "error": "unknown request"}
            else:
                result = bridge.request(req)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        conn.sendall((json.dumps(result) + "\n").encode())


def _socket_request(req: dict) -> dict:
    path = socket_path()
    if not path.exists():
        raise RuntimeError("the Vicus channel isn't running (start `aria-channel vicus`, "
                           "or open `aria` if it's attached)")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(_REQUEST_TIMEOUT + 10)
        try:
            s.connect(str(path))
        except OSError as exc:
            raise RuntimeError(f"the Vicus channel isn't running ({exc})") from None
        s.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode() or "{}")


def _request(req: dict) -> dict:
    with _active_lock:
        bridge = _active
    if bridge is not None and bridge.alive():
        return bridge.request(req)
    return _socket_request(req)


def request_send(text: str, to: str | None) -> None:
    if to:
        result = _request({"type": "send", "convId": to, "text": text})
    else:
        accounts = sorted(allowed())
        if not accounts:
            raise RuntimeError("VICUS_ALLOWED is empty — nobody to notify on Vicus")
        result = _request({"type": "notify", "accounts": accounts, "text": text})
        if result.get("ok"):
            from aria.channel_util import record_feed
            record_feed(text)
    if not result.get("ok") and not result.get("queued"):
        raise RuntimeError(f"Vicus: {result.get('error') or 'send failed'}")


def request_send_file(path: Path, caption: str, to: str) -> str:
    import mimetypes
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    result = _request({"type": "sendfile", "convId": to, "path": str(path),
                       "name": path.name, "mime": mime, "caption": caption or None})
    if not result.get("ok"):
        raise RuntimeError(f"Vicus: {result.get('error') or 'send failed'}")
    return path.name


# ── Serve ─────────────────────────────────────────────────────────────────────

def serve(stop: threading.Event, service: bool) -> None:
    """Run the channel until `stop` is set. A service keeps retrying after a
    configuration error (a crash-looping unit would trip the update rollback
    watchdog); attached mode raises it so /remote shows what's wrong."""
    global _active
    lock = None
    if service:
        from aria.channels.runlock import hold_for_service
        lock = hold_for_service(CHANNEL, log)
    bridge_ref: dict[str, Any] = {"bridge": None}

    def send(conv: str, text: str) -> None:
        bridge = bridge_ref["bridge"]
        if bridge is None:
            raise RuntimeError("the Vicus bridge isn't running")
        result = bridge.request({"type": "send", "convId": conv, "text": text})
        if not result.get("ok") and not result.get("queued"):
            raise RuntimeError(result.get("error") or "send failed")
        # queued: kept in the device's outbox, sent on the next resume — not lost

    def ack(msg_id: str) -> None:
        bridge = bridge_ref["bridge"]
        if bridge is not None and msg_id:
            bridge.ack(msg_id)

    router = _Router(send, ack)
    sock_thread = threading.Thread(target=_serve_socket, args=(bridge_ref, stop),
                                   daemon=True, name="vicus-socket")
    sock_thread.start()
    delay = _RESTART_MIN
    try:
        while not stop.is_set():
            try:
                bridge = _Bridge(router.on_message)
            except _Fatal as exc:
                if not service:
                    raise RuntimeError(str(exc)) from None
                log.error("Vicus: %s — retrying in %.0f s", exc, _RESTART_MAX)
                stop.wait(_RESTART_MAX)
                continue
            bridge_ref["bridge"] = bridge
            with _active_lock:
                _active = bridge
            bridge.start()
            started = time.monotonic()
            while not stop.is_set() and bridge.alive():
                stop.wait(0.5)
            if stop.is_set():
                bridge.close()
                break
            if bridge.fatal:
                if not service:
                    raise RuntimeError(bridge.fatal)
                delay = _RESTART_MAX           # misconfiguration: don't hammer
            elif time.monotonic() - started > 600:
                delay = _RESTART_MIN           # it ran fine for a while
            log.warning("Vicus bridge exited (code %s) — restarting in %.0f s",
                        bridge.proc.returncode, delay)
            stop.wait(delay)
            delay = min(delay * 2, _RESTART_MAX)
    finally:
        with _active_lock:
            if _active is bridge_ref["bridge"]:
                _active = None
        bridge = bridge_ref["bridge"]
        if bridge is not None and bridge.alive():
            bridge.close()
        stop.set()
        sock_thread.join(2)
        from aria.channels import host
        host.shutdown(CHANNEL)
        if lock is not None:
            lock.release()
