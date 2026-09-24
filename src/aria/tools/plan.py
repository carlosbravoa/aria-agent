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

Plans are scoped per conversation (the agent's window key: "repl",
"telegram:<id>", "supervisor", …). A single global plan leaked across channels:
every request carries the unfinished plan with "continue from the first
unfinished step", so a Telegram plan step like "schedule the daily digest" was
re-executed by background supervisor tasks — one source of duplicated tasks.
The agent sets the scope around each tool call (set_scope/reset_scope).
"""

from __future__ import annotations

import contextvars
import json
import re

from aria import config
from aria.workspace import Workspace

_STATUS_ICON = {"pending": "☐", "in_progress": "◐", "done": "☑"}
_VALID = set(_STATUS_ICON)


_scope: contextvars.ContextVar[str] = contextvars.ContextVar("aria_plan_scope", default="repl")


def set_scope(key: str | None):
    """Bind the plan scope for the current thread/context. Returns a token."""
    return _scope.set(key or "repl")


def reset_scope(token) -> None:
    try:
        _scope.reset(token)
    except (ValueError, LookupError):
        _scope.set("repl")


def _plan_path(scope: str | None = None):
    key = scope or _scope.get()
    mem = config.workspace_dir() / "memory"
    if key == "repl":
        return mem / "current_plan.json"      # pre-scoping location, kept for the REPL
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
    return mem / f"plan__{safe}.json"


def clear(scope: str | None = None) -> None:
    """Delete the plan for `scope` (default: the current scope)."""
    try:
        _plan_path(scope).unlink(missing_ok=True)
    except OSError:
        pass


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


def context_block(scope: str | None = None) -> str:
    """The rendered current plan when it has unfinished steps, else "". The
    agent injects this into every model request (the trailing context message)
    so an in-flight task survives interruptions — errors, compaction, restarts:
    the plan lives on disk and each request re-reads it, so 'continue' can
    always pick up from the first unfinished step."""
    path = _plan_path(scope)
    if not path.exists():
        return ""
    try:
        todos = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(todos, list) or not todos:
        return ""
    live = [t for t in todos if isinstance(t, dict)]
    if not live or all(t.get("status") == "done" for t in live):
        return ""
    return _render(live)


def execute(args: dict) -> str:
    Workspace(config.workspace_dir())   # ensures the memory dir exists with secure perms
    path = _plan_path()
    action = args.get("action") or ("set" if args.get("todos") is not None else "show")

    if action == "clear":
        clear()
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
