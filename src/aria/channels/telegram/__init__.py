"""
aria/channels/telegram — the built-in Telegram channel plugin.

  bot.py     the long-running bot (python-telegram-bot polling loop)
  notify.py  push-only sender (stdlib urllib; httpx for file uploads)

Import rule: this module is imported whenever the channel registry is
consulted, so it stays cheap — python-telegram-bot and httpx are only imported
inside run() / send_file().
"""

from __future__ import annotations

from pathlib import Path

from aria.channels.base import ChannelPlugin, ConfigField, ServiceSpec


class TelegramChannel(ChannelPlugin):
    name = "telegram"
    title = "Telegram"
    # Same label the pre-plugin installer menu showed.
    description = "Telegram bot  (aria-telegram + aria --notify)"
    setup_help = "Get token from @BotFather — get your chat ID from @userinfobot"
    # What the installer used to infer the feature from (pre-plugin installs).
    legacy_keys = ("TELEGRAM_TOKEN",)
    config_fields = (
        ConfigField(key="TELEGRAM_TOKEN", secret=True, required=True,
                    ),
        ConfigField(key="TELEGRAM_ALLOWED", required=True,
                    help="Comma-separated chat IDs allowed to use the bot"),
    )
    supports_files = True

    def run(self) -> None:
        from aria.channels.telegram import bot
        bot.main()

    @staticmethod
    def _chat_id(to: str | None) -> int | None:
        if to is None or str(to).strip() == "":
            return None     # active turn's chat, else broadcast to TELEGRAM_ALLOWED
        s = str(to).strip()
        if not s.lstrip("-").isdigit():
            raise RuntimeError(f"invalid Telegram chat id: {to!r}")
        return int(s)

    def send(self, text: str, to: str | None = None) -> None:
        from aria.channels.telegram import notify
        notify.send(text, chat_id=self._chat_id(to))

    def send_file(self, path: Path, caption: str = "", to: str | None = None) -> str:
        from aria.channels.telegram import notify
        return notify.send_document(path, caption=caption, chat_id=self._chat_id(to))

    def services(self) -> list[ServiceSpec]:
        # Legacy unit name/description — existing systemd units stay unchanged.
        return [ServiceSpec(unit="aria-telegram", description="Aria Telegram Bot",
                            exec_start=("aria-telegram",))]


PLUGIN = TelegramChannel()
