"""
aria/tools/shell_run.py — Execute shell commands with smart confirmation policy.

Confirmation rules:
  - Destructive commands (rm, mv, kill, shutdown …) → ask in terminal,
    auto-reject in non-interactive mode (Telegram, WhatsApp, cron).
  - Everything else → runs immediately.

Special fields:
  - script: pass raw script content; written to a temp file and executed.
    Bypasses JSON escaping issues for code with quotes/braces/backslashes.
  - stdin:  pipe text into the command's stdin.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from aria.tools._env import build_env, is_tty_command

DEFINITION = {
    "name": "shell_run",
    "description": (
        "Run a shell command. "
        "Pass 'script' to execute a block of code without JSON escaping issues. "
        "Pass 'stdin' to pipe text into the command. "
        "Destructive commands require confirmation; everything else runs immediately."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Shell command to run. "
                    "Omit when using 'script' for multi-line code execution."
                ),
            },
            "script": {
                "type": "string",
                "description": (
                    "Script content to execute. Written to a temp file and run with bash. "
                    "Use this instead of 'command' when the code contains quotes, braces, "
                    "or backslashes that are hard to escape in JSON."
                ),
            },
            "interpreter": {
                "type": "string",
                "description": "Interpreter for 'script' field. Default: bash. Use 'python3' for Python.",
                "default": "bash",
            },
            "stdin": {
                "type": "string",
                "description": "Text to pipe into the command's stdin.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory (optional).",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 60).",
                "default": 60,
            },
        },
    },
}

# Commands that are always destructive — must confirm or auto-reject
_DESTRUCTIVE_RE = re.compile(
    r"^\s*("
    r"rm\b|rmdir\b|"
    r"dd\b|mkfs\b|fdisk\b|parted\b|"
    r"shutdown\b|reboot\b|halt\b|poweroff\b|"
    r"kill\b|killall\b|pkill\b|"
    r"chown\b|chgrp\b|"
    r"mv\b|"
    r"shred\b|wipe\b|"
    r"crontab\s+-r|"
    r"userdel\b|groupdel\b|passwd\b"
    r")\b",
    re.IGNORECASE,
)

# Interactive-only Python invocations (no args = REPL)
_PYTHON_REPL_RE = re.compile(r"^\s*python3?\s*$")


def _is_interactive() -> bool:
    return os.isatty(0)


def _confirm(command: str) -> bool:
    if not _is_interactive():
        return False
    print(f"\n⚠️  Shell command requested:\n  $ {command}")
    answer = input("  Run? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def execute(args: dict) -> str:
    script_content = args.get("script", "").strip()
    command        = args.get("command", "").strip()
    interpreter    = args.get("interpreter", "bash")
    stdin_text     = args.get("stdin")
    cwd            = args.get("cwd")
    timeout        = int(args.get("timeout", 60))

    # ── Script mode ───────────────────────────────────────────────────────
    if script_content:
        suffix = ".py" if "python" in interpreter else ".sh"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(script_content)
            tmp_path = tmp.name
        try:
            return _run_cmd(
                f"{interpreter} {tmp_path}",
                stdin_text=stdin_text,
                cwd=cwd,
                timeout=timeout,
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # ── Command mode ──────────────────────────────────────────────────────
    if not command:
        return "[shell_run] Provide either 'command' or 'script'."

    # Reject bare Python REPL
    if _PYTHON_REPL_RE.match(command):
        return (
            "[shell_run] Running 'python3' with no arguments opens an interactive REPL "
            "which cannot run in a background process. "
            "Use 'script' field to run Python code, or pass a script file: python3 script.py"
        )

    # Reject interactive TTY commands
    if is_tty_command(command):
        first = command.strip().split()[0]
        return (
            f"[shell_run] '{first}' requires an interactive terminal. "
            "Use a non-interactive alternative."
        )

    # Destructive commands always require confirmation.
    # Non-interactive mode (Telegram, WhatsApp, supervisor) always rejects them —
    # there is no safe way to confirm, and silent destruction is never acceptable.
    if _DESTRUCTIVE_RE.match(command):
        if not _is_interactive():
            return (
                f"[shell_run] Destructive command rejected in non-interactive mode: "
                f"`{command}` — run it manually in a terminal if intended."
            )
        if not _confirm(command):
            return "[shell_run] Cancelled by user."

    return _run_cmd(command, stdin_text=stdin_text, cwd=cwd, timeout=timeout)


def _run_cmd(
    command: str,
    stdin_text: str | None,
    cwd: str | None,
    timeout: int,
) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=build_env(),
            input=stdin_text,
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
        return f"[shell_run error] Command timed out after {timeout}s."
    except Exception as exc:
        return f"[shell_run error] {exc}"
