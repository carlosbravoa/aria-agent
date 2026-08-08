"""
aria/tools/notify.py — Send a push message to the user.

Allows the agent (and scheduled tasks) to push results proactively.
No TTY or interactive session required — works from cron, scripts, nohup.

Routing: the message goes to the channel the current turn belongs to
(aria.context). A Telegram turn replies in that same chat (telegram_notify
resolves the active chat id). Outside any channel — REPL, supervisor tasks,
cron, `aria --notify` — it broadcasts to TELEGRAM_ALLOWED, which is the
correct behaviour there.

A WhatsApp turn is delivered over WhatsApp (never silently rerouted to
Telegram): whatsapp_notify.send POSTs to the Node bridge's local push listener,
which calls client.sendMessage for the active turn's number (or broadcasts to
WHATSAPP_ALLOWED outside a channel).

The agent should use this tool when:
  - It finishes a long-running task and needs to report results
  - A scheduled task asks it to send a summary
  - The user explicitly asks to be notified
"""

from __future__ import annotations

DEFINITION = {
    "name": "notify",
    "description": (
        "Send a push notification message to the user. Delivered on the "
        "channel of the current conversation (a Telegram chat replies in "
        "place); outside a channel it broadcasts via Telegram. "
        "Use this to deliver results of scheduled tasks, summaries, or any "
        "output the user should receive as a notification."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message text to send. Keep it concise and human-readable.",
            },
        },
        "required": ["message"],
    },
}


def _route() -> str | None:
    """The outbound channel for this turn: 'telegram' when no channel is
    active (REPL/supervisor/cron broadcast over Telegram), else the active
    turn's channel name."""
    from aria import context
    active = context.current()
    return active.channel if active else "telegram"


def execute(args: dict) -> str:
    message = args.get("message", "").strip()
    if not message:
        return "[notify] No message provided."

    try:
        from aria import config
        config.load()

        channel = _route()

        if channel == "whatsapp":
            # WhatsApp outbound push: POST to the Node bridge's local push
            # listener (whatsapp/bridge.js), which calls client.sendMessage.
            from aria.whatsapp_notify import send as wa_send
            wa_send(message)
            return "[notify] Message sent."

        if channel != "telegram":
            # Unknown future channel — never misroute to Telegram silently.
            return (
                f"[notify error] Push notifications are not wired for the "
                f"'{channel}' channel. Put the message in your normal reply "
                f"instead."
            )

        from aria.telegram_notify import send
        send(message)
        return "[notify] Message sent."
    except RuntimeError as e:
        return f"[notify error] {e}"
    except Exception as e:
        return f"[notify error] Unexpected error: {e}"
