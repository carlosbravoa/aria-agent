"""
aria/tools/remember.py — Manage permanent facts about the user in core memory.

Replaces the legacy `REMEMBER:` text sentinel. In the native tool-calling engine
(2.0) the model persists user facts by calling this tool; the agent treats any
message content accompanying the call as the user-facing answer. Beyond saving,
the model can now `list` stored facts and `forget` a wrong one — previously a
mistaken fact could only be removed by hand-editing the memory file.
"""

from __future__ import annotations

DEFINITION = {
    "name": "remember",
    "description": (
        "Manage permanent facts about the user in core memory. action='add' "
        "(default) saves a fact that is always true — name, role, timezone, "
        "language, preferences, recurring contacts; call it the moment you learn "
        "one (you can answer in the same turn). action='list' shows what is "
        "stored. action='forget' removes every stored fact matching a phrase — "
        "use it when the user corrects something you saved wrong."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "forget"],
                "description": "add (default), list, or forget.",
            },
            "fact": {
                "type": "string",
                "description": "For add: the fact as a short declarative sentence. "
                               "For forget: a phrase; every stored fact containing "
                               "it (case-insensitive) is removed.",
            },
        },
        "required": [],
    },
}


def execute(args: dict) -> str:
    action = (args.get("action") or "add").strip().lower()
    try:
        from aria import config
        from aria.workspace import Workspace

        ws = Workspace(config.workspace_dir())

        if action == "list":
            facts = ws.list_memory_facts("core.md")
            if not facts:
                return "[remember] Core memory is empty."
            return "[remember] Core memory:\n" + "\n".join(facts)

        if action == "forget":
            query = (args.get("fact") or "").strip()
            if not query:
                return "[remember] No phrase provided to forget."
            n = ws.forget_memory(query, "core.md")
            return (f"[remember] Removed {n} fact(s) matching {query!r}."
                    if n else f"[remember] No stored fact matched {query!r}.")

        fact = (args.get("fact") or "").strip()
        if not fact:
            return "[remember] No fact provided."
        ws.append_memory(f"- {fact}")
        return f"[remember] Saved to core memory: {fact}"
    except Exception as e:
        return f"[remember error] {e}"
