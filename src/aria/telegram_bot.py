"""
aria/telegram_bot.py — Telegram interface for Aria.

Each allowed user gets their own Agent instance (isolated history + session log).
Shell commands that require confirmation are auto-declined in Telegram mode
since there's no interactive terminal to confirm them.

Setup:
  1. Add to ~/.aria/.env:
       TELEGRAM_TOKEN=<your bot token>
       TELEGRAM_ALLOWED=<comma-separated list of your chat IDs>
       # Get your chat ID by messaging @userinfobot on Telegram

  2. Run:
       aria-telegram
       # or: python -m aria.telegram_bot

Dependencies (install separately):
  pip install python-telegram-bot>=21
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from aria import config
from aria.agent import Agent

log = logging.getLogger(__name__)

# Per-user agent registry  {chat_id: Agent}
_agents: dict[int, Agent] = {}


def _get_agent(chat_id: int) -> Agent:
    """Return the Agent for this chat, creating one if needed."""
    if chat_id not in _agents:
        _agents[chat_id] = Agent()
    return _agents[chat_id]


def _allowed_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED", "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def _is_allowed(update: Update) -> bool:
    allowed = _allowed_ids()
    if not allowed:
        log.warning("TELEGRAM_ALLOWED is not set — rejecting all users.")
        return False
    return update.effective_chat.id in allowed  # type: ignore[union-attr]


# ── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = _get_agent(update.effective_chat.id)  # type: ignore[union-attr]
    await update.message.reply_text(  # type: ignore[union-attr]
        f"✦ *{agent.name}* ready\\.\n\n"
        "Send me a message to chat\\. Commands:\n"
        "`/memory` — show memory\n"
        "`/tools` — list tools\n"
        "`/clear` — clear conversation history\n"
        "`/save <note>` — save a note to memory",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = _get_agent(update.effective_chat.id)  # type: ignore[union-attr]
    memory = agent.ws.load_memory()
    await update.message.reply_text(memory or "_Nothing stored yet._")  # type: ignore[union-attr]


async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = _get_agent(update.effective_chat.id)  # type: ignore[union-attr]
    lines = [f"• *{t['function']['name']}* — {t['function']['description'].splitlines()[0]}"
             for t in agent.tool_schemas]
    await update.message.reply_text("\n".join(lines) or "No tools loaded.", parse_mode=ParseMode.MARKDOWN)  # type: ignore[union-attr]


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = _get_agent(update.effective_chat.id)  # type: ignore[union-attr]
    agent.history = agent._few_shot_examples()
    await update.message.reply_text("History cleared.")  # type: ignore[union-attr]


async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    note = " ".join(context.args or [])
    if not note:
        await update.message.reply_text("Usage: /save <note>")  # type: ignore[union-attr]
        return
    agent = _get_agent(update.effective_chat.id)  # type: ignore[union-attr]
    agent.ws.append_memory(note)
    await update.message.reply_text(f"Saved: {note}")  # type: ignore[union-attr]


# ── Message handler ───────────────────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await update.message.reply_text("Unauthorised.")  # type: ignore[union-attr]
        return

    chat_id = update.effective_chat.id  # type: ignore[union-attr]
    user_text = update.message.text or ""  # type: ignore[union-attr]

    # Show typing indicator while processing
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    agent = _get_agent(chat_id)

    # Run the agent in a thread so the event loop stays free
    loop = asyncio.get_event_loop()
    reply = await loop.run_in_executor(None, agent.chat_collect, user_text)

    # Clean up the reply: strip the "Agent: " prefix chat() adds
    prefix = f"\n{agent.name}: "
    if reply.startswith(prefix.strip()):
        reply = reply[len(prefix.strip()):].strip()

    if reply:
        # Split long replies (Telegram max is 4096 chars)
        for chunk in _split(reply, 4000):
            await update.message.reply_text(chunk)  # type: ignore[union-attr]


def _split(text: str, max_len: int) -> list[str]:
    """Split text into chunks without breaking words."""
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


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    config.load()

    from aria.setup import is_first_run, run as setup_run
    if is_first_run():
        setup_run()

    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        raise SystemExit(
            "TELEGRAM_TOKEN not set.\n"
            "Add it to ~/.aria/.env:\n"
            "  TELEGRAM_TOKEN=<your bot token>"
        )

    logging.basicConfig(level=logging.INFO)

    app = (
        Application.builder()
        .token(token)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("tools",  cmd_tools))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(CommandHandler("save",   cmd_save,  has_args=True))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Aria Telegram bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
