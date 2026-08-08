"""
aria/tools/learn.py — Manage operational/procedural notes in operational memory.

Replaces the legacy `LEARN:` text sentinel. Operational memory is injected into
the system prompt as non-mandatory hints from past sessions; it is capped at
ARIA_OPSMEM_MAX_LINES and pruned by reflection. The model can now also `list`
notes and `forget` a stale one directly instead of waiting for reflection.
"""

from __future__ import annotations

DEFINITION = {
    "name": "learn",
    "description": (
        "Manage operational notes — how to be useful in this user's context: "
        "which accounts/tools to use for a task, Jira project keys, calendar IDs, "
        "recurring task patterns, shortcuts. action='add' (default) saves a note; "
        "the more you save, the less you re-derive each session. Set "
        "scope='project' on add for a note specific to the current working "
        "directory's codebase (test command, deploy steps, gotchas). "
        "action='list' shows stored notes; action='forget' removes every note "
        "matching a phrase (use when a note is now wrong)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "forget"],
                "description": "add (default), list, or forget.",
            },
            "procedure": {
                "type": "string",
                "description": "For add: the procedure/shortcut as a short note. "
                               "For forget: a phrase; every note containing it "
                               "(case-insensitive) is removed.",
            },
            "scope": {
                "type": "string",
                "enum": ["global", "project"],
                "description": "add only: 'global' (default) or 'project' for the "
                               "current directory's codebase.",
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
            notes = ws.list_memory_facts("operational_memory.md")
            if not notes:
                return "[learn] Operational memory is empty."
            return "[learn] Operational memory:\n" + "\n".join(notes)

        if action == "forget":
            query = (args.get("procedure") or "").strip()
            if not query:
                return "[learn] No phrase provided to forget."
            n = ws.forget_memory(query, "operational_memory.md")
            return (f"[learn] Removed {n} note(s) matching {query!r}."
                    if n else f"[learn] No stored note matched {query!r}.")

        procedure = (args.get("procedure") or "").strip()
        if not procedure:
            return "[learn] No procedure provided."

        if args.get("scope") == "project":
            from aria import project
            root = project.find_project_root()
            project.append_note(procedure, config.workspace_dir())
            return f"[learn] Saved a project note for {root.name}: {procedure}"

        ws.append_operational_memory(f"- {procedure}")
        return f"[learn] Saved to operational memory: {procedure}"
    except Exception as e:
        return f"[learn error] {e}"
