"""
aria/whatsapp_notify.py — Push a message to WhatsApp without running the bridge.

Used by:
  - The `notify` tool  (agent-initiated push on a WhatsApp turn)
  - Scheduled tasks / cron that target a WhatsApp user

The Node bridge (whatsapp/bridge.js) runs a local push listener on
ARIA_WA_PUSH_PORT; this module POSTs the message to it. send() is stdlib-only
(urllib) so it works from bare cron, mirroring telegram_notify.send.

Requires in ~/.aria/.env:
  ARIA_WA_SECRET=<token>              # shared secret for Node↔Python auth
  WHATSAPP_ALLOWED=<phone1,phone2>   # allowed numbers (international, no +)
  ARIA_WA_PUSH_PORT=7533             # optional, defaults to 7533

Target resolution mirrors telegram_notify._targets: an explicit `to` wins;
else the active turn's WhatsApp user_id; else broadcast to WHATSAPP_ALLOWED.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_DEFAULT_PUSH_PORT = 7533


def _secret() -> str:
    secret = os.environ.get("ARIA_WA_SECRET", "")
    if not secret:
        raise RuntimeError("ARIA_WA_SECRET not set. Add it to ~/.aria/.env")
    return secret


def _push_port() -> int:
    raw = os.environ.get("ARIA_WA_PUSH_PORT", "")
    if raw.strip().isdigit():
        return int(raw.strip())
    return _DEFAULT_PUSH_PORT


def _allowed() -> list[str]:
    raw = os.environ.get("WHATSAPP_ALLOWED", "")
    nums = [x.strip() for x in raw.split(",") if x.strip()]
    if not nums:
        raise RuntimeError("WHATSAPP_ALLOWED not set. Add numbers to ~/.aria/.env")
    return nums


def current_user() -> str | None:
    """The WhatsApp number this turn belongs to, when serving a WhatsApp user.

    Returns None in the REPL, supervisor tasks and cron — there the push has no
    single target and falls back to broadcasting to WHATSAPP_ALLOWED.
    """
    try:
        from aria import context
        ctx = context.current()
    except Exception:
        return None
    if ctx and ctx.channel == "whatsapp":
        uid = str(ctx.user_id).strip()
        if uid:
            return uid
    return None


def _targets(to: str | None) -> list[str]:
    """Explicit target wins; else the active turn's user; else broadcast."""
    if to:
        return [str(to).strip()]
    current = current_user()
    if current:
        return [current]
    return _allowed()


def send(text: str, to: str | None = None) -> None:
    """
    Push text to one specific WhatsApp number, to the number of the active turn,
    or to all WHATSAPP_ALLOWED numbers when there is no active channel.

    Raises RuntimeError on missing config (no secret / no allowed numbers /
    no resolvable target) and on HTTP errors. Uses only stdlib.
    """
    secret  = _secret()
    targets = _targets(to)
    if not targets:
        raise RuntimeError("No WhatsApp target to send to.")

    url = f"http://127.0.0.1:{_push_port()}/send"

    for number in targets:
        payload = json.dumps({"to": number, "text": text}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Aria-Secret": secret,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            body_err = e.read().decode(errors="replace")
            raise RuntimeError(f"WhatsApp push error {e.code}: {body_err}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"WhatsApp bridge unreachable at {url}: {e.reason}. "
                f"Is the Node bridge running?"
            ) from e

    _record_feed(text)


def _record_feed(text: str) -> None:
    """Record an outbound push so the agent has context when the user replies."""
    try:
        from aria import config
        from aria.workspace import Workspace
        ws = Workspace(config.workspace_dir())
        ws.append_notify_feed(text)
    except Exception:
        pass  # best-effort — never block on feed write
