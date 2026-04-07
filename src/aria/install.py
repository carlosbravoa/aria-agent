"""
aria/install.py — Service installation wizard.

Interactively collects configuration, writes ~/.aria/.env, creates systemd
user service files, enables lingering, starts all services, and verifies them.

Usage:
  aria-install              # full interactive wizard
  aria-install --dry-run    # show what would be done without changes
  aria-install --uninstall  # stop and remove all services
  aria-install --services   # skip env config, just (re)install services
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


# ── Colours ───────────────────────────────────────────────────────────────────

def _green(s: str)  -> str: return f"\033[32m{s}\033[0m"
def _yellow(s: str) -> str: return f"\033[33m{s}\033[0m"
def _red(s: str)    -> str: return f"\033[31m{s}\033[0m"
def _bold(s: str)   -> str: return f"\033[1m{s}\033[0m"
def _dim(s: str)    -> str: return f"\033[2m{s}\033[0m"

def ok(msg: str)   -> None: print(f"  {_green('✓')} {msg}")
def warn(msg: str) -> None: print(f"  {_yellow('⚠')}  {msg}")
def err(msg: str)  -> None: print(f"  {_red('✗')} {msg}")
def info(msg: str) -> None: print(f"    {_dim(msg)}")
def section(title: str) -> None:
    print()
    print(_bold(f"── {title} "))


# ── Interactive prompt helpers ────────────────────────────────────────────────

def _ask(
    prompt: str,
    default: str = "",
    secret: bool = False,
    required: bool = False,
    hint: str = "",
) -> str:
    """
    Prompt the user for a value.
    - Shows existing/default value in brackets.
    - Pressing Enter keeps the existing value.
    - secret=True masks input (for API keys).
    - required=True loops until a non-empty value is given.
    """
    if hint:
        print(f"    {_dim(hint)}")

    display_default = "****" if (secret and default) else default
    suffix = f" [{display_default}]" if display_default else ""
    label  = f"  {prompt}{suffix}: "

    while True:
        if secret and default:
            # Don't use getpass — just show masked and allow override
            raw = input(label).strip()
        else:
            raw = input(label).strip()

        value = raw or default

        if required and not value:
            warn("This field is required.")
            continue

        return value


def _ask_bool(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"  {prompt} {suffix}: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def _load_existing_env(path: Path) -> dict[str, str]:
    """Parse existing .env into a dict for use as defaults."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def _write_env(path: Path, values: dict[str, str]) -> None:
    """
    Write values to .env safely.

    Strategy:
      1. Start with the template for comments and structure.
      2. For every key=value line in the template, replace with the
         collected value if non-empty, otherwise comment it out.
      3. Append any extra keys that aren't in the template.

    This ensures existing values are never silently dropped.
    """
    from aria.setup import _ENV_TEMPLATE

    template_lines = _ENV_TEMPLATE.splitlines()
    template_keys: set[str] = set()
    output: list[str] = []

    for line in template_lines:
        stripped = line.strip()
        # Blank lines and comments pass through unchanged
        if not stripped or stripped.startswith("#"):
            output.append(line)
            continue
        # Uncommented key=value line
        if "=" not in stripped:
            output.append(line)
            continue
        key = stripped.split("=")[0].strip()
        template_keys.add(key)
        val = values.get(key, "").strip()
        if val:
            output.append(f"{key}={val}")
        else:
            # No value — keep as a commented-out placeholder
            output.append(f"# {key}=")

    # Append keys that exist in values but not in the template
    for key, val in values.items():
        if key not in template_keys and val.strip():
            output.append(f"{key}={val}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


# ── Env config wizard ─────────────────────────────────────────────────────────

def configure_env(dry_run: bool = False) -> dict[str, str]:
    """
    Interactively collect all configuration values.
    Returns a dict of key → value.
    """
    env_path = Path.home() / ".aria" / ".env"
    existing = _load_existing_env(env_path)

    def e(key: str) -> str:
        return existing.get(key, "")

    print()
    print(_bold("╭──────────────────────────────────────────────╮"))
    print(_bold("│         Aria Configuration Wizard             │"))
    print(_bold("╰──────────────────────────────────────────────╯"))

    if env_path.exists():
        print()
        print(f"  Existing config found at {_dim(str(env_path))}")
        print(f"  Press {_bold('Enter')} to keep existing values.")

    values: dict[str, str] = {}

    # ── LLM ──────────────────────────────────────────────────────────────────
    section("LLM (required)")
    info("Any OpenAI-compatible endpoint. Examples:")
    info("  Anthropic: https://api.anthropic.com/v1")
    info("  OpenAI:    https://api.openai.com/v1")
    info("  Ollama:    http://localhost:11434/v1")

    values["LLM_BASE_URL"] = _ask(
        "LLM_BASE_URL", e("LLM_BASE_URL") or "http://localhost:11434/v1", required=True
    )
    values["LLM_API_KEY"] = _ask(
        "LLM_API_KEY", e("LLM_API_KEY") or "ollama", secret=True,
        hint="Use any string for local models (Ollama, LM Studio)"
    )
    values["LLM_MODEL"] = _ask(
        "LLM_MODEL", e("LLM_MODEL") or "llama3.2", required=True,
        hint="Model must be available at your endpoint"
    )
    values["AGENT_NAME"] = _ask(
        "AGENT_NAME", e("AGENT_NAME") or "Aria",
        hint="Display name shown in the terminal and messages"
    )

    # ── Telegram ──────────────────────────────────────────────────────────────
    section("Telegram (optional)")
    info("Required for: aria-telegram bot and aria --notify")
    info("Get token from @BotFather — get your chat ID from @userinfobot")

    if _ask_bool("Configure Telegram?", default=bool(e("TELEGRAM_TOKEN"))):
        values["TELEGRAM_TOKEN"] = _ask(
            "TELEGRAM_TOKEN", e("TELEGRAM_TOKEN"), secret=True, required=True
        )
        values["TELEGRAM_ALLOWED"] = _ask(
            "TELEGRAM_ALLOWED", e("TELEGRAM_ALLOWED"), required=True,
            hint="Comma-separated chat IDs allowed to message the bot"
        )
    else:
        values["TELEGRAM_TOKEN"]   = e("TELEGRAM_TOKEN")
        values["TELEGRAM_ALLOWED"] = e("TELEGRAM_ALLOWED")

    # ── WhatsApp ──────────────────────────────────────────────────────────────
    section("WhatsApp (optional)")
    info("Required for: aria-whatsapp bridge. Needs Node.js.")

    if _ask_bool("Configure WhatsApp?", default=bool(e("WHATSAPP_ALLOWED"))):
        values["ARIA_WA_PORT"] = _ask(
            "ARIA_WA_PORT", e("ARIA_WA_PORT") or "7532",
            hint="Port for the Python↔Node.js bridge (default: 7532)"
        )
        values["ARIA_WA_SECRET"] = _ask(
            "ARIA_WA_SECRET", e("ARIA_WA_SECRET"), secret=True,
            hint="Shared secret between Python and Node.js bridges"
        )
        values["WHATSAPP_ALLOWED"] = _ask(
            "WHATSAPP_ALLOWED", e("WHATSAPP_ALLOWED"),
            hint="Your WhatsApp number in international format, no + (e.g. 34612345678)"
        )
    else:
        for k in ("ARIA_WA_PORT", "ARIA_WA_SECRET", "WHATSAPP_ALLOWED"):
            values[k] = e(k)

    # ── Gmail / Calendar ──────────────────────────────────────────────────────
    section("Gmail & Calendar (optional)")
    info("Required for: gmail and calendar tools. Uses gogcli (gog).")
    info("Setup: gog auth credentials ~/client_secret.json && gog auth add you@gmail.com")

    if _ask_bool("Configure Gmail/Calendar?", default=bool(e("GOG_ACCOUNT"))):
        values["GMAIL_CLI"] = _ask(
            "GMAIL_CLI", e("GMAIL_CLI") or "gog",
            hint="CLI binary name (usually 'gog')"
        )
        values["GOG_ACCOUNT"] = _ask(
            "GOG_ACCOUNT", e("GOG_ACCOUNT"), required=True,
            hint="Your Gmail address"
        )
    else:
        for k in ("GMAIL_CLI", "GOG_ACCOUNT"):
            values[k] = e(k)

    # ── Agent behaviour ───────────────────────────────────────────────────────
    section("Agent behaviour (optional — press Enter for defaults)")

    values["ARIA_MAX_LOOPS"]            = _ask("ARIA_MAX_LOOPS",            e("ARIA_MAX_LOOPS")            or "20",  hint="Max tool-call loops per turn")
    values["ARIA_MAX_HISTORY"]          = _ask("ARIA_MAX_HISTORY",          e("ARIA_MAX_HISTORY")          or "60",  hint="Conversation turns kept in context")
    values["ARIA_CHANNEL_IDLE_MINUTES"] = _ask("ARIA_CHANNEL_IDLE_MINUTES", e("ARIA_CHANNEL_IDLE_MINUTES") or "60",  hint="Idle minutes before Telegram/WhatsApp session is summarised")
    values["ARIA_REFLECT_BATCH"]        = _ask("ARIA_REFLECT_BATCH",        e("ARIA_REFLECT_BATCH")        or "10",  hint="Sessions per reflection batch")
    values["ARIA_REFLECT_MAX_LINES"]    = _ask("ARIA_REFLECT_MAX_LINES",    e("ARIA_REFLECT_MAX_LINES")    or "40",  hint="Max bullet points in patterns.md")
    values["ARIA_SUPERVISOR_INTERVAL"]  = _ask("ARIA_SUPERVISOR_INTERVAL",  e("ARIA_SUPERVISOR_INTERVAL")  or "30",  hint="Seconds between task queue polls")

    # ── Write ─────────────────────────────────────────────────────────────────
    print()
    if dry_run:
        info(f"[dry-run] would write {env_path}")
    else:
        _write_env(env_path, values)
        ok(f"Config written: {env_path}")

    return values


# ── Detection ─────────────────────────────────────────────────────────────────

def _aria_bin(name: str) -> str | None:
    for candidate in [
        shutil.which(name),
        str(Path.home() / ".local" / "bin" / name),
        str(Path(sys.executable).parent / name),
    ]:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _node_bin() -> str | None:
    for cmd in ("node", "nodejs"):
        path = shutil.which(cmd)
        if path:
            return path
    return None


def _systemd_available() -> bool:
    result = subprocess.run(["systemctl", "--user", "status"], capture_output=True)
    return result.returncode in (0, 1, 3)


def _linger_enabled() -> bool:
    result = subprocess.run(
        ["loginctl", "show-user", os.environ.get("USER", ""), "--property=Linger"],
        capture_output=True, text=True,
    )
    return "Linger=yes" in result.stdout


# ── Service templates ─────────────────────────────────────────────────────────

def _service(description: str, exec_start: str, env_file: str,
             after: str = "network-online.target",
             wants: str = "network-online.target",
             requires: str = "") -> str:
    requires_block = f"Requires={requires}\n" if requires else ""
    return (
        f"[Unit]\nDescription={description}\nAfter={after}\nWants={wants}\n"
        f"{requires_block}\n"
        f"[Service]\nExecStart={exec_start}\nRestart=on-failure\nRestartSec=10\n"
        f"EnvironmentFile={env_file}\n\n"
        f"[Install]\nWantedBy=default.target\n"
    )


# ── Service installation ──────────────────────────────────────────────────────

def install_services(dry_run: bool = False) -> None:
    section("Checking environment")

    if not _systemd_available():
        err("systemd not available. Use nohup instead — see README.")
        sys.exit(1)
    ok("systemd available")

    env_file = Path.home() / ".aria" / ".env"
    if not env_file.exists():
        err(f"Config not found: {env_file}")
        info("Run `aria-install` to create it.")
        sys.exit(1)
    ok(f"Config: {env_file}")

    section("Detecting binaries")

    # Read env to decide which services are actually configured
    env_values = _load_existing_env(env_file)

    def _configured(*keys: str) -> bool:
        """Return True if all given env keys have non-empty values."""
        return all(env_values.get(k, "").strip() for k in keys)

    services: dict[str, dict] = {}
    for name, desc, required_keys in [
        ("aria-telegram",   "Aria Telegram Bot",            ("TELEGRAM_TOKEN", "TELEGRAM_ALLOWED")),
        ("aria-supervisor", "Aria Task Supervisor",          ()),   # always install if binary exists
        ("aria-whatsapp",   "Aria WhatsApp Python Bridge",  ("WHATSAPP_ALLOWED",)),
    ]:
        bin_path = _aria_bin(name)
        if not bin_path:
            warn(f"{name}: binary not found — skipping")
            continue
        if required_keys and not _configured(*required_keys):
            warn(f"{name}: not configured in .env — skipping")
            info(f"Missing: {', '.join(k for k in required_keys if not env_values.get(k, '').strip())}")
            continue
        ok(f"{name}: {bin_path}")
        services[name] = {"description": desc, "exec": bin_path}

    node      = _node_bin()
    wa_bridge = Path.home() / ".aria" / "whatsapp" / "bridge.js"
    if "aria-whatsapp" in services:
        if node and wa_bridge.exists():
            ok(f"node: {node}")
            services["aria-whatsapp-node"] = {
                "description": "Aria WhatsApp Node.js Bridge",
                "exec":        f"{node} {wa_bridge}",
                "requires":    "aria-whatsapp.service",
            }
        else:
            if not node:
                warn("node: not found — aria-whatsapp-node skipped")
            if not wa_bridge.exists():
                warn(f"bridge.js not found at {wa_bridge}")
                info("See README — WhatsApp section")

    if not services:
        err("No services to install. Check your .env configuration.")
        info("Run `aria-install` to configure, or `aria-install --services` to retry.")
        sys.exit(1)

    section(f"Installing {len(services)} service(s)")
    for name in services:
        info(f"• {name}")

    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    if not dry_run:
        systemd_dir.mkdir(parents=True, exist_ok=True)

    for name, cfg in services.items():
        requires = cfg.get("requires", "")
        content  = _service(
            description = cfg["description"],
            exec_start  = cfg["exec"],
            env_file    = str(env_file),
            after       = "aria-whatsapp.service" if requires else "network-online.target",
            wants       = "" if requires else "network-online.target",
            requires    = requires,
        )
        path = systemd_dir / f"{name}.service"
        if dry_run:
            info(f"[dry-run] would write {path}")
        else:
            path.write_text(content, encoding="utf-8")
            ok(f"Written: {path.name}")

    section("Enabling user lingering")
    if _linger_enabled():
        ok("Already enabled")
    elif dry_run:
        info("[dry-run] would run: loginctl enable-linger")
    else:
        r = subprocess.run(["loginctl", "enable-linger"], capture_output=True)
        if r.returncode == 0:
            ok("Enabled")
        else:
            warn("Could not enable — try: sudo loginctl enable-linger $USER")

    section("Starting services")
    if dry_run:
        info("[dry-run] would reload daemon and start services")
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        ok("Daemon reloaded")
        for name in services:
            r = subprocess.run(
                ["systemctl", "--user", "enable", "--now", name],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                ok(f"Started: {name}")
            else:
                err(f"Failed: {name}")
                info(r.stderr.strip())

    if not dry_run:
        section("Verifying")
        import time; time.sleep(2)
        all_ok = True
        for name in services:
            r = subprocess.run(
                ["systemctl", "--user", "is-active", name],
                capture_output=True, text=True,
            )
            status = r.stdout.strip()
            if status == "active":
                ok(f"{name}: active")
            else:
                err(f"{name}: {status}")
                info(f"Logs: journalctl --user -u {name} -n 20")
                all_ok = False

        print()
        if all_ok:
            print(_green(_bold("  All services running. ✦")))
        else:
            print(_yellow(_bold("  Some services failed — check logs above.")))

    print()
    print(_bold("  Useful commands:"))
    for name in services:
        info(f"journalctl --user -fu {name}")
    print()
    restart_names = " ".join(services)
    info(f"After code update: systemctl --user restart {restart_names}")
    print()


# ── Uninstall ─────────────────────────────────────────────────────────────────

def uninstall() -> None:
    section("Uninstalling Aria services")
    names = ["aria-telegram", "aria-supervisor", "aria-whatsapp", "aria-whatsapp-node"]
    for name in names:
        subprocess.run(["systemctl", "--user", "disable", "--now", name], capture_output=True)
        path = Path.home() / ".config" / "systemd" / "user" / f"{name}.service"
        if path.exists():
            path.unlink()
            ok(f"Removed: {path.name}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    ok("Done.")
    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="aria-install",
        description="Interactive setup wizard — configure, install, and start Aria services.",
    )
    parser.add_argument("--dry-run",   action="store_true", help="Show what would be done without changes")
    parser.add_argument("--uninstall", action="store_true", help="Stop and remove all services")
    parser.add_argument("--services",  action="store_true", help="Skip env config, only (re)install services")
    args = parser.parse_args()

    if args.uninstall:
        uninstall()
        return

    if not args.services:
        configure_env(dry_run=args.dry_run)

    install_services(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
