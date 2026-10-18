"""
aria/approval.py — Ask the user before a risky action runs when nobody is at
the terminal, and remember the answer for the rest of the task.

Three situations, decided here and read by the tools:

  TTY (the REPL)           shell_run asks on the terminal ([y/N/always]);
                           other tools run as they always did.
  attended remotely        a channel turn — the user just sent the message and
  (attended_remote())      is reading the reply, incl. remote control. Ordinary
                           work runs; a risky action asks IN THAT CHAT
                           (Telegram: inline buttons; any channel: "yes 1234").
  unattended (a task)      a scheduled task: nobody is necessarily watching.
                           Shell stays on the read-only allowlist; approvals
                           are refused unless ARIA_APPROVAL_TASKS=on, in which
                           case the push channel (ARIA_NOTIFY_CHANNEL) is asked.

Task grants: an approval can be answered "for this task" (Telegram button, or
reply `task 1234`). That records a GRANT for the action kind (shell, delete,
git_push, …) scoped to the conversation (or task) it came from. Later actions
of the same kind in that scope run without asking. The grant ends when a turn
finishes with no unfinished plan steps (Agent.chat → end_turn), on /clear, or
after ARIA_APPROVAL_GRANT_MAX_MIN at the latest. Some asks are not grantable
(e.g. a command touching SSH keys): they always ask individually.

The tool thread waits for the answer (ARIA_APPROVAL_TIMEOUT, default 300 s;
no answer = denied; /stop cancels the wait). Requests and grants live as files
in ~/.aria/approvals/, so the answer can arrive in another process — e.g. a
supervisor task approved from the Telegram service.

Config:
  ARIA_APPROVALS=on|off          off → never ask (risky actions are refused
                                 where they used to be, and run elsewhere)
  ARIA_APPROVAL_REQUIRED=…       actions that need approval when not on a TTY
                                 (default: delete,git_push,update,shell; add
                                 gmail_send, calendar_create, … or "none")
  ARIA_APPROVAL_TASKS=off|on     also ask inside scheduled tasks
  ARIA_APPROVAL_TIMEOUT=300
  ARIA_APPROVAL_GRANT_MAX_MIN=480  hard cap on a task grant's lifetime
"""

from __future__ import annotations

import contextvars
import inspect
import json
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

_DEFAULT_REQUIRED = "delete,git_push,update,shell"
# "yes 1234" / "no 1234" / "task 1234" / "yes task 1234" / "yes all 1234"
_ANSWER_RE = re.compile(
    r"^\s*(yes|y|approve|ok|no|n|deny|task|all)(?:\s+(task|all))?\s+(\d{4})\s*$", re.I)
_POLL = 0.5
_GRANTS_FILE = "grants.json"
_GRANTS_LOCK = threading.Lock()

DENIED_NOTE = ("Skip this step and continue with the rest of the task; do not retry "
               "it or work around the refusal.")


def enabled() -> bool:
    return os.environ.get("ARIA_APPROVALS", "on").strip().lower() not in ("off", "0", "false", "no")


def required(kind: str) -> bool:
    """Does action `kind` (e.g. "delete", "git_push", "shell") need approval
    when nobody is at a terminal?"""
    raw = os.environ.get("ARIA_APPROVAL_REQUIRED", _DEFAULT_REQUIRED).strip().lower()
    if raw in ("", "none"):
        return False
    return kind.lower() in {k.strip() for k in raw.split(",")}


def in_task() -> bool:
    """Inside a scheduled supervisor task."""
    return bool(os.environ.get("ARIA_TASK_ID"))


def unattended() -> bool:
    """Nobody at a terminal for this turn: a channel turn (incl. remote
    control) or a scheduled task. The opposite of 'the REPL'."""
    from aria import context
    return context.current() is not None or in_task()


def attended_remote() -> bool:
    """A live channel turn: the user just sent this message and is reading the
    reply — attended, just not on a TTY. Remote control counts. A scheduled
    task does not, even when it carries a channel context."""
    from aria import context
    return context.current() is not None and not in_task()


def tasks_enabled() -> bool:
    return os.environ.get("ARIA_APPROVAL_TASKS", "off").strip().lower() in ("on", "1", "true", "yes")


def should_ask() -> bool:
    """Approvals are on and there's someone to ask: a channel turn, or a
    scheduled task with ARIA_APPROVAL_TASKS=on."""
    if not enabled():
        return False
    if attended_remote():
        return True
    return in_task() and tasks_enabled()


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


def _grant_max_sec() -> float:
    try:
        return max(60.0, float(os.environ.get("ARIA_APPROVAL_GRANT_MAX_MIN", "480")) * 60)
    except ValueError:
        return 480 * 60.0


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
        if p.name == _GRANTS_FILE:
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


# ── Task grants ───────────────────────────────────────────────────────────────

def grant_scope() -> str | None:
    """The key a task grant is stored under: the conversation
    ("<channel>:<user>") for a channel turn, "task:<id>" for a scheduled task,
    None on a TTY (the REPL has its own [always] allowlist)."""
    from aria import context
    if in_task():
        return f"task:{os.environ.get('ARIA_TASK_ID')}"
    ctx = context.current()
    if ctx is not None:
        return f"{ctx.channel}:{ctx.user_id}"
    return None


def _load_grants() -> dict:
    try:
        data = json.loads((_dir() / _GRANTS_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_grants(data: dict) -> None:
    _write(_dir() / _GRANTS_FILE, data)


def grant(kind: str, scope: str | None = None, by: str | None = None) -> None:
    """Record that `kind` may run without asking in `scope` (default: the
    current one) until end_turn() finds the task finished."""
    scope = scope or grant_scope()
    if not scope:
        return
    with _GRANTS_LOCK:
        data = _load_grants()
        data.setdefault(scope, {})[kind.lower()] = {"granted": time.time(), "by": by}
        _save_grants(data)


def has_grant(kind: str, scope: str | None = None) -> bool:
    """Is there a live task grant for `kind` in `scope` (default: current)?"""
    scope = scope or grant_scope()
    if not scope:
        return False
    entry = _load_grants().get(scope, {}).get(kind.lower())
    if not entry:
        return False
    if time.time() - float(entry.get("granted", 0)) > _grant_max_sec():
        revoke_grants(scope)
        return False
    return True


def grants(scope: str | None = None) -> list[str]:
    """Kinds currently granted in `scope` (default: current), for status lines."""
    scope = scope or grant_scope()
    if not scope:
        return []
    return sorted(_load_grants().get(scope, {}))


def revoke_grants(scope: str | None = None) -> bool:
    """Drop every grant of `scope` (default: current). True if any existed."""
    scope = scope or grant_scope()
    if not scope:
        return False
    with _GRANTS_LOCK:
        data = _load_grants()
        if scope not in data:
            return False
        del data[scope]
        _save_grants(data)
        return True


def end_turn(plan_scope: str | None = None) -> bool:
    """Called when a turn ends (Agent.chat). A task grant lives as long as the
    task: it is dropped once the turn ends with no unfinished plan steps for
    this conversation. Returns True when grants were dropped."""
    scope = grant_scope()
    if not scope:
        return False
    try:
        from aria.tools import plan
        if plan.context_block(plan_scope or scope):
            return False                  # steps still open → the task goes on
    except Exception:
        pass
    return revoke_grants(scope)


# ── Requests ──────────────────────────────────────────────────────────────────

def _send_approval(plugin, code: str, summary: str, to, expires_min: int,
                   grantable: bool) -> None:
    """Call the plugin's send_approval; plugins written before task grants
    don't take `grantable`."""
    fn = plugin.send_approval
    try:
        takes_grantable = "grantable" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        takes_grantable = False
    if takes_grantable:
        fn(code, summary, to=to, expires_min=expires_min, grantable=grantable)
    else:
        fn(code, summary, to=to, expires_min=expires_min)


def request(summary: str, kind: str | None = None,
            grantable: bool = True) -> tuple[bool, str]:
    """Ask the user to approve `summary`. Returns (approved, reason). Blocks
    until answered, timed out, or the turn is stopped. Call only when
    should_ask() is True. With `kind`, the user may answer "for this task",
    which also records a grant for that kind (see grant()); `grantable=False`
    withholds that option."""
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

    grantable = bool(grantable and kind and grant_scope())
    _prune()
    code = _new_code()
    timeout = _timeout()
    path = _dir() / f"{code}.json"
    _write(path, {"code": code, "channel": plugin.name, "to": to, "summary": summary,
                  "kind": kind, "grantable": grantable, "scope": grant_scope(),
                  "status": "pending", "created": time.time(), "pid": os.getpid()})
    try:
        _send_approval(plugin, code, summary, to, max(1, round(timeout / 60)), grantable)
    except Exception as exc:
        _write(path, {**(_read(code) or {}), "status": "failed"})
        return False, f"couldn't ask for approval on {plugin.name}: {exc}"

    cancel = _cancel.get()
    deadline = time.monotonic() + timeout
    while True:
        cur = _read(code) or {}
        if cur.get("status") in ("approved", "denied"):
            return _settle(cur, kind)
        if cancel is not None and cancel.is_set():
            outcome, why = "cancelled", "stopped by the user"
            break
        if time.monotonic() >= deadline:
            outcome, why = "expired", f"not approved within {round(timeout)} s"
            break
        time.sleep(_POLL)
    # Close it — but an answer that landed in the meantime wins.
    cur = _read(code) or {}
    if cur.get("status") in ("approved", "denied"):
        return _settle(cur, kind)
    _write(path, {**cur, "status": outcome})
    return False, why


def _settle(cur: dict, kind: str | None) -> tuple[bool, str]:
    if cur.get("status") != "approved":
        return False, "denied by the user"
    if cur.get("grant") and kind and cur.get("grantable"):
        grant(kind, cur.get("scope"), by=cur.get("answered_by"))
        return True, "approved for this task"
    return True, "approved"


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


def answer(code: str, approve: bool, channel: str, user_id: str | None,
           grant: bool = False) -> str:
    """Record an answer (from a button or a "yes 1234" reply). Only the
    channel the request was sent to — and, when it went to one user, only that
    user — may answer. `grant=True` approves for the rest of the task (ignored
    when the request wasn't grantable). A request whose process has ended
    (e.g. a killed task) can't be approved."""
    cur = _pending_for(code, channel, user_id)
    if cur is None:
        return f"No pending approval {code}."
    if not _alive(cur.get("pid")):
        _write(_dir() / f"{code}.json", {**cur, "status": "abandoned"})
        return f"Approval {code} is no longer waiting (whatever asked has ended)."
    grant = bool(grant and approve and cur.get("grantable"))
    cur.update(status="approved" if approve else "denied", answered_by=str(user_id),
               answered=time.time(), grant=grant)
    _write(_dir() / f"{code}.json", cur)
    if not approve:
        return "❌ Denied."
    return "✅ Approved for this task." if grant else "✅ Approved."


def try_answer_text(channel: str, user_id: str, text: str) -> str | None:
    """If `text` answers a PENDING approval for this user ("yes 1234" /
    "no 1234" / "task 1234"), record it and return the reply; else None — so
    an ordinary "no 2025" still reaches the agent. Channels call this BEFORE
    any per-chat locking: the turn waiting for this answer holds those locks."""
    m = _ANSWER_RE.match(text or "")
    if not m or _pending_for(m.group(3), channel, user_id) is None:
        return None
    word, width = m.group(1).lower(), (m.group(2) or "").lower()
    if word in ("no", "n", "deny"):
        return answer(m.group(3), False, channel, user_id)
    for_task = word in ("task", "all") or width in ("task", "all")
    return answer(m.group(3), True, channel, user_id, grant=for_task)


def check(kind: str, summary: str) -> str | None:
    """For tools: None if the action may proceed, else a refusal string to
    return. Asks only when `kind` needs approval and there's someone to ask
    (should_ask); otherwise — terminal, ARIA_APPROVALS=off, a task without
    ARIA_APPROVAL_TASKS — the action runs as it did before approvals. A live
    task grant for `kind` (see grant()) also lets it proceed without asking."""
    if not (required(kind) and should_ask()):
        return None
    if has_grant(kind):
        return None
    ok, why = request(summary, kind=kind)
    return None if ok else f"[approval] Not done — {summary}: {why}. {DENIED_NOTE}"
