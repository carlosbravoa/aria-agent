"""
aria/tools/learn.py — Save an operational/procedural note to operational memory.

Replaces the legacy `LEARN:` text sentinel. Operational memory is injected into
the system prompt as non-mandatory hints from past sessions; it is capped at
ARIA_OPSMEM_MAX_LINES and pruned by reflection.
"""

from __future__ import annotations

DEFINITION = {
    "name": "learn",
    "description": (
        "Save an operational note — how to be useful in this user's context: "
        "which accounts/tools to use for a task, Jira project keys, calendar IDs, "
        "recurring task patterns, shortcuts discovered while using tools. The more "
        "you learn, the less you re-derive each session. If you find a better "
        "approach than a past note, record the new one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "procedure": {
                "type": "string",
                "description": "The procedure or shortcut to remember, as a short note.",
            },
        },
        "required": ["procedure"],
    },
}


def execute(args: dict) -> str:
    procedure = (args.get("procedure") or "").strip()
    if not procedure:
        return "[learn] No procedure provided."
    try:
        from aria import config
        from aria.workspace import Workspace

        ws = Workspace(config.workspace_dir())
        ws.append_operational_memory(f"- {procedure}")
        return f"[learn] Saved to operational memory: {procedure}"
    except Exception as e:
        return f"[learn error] {e}"
