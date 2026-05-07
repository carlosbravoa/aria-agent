"""
aria/tools/update.py — Self-update the agent from its source directory.

Pulls the latest code, reinstalls the package, and restarts systemd services.

Required in ~/.aria/.env:
  ARIA_SOURCE_DIR=~/aria-agent    # path to the local source directory

Optional:
  ARIA_UPDATE_BRANCH=main         # branch to pull from (default: main)
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

DEFINITION = {
    "name": "update",
    "description": (
        "Update Aria to the latest version by pulling from git, "
        "reinstalling the package, and restarting all systemd services. "
        "Reports what changed and which services were restarted. "
        "Requires ARIA_SOURCE_DIR to be set in ~/.aria/.env."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "restart_services": {
                "type": "boolean",
                "description": "Restart systemd services after update. Default: true.",
                "default": True,
            },
            "dry_run": {
                "type": "boolean",
                "description": "Show what would be done without making changes.",
                "default": False,
            },
        },
    },
}

_SERVICES = [
    "aria-telegram",
    "aria-supervisor",
    "aria-whatsapp",
    "aria-whatsapp-node",
]


def _run(cmd: str, cwd: Path | None = None) -> tuple[int, str, str]:
    result = subprocess.run(
        shlex.split(cmd),
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=120,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _active_services() -> list[str]:
    active = []
    for name in _SERVICES:
        _, out, _ = _run(f"systemctl --user is-active {name}")
        if out == "active":
            active.append(name)
    return active


def execute(args: dict) -> str:
    restart = args.get("restart_services", True)
    dry_run = args.get("dry_run", False)
    branch  = os.environ.get("ARIA_UPDATE_BRANCH", "main")
    lines: list[str] = []

    source_dir = os.environ.get("ARIA_SOURCE_DIR", "").strip()
    if not source_dir:
        return (
            "[update] ARIA_SOURCE_DIR not set.\n"
            "Add ARIA_SOURCE_DIR=~/aria-agent to ~/.aria/.env"
        )

    src = Path(source_dir).expanduser().resolve()
    if not src.exists():
        return f"[update] Source directory not found: {src}"
    if not (src / ".git").exists():
        return f"[update] Not a git repository: {src}"

    lines.append(f"📂 Source: {src}")

    if dry_run:
        lines.append("🔍 Dry run — no changes will be made.\n")

    # ── 1. Get current commit ─────────────────────────────────────────────────
    _, before_sha, _ = _run("git rev-parse --short HEAD", cwd=src)

    # ── 2. Git pull ───────────────────────────────────────────────────────────
    lines.append(f"📦 Pulling from origin/{branch}...")
    if dry_run:
        _, fetch_out, _ = _run(f"git fetch --dry-run origin {branch}", cwd=src)
        lines.append(f"  {fetch_out or '(nothing to fetch)'}")
    else:
        rc, pull_out, pull_err = _run(f"git pull origin {branch}", cwd=src)
        if rc != 0:
            return f"[update] git pull failed:\n{pull_err or pull_out}"
        lines.append(f"  {pull_out}")

    # ── 3. Show what changed ──────────────────────────────────────────────────
    if not dry_run:
        _, after_sha, _ = _run("git rev-parse --short HEAD", cwd=src)
        if before_sha != after_sha:
            _, log_out, _ = _run(
                f"git log --oneline {before_sha}..{after_sha}", cwd=src
            )
            lines.append(f"\n📝 Changes ({before_sha} → {after_sha}):")
            for log_line in log_out.splitlines():
                lines.append(f"  {log_line}")
        else:
            lines.append("  Already up to date — no reinstall needed.")
            return "\n".join(lines)

    # ── 4. pip install ────────────────────────────────────────────────────────
    import sys
    pip = f"{sys.executable} -m pip"
    lines.append(f"\n🔧 Installing package...")
    if dry_run:
        lines.append(f"  would run: {pip} install {src}")
    else:
        rc, pip_out, pip_err = _run(f"{pip} install {src}", cwd=src)
        if rc != 0:
            return f"[update] pip install failed:\n{pip_err or pip_out}"
        summary = [l for l in pip_out.splitlines()
                   if "Successfully" in l or "already" in l.lower()]
        lines.append(f"  {summary[-1] if summary else 'Done.'}")

    # ── 5. Restart services ───────────────────────────────────────────────────
    if restart:
        active = _active_services()
        if active:
            lines.append(f"\n🔄 Restarting: {', '.join(active)}")
            if dry_run:
                lines.append("  (dry run — not restarting)")
            else:
                # Delay restart so this message is delivered before the process dies
                service_list = " ".join(active)
                subprocess.Popen(
                    f"bash -c 'sleep 3 && systemctl --user restart {service_list}'",
                    shell=True,
                )
                lines.append("  Services will restart in ~3 seconds.")
        else:
            lines.append("\nℹ️  No active systemd services — restart manually if needed.")

    lines.append("\n✅ Update complete.")
    return "\n".join(lines)
