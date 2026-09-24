"""
aria/telegram_bot.py — Telegram bot interface.

Session model: one Agent per (channel, chat_id) — history is isolated per
(telegram, chat_id) but workspace/memory is shared with other channels.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from aria import attachments, config, __version__
from aria.channel import get_session, handle, shutdown
from aria.telegram_notify import _split  # single shared implementation

log     = logging.getLogger(__name__)
CHANNEL = "telegram"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_allowed(update: Update) -> bool:
    allowed_raw = os.environ.get("TELEGRAM_ALLOWED", "")
    allowed     = {s.strip() for s in allowed_raw.split(",") if s.strip()}
    chat_id     = str(update.effective_chat.id)  # type: ignore[union-attr]
    return chat_id in allowed


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
    if hasattr(agent, "clear_session"):
        agent.clear_session()   # also resets the persisted window + plan
    else:
        agent.history = list(agent._seed)
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


# ── Live progress bridge (sync agent loop ↔ async bot) ────────────────────────

_MAX_RETRY_AFTER = 30.0   # cap flood-control sleeps so a turn can't hang


def _retry_seconds(exc: RetryAfter) -> float:
    """RetryAfter.retry_after is an int or (PTB >= 22) a timedelta."""
    ra = exc.retry_after
    secs = ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)
    return max(0.0, min(secs, _MAX_RETRY_AFTER))


class _Progress:
    """Bridges the synchronous agent loop (run in a worker thread) to the bot's
    asyncio loop: keeps the typing indicator alive, maintains ONE live tool-trail
    message it edits as steps complete, and streams each response the moment it's
    produced. The agent calls `activity`/`response` from the worker thread; both
    hop onto the loop via run_coroutine_threadsafe and block for ordering."""

    def __init__(self, bot, chat_id: str, loop) -> None:
        self.bot       = bot
        self.chat_id   = int(chat_id)
        self.loop      = loop
        self.status_id = None          # message_id of the live trail (lazy)
        self.steps: list[str] = []
        self.sent      = 0             # responses streamed (0 → send a fallback)
        self.undelivered: list[str] = []   # text that never went out (flushed at end)
        self._alive    = True
        self._task     = None
        self._show_trail = os.environ.get(
            "ARIA_TELEGRAM_PROGRESS", "on").strip().lower() not in (
            "off", "0", "false", "no")

    # ---- async side (runs on the event loop) --------------------------------
    async def _typing_loop(self) -> None:
        while self._alive:
            try:
                await self.bot.send_chat_action(self.chat_id, ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.sleep(4)
            except asyncio.CancelledError:
                break

    async def _update_trail(self, detail: str) -> None:
        self.steps.append(detail)
        text = "🛠 " + "  ·  ".join(self.steps[-6:])
        try:
            if self.status_id is None:
                msg = await self.bot.send_message(self.chat_id, text)
                self.status_id = msg.message_id
            else:
                await self.bot.edit_message_text(
                    text, chat_id=self.chat_id, message_id=self.status_id)
        except Exception:
            pass

    async def _send_chunk(self, chunk: str) -> bool:
        """Send one chunk as HTML, falling back to plain text. A RetryAfter
        (flood control) sleeps the requested time and retries once."""
        from aria.telegram_notify import _md_to_html
        for html_mode in (True, False):
            body = _md_to_html(chunk) if html_mode else chunk
            mode = "HTML" if html_mode else None
            for attempt in range(2):
                try:
                    await self.bot.send_message(self.chat_id, body, parse_mode=mode)
                    return True
                except RetryAfter as exc:
                    if attempt:
                        log.error("stream send rate-limited twice: %s", exc)
                        return False
                    await asyncio.sleep(_retry_seconds(exc))
                except Exception as exc:
                    log.error("stream send failed (%s): %s",
                              "HTML" if html_mode else "plain", exc)
                    break           # HTML rejected → try plain text
        return False

    async def _send_chunks(self, chunks: list[str]) -> list[str]:
        """Send chunks in order; return the ones that could not be delivered."""
        return [c for c in chunks if not await self._send_chunk(c)]

    async def _send_response(self, text: str) -> bool:
        """Send the response (chunked). Returns True if at least one chunk was
        delivered. A partial delivery retries the missing chunks once; any still
        missing are kept in `undelivered` for _run_turn to flush at the end, so
        the fallback never re-sends (duplicates) the chunks that did go out."""
        chunks = [c for c in _split(text) if c.strip()]
        failed = await self._send_chunks(chunks)
        if not failed:
            return True
        if len(failed) == len(chunks):
            return False
        failed = await self._send_chunks(failed)
        self.undelivered.extend(failed)
        return True

    # ---- worker-thread side (agent callbacks) -------------------------------
    def activity(self, detail: str) -> None:
        if not self._show_trail:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._update_trail(detail), self.loop).result(timeout=10)
        except Exception:
            pass

    def response(self, text: str) -> None:
        # Only count the response as sent if it actually went out. Incrementing
        # before the attempt meant a failed send both lost the message AND made
        # `progress.sent > 0` suppress the fallback in _run_turn — a silent total
        # loss. Now a failed stream leaves sent==0 so the fallback re-sends it.
        try:
            delivered = asyncio.run_coroutine_threadsafe(
                self._send_response(text), self.loop).result(timeout=300)
        except Exception:
            delivered = False
        if delivered:
            self.sent += 1
        else:
            # Keep it: if an earlier response did go out (sent > 0) the
            # whole-turn fallback won't fire, so _run_turn flushes these.
            self.undelivered.append(text)

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._typing_loop())

    async def stop(self) -> None:
        self._alive = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass


# ── Message handler ───────────────────────────────────────────────────────────

async def _run_turn(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    chat_id: str, user_text: str) -> None:
    """Run one agent turn with the live progress bridge attached.

    Shared by text messages and attachments so both get streaming, the typing
    heartbeat, and the tool trail.
    """
    loop     = asyncio.get_running_loop()
    progress = _Progress(context.bot, chat_id, loop)
    progress.start()
    try:
        responses = await loop.run_in_executor(
            None,
            lambda: handle(CHANNEL, chat_id, user_text,
                           response_cb=progress.response,
                           activity_cb=progress.activity),
        )
    except Exception as exc:
        log.error("handle() raised exception for chat %s: %s", chat_id, exc, exc_info=True)
        responses = [f"Sorry, something went wrong: {exc}"]
    finally:
        await progress.stop()

    # Responses were already streamed via progress.response as they were produced.
    # Only fall back to a direct send if nothing was streamed (e.g. a hard error
    # before any response, or an unexpected empty turn).
    if progress.sent == 0:
        if not responses:
            log.warning("Empty responses for chat %s input: %r", chat_id, user_text[:80])
            responses = ["(no response)"]
        for response in responses:
            await _reply(update, response)
    elif progress.undelivered:
        for text in progress.undelivered:
            await _reply(update, text)


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

    await _run_turn(update, context, chat_id, user_text)


# ── Attachments ───────────────────────────────────────────────────────────────

# Telegram's Bot API refuses getFile for anything larger. Not something we can
# raise without running a self-hosted Bot API server.
_MAX_DOWNLOAD = 20 * 1024 * 1024


def _pick_attachment(msg) -> tuple[str, str | None, str | None, int | None, str] | None:
    """Return (file_id, filename, mime, size, kind) for a message's attachment."""
    if msg.document:
        d = msg.document
        return d.file_id, d.file_name, d.mime_type, d.file_size, "document"
    if msg.photo:
        p = msg.photo[-1]            # last entry is the largest rendition
        return p.file_id, f"photo_{p.file_unique_id}.jpg", "image/jpeg", p.file_size, "photo"
    if msg.voice:
        v = msg.voice
        return (v.file_id, f"voice_{v.file_unique_id}.ogg",
                v.mime_type or "audio/ogg", v.file_size, "voice")
    if msg.audio:
        a = msg.audio
        return (a.file_id, a.file_name or f"audio_{a.file_unique_id}.mp3",
                a.mime_type, a.file_size, "audio")
    if msg.video:
        v = msg.video
        return (v.file_id, v.file_name or f"video_{v.file_unique_id}.mp4",
                v.mime_type, v.file_size, "video")
    if msg.video_note:
        v = msg.video_note
        return v.file_id, f"videonote_{v.file_unique_id}.mp4", "video/mp4", v.file_size, "video_note"
    if msg.animation:
        a = msg.animation
        return (a.file_id, a.file_name or f"animation_{a.file_unique_id}.mp4",
                a.mime_type, a.file_size, "animation")
    return None


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download an incoming file into the workspace inbox, then run a turn so
    the agent can read it with file_access like any other path."""
    if not _is_allowed(update):
        await update.message.reply_text("Unauthorised.")  # type: ignore[union-attr]
        return

    msg     = update.message
    chat_id = str(update.effective_chat.id)  # type: ignore[union-attr]
    picked  = _pick_attachment(msg)
    if picked is None:
        await _reply(update, "I can't handle that kind of attachment yet.")
        return

    file_id, filename, mime, size, kind = picked
    if size and size > _MAX_DOWNLOAD:
        await _reply(
            update,
            f"That file is {attachments.human_size(size)} — Telegram only lets "
            f"bots download files up to 20 MB, so I can't fetch it. Could you "
            f"send a smaller version, or put it somewhere I can reach?"
        )
        return

    dest = attachments.destination(CHANNEL, chat_id, filename)
    try:
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=str(dest))
    except Exception as exc:
        log.error("attachment download failed for chat %s: %s", chat_id, exc)
        await _reply(update, f"I couldn't download that file: {exc}")
        return
    attachments.finalize(dest)

    log.info("Saved %s attachment for chat %s to %s", kind, chat_id, dest)
    user_text = attachments.describe(
        dest, channel=CHANNEL, kind=kind, original_name=filename,
        mime=mime, size=size, caption=msg.caption or "",
    )
    await _run_turn(update, context, chat_id, user_text)


# ── Polling stall watchdog ────────────────────────────────────────────────────
#
# PTB's network_retry_loop retries NetworkError forever, but a ReadError can
# leave the single getUpdates httpx connection (pool size 1) in a broken state
# where every subsequent poll dies on TimedOut/PoolTimeout — retried instantly
# and logged only at DEBUG, i.e. the bot wedges silently until someone restarts
# it. The watchdog stamps every successful getUpdates round-trip; if none
# succeeds for ARIA_TELEGRAM_STALL_MIN minutes it hard-exits the process so
# systemd (Restart=on-failure) brings up a fresh one. os._exit is deliberate:
# the event loop is not trustworthy at that point, and the conversation window
# self-trims on every append so there is nothing to flush.

_STALL_CHECK_SEC = 30


def _stall_seconds() -> float:
    """ARIA_TELEGRAM_STALL_MIN in seconds; 0 disables (default 10 minutes)."""
    raw = os.environ.get("ARIA_TELEGRAM_STALL_MIN", "10").strip().lower()
    if raw in ("off", "no", "false", ""):
        return 0.0
    try:
        minutes = float(raw)
    except ValueError:
        return 600.0
    return max(0.0, minutes * 60)


class _StallWatchdog:
    def __init__(self, stall_seconds: float, *, clock=time.monotonic,
                 on_stall=None) -> None:
        self.stall_seconds = stall_seconds
        self._clock    = clock
        self._last_ok  = clock()
        self._on_stall = on_stall or self._exit_for_restart

    def beat(self) -> None:
        self._last_ok = self._clock()

    def stalled(self) -> bool:
        return (self._clock() - self._last_ok) > self.stall_seconds

    def check(self) -> bool:
        """Fire on_stall if stalled. Returns True when it fired."""
        if not self.stalled():
            return False
        self._on_stall()
        return True

    def _exit_for_restart(self) -> None:
        log.error(
            "No successful getUpdates for %.0f min — polling loop is wedged. "
            "Exiting so systemd can restart the service.",
            self.stall_seconds / 60,
        )
        logging.shutdown()
        os._exit(75)  # EX_TEMPFAIL; nonzero → Restart=on-failure fires

    def start(self) -> None:
        def loop() -> None:
            while True:
                time.sleep(_STALL_CHECK_SEC)
                self.check()
        threading.Thread(target=loop, name="tg-stall-watchdog",
                         daemon=True).start()


class _WatchdogRequest(HTTPXRequest):
    """getUpdates-only request object (the builder keeps it separate from the
    message-sending pool) that beats the watchdog on every completed
    round-trip. Exceptions don't beat — only actual responses from Telegram
    count as 'polling works'."""

    def __init__(self, watchdog: _StallWatchdog) -> None:
        super().__init__(connection_pool_size=1)  # PTB's own get_updates default
        self._watchdog = watchdog

    async def do_request(self, *args, **kwargs):
        result = await super().do_request(*args, **kwargs)
        self._watchdog.beat()
        return result


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

    builder  = Application.builder().token(token)
    watchdog = None
    stall    = _stall_seconds()
    if stall > 0:
        watchdog = _StallWatchdog(stall)
        builder  = builder.get_updates_request(_WatchdogRequest(watchdog))
    app = builder.build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("tools",  cmd_tools))
    app.add_handler(CommandHandler("clear",  cmd_clear))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("model",  cmd_model))
    app.add_handler(CommandHandler("models", cmd_model))   # alias
    app.add_handler(CommandHandler("save",   cmd_save, has_args=True))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    # Explicit media filter rather than filters.ATTACHMENT, which also matches
    # locations, contacts, polls and dice — none of which are files.
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO
        | filters.VOICE | filters.VIDEO_NOTE | filters.ANIMATION,
        on_media,
    ))

    log.info("Telegram bot starting...")
    if watchdog is not None:
        watchdog.start()
    try:
        # bootstrap_retries=-1: if we come up while the network is still down
        # (e.g. right after a watchdog restart), retry the bootstrap phase
        # forever with backoff instead of exiting immediately — five fast exits
        # inside 300s would trip StartLimitBurst and leave the service dead.
        app.run_polling(drop_pending_updates=True, bootstrap_retries=-1)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
