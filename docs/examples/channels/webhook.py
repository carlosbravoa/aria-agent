"""
Example channel plugin — a generic HTTP webhook (stdlib only).

Copy to ~/.aria/channels/webhook.py, then either add "webhook" to ARIA_CHANNELS
or run `aria-install` and select it. Run it with `aria-channel webhook` (the
installer creates an `aria-channel-webhook` systemd unit for you).

Inbound:  POST http://127.0.0.1:$ARIA_WEBHOOK_PORT/message
          header  X-Aria-Secret: $ARIA_WEBHOOK_SECRET
          body    {"user": "alice", "text": "what's on my calendar?"}
          reply   {"replies": ["You have …"]}
Outbound: notify / supervisor pushes are POSTed as {"to": user|null, "text": …}
          to $ARIA_WEBHOOK_OUT_URL (optional).

Useful as a bridge for Home Assistant, n8n, a Discord/Slack bot you already
run, or anything else that can make an HTTP request.
"""

from __future__ import annotations

import hmac
import json
import os

from aria.channels import ChannelPlugin, ConfigField


class WebhookChannel(ChannelPlugin):
    name = "webhook"
    description = "Generic HTTP webhook (example plugin)"
    config_fields = (
        ConfigField("ARIA_WEBHOOK_SECRET", prompt="Shared secret for inbound requests",
                    secret=True, required=True),
        ConfigField("ARIA_WEBHOOK_PORT", prompt="Local port to listen on", default="7540"),
        ConfigField("ARIA_WEBHOOK_OUT_URL", prompt="URL to POST pushes to (optional)"),
    )

    def run(self) -> None:
        import http.server

        from aria.channels import host

        secret = os.environ["ARIA_WEBHOOK_SECRET"]
        port = int(os.environ.get("ARIA_WEBHOOK_PORT", "7540"))
        channel = self.name

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                if self.path != "/message" or not hmac.compare_digest(
                        self.headers.get("X-Aria-Secret", ""), secret):
                    self.send_error(403)
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    user, text = str(body["user"]), str(body["text"])
                except (ValueError, KeyError):
                    self.send_error(400, "expected JSON {user, text}")
                    return
                replies = host.handle_message(channel, user, text)
                data = json.dumps({"replies": replies}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        try:
            server.serve_forever()
        finally:
            host.shutdown()

    def send(self, text: str, to: str | None = None) -> None:
        import httpx
        url = os.environ.get("ARIA_WEBHOOK_OUT_URL", "").strip()
        if not url:
            raise RuntimeError("ARIA_WEBHOOK_OUT_URL is not set — the webhook channel can't push")
        r = httpx.post(url, json={"to": to, "text": text}, timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"webhook push failed: HTTP {r.status_code}")


PLUGIN = WebhookChannel()
