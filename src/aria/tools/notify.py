"""
aria/tools/notify.py — Send a message to the user via Telegram.

Allows the agent (and scheduled tasks) to push results proactively.
No TTY or interactive session required — works from cron, scripts, nohup.

The agent should use this tool when:
  - It finishes a long-running task and needs to report results
  - A scheduled task asks it to send a summary
  - The user explicitly asks to be notified on Telegram
"""

from __future__ import annotations

DEFINITION = {
    "name": "notify",
    "description": (
        "Send a message to the user via Telegram. "
        "Use this to deliver results of scheduled tasks, summaries, or any "
        "output the user should receive as a Telegram notification."
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


def execute(args: dict) -> str:
    message = args.get("message", "").strip()
    if not message:
        return "[notify] No message provided."

    try:
        from aria import config
        config.load()
        from aria.telegram_notify import send
        send(message)
        return "[notify] Message sent."
    except RuntimeError as e:
        return f"[notify error] {e}"
    except Exception as e:
        return f"[notify error] Unexpected error: {e}"
