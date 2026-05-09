"""
aria/telegram_bot.py — Telegram bot interface.

Session model: one Agent per (channel, chat_id) — history is isolated per
(telegram, chat_id) but workspace/memory is shared with other channels.
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

from aria import config, __version__
from aria.channel import get_session, handle, shutdown

log     = logging.getLogger(__name__)
CHANNEL = "telegram"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_allowed(update: Update) -> bool:
    allowed_raw = os.environ.get("TELEGRAM_ALLOWED", "")
    allowed     = {s.strip() for s in allowed_raw.split(",") if s.strip()}
    chat_id     = str(update.effective_chat.id)  # type: ignore[union-attr]
    return chat_id in allowed


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


async def _reply(update: Update, text: str, parse_html: bool = True) -> None:
    """Send a reply, with HTML formatting and plain-text fallback."""
    from aria.telegram_notify import _md_to_html
    for chunk in _split(text):
        if not chunk.strip():
            continue
        try:
            body = _md_to_html(chunk) if parse_html else chunk
            mode = "HTML" if parse_html else None
            await update.message.reply_text(body, parse_mode=mode)  # type: ignore[union-attr]
        except Exception as exc:
            log.error("reply_text failed: %s", exc)
            try:
                await update.message.reply_text(chunk)  # type: ignore[union-attr]
            except Exception as exc2:
                log.error("plain reply also failed: %s", exc2)


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await update.message.reply_text("Unauthorised.")  # type: ignore[union-attr]
        return
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    await _reply(update,
        f"👋 Hi, I'm **{agent.name}** v{__version__}.\n"
        f"Commands: /memory /tools /clear /save /version /model [name] /models"
    )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    await _reply(update, agent.ws.load_memory() or "_Nothing stored yet._")


async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    lines = []
    for t in agent.tool_schemas:
        fn = t["function"]
        lines.append(f"• <b>{fn['name']}</b> — {fn['description'][:60]}")
    await _reply(update, "\n".join(lines) or "No tools loaded.")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    agent.history = agent._few_shot_examples()
    await _reply(update, "History cleared.")


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    await _reply(update, f"{agent.name} v{__version__}")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle both /model and /models."""
    if not _is_allowed(update):
        return
    chat_id = str(update.effective_chat.id)  # type: ignore[union-attr]
    agent   = get_session(CHANNEL, chat_id)
    args    = context.args or []
    if not args:
        # List all profiles
        lines = []
        for p in agent.list_profiles():
            active = " ✓" if p["active"] else ""
            lines.append(f"<code>{p['name']:12}</code> {p['model']}{active}")
        await update.message.reply_text(  # type: ignore[union-attr]
            "\n".join(lines), parse_mode="HTML"
        )
    else:
        result = agent.switch_profile(args[0])
        await _reply(update, result)


async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    note = " ".join(context.args or [])
    if not note:
        await _reply(update, "Usage: /save <note>")
        return
    agent = get_session(CHANNEL, str(update.effective_chat.id))  # type: ignore[union-attr]
    agent.ws.append_memory(note)
    await _reply(update, f"Saved: {note}")


# ── Message handler ───────────────────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await update.message.reply_text("Unauthorised.")  # type: ignore[union-attr]
        return

    chat_id   = str(update.effective_chat.id)  # type: ignore[union-attr]
    user_text = update.message.text or ""  # type: ignore[union-attr]

    # If the user replied to a bot message, prepend the original text
    # so the agent understands what they're responding to.
    replied_to = update.message.reply_to_message  # type: ignore[union-attr]
    if replied_to and replied_to.text:
        original  = replied_to.text.strip()[:500]
        user_text = f"[Replying to: {original}]\n\n{user_text}"

    await context.bot.send_chat_action(
        chat_id=int(chat_id), action=ChatAction.TYPING
    )

    loop = asyncio.get_event_loop()
    try:
        responses = await loop.run_in_executor(None, handle, CHANNEL, chat_id, user_text)
    except Exception as exc:
        log.error("handle() raised exception for chat %s: %s", chat_id, exc, exc_info=True)
        responses = [f"Sorry, something went wrong: {exc}"]

    if not responses:
        log.warning("Empty responses for chat %s input: %r", chat_id, user_text[:80])
        responses = ["(no response)"]

    for response in responses:
        await _reply(update, response)


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
    app.add_handler(CommandHandler("model",  cmd_model))
    app.add_handler(CommandHandler("models", cmd_model))   # alias
    app.add_handler(CommandHandler("save",   cmd_save, has_args=True))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Telegram bot starting...")
    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
