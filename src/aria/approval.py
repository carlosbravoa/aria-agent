"""
aria/approval.py — Ask the user before a risky action runs unattended.

In the terminal, risky actions ask on the TTY (shell_run's [y/N/always]). On a
channel turn or in remote control there is nobody at the keyboard — previously
those actions were simply refused (shell) or ran without asking (deletes, git
push, update). Now they ask the user in that chat (Telegram: inline buttons;
any channel: reply "yes 1234" / "no 1234").

Scheduled tasks keep their old behaviour by default (shell refusals are
instant, deletes run): a task waiting on an unanswered approval at night would
run into ARIA_TASK_TIMEOUT. ARIA_APPROVAL_TASKS=on makes tasks ask the push
channel (ARIA_NOTIFY_CHANNEL) too — that needs a receiver that's online (a
service, or `aria` open for an attached channel).

The tool thread waits for the answer (ARIA_APPROVAL_TIMEOUT, default 300 s;
no answer = denied; /stop cancels the wait). Requests live as files in
~/.aria/approvals/, so the answer can arrive in another process — e.g. a
supervisor task approved from the Telegram service.

Config:
  ARIA_APPROVALS=on|off          off → old behaviour (refuse; never ask)
  ARIA_APPROVAL_REQUIRED=…       actions that need approval when unattended
                                 (default: delete,git_push,update,shell; add
                                 gmail_send, calendar_create, … or "none")
  ARIA_APPROVAL_TASKS=off|on     also ask inside scheduled tasks
  ARIA_APPROVAL_TIMEOUT=300
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

_DEFAULT_REQUIRED = "delete,git_push,update,shell"
_ANSWER_RE = re.compile(r"^\s*(yes|y|approve|ok|no|n|deny)\s+(\d{4})\s*$", re.I)
_POLL = 0.5


def enabled() -> bool:
    return os.environ.get("ARIA_APPROVALS", "on").strip().lower() not in ("off", "0", "false", "no")


def required(kind: str) -> bool:
    """Does action `kind` (e.g. "delete", "git_push", "shell") need approval
    when unattended?"""
    raw = os.environ.get("ARIA_APPROVAL_REQUIRED", _DEFAULT_REQUIRED).strip().lower()
    if raw in ("", "none"):
        return False
    return kind.lower() in {k.strip() for k in raw.split(",")}


def unattended() -> bool:
    """Nobody at a terminal for this turn: a channel turn (incl. remote
    control) or a scheduled task."""
    from aria import context
    return context.current() is not None or bool(os.environ.get("ARIA_TASK_ID"))


def tasks_enabled() -> bool:
    return os.environ.get("ARIA_APPROVAL_TASKS", "off").strip().lower() in ("on", "1", "true", "yes")


def should_ask() -> bool:
    """Approvals are on and there's someone to ask: a channel turn, or a
    scheduled task with ARIA_APPROVAL_TASKS=on."""
    from aria import context
    if not enabled():
        return False
    if context.current() is not None:
        return True
    return bool(os.environ.get("ARIA_TASK_ID")) and tasks_enabled()


# The running turn's /stop event: a pending approval wait ends when it's set.
_cancel: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "aria_approval_cancel", default=None)


def bind_cancel(event: threading.Event):
    """Bind the turn's stop event (Agent.chat). Returns a token for unbind_cancel()."""
    return _cancel.set(event)


def unbind_cancel(token) -> None:
    try:
        _cancel.reset(token)
    except (ValueError, LookupError):
        _cancel.set(None)


def _timeout() -> float:
    try:
        return max(10.0, float(os.environ.get("ARIA_APPROVAL_TIMEOUT", "300")))
    except ValueError:
        return 300.0


def _dir() -> Path:
    d = Path.home() / ".aria" / "approvals"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _write(path: Path, data: dict) -> None:
    # Unique temp name: a request and its answer can be written by two threads
    # of the same process (e.g. the Telegram service).
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read(code: str) -> dict | None:
    try:
        return json.loads((_dir() / f"{code}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _new_code() -> str:
    for _ in range(100):
        code = f"{secrets.randbelow(10000):04d}"
        cur = _read(code)
        if cur is None or cur.get("status") != "pending":
            return code
    raise RuntimeError("too many pending approvals")


def _prune() -> None:
    cutoff = time.time() - 86400
    for p in _dir().glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def request(summary: str) -> tuple[bool, str]:
    """Ask the user to approve `summary`. Returns (approved, reason). Blocks
    until answered, timed out, or the turn is stopped. Call only when
    unattended() is True."""
    if not enabled():
        return False, "approvals are disabled (ARIA_APPROVALS=off)"
    from aria import channels, context
    ctx = context.current()
    if ctx is not None:
        plugin, to = channels.get(ctx.channel), ctx.user_id
    else:
        plugin, to = channels.push_channel(), None
    if plugin is None or not plugin.supports_push:
        return False, "it needs approval, but there's no channel to ask on"
    if not plugin.answers_approvals:
        return False, (f"it needs approval, and {plugin.name} can't receive an "
                       f"answer while a reply is running")

    _prune()
    code = _new_code()
    timeout = _timeout()
    path = _dir() / f"{code}.json"
    _write(path, {"code": code, "channel": plugin.name, "to": to, "summary": summary,
                  "status": "pending", "created": time.time(), "pid": os.getpid()})
    try:
        plugin.send_approval(code, summary, to=to, expires_min=max(1, round(timeout / 60)))
    except Exception as exc:
        _write(path, {**(_read(code) or {}), "status": "failed"})
        return False, f"couldn't ask for approval on {plugin.name}: {exc}"

    cancel = _cancel.get()
    deadline = time.monotonic() + timeout
    while True:
        cur = _read(code) or {}
        if cur.get("status") == "approved":
            return True, "approved"
        if cur.get("status") == "denied":
            return False, "denied by the user"
        if cancel is not None and cancel.is_set():
            outcome, why = "cancelled", "stopped by the user"
            break
        if time.monotonic() >= deadline:
            outcome, why = "expired", f"not approved within {round(timeout)} s"
            break
        time.sleep(_POLL)
    # Close it — but an answer that landed in the meantime wins.
    cur = _read(code) or {}
    if cur.get("status") == "approved":
        return True, "approved"
    if cur.get("status") == "denied":
        return False, "denied by the user"
    _write(path, {**cur, "status": outcome})
    return False, why


def _pending_for(code: str, channel: str, user_id: str | None) -> dict | None:
    """The pending request `code` if this channel — and, when it went to one
    user, this user — may answer it."""
    cur = _read(code)
    if cur is None or cur.get("status") != "pending":
        return None
    if cur.get("channel") != channel or (cur.get("to") and str(cur["to"]) != str(user_id)):
        return None
    return cur


def _alive(pid) -> bool:
    if not pid:
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, OSError):
        return True
    return True


def answer(code: str, approve: bool, channel: str, user_id: str | None) -> str:
    """Record an answer (from a button or a "yes 1234" reply). Only the
    channel the request was sent to — and, when it went to one user, only that
    user — may answer. A request whose process has ended (e.g. a killed task)
    can't be approved."""
    cur = _pending_for(code, channel, user_id)
    if cur is None:
        return f"No pending approval {code}."
    if not _alive(cur.get("pid")):
        _write(_dir() / f"{code}.json", {**cur, "status": "abandoned"})
        return f"Approval {code} is no longer waiting (whatever asked has ended)."
    cur.update(status="approved" if approve else "denied", answered_by=str(user_id),
               answered=time.time())
    _write(_dir() / f"{code}.json", cur)
    return "✅ Approved." if approve else "❌ Denied."


def try_answer_text(channel: str, user_id: str, text: str) -> str | None:
    """If `text` answers a PENDING approval for this user ("yes 1234" /
    "no 1234"), record it and return the reply; else None — so an ordinary
    "no 2025" still reaches the agent. Channels call this BEFORE any per-chat
    locking: the turn waiting for this answer holds those locks."""
    m = _ANSWER_RE.match(text or "")
    if not m or _pending_for(m.group(2), channel, user_id) is None:
        return None
    approve = m.group(1).lower() in ("yes", "y", "approve", "ok")
    return answer(m.group(2), approve, channel, user_id)


def check(kind: str, summary: str) -> str | None:
    """For tools: None if the action may proceed, else a refusal string to
    return. Asks only when `kind` needs approval and there's someone to ask
    (should_ask); otherwise — terminal, ARIA_APPROVALS=off, a task without
    ARIA_APPROVAL_TASKS — the action runs as it did before approvals."""
    if not (required(kind) and should_ask()):
        return None
    ok, why = request(summary)
    return None if ok else f"[approval] Not done — {summary}: {why}."
