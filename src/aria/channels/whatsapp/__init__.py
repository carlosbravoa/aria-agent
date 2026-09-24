"""
aria/channels/whatsapp — Built-in WhatsApp channel plugin.

Two processes (unchanged from the pre-plugin layout):
  - bridge.py   the Python HTTP bridge (`aria-whatsapp`), receives messages
                from the Node side and runs them through the agent
  - Node        ~/.aria/whatsapp/bridge.js (whatsapp-web.js), deployed from the
                repo's top-level whatsapp/ dir by deploy.py
Outbound push (notify tool, supervisor) goes through notify.py → the Node
bridge's local push listener.

Top level stays cheap: bridge/notify/deploy are imported inside the methods.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from aria.channels.base import ChannelPlugin, ConfigField, Note, ServiceSpec


def _node_bin() -> str:
    # Same lookup as aria.install._node_bin; fall back to a bare "node" so the
    # unit is still described (it is optional and skipped when node is missing).
    return shutil.which("node") or shutil.which("nodejs") or "node"


class WhatsAppChannel(ChannelPlugin):
    name = "whatsapp"
    title = "WhatsApp"
    # Same label the pre-plugin installer menu showed.
    description = "WhatsApp bridge  (aria-whatsapp, needs Node.js)"
    setup_help = "Needs Node.js and ~/.aria/whatsapp/bridge.js — see README"
    legacy_keys = ("WHATSAPP_ALLOWED",)
    supports_files = False
    config_fields = (
        ConfigField("ARIA_WA_PORT", prompt="ARIA_WA_PORT", default="7532",
                    help="Port for Python↔Node.js bridge"),
        ConfigField("ARIA_WA_PUSH_PORT", prompt="ARIA_WA_PUSH_PORT", default="7533",
                    help="Port the Node bridge listens on for outbound push"),
        ConfigField("ARIA_WA_SECRET", prompt="ARIA_WA_SECRET", secret=True,
                    help="Shared secret between Python and Node.js bridges"),
        ConfigField("WHATSAPP_ALLOWED", prompt="WHATSAPP_ALLOWED", required=True,
                    help="Your number in international format, no + (e.g. 34612345678)"),
    )

    def run(self) -> None:
        from aria.channels.whatsapp import bridge
        bridge.main()

    def send(self, text: str, to: str | None = None) -> None:
        from aria.channels.whatsapp import notify
        notify.send(text, to=to)

    def services(self) -> list[ServiceSpec]:
        bridge_js = Path.home() / ".aria" / "whatsapp" / "bridge.js"
        return [
            ServiceSpec("aria-whatsapp", "Aria WhatsApp Python Bridge", ("aria-whatsapp",)),
            ServiceSpec("aria-whatsapp-node", "Aria WhatsApp Node.js Bridge",
                        (_node_bin(), str(bridge_js)),
                        requires="aria-whatsapp.service", optional=True),
        ]

    def install(self, dry_run: bool = False) -> list[Note]:
        """Deploy/refresh bridge.js + package files into ~/.aria/whatsapp/
        (node_modules/ and the WhatsApp login state are never touched) and
        return the notes the installer used to print."""
        from aria.channels.whatsapp import deploy

        notes: list[Note] = []
        if dry_run:
            src = deploy.source_dir()
            if src is None:
                notes.append(("warn", "WhatsApp bridge source not found — set ARIA_SOURCE_DIR "
                              "to your aria-agent checkout, or copy whatsapp/bridge.js manually."))
            else:
                notes.append(("info", f"[dry-run] would deploy bridge files from {src} → "
                              f"{deploy.dest_dir()}"))
            return notes

        try:
            res = deploy.deploy()
        except Exception as exc:
            notes.append(("warn", f"WhatsApp bridge deploy skipped: {exc}"))
            return notes
        dest: Path = res["dest"]
        if res["error"]:
            notes.append(("warn", res["error"]))
        elif res["copied"]:
            notes.append(("ok", f"Deployed bridge files: {', '.join(res['copied'])} → {dest}"))
        else:
            notes.append(("info", "WhatsApp bridge files already up to date."))
        if res["source"] and (res["package_changed"] or not (dest / "node_modules").exists()):
            npm = "npm ci" if (dest / "package-lock.json").exists() else "npm install"
            # A dependency change is actionable (the Node unit crash-loops
            # without it), so it must survive the updater's ok/warn filter.
            level = "warn" if res["package_changed"] else "info"
            notes.append((level, f"Run: cd {dest} && {npm}"))
        return notes


PLUGIN = WhatsAppChannel()
