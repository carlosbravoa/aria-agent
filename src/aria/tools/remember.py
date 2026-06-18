"""
aria/tools/remember.py — Save a permanent fact about the user to core memory.

Replaces the legacy `REMEMBER:` text sentinel. In the native tool-calling engine
(2.0) the model persists user facts by calling this tool; the agent treats any
message content accompanying the call as the user-facing answer.
"""

from __future__ import annotations

DEFINITION = {
    "name": "remember",
    "description": (
        "Save a permanent fact about the user to core memory. Use for things that "
        "are always true: the user's name, role, timezone, language, preferences, "
        "recurring contacts. Call this the moment you learn such a fact — you can "
        "answer the user in the same turn."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "The fact to remember, as a short declarative sentence.",
            },
        },
        "required": ["fact"],
    },
}


def execute(args: dict) -> str:
    fact = (args.get("fact") or "").strip()
    if not fact:
        return "[remember] No fact provided."
    try:
        from aria import config
        from aria.workspace import Workspace

        ws = Workspace(config.workspace_dir())
        ws.append_memory(f"- {fact}")
        return f"[remember] Saved to core memory: {fact}"
    except Exception as e:
        return f"[remember error] {e}"
