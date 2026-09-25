"""
aria/channels/telegram/notify.py — Send a message to Telegram without running the bot.

Used by:
  - `aria --notify "..."` CLI flag  (single-shot + push result)
  - The `notify` tool               (agent-initiated push)
  - Cron jobs / shell scripts

Requires in ~/.aria/.env:
  TELEGRAM_TOKEN=<bot token>
  TELEGRAM_ALLOWED=<comma-separated chat IDs to notify>

send() is stdlib-only so it works from bare cron; send_document() uses httpx
(a core dependency) because multipart uploads over urllib are not worth
hand-rolling. Both target the active turn's chat when there is one — see
current_chat_id().
"""

from __future__ import annotations

import html
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import json
import logging
from pathlib import Path

from aria.channel_util import parse_allowed, record_feed as _record_feed

log = logging.getLogger(__name__)

# Telegram's own limits.
_MAX_UPLOAD  = 50 * 1024 * 1024
_MAX_CAPTION = 1024


def _token() -> str:
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN not set. Add it to ~/.aria/.env")
    return token


def _chat_ids() -> list[int]:
    ids = [int(x) for x in parse_allowed("TELEGRAM_ALLOWED") if x.isdigit()]
    if not ids:
        raise RuntimeError("TELEGRAM_ALLOWED not set. Add chat IDs to ~/.aria/.env")
    return ids


def _split(text: str, max_len: int = 4000) -> list[str]:
    """Split text into chunks of at most max_len chars, preferring line breaks.
    A single line longer than max_len is hard-split so no chunk ever exceeds
    the limit. Shared by the bot module — keep this the only implementation."""
    if len(text) <= max_len:
        return [text]
    chunks, buf = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_len:           # overlong line → hard-split
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:max_len])
            line = line[max_len:]
        if len(buf) + len(line) > max_len:
            if buf:
                chunks.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        chunks.append(buf)
    return chunks or [text[:max_len]]


def _md_to_html(text: str) -> str:
    """
    Convert common Markdown patterns to Telegram HTML.
    Telegram HTML supports: <b>, <i>, <u>, <s>, <code>, <pre>.
    Code spans/blocks are pulled out into placeholders before inline formatting
    runs, so `__init__` or `**kwargs` inside code is never turned into bold.
    """
    # 1. Escape HTML special chars first (NULs dropped: they delimit placeholders)
    result = html.escape(text.replace("\x00", ""))

    stash: list[str] = []

    def _keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    # 2. Fenced code blocks ```lang\n...\n``` → <pre><code>...</code></pre>
    result = re.sub(
        r"```(?:\w+)?\n(.*?)```",
        lambda m: _keep(f"<pre><code>{m.group(1).rstrip()}</code></pre>"),
        result,
        flags=re.DOTALL,
    )

    # 3. Inline code `...` → <code>...</code>
    result = re.sub(r"`([^`\n]+)`", lambda m: _keep(f"<code>{m.group(1)}</code>"),
                    result)

    # 4. Bold **text** or __text__ → <b>text</b>  (DOTALL for multiline)
    result = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", result, flags=re.DOTALL)
    result = re.sub(r"__(.+?)__",     r"<b>\1</b>", result, flags=re.DOTALL)

    # 5. Italic *text* → <i>text</i>  (only single *, not **)
    result = re.sub(r"\*([^*\n]+?)\*", r"<i>\1</i>", result)

    # 6. Strikethrough ~~text~~ → <s>text</s>
    result = re.sub(r"~~(.+?)~~", r"<s>\1</s>", result, flags=re.DOTALL)

    # 7. Headers # ## ### → <b>text</b>
    result = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", result, flags=re.MULTILINE)

    # 8. Restore the protected code fragments
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], result)


def current_chat_id() -> int | None:
    """The chat this turn belongs to, when the agent is serving a Telegram user.

    Lets notify/send_file reply in the conversation the user is actually in
    rather than broadcasting to every chat in TELEGRAM_ALLOWED. Returns None in
    the REPL, supervisor tasks and cron runs — there, broadcasting is correct.
    """
    try:
        from aria import context
        ctx = context.current()
    except Exception:
        return None
    if ctx and ctx.channel == "telegram":
        uid = str(ctx.user_id)
        if uid.lstrip("-").isdigit():
            return int(uid)
    return None


def _targets(chat_id: int | None) -> list[int]:
    """Explicit chat wins; else the active channel's chat; else broadcast."""
    if chat_id:
        return [chat_id]
    current = current_chat_id()
    return [current] if current else _chat_ids()


def _post_message(url: str, chat_id: int, text: str, html_mode: bool,
                  reply_markup: dict | None = None) -> None:
    """POST one sendMessage. Raises urllib.error.HTTPError / URLError."""
    body: dict = {"chat_id": chat_id, "text": text}
    if html_mode:
        body["parse_mode"] = "HTML"
    if reply_markup:
        body["reply_markup"] = reply_markup
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def _send_chunk(url: str, chat_id: int, chunk: str) -> None:
    """Send one Markdown chunk as HTML; on an HTML rejection (400, typically a
    'can't parse entities' error) retry the same chunk as plain text."""
    try:
        _post_message(url, chat_id, _md_to_html(chunk), html_mode=True)
        return
    except urllib.error.HTTPError as e:
        if e.code != 400:
            body_err = e.read().decode(errors="replace")
            raise RuntimeError(f"Telegram API error {e.code}: {body_err}") from e
    try:
        _post_message(url, chat_id, chunk, html_mode=False)
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="replace")
        raise RuntimeError(f"Telegram API error {e.code}: {body_err}") from e


def send(text: str, chat_id: int | None = None) -> None:
    """
    Send text to one specific chat_id, to the chat of the active turn, or to
    all TELEGRAM_ALLOWED chats when there is no active channel.
    Converts Markdown to Telegram HTML so formatting renders correctly.
    Uses only stdlib — no python-telegram-bot dependency needed.

    The Markdown is split BEFORE conversion (so a split can never cut an HTML
    tag), and each chunk falls back to plain text if Telegram rejects its HTML.
    A failing chat does not abort the broadcast to the others; raises
    RuntimeError only when no chat received the message.
    """
    token   = _token()
    targets = _targets(chat_id)
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks  = [c for c in _split(text) if c.strip()] or [text]

    errors: list[str] = []
    delivered = 0
    for cid in targets:
        chat_ok = True
        for chunk in chunks:
            try:
                _send_chunk(url, cid, chunk)
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                chat_ok = False
                errors.append(f"chat {cid}: {e}")
                log.error("Telegram send to chat %s failed: %s", cid, e)
        if chat_ok:
            delivered += 1

    if delivered:
        _record_feed(text)
    if not delivered:
        raise RuntimeError("; ".join(errors) or "Telegram send failed")


APPROVAL_PREFIX = "aria-approve:"


def send_approval(code: str, summary: str, chat_id: int | None = None,
                  expires_min: int = 5) -> None:
    """Ask for approval with ✅/❌ inline buttons (answered by the bot's
    callback handler); the text also says how to answer by typing, for a
    client that doesn't show buttons. Same targeting as send()."""
    import html
    token   = _token()
    targets = _targets(chat_id)
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    text = (f"🔐 <b>Approval needed</b>\n{html.escape(summary)}\n\n"
            f"<i>Expires in {expires_min} min · or reply</i> <code>yes {code}</code> / "
            f"<code>no {code}</code>")
    markup = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"{APPROVAL_PREFIX}{code}:y"},
        {"text": "❌ Deny",    "callback_data": f"{APPROVAL_PREFIX}{code}:n"},
    ]]}
    errors: list[str] = []
    delivered = 0
    for cid in targets:
        try:
            _post_message(url, cid, text, html_mode=True, reply_markup=markup)
            delivered += 1
        except (urllib.error.URLError, OSError) as e:
            errors.append(f"chat {cid}: {e}")
            log.error("Telegram approval request to chat %s failed: %s", cid, e)
    if not delivered:
        raise RuntimeError("; ".join(errors) or "Telegram send failed")


def send_document(path: str | Path, caption: str = "",
                  chat_id: int | None = None) -> str:
    """Upload a file as a Telegram document attachment.

    Callers are responsible for authorising the path — this function does no
    allow-list checking of its own. Uses httpx (already a core dependency) for
    multipart rather than hand-rolling it over urllib.
    """
    import httpx

    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"File not found: {p}")
    if not p.is_file():
        raise RuntimeError(f"Not a file: {p}")

    size = p.stat().st_size
    if size == 0:
        raise RuntimeError(f"{p.name} is empty — nothing to send.")
    if size > _MAX_UPLOAD:
        raise RuntimeError(
            f"{p.name} is {size / 1024 / 1024:.1f} MB; Telegram caps bot "
            f"uploads at {_MAX_UPLOAD // 1024 // 1024} MB."
        )

    token   = _token()
    targets = _targets(chat_id)
    url     = f"https://api.telegram.org/bot{token}/sendDocument"
    blob    = p.read_bytes()

    for cid in targets:
        data: dict[str, str] = {"chat_id": str(cid)}
        if caption.strip():
            data["caption"]    = _md_to_html(caption)[:_MAX_CAPTION]
            data["parse_mode"] = "HTML"
        try:
            resp = httpx.post(
                url, data=data,
                files={"document": (p.name, blob, "application/octet-stream")},
                timeout=120,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Telegram upload failed: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"Telegram API error {resp.status_code}: {resp.text[:300]}")

    _record_feed(f"[sent file] {p.name}" + (f" — {caption.strip()}" if caption.strip() else ""))
    return p.name
