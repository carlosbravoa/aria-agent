"""
aria/tools/notify.py — Send a push message to the user.

Allows the agent (and scheduled tasks) to push results proactively.
No TTY or interactive session required — works from cron, scripts, nohup.

Routing goes through the channel plugin registry (aria.channels):
  - during a channel turn (aria.context) the message goes to THAT channel's
    plugin, which replies in the active conversation (a Telegram turn → that
    chat, a WhatsApp turn → that number). It is never silently rerouted: a
    channel with no plugin, or one that cannot push, gets an error instead.
  - outside any channel — REPL, supervisor tasks, cron, `aria --notify` — it
    goes to channels.push_channel(): ARIA_NOTIFY_CHANNEL, else Telegram (the
    historical behaviour), else the first enabled channel that can push. The
    plugin broadcasts to its allow-list.
  - an explicit `channel` argument ("send it to me on Vicus") picks that
    channel instead. When it is the active turn's channel the reply still
    lands in that conversation; any other channel broadcasts to its allow-list.

The agent should use this tool when:
  - It finishes a long-running task and needs to report results
  - A scheduled task asks it to send a summary
  - The user explicitly asks to be notified
"""

from __future__ import annotations

import os

DEFINITION = {
    "name": "notify",
    "description": (
        "Send a push notification message to the user. Delivered on the "
        "channel of the current conversation (replies in place); outside a "
        "conversation it goes to the configured notification channel. "
        "Use this to deliver results of scheduled tasks, summaries, or any "
        "output the user should receive as a notification. Set `channel` when "
        "the user names one (e.g. 'message me on Vicus')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text to send. Keep it concise and human-readable.",
            },
            "channel": {
                "type": "string",
                "description": (
                    "Optional channel name to send on, e.g. 'vicus', 'telegram', "
                    "'whatsapp'. Omit to use the current conversation's channel "
                    "or the default notification channel."
                ),
            },
        },
        "required": ["message"],
    },
}


def _route() -> str | None:
    """Name of the outbound channel for this turn: the active turn's channel,
    else the default push channel (None when no channel can take it)."""
    from aria import channels, context
    active = context.current()
    if active:
        return active.channel
    plugin = channels.push_channel()
    return plugin.name if plugin else None


def available() -> list[str]:
    """Names of the enabled channels that can push, for error messages."""
    from aria import channels
    return [p.name for p in channels.enabled() if p.supports_push]


def unknown_channel(tag: str, name: str, names: list[str]) -> str:
    return (f"[{tag}] No channel named '{name}'. Available: "
            f"{', '.join(names) or 'none — enable one with ARIA_CHANNELS'}.")


def _not_wired(channel: str) -> str:
    return (
        f"[notify error] Push notifications are not wired for the "
        f"'{channel}' channel. Put the message in your normal reply "
        f"instead."
    )


def execute(args: dict) -> str:
    message = args.get("message", "").strip()
    if not message:
        return "[notify] No message provided."

    try:
        from aria import channels, config, context
        config.load()

        active = context.current()
        wanted = (args.get("channel") or "").strip().lower()
        if wanted:
            plugin = channels.get(wanted)
            if plugin is None:
                return unknown_channel("notify error", wanted, available())
            if not plugin.supports_push:
                return _not_wired(wanted)
        elif active:
            # Reply on the channel the user is talking on — never misroute.
            plugin = channels.get(active.channel)
            if plugin is None or not plugin.supports_push:
                return _not_wired(active.channel)
        else:
            plugin = channels.push_channel()
            if plugin is None:
                override = os.environ.get("ARIA_NOTIFY_CHANNEL", "").strip()
                if override:
                    return _not_wired(override)
                return ("[notify error] No channel is enabled for notifications — "
                        "set ARIA_NOTIFY_CHANNEL or enable a channel (ARIA_CHANNELS).")
            if not plugin.supports_push:
                return _not_wired(plugin.name)

        # to=None: the plugin resolves the active conversation itself, or
        # broadcasts to its allow-list outside a channel.
        from aria.channels.output import convert
        plugin.send(convert(message, plugin.output), to=None)
        from aria.channels import autostart
        started = autostart.take_note()
        return "[notify] Message sent." + (f" ({started})" if started else "")
    except RuntimeError as e:
        return f"[notify error] {e}"
    except Exception as e:
        return f"[notify error] Unexpected error: {e}"
