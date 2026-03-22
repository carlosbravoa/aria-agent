"""
tools/shell_run.py — Execute a shell command after user confirmation.
"""

import subprocess
import shlex

DEFINITION = {
    "name": "shell_run",
    "description": (
        "Run a shell command on the local machine. "
        "Always requires explicit user confirmation before execution. "
        "Use for file operations, system queries, scripts, etc."
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


def _confirm(command: str) -> bool:
    print(f"\n⚠️  Shell command requested:\n  $ {command}")
    answer = input("  Run? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def execute(args: dict) -> str:
    command: str = args["command"]
    cwd: str | None = args.get("cwd")

    if not _confirm(command):
        return "[shell_run] Cancelled by user."

    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
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
        return "[shell_run error] Command timed out after 30s."
    except Exception as exc:
        return f"[shell_run error] {exc}"
