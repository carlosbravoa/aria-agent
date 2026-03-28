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

import os
import urllib.error
import urllib.parse
import urllib.request
import json


def _token() -> str:
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        raise RuntimeError(
            "TELEGRAM_TOKEN not set. Add it to ~/.aria/.env"
        )
    return token


def _chat_ids() -> list[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED", "")
    ids = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    if not ids:
        raise RuntimeError(
            "TELEGRAM_ALLOWED not set. Add chat IDs to ~/.aria/.env"
        )
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


def send(text: str, chat_id: int | None = None) -> None:
    """
    Send text to one specific chat_id, or to all TELEGRAM_ALLOWED chats.
    Uses only stdlib — no python-telegram-bot dependency needed.
    """
    token = _token()
    targets = [chat_id] if chat_id else _chat_ids()
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for cid in targets:
        for chunk in _split(text):
            payload = json.dumps({
                "chat_id": cid,
                "text": chunk,
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
                body = e.read().decode(errors="replace")
                raise RuntimeError(f"Telegram API error {e.code}: {body}") from e
