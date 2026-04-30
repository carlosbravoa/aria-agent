"""
aria/telegram_bot.py — Telegram interface for Aria.

Uses the shared channel session registry so history is isolated per
(telegram, chat_id) but workspace/memory is shared with other channels.

Setup:
  1. Add to ~/.aria/.env:
       TELEGRAM_TOKEN=<your bot token>
       TELEGRAM_ALLOWED=<comma-separated chat IDs>

  2. Run:
       aria-telegram

Dependencies:
  pip install "aria-agent[telegram]"
"""

from __future__ import annotations

import asyncio
import logging
import os

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from aria import config
from aria.channel import get_session, handle

log = logging.getLogger(__name__)

CHANNEL = "telegram"


def _allowed_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED", "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def _is_allowed(update: Update) -> bool:
    allowed = _allowed_ids()
    if not allowed:
        log.warning("TELEGRAM_ALLOWED is not set — rejecting all users.")
        return False
    return update.effective_chat.id in allowed  # type: ignore[union-attr]


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
    return (chunks + [buf]) if buf else (chunks or [text[:max_len]])


# ── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    await update.message.reply_text(  # type: ignore[union-attr]
        f"✦ {agent.name} ready.\n\n"
        "Commands: /memory /tools /clear /save <note> /version"
    )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    await update.message.reply_text(agent.ws.load_memory() or "_Nothing stored yet._")  # type: ignore[union-attr]


async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    lines = [f"• {t['function']['name']} — {t['function']['description'].splitlines()[0]}"
             for t in agent.tool_schemas]
    await update.message.reply_text("\n".join(lines) or "No tools loaded.")  # type: ignore[union-attr]


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    agent.history = agent._few_shot_examples()
    await update.message.reply_text("History cleared.")  # type: ignore[union-attr]


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    from aria import __version__
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    await update.message.reply_text(f"{agent.name} {__version__}")  # type: ignore[union-attr]


async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    note = " ".join(context.args or [])
    if not note:
        await update.message.reply_text("Usage: /save <note>")  # type: ignore[union-attr]
        return
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    agent.ws.append_memory(note)
    await update.message.reply_text(f"Saved: {note}")  # type: ignore[union-attr]


# ── Message handler ───────────────────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await update.message.reply_text("Unauthorised.")  # type: ignore[union-attr]
        return

    chat_id = str(update.effective_chat.id)  # type: ignore[union-attr]
    user_text = update.message.text or ""  # type: ignore[union-attr]

    await context.bot.send_chat_action(
        chat_id=int(chat_id), action=ChatAction.TYPING
    )

    loop = asyncio.get_event_loop()
    reply = await loop.run_in_executor(None, handle, CHANNEL, chat_id, user_text)

    for chunk in _split(reply):
        if chunk.strip():
            await update.message.reply_text(  # type: ignore[union-attr]
                _md_to_html(chunk), parse_mode="HTML"
            )


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    config.load()

    from aria.setup import is_first_run, run as setup_run
    if is_first_run():
        setup_run()

    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        raise SystemExit(
            "TELEGRAM_TOKEN not set.\nAdd it to ~/.aria/.env:\n  TELEGRAM_TOKEN=<token>"
        )

    logging.basicConfig(level=logging.INFO)

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("tools",  cmd_tools))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("save",   cmd_save, has_args=True))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Telegram bot starting...")
    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        from aria.channel import shutdown
        shutdown()


if __name__ == "__main__":
    main()
