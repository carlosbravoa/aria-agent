"""
aria/tools/plan.py — A lightweight task plan / todo checklist.

For multi-step work the agent records a short plan and keeps it updated as it
makes progress, so the user can see what it intends to do and where it is. The
model passes the FULL list of todos on every call (replace semantics) — the same
shape coding assistants use for a todo tool. The REPL renders the checklist; the
returned text is also what the model sees, reinforcing that it should track and
finish each step.

State is stored in the workspace so "show me the plan" works and it survives a
restart. Local state → not parallel-safe.
"""

from __future__ import annotations

import json

from aria import config
from aria.workspace import Workspace

_STATUS_ICON = {"pending": "☐", "in_progress": "◐", "done": "☑"}
_VALID = set(_STATUS_ICON)


def _plan_path():
    return config.workspace_dir() / "memory" / "current_plan.json"


DEFINITION = {
    "name": "plan",
    "description": (
        "Track a short task plan as a checklist for a multi-step task. Pass the "
        "FULL list of todos every call (replace semantics) and update each item's "
        "status as you go: pending → in_progress → done. Use it for non-trivial, "
        "multi-step tasks so progress is visible; skip it for simple one-step "
        "requests. Set action='show' to print the current plan, 'clear' to reset."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "show", "clear"],
                "description": "Default 'set' (when todos are provided).",
            },
            "todos": {
                "type": "array",
                "description": "The full ordered list of steps.",
                "items": {
                    "type": "object",
                    "properties": {
                        "task":   {"type": "string"},
                        "status": {"type": "string",
                                   "enum": ["pending", "in_progress", "done"]},
                    },
                    "required": ["task"],
                },
            },
        },
    },
}


def _render(todos: list) -> str:
    if not todos:
        return "[plan] (empty)"
    lines = []
    done = 0
    for t in todos:
        status = t.get("status", "pending")
        if status not in _VALID:
            status = "pending"
        if status == "done":
            done += 1
        lines.append(f"{_STATUS_ICON[status]} {t.get('task', '').strip()}")
    header = f"Plan — {done}/{len(todos)} done"
    return header + "\n" + "\n".join(lines)


def execute(args: dict) -> str:
    ws = Workspace(config.workspace_dir())
    path = _plan_path()
    action = args.get("action") or ("set" if args.get("todos") is not None else "show")

    if action == "clear":
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return "[plan] Cleared."

    if action == "show":
        if not path.exists():
            return "[plan] No plan yet."
        try:
            todos = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return "[plan] No plan yet."
        return _render(todos)

    # set
    todos = args.get("todos")
    if not isinstance(todos, list) or not todos:
        return "[plan] 'todos' (a non-empty list of {task, status}) is required."
    clean = []
    for t in todos:
        if not isinstance(t, dict) or not str(t.get("task", "")).strip():
            continue
        status = t.get("status", "pending")
        clean.append({"task": str(t["task"]).strip(),
                      "status": status if status in _VALID else "pending"})
    if not clean:
        return "[plan] No valid todos provided."
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(clean), encoding="utf-8")
        path.chmod(0o600)
    except OSError as exc:
        return f"[plan] Could not save: {exc}"
    return _render(clean)
