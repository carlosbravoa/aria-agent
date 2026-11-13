"""
aria/tools/skill.py — Load, list, save, import and delete skills (see aria/skills.py).

Skills are on-demand procedures: the system prompt carries only a capped index
of names + one-line descriptions, and this tool fetches a body when a task
matches. The schema below is deliberately terse — it is sent on every request.
"""

from __future__ import annotations

DEFINITION = {
    "name": "skill",
    "description": (
        "Skills are stored how-to procedures for specific tools and tasks. "
        "action='load' fetches one by name (follow it); 'list' finds skills, "
        "optionally by query; 'save' stores a reusable multi-step procedure you "
        "worked out (exact commands/API calls) so future sessions skip the "
        "discovery; 'import' installs a skill folder or .md file the user "
        "points to (path); 'delete' removes one you saved."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["load", "list", "save", "import", "delete"],
            },
            "name": {
                "type": "string",
                "description": "Skill name (load/save/delete), e.g. 'gh-release'.",
            },
            "path": {"type": "string", "description": "import: folder or .md file."},
            "query": {"type": "string", "description": "list: words to match."},
            "file": {
                "type": "string",
                "description": "load: a supporting file inside the skill.",
            },
            "offset": {
                "type": "integer",
                "description": "load: continue a long skill from this character.",
            },
            "description": {
                "type": "string",
                "description": "save: one line saying when to use the skill.",
            },
            "body": {
                "type": "string",
                "description": "save: the procedure in markdown — steps and "
                               "exact commands, no secrets.",
            },
        },
        "required": ["action"],
    },
}

_LIST_MAX = 30


def _import(args: dict, name: str) -> str:
    """Import is the one action that turns arbitrary text on disk into a
    procedure the agent follows — so the source must be readable under the
    file_access block list, and channel turns ask the user first."""
    from pathlib import Path

    from aria import approval, skills
    from aria.tools import file_access

    raw = (args.get("path") or "").strip()
    if not raw:
        return "[skill] error: path is required for import."
    try:
        src = Path(raw).expanduser().resolve()
    except OSError as exc:
        return f"[skill] error: no way — can't import {raw}: {exc}"
    if file_access._is_blocked(src):
        return f"[skill] error: no way — {raw} is a protected path."
    refusal = approval.check("skill_import", f"Import skill from {src}")
    if refusal:
        return refusal
    try:
        return "[skill] " + skills.import_skill(src, name=name)
    except skills.SkillError as exc:
        return f"[skill] error: no way — can't import {src.name}: {exc}."


def execute(args: dict) -> str:
    from aria import skills

    action = (args.get("action") or "load").strip().lower()
    name = (args.get("name") or "").strip()
    try:
        if action == "load":
            if not name:
                return "[skill] error: name is required for load."
            return skills.load(name, file=(args.get("file") or "").strip(),
                               offset=args.get("offset") or 0)

        if action == "list":
            query = (args.get("query") or "").strip()
            found = skills.search(query)
            if not found:
                return ("[skill] No skill matches " + repr(query) + "."
                        if query else "[skill] No skills installed.")
            lines = [f"- {s.name}: {skills._short(s.description)}"
                     + (" (self-written)" if s.source == skills.SELF else "")
                     for s in found[:_LIST_MAX]]
            if len(found) > _LIST_MAX:
                lines.append(f"(+{len(found) - _LIST_MAX} more — narrow the query)")
            return "[skill] Skills:\n" + "\n".join(lines)

        if action == "save":
            s = skills.save(name, args.get("description") or "",
                            args.get("body") or "")
            return (f"[skill] Saved skill {s.name!r}. It is listed in your "
                    "skills from the next session on.")

        if action == "import":
            return _import(args, name)

        if action == "delete":
            if not name:
                return "[skill] error: name is required for delete."
            skills.delete(name)
            return f"[skill] Deleted skill {name!r}."

        return f"[skill] error: unknown action {action!r}."
    except skills.SkillError as exc:
        return f"[skill] error: {exc}"
    except Exception as exc:
        return f"[skill] error: {exc}"
