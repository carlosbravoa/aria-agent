"""
aria/channels/vicus — Vicus, the end-to-end-encrypted (MLS) messenger, as a channel.

Aria takes part in Vicus as its own account (a bot account invited into the
tenant) with its own device. Vicus has no Python MLS binding, and its client
protocol (PROTOCOL.md §7) is exacting, so the transport is a small Node
sidecar (the repo's top-level `vicus/bridge.mjs`) that drives the reference
client — `VicusClient` plus the MLS crate built to wasm — loaded at runtime
from a Vicus checkout (VICUS_SOURCE_DIR). No Vicus code ships with Aria.

The sidecar speaks JSON lines on stdio with runner.py, which routes messages
through the Aria host like any channel. Sessions are per conversation
(`vicus:<convId>`); only accounts in VICUS_ALLOWED are answered, and in group
conversations Aria only answers when mentioned (VICUS_GROUP_REPLIES=mention,
default; `all` answers everything).

Settings (~/.aria/.env):
  VICUS_SITE           the deployment's web address (its /config.json is read)
  VICUS_EMAIL          the bot account's sign-in email
  VICUS_PASSWORD       its password
  VICUS_ALLOWED        accounts Aria answers (comma-separated emails)
  VICUS_SOURCE_DIR     a built Vicus checkout (see vicus/README.md)
  VICUS_GROUP_REPLIES  mention (default) | all
  VICUS_STATE_DIR      device state (default ~/.aria/vicus) — MLS keys: keep it private
  VICUS_DISPLAY_NAME   name shown to people (default: AGENT_NAME)
"""

from __future__ import annotations

import os
from pathlib import Path

from aria.channels.base import ChannelPlugin, ConfigField, Note


class VicusChannel(ChannelPlugin):
    name = "vicus"
    title = "Vicus"
    description = "Vicus encrypted messenger  (needs Node.js + a Vicus checkout)"
    setup_help = ("Aria needs its own Vicus account (invite one, e.g. aria@your-domain) "
                  "and a built Vicus checkout — see vicus/README.md")
    config_fields = (
        ConfigField("VICUS_SITE", prompt="VICUS_SITE", required=True,
                    help="The deployment's web address, e.g. https://vicus.example.org"),
        ConfigField("VICUS_EMAIL", prompt="VICUS_EMAIL", required=True,
                    help="Aria's own Vicus account (not yours)"),
        ConfigField("VICUS_PASSWORD", prompt="VICUS_PASSWORD", secret=True, required=True),
        ConfigField("VICUS_ALLOWED", prompt="VICUS_ALLOWED", required=True,
                    help="Accounts Aria answers, comma-separated emails"),
        ConfigField("VICUS_SOURCE_DIR", prompt="VICUS_SOURCE_DIR", required=True,
                    help="Path to a built Vicus checkout"),
        ConfigField("VICUS_GROUP_REPLIES", prompt="VICUS_GROUP_REPLIES", default="mention",
                    help="In group conversations: mention (reply when mentioned) or all"),
    )
    supports_files = True
    supports_attached = True

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        import threading
        from aria.channels.vicus import runner
        runner.serve(threading.Event(), service=True)

    def start(self, stop) -> None:
        from aria.channels.vicus import runner
        runner.serve(stop, service=False)

    # ── Outbound ──────────────────────────────────────────────────────────────

    def send(self, text: str, to: str | None = None) -> None:
        """`to` is a conversation id; None → the one-to-one conversation with
        each allowed account."""
        from aria.channels.vicus import runner
        runner.request_send(text, to)

    def send_file(self, path: Path, caption: str = "", to: str | None = None) -> str:
        from aria.channels.vicus import runner
        if not to:
            raise RuntimeError("sending a file on Vicus needs a conversation")
        return runner.request_send_file(Path(path), caption, to)

    # ── Installation ──────────────────────────────────────────────────────────

    def install(self, dry_run: bool = False) -> list[Note]:
        """Deploy the sidecar next to the other bridge files and check what it
        needs from the Vicus checkout."""
        from aria.channels.vicus import deploy
        return deploy.install(dry_run)


PLUGIN = VicusChannel()


def state_dir() -> Path:
    raw = os.environ.get("VICUS_STATE_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".aria" / "vicus"
