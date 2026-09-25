"""
aria/channels/whatsapp/notify.py — Push a message to WhatsApp without running the bridge.

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

Files: send_file() POSTs {"to", "caption", "media": {mimetype, filename,
data(base64)}} to the same /send endpoint (capped at ARIA_WA_MAX_MB, default
16). The payload deliberately carries no `text`: a pre-media bridge.js rejects
it with 400 "missing 'to' or 'text'" instead of silently sending just the
caption, and a new bridge acknowledges with {"ok": true, "media": true} — so an
outdated bridge surfaces as a clear error, never a false "Sent".
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.error
import urllib.request
from pathlib import Path

from aria.channel_util import parse_allowed, record_feed as _record_feed

_DEFAULT_PUSH_PORT = 7533
_DEFAULT_MAX_MB    = 16
_OUTDATED = ("the WhatsApp Node bridge (~/.aria/whatsapp/bridge.js) is outdated and "
             "can't send files — run `aria-install` (or the update tool) to redeploy it, "
             "then restart aria-whatsapp-node")


def max_bytes() -> int:
    """ARIA_WA_MAX_MB (default 16): cap for files in either direction. Shared
    with bridge.js, which applies the same limit to inbound media."""
    raw = os.environ.get("ARIA_WA_MAX_MB", "").strip()
    try:
        mb = float(raw) if raw else float(_DEFAULT_MAX_MB)
    except ValueError:
        mb = float(_DEFAULT_MAX_MB)
    return int(max(mb, 0.0) * 1024 * 1024)


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
    nums = parse_allowed("WHATSAPP_ALLOWED")
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


def _post(number: str, payload: dict, secret: str, timeout: float = 30) -> dict:
    """POST one push to the Node listener; return its JSON reply ({} if none)."""
    url = f"http://127.0.0.1:{_push_port()}/send"
    req = urllib.request.Request(
        url,
        data=json.dumps({"to": number, **payload}).encode(),
        headers={"Content-Type": "application/json", "X-Aria-Secret": secret},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="replace")
        raise RuntimeError(f"WhatsApp push error {e.code}: {body_err}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"WhatsApp bridge unreachable at {url}: {e.reason}. "
            f"Is the Node bridge running?"
        ) from e
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def send(text: str, to: str | None = None, *, record: bool = True) -> None:
    """
    Push text to one specific WhatsApp number, to the number of the active turn,
    or to all WHATSAPP_ALLOWED numbers when there is no active channel.

    `record=False` skips the notify feed — used for ordinary turn replies,
    which are conversation, not proactive messages.

    Raises RuntimeError on missing config (no secret / no allowed numbers /
    no resolvable target) and on HTTP errors. Uses only stdlib.
    """
    secret  = _secret()
    targets = _targets(to)
    if not targets:
        raise RuntimeError("No WhatsApp target to send to.")

    for number in targets:
        _post(number, {"text": text}, secret)

    if record:
        _record_feed(text)


def send_file(path: str | Path, caption: str = "", to: str | None = None) -> str:
    """Send a file as a WhatsApp document; return the name it was sent as.

    Callers authorise the path (send_file tool → file_access). Raises
    RuntimeError with a human-readable reason on any failure."""
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"File not found: {p}")
    if not p.is_file():
        raise RuntimeError(f"Not a file: {p}")
    size = p.stat().st_size
    if size == 0:
        raise RuntimeError(f"{p.name} is empty — nothing to send.")
    cap = max_bytes()
    if size > cap:
        raise RuntimeError(
            f"{p.name} is {size / 1024 / 1024:.1f} MB; the WhatsApp bridge sends files "
            f"up to {cap / 1024 / 1024:.0f} MB (ARIA_WA_MAX_MB).")

    secret  = _secret()
    targets = _targets(to)
    if not targets:
        raise RuntimeError("No WhatsApp target to send to.")

    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    media = {"mimetype": mime, "filename": p.name,
             "data": base64.b64encode(p.read_bytes()).decode("ascii")}
    for number in targets:
        try:
            ack = _post(number, {"caption": caption.strip(), "media": media}, secret,
                        timeout=120)
        except RuntimeError as exc:
            if "missing 'to' or 'text'" in str(exc):
                raise RuntimeError(_OUTDATED) from exc
            raise
        if not ack.get("media"):
            raise RuntimeError(_OUTDATED)

    _record_feed(f"[sent file] {p.name}" + (f" — {caption.strip()}" if caption.strip() else ""))
    return p.name
