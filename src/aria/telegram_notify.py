"""
aria/telegram_notify.py — Send a message to Telegram without running the bot.

Used by:
  - `aria --notify "..."` CLI flag  (single-shot + push result)
  - The `notify` tool               (agent-initiated push)
  - Cron jobs / shell scripts

Requires in ~/.aria/.env:
  TELEGRAM_TOKEN=<bot token>
  TELEGRAM_ALLOWED=<comma-separated chat IDs to notify>
"""

from __future__ import annotations

import html
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import json


def _token() -> str:
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_TOKEN not set. Add it to ~/.aria/.env")
    return token


def _chat_ids() -> list[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED", "")
    ids = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    if not ids:
        raise RuntimeError("TELEGRAM_ALLOWED not set. Add chat IDs to ~/.aria/.env")
    return ids


def _split(text: str, max_len: int = 4000) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks, buf = [], ""
    for line in text.splitlines(keepends=True):
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

    We escape the raw text first then convert markdown patterns so that
    any literal < > & in the content don't get interpreted as HTML tags.
    """
    # 1. Escape HTML special chars in the raw text
    result = html.escape(text)

    # 2. Fenced code blocks ```lang\n...\n``` → <pre><code>...</code></pre>
    result = re.sub(
        r"```(?:\w+)?\n(.*?)```",
        lambda m: f"<pre><code>{m.group(1).rstrip()}</code></pre>",
        result,
        flags=re.DOTALL,
    )

    # 3. Inline code `...` → <code>...</code>
    result = re.sub(r"`([^`]+)`", r"<code>\1</code>", result)

    # 4. Bold **text** or __text__ → <b>text</b>
    result = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", result)
    result = re.sub(r"__(.+?)__",     r"<b>\1</b>", result)

    # 5. Italic *text* or _text_ → <i>text</i>
    #    Use word-boundary lookahead to avoid matching inside words
    result = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", result)
    result = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)",       r"<i>\1</i>", result)

    # 6. Strikethrough ~~text~~ → <s>text</s>
    result = re.sub(r"~~(.+?)~~", r"<s>\1</s>", result)

    # 7. Headers # ## ### → <b>text</b> (Telegram has no heading tag)
    result = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", result, flags=re.MULTILINE)

    return result


def send(text: str, chat_id: int | None = None) -> None:
    """
    Send text to one specific chat_id, or to all TELEGRAM_ALLOWED chats.
    Converts Markdown to Telegram HTML so formatting renders correctly.
    Uses only stdlib — no python-telegram-bot dependency needed.
    """
    token   = _token()
    targets = [chat_id] if chat_id else _chat_ids()
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    body    = _md_to_html(text)

    for cid in targets:
        for chunk in _split(body):
            payload = json.dumps({
                "chat_id":    cid,
                "text":       chunk,
                "parse_mode": "HTML",
            }).encode()
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp.read()
            except urllib.error.HTTPError as e:
                body_err = e.read().decode(errors="replace")
                raise RuntimeError(f"Telegram API error {e.code}: {body_err}") from e
