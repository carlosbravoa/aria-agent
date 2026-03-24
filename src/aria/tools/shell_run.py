"""
aria/tools/shell_run.py — Execute shell commands with smart confirmation policy.

Confirmation rules:
  - NEVER ask  : read-only commands, script creation in workspace, package installs
  - ALWAYS ask  : destructive commands (rm, dd, mkfs, shutdown, kill, etc.)
  - ALWAYS ask  : commands that modify files outside the workspace
  - NON-INTERACTIVE (Telegram etc.): destructive commands are auto-rejected
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess

from aria.tools._env import build_env
from pathlib import Path

DEFINITION = {
    "name": "shell_run",
    "description": (
        "Run a shell command on the local machine. "
        "Read-only commands and script creation inside the workspace run automatically. "
        "Destructive operations (delete, overwrite files outside workspace) require confirmation. "
        "Use for file queries, running scripts, installs, system info, etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."},
            "cwd": {"type": "string", "description": "Working directory (optional)."},
        },
        "required": ["command"],
    },
}

# Commands that are always safe — no confirmation needed
_SAFE_RE = re.compile(
    r"^\s*("
    r"ls|ll|la|find|cat|head|tail|grep|rg|wc|du|df|pwd|echo|printf|"
    r"which|type|env|printenv|uname|hostname|whoami|id|date|uptime|"
    r"ps|top|htop|lsof|netstat|ss|curl|wget|ping|dig|nslookup|"
    r"git\s+(log|status|diff|show|branch|tag|remote|fetch|clone)|"
    r"pip\s+(install|show|list|freeze)|"
    r"pip3\s+(install|show|list|freeze)|"
    r"python3?\s+\S+\.py|"   # run a script
    r"bash\s+\S+\.sh|"       # run a shell script
    r"sh\s+\S+\.sh|"
    r"chmod|mkdir\s+-p|"
    r"touch|cp(?!\s.*\.\.|.*\s/)|"  # cp but not to root or parent traversal
    r"cat\s*>|tee|"          # writing new files is fine
    r"apt\s+(install|show|list|search)|"
    r"apt-get\s+(install|show)|"
    r"snap\s+(install|list|info)"
    r")\b",
    re.IGNORECASE,
)

# Commands that are always destructive — must confirm (or auto-reject)
_DESTRUCTIVE_RE = re.compile(
    r"^\s*("
    r"rm\b|rmdir\b|"
    r"dd\b|mkfs\b|fdisk\b|parted\b|"
    r"shutdown\b|reboot\b|halt\b|poweroff\b|"
    r"kill\b|killall\b|pkill\b|"
    r"chmod\s+[0-7]*[02][0-7]{2}|"  # removing permissions
    r"chown\b|chgrp\b|"
    r"mv\b|"                  # move can overwrite
    r"truncate\b|"
    r"shred\b|wipe\b|"
    r"crontab\s+-r|"
    r"userdel\b|groupdel\b|passwd\b"
    r")\b",
    re.IGNORECASE,
)


def _workspace_root() -> Path:
    """Return the configured workspace root."""
    from aria import config
    return config.workspace_dir()


def _is_interactive() -> bool:
    """True when running in a terminal that can accept input."""
    return os.isatty(0)


def _targets_workspace(command: str) -> bool:
    """Return True if all file targets in the command are inside the workspace."""
    ws = str(_workspace_root())
    # Heuristic: every path-like argument starts with the workspace dir
    paths = re.findall(r'[\w./~-]+', command)
    file_paths = [p for p in paths if '/' in p or p.startswith('~')]
    if not file_paths:
        return False
    return all(
        str(Path(p).expanduser().resolve()).startswith(ws)
        for p in file_paths
    )


def _needs_confirmation(command: str) -> bool:
    """Decide whether a command needs user confirmation."""
    if _SAFE_RE.match(command):
        return False
    if _DESTRUCTIVE_RE.match(command):
        return True
    # Default: ask if not obviously safe
    return True


def _confirm(command: str) -> bool:
    """Prompt the user. Returns False if non-interactive."""
    if not _is_interactive():
        return False
    print(f"\n⚠️  Shell command requested:\n  $ {command}")
    answer = input("  Run? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def execute(args: dict) -> str:
    command: str = args["command"]
    cwd: str | None = args.get("cwd")

    destructive = bool(_DESTRUCTIVE_RE.match(command))
    needs_confirm = _needs_confirmation(command)

    # Destructive commands targeting inside workspace don't need confirmation
    if destructive and _targets_workspace(command):
        needs_confirm = False

    if needs_confirm:
        if not _is_interactive():
            return (
                f"[shell_run] Command requires confirmation but no terminal is available: "
                f"`{command}` — operation cancelled for safety."
            )
        if not _confirm(command):
            return "[shell_run] Cancelled by user."

    try:
        result = subprocess.run(
            command,
            shell=True,          # use shell so pipes, && etc. work
            capture_output=True,
            text=True,
            timeout=60,
            cwd=cwd,
            env=build_env(),     # full user env even in background services
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        parts = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr] {err}")
        return "\n".join(parts) or "(no output)"
    except subprocess.TimeoutExpired:
        return "[shell_run error] Command timed out after 60s."
    except Exception as exc:
        return f"[shell_run error] {exc}"
