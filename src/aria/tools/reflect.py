"""
aria/tools/reflect.py — Trigger memory reflection from within a conversation.

Allows the agent to proactively analyse past sessions and update its
pattern memory when the user asks about its self-knowledge, or when
it detects that its understanding might be stale.
"""

from __future__ import annotations

DEFINITION = {
    "name": "reflect",
    "description": (
        "Analyse recent conversation history to extract behavioural patterns "
        "and update long-term memory. Use when the user asks you to learn from "
        "past interactions, improve your understanding of their preferences, "
        "or when you want to consolidate insights from previous sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "notify": {
                "type": "boolean",
                "description": "Send a Telegram notification when reflection is complete.",
                "default": False,
            },
        },
    },
}


def execute(args: dict) -> str:
    notify = args.get("notify", False)
    try:
        from aria.reflect import run
        return run(notify=notify)
    except Exception as exc:
        return f"[reflect error] {exc}"
