"""
aria/channels/whatsapp/bridge.py — HTTP bridge between whatsapp-web.js and the Aria agent.

Architecture:
  whatsapp-web.js (Node.js)
    → POST http://localhost:ARIA_WA_PORT/message
         {"from": "...", "text": "...", "media": {mimetype, filename, data, kind}?}
         header X-Aria-Bridge: 2   (sent by the current bridge.js)
    ← {"queued": true}                       normal message: answered via push
    ← {"reply": "..."}                       command / approval answer: send it

  The request is answered IMMEDIATELY — nothing waits for an agent turn:
    - approval answers ("yes 1234") and shared slash commands (/stop, /clear,
      /model …) run synchronously and come back in `reply` (they are instant,
      and /stop must never queue behind the turn it is meant to stop);
    - everything else goes to a per-sender FIFO worker (one thread per active
      sender, exits when idle): one sender's messages keep their order while
      different senders run in parallel. Each response is PUSHED to WhatsApp as
      its own message the moment the agent produces it (notify.send → the Node
      bridge's push listener), like Telegram.
  So a long turn can no longer time out the HTTP call; ARIA_WA_TIMEOUT is only
  read by bridge.js now (how long it waits for this acknowledgement).

  Compatibility with an OLD bridge.js (no X-Aria-Bridge header): it treats a
  response without a non-empty `reply` as an error and tells the user
  "Something went wrong". So a legacy bridge gets a short acknowledgement as
  its `reply` (_LEGACY_ACK) and the real responses arrive through its push
  listener, which every deployed bridge.js since the push API has. The
  self-update redeploys bridge.js and restarts the Node unit, so this only
  shows until then.

  Inbound media (new bridge.js only) is saved to the workspace inbox via
  aria.attachments — same path rules, permissions and pruning as Telegram —
  and the turn text becomes attachments.describe(...).

Setup:
  1. Add to ~/.aria/.env:
       ARIA_WA_PORT=7532           # port for this bridge (default 7532)
       ARIA_WA_PUSH_PORT=7533      # Node push listener for outbound (default 7533)
       ARIA_WA_SECRET=<token>      # shared secret for Node↔Python auth
       WHATSAPP_ALLOWED=<phone1,phone2>  # allowed sender numbers (international format)
       ARIA_WA_MAX_MB=16           # max file size, both directions (default 16)

  2. Start the bridge:
       aria-whatsapp          (or: aria-channel whatsapp)

  3. Start the Node.js side:
       node ~/.aria/whatsapp/bridge.js

Dependencies: none (uses stdlib http.server)
"""

from __future__ import annotations

import base64
import binascii
import hmac
import http.server
import json
import logging
import mimetypes
import os
import queue
import threading
import time

from aria import attachments, config
from aria.channels import host
from aria.channels.host import parse_allowed
from aria.channels.whatsapp import notify

log = logging.getLogger(__name__)

CHANNEL = "whatsapp"

# Header the current bridge.js sends; its absence means a pre-push bridge.js.
_BRIDGE_HEADER = "X-Aria-Bridge"
# Legacy bridge.js must get a non-empty `reply` or it shows an error message.
_LEGACY_ACK = "⏳ On it…"

_WORKER_IDLE = 60.0                  # seconds a sender's worker lingers when idle
_PUSH_ATTEMPTS = 3
_PUSH_BACKOFF = (1.0, 3.0)

# whatsapp-web.js message types → aria.attachments kinds.
_KINDS = {"image": "photo", "sticker": "photo", "ptt": "voice", "audio": "audio",
          "video": "video", "document": "document"}


def _allowed() -> set[str]:
    return set(parse_allowed("WHATSAPP_ALLOWED"))


def _secret() -> str:
    return os.environ.get("ARIA_WA_SECRET", "")


def _strip_agent_prefix(reply: str) -> str:
    """Strip a leading agent-name prefix ("Aria: ") only — never a colon that
    legitimately appears in the reply (e.g. "Status: done")."""
    prefix = f"{os.environ.get('AGENT_NAME', 'Aria')}: "
    if reply.startswith(prefix):
        return reply[len(prefix):].strip()
    return reply


def _max_body() -> int:
    # base64 inflates by 4/3; leave room for JSON framing and the caption.
    return notify.max_bytes() * 4 // 3 + 1024 * 1024


# ── Outbound: push each response as its own WhatsApp message ────────────────

def _push(sender: str, text: str) -> bool:
    """Deliver one reply; a few retries cover a Node bridge that is briefly
    not ready (503) or restarting. Returns False if it never went out."""
    for attempt in range(_PUSH_ATTEMPTS):
        try:
            notify.send(text, to=sender, record=False)
            return True
        except Exception as exc:
            log.warning("WhatsApp push to %s failed (attempt %d/%d): %s",
                        sender, attempt + 1, _PUSH_ATTEMPTS, exc)
            if attempt < len(_PUSH_BACKOFF):
                time.sleep(_PUSH_BACKOFF[attempt])
    log.error("Dropped a reply to %s after %d attempts: %.80r", sender, _PUSH_ATTEMPTS, text)
    return False


def _run_turn(sender: str, text: str) -> None:
    """Run one agent turn for `sender`, pushing every response as it's made."""
    sent = 0

    def on_response(reply: str) -> None:
        nonlocal sent
        reply = _strip_agent_prefix(reply)
        if reply.strip() and _push(sender, reply):
            sent += 1

    try:
        responses = host.handle_message(CHANNEL, sender, text, response_cb=on_response)
    except Exception as exc:
        log.error("handle_message failed for %s: %s", sender, exc, exc_info=True)
        responses = [f"Sorry, something went wrong: {exc}"]

    # Responses were pushed as they were produced; fall back to the returned
    # list only when nothing streamed (hard error, empty turn, a path that
    # doesn't stream).
    if sent == 0:
        replies = [r for r in (_strip_agent_prefix(r) for r in responses) if r.strip()]
        if not replies:
            log.warning("Empty responses for %s input: %r", sender, text[:80])
            replies = ["(no response)"]
        for reply in replies:
            _push(sender, reply)


class _SenderQueues:
    """One FIFO worker thread per active sender: a sender's messages run in
    order, different senders in parallel. A worker exits after `idle` seconds
    with nothing to do; the next message starts a new one."""

    def __init__(self, run=None, idle: float = _WORKER_IDLE) -> None:
        self._run = run or _run_turn
        self._idle = idle
        self._lock = threading.Lock()
        self._queues: dict[str, queue.Queue[str]] = {}

    def submit(self, sender: str, text: str) -> None:
        with self._lock:
            q = self._queues.get(sender)
            if q is not None:
                q.put(text)
                return
            q = queue.Queue()
            q.put(text)
            self._queues[sender] = q
            threading.Thread(target=self._worker, args=(sender, q), daemon=True,
                             name=f"wa-turn-{sender}").start()

    def active(self) -> list[str]:
        with self._lock:
            return list(self._queues)

    def _worker(self, sender: str, q: queue.Queue[str]) -> None:
        while True:
            try:
                text = q.get(timeout=self._idle)
            except queue.Empty:
                # submit() puts under the same lock, so "empty here" means no
                # message can be stranded in a queue nobody reads.
                with self._lock:
                    if q.empty():
                        self._queues.pop(sender, None)
                        return
                continue
            try:
                self._run(sender, text)
            except Exception as exc:          # never let one turn kill the worker
                log.error("WhatsApp turn for %s crashed: %s", sender, exc, exc_info=True)


_queues = _SenderQueues()


# ── Inbound media ────────────────────────────────────────────────────────────

class _BadRequest(Exception):
    pass


def _save_media(sender: str, media: dict, caption: str) -> str:
    """Store an inbound file in the inbox; return the turn text describing it.
    Raises _BadRequest (malformed) or ValueError (too big: user-facing text)."""
    if not isinstance(media, dict) or not isinstance(media.get("data"), str):
        raise _BadRequest("invalid 'media'")
    mime = str(media.get("mimetype") or "application/octet-stream").split(";")[0].strip()
    try:
        blob = base64.b64decode(media["data"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _BadRequest("invalid 'media' data") from exc
    if not blob:
        raise _BadRequest("empty 'media' data")
    cap = notify.max_bytes()
    if len(blob) > cap:
        raise ValueError(
            f"That file is {attachments.human_size(len(blob))} — I only accept files up "
            f"to {attachments.human_size(cap)} on WhatsApp. Could you send a smaller "
            f"version, or put it somewhere I can reach?")

    wa_type = str(media.get("kind") or "")
    kind = _KINDS.get(wa_type) or next(
        (k for prefix, k in (("image/", "photo"), ("audio/", "audio"), ("video/", "video"))
         if mime.startswith(prefix)), "document")
    filename = str(media.get("filename") or "").strip() or None
    if filename is None:
        ext = mimetypes.guess_extension(mime) or ".bin"
        filename = f"whatsapp_{wa_type or kind}{ext}"

    dest = attachments.destination(CHANNEL, sender, filename)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(blob)
    attachments.finalize(dest)
    log.info("Saved %s attachment for %s to %s", kind, sender, dest)
    return attachments.describe(dest, channel=CHANNEL, kind=kind, original_name=filename,
                                mime=mime, size=len(blob), caption=caption)


# ── HTTP handler ─────────────────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt: str, *args: object) -> None:
        log.info(fmt, *args)

    def _reject(self, code: int, msg: str) -> None:
        body = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/message":
            self._reject(404, "not found")
            return

        # Auth via shared secret header — FAIL CLOSED: no secret configured
        # means no requests are accepted (an unset secret used to disable auth).
        secret = _secret()
        if not secret:
            self._reject(403, "bridge not configured: set ARIA_WA_SECRET")
            return
        if not hmac.compare_digest(self.headers.get("X-Aria-Secret", ""), secret):
            self._reject(403, "forbidden")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._reject(400, "invalid Content-Length")
            return
        if length > _max_body():
            self._reject(413, "request too large")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._reject(400, "invalid JSON")
            return
        if not isinstance(payload, dict):
            self._reject(400, "invalid JSON")
            return

        sender = str(payload.get("from") or "").strip()
        text   = str(payload.get("text") or "").strip()
        media  = payload.get("media")

        if not sender or not (text or media):
            self._reject(400, "missing 'from' or 'text'")
            return

        # Allowlist check — FAIL CLOSED: an empty WHATSAPP_ALLOWED rejects every
        # sender (matches Telegram). Previously an unset allowlist served anyone
        # who messaged the linked number.
        if sender not in _allowed():
            log.warning("Rejected WhatsApp message from %s (not in WHATSAPP_ALLOWED)", sender)
            self._reject(403, "sender not allowed")
            return

        legacy = not self.headers.get(_BRIDGE_HEADER)
        log.info("WhatsApp message from %s: %s%s", sender, text[:80],
                 " [+media]" if media else "")

        if not media:
            # Instant, and must not queue behind a running turn: an approval
            # answer unblocks that turn; /stop interrupts it.
            reply = host.answer_approval(CHANNEL, sender, text)
            if reply is None:
                reply = host.run_command(CHANNEL, sender, text)
            if reply is not None:
                self._respond({"reply": reply or "OK."})
                return
        else:
            try:
                text = _save_media(sender, media, text)
            except _BadRequest as exc:
                self._reject(400, str(exc))
                return
            except ValueError as exc:            # over the size cap
                self._respond({"reply": str(exc)})
                return
            except OSError as exc:
                log.error("Saving WhatsApp attachment for %s failed: %s", sender, exc)
                self._respond({"reply": f"I couldn't save that file: {exc}"})
                return

        _queues.submit(sender, text)
        self._respond({"reply": _LEGACY_ACK} if legacy else {"queued": True})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._respond({"status": "ok"})
        else:
            self._reject(404, "not found")


def main() -> None:
    config.load()

    from aria.setup import is_first_run, run as setup_run
    if is_first_run():
        setup_run()

    logging.basicConfig(level=logging.INFO)

    # Fail-closed config check — surface misconfiguration loudly at startup.
    if not _secret():
        log.warning("ARIA_WA_SECRET is not set — the bridge will REJECT all "
                    "requests. Set it (and on the Node side) to enable WhatsApp.")
    if not _allowed():
        log.warning("WHATSAPP_ALLOWED is empty — every sender will be REJECTED. "
                    "Set it to the allowed phone number(s).")

    port = int(os.environ.get("ARIA_WA_PORT", 7532))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)

    log.info("Aria WhatsApp bridge listening on http://127.0.0.1:%d", port)
    log.info("Start the Node.js side: node ~/.aria/whatsapp/bridge.js")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
        server.shutdown()
    finally:
        host.shutdown()


if __name__ == "__main__":
    main()
