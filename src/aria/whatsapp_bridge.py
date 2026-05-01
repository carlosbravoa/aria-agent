"""
aria/whatsapp_bridge.py — HTTP bridge between whatsapp-web.js and the Aria agent.

Architecture:
  whatsapp-web.js (Node.js)
    → POST http://localhost:ARIA_WA_PORT/message  {"from": "...", "text": "..."}
    ← {"reply": "..."}

  This server receives the message, runs it through the agent via the shared
  channel session registry, and returns the reply. The Node.js side then sends
  it back to WhatsApp.

Setup:
  1. Add to ~/.aria/.env:
       ARIA_WA_PORT=7532           # port for this bridge (default 7532)
       ARIA_WA_SECRET=<token>      # shared secret for Node↔Python auth
       WHATSAPP_ALLOWED=<phone1,phone2>  # allowed sender numbers (international format)

  2. Start the bridge:
       aria-whatsapp

  3. Start the Node.js side:
       node ~/.aria/whatsapp/bridge.js

Dependencies: none (uses stdlib http.server)
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import threading

from aria import config
from aria.channel import handle

log = logging.getLogger(__name__)

CHANNEL = "whatsapp"


def _allowed() -> set[str]:
    raw = os.environ.get("WHATSAPP_ALLOWED", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _secret() -> str:
    return os.environ.get("ARIA_WA_SECRET", "")


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

        # Auth via shared secret header
        secret = _secret()
        if secret and self.headers.get("X-Aria-Secret") != secret:
            self._reject(403, "forbidden")
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._reject(400, "invalid JSON")
            return

        sender = payload.get("from", "").strip()
        text   = payload.get("text", "").strip()

        if not sender or not text:
            self._reject(400, "missing 'from' or 'text'")
            return

        # Allowlist check
        allowed = _allowed()
        if allowed and sender not in allowed:
            log.warning("Rejected WhatsApp message from %s", sender)
            self._reject(403, "sender not allowed")
            return

        log.info("WhatsApp message from %s: %s", sender, text[:80])

        # Run through the agent (blocking — bridge runs handler in a thread)
        responses = handle(CHANNEL, sender, text)
        reply = "\n\n".join(r for r in responses if r.strip())

        # Strip "Aria: " prefix
        if ":" in reply:
            _, _, after = reply.partition(":")
            reply = after.strip()

        self._respond({"reply": reply})

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
        from aria.channel import shutdown
        shutdown()


if __name__ == "__main__":
    main()
