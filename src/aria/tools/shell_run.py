"""
aria/tools/shell_run.py — Execute shell commands with a context-aware policy.

Safety policy (applies to BOTH command and script content):
  - Interactive REPL: destructive (rm, dd, kill, mv …) or secret-touching
    (~/.ssh, cloud creds) ops prompt for confirmation; everything else runs.
  - Non-interactive (Telegram/WhatsApp/supervisor), via ARIA_SHELL_UNATTENDED:
      safe (default) → ONLY a curated allowlist of read-only commands runs
                       (ls, cat, grep, git status, …; extensible via
                       ARIA_SHELL_SAFE_EXTRA); everything else is refused
      off            → no shell at all outside the REPL
      full           → destructive + secret-path rejected, ordinary cmds allowed
                       (legacy blacklist behaviour)
  Destructive detection scans every sub-command (split on ; && || | $() ),
  so chaining like `echo ok && rm -rf ~` is caught. Wrapper prefixes (sudo,
  env, nohup, time, xargs, …) are peeled off before matching, `bash -c '…'`
  payloads are recursed into, and download-and-run pipelines
  (`curl … | bash`) are flagged.
  "Interactive" requires a real TTY AND no active channel context — a bot
  launched from a terminal must never block on input() for a remote user.

Special fields:
  - script: raw script content; written to a temp file and run via
    [interpreter, tmp_path] with shell=False — multi-line, any allowed
    interpreter, and no shell re-parsing of quotes/backticks.
  - stdin:  pipe text into the command's stdin.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import sys

from aria.tools._env import build_env, is_tty_command

# This tool may block on an interactive confirmation prompt (see _confirm). The
# REPL reads this flag and runs shell_run without its live spinner — a redrawing
# rich spinner would otherwise clobber the prompt line and mangle it.
INTERACTIVE = True

# Learnable approval store: command prefixes the user approved with "always" at
# the interactive prompt. A risky command matching a stored prefix skips the
# confirmation. User-driven only (written on an explicit "always"); the agent
# never writes here. Interactive REPL only — channels stay policy-gated.
_ALLOWLIST_FILE = Path.home() / ".aria" / "shell_allowlist.json"

DEFINITION = {
    "name": "shell_run",
    "description": (
        "Run a shell command or script. Provide exactly ONE of:\n"
        "  'command' — a single shell line, run through the shell so pipes (|), "
        "&&, redirects (>) and globs (*) work. Best for simple one-liners.\n"
        "  'script'  — multi-line code written to a temp file and run by "
        "'interpreter' (bash default; also python3, node, ruby, perl) WITHOUT a "
        "shell. Use for multi-line logic, non-bash languages, or any command with "
        "quotes/backticks/filters (a command containing quotes is auto-run as a "
        "script anyway).\n"
        "Destructive ops (rm, dd, mv, kill, …) and commands touching secret paths "
        "(~/.ssh, cloud credentials) need confirmation in the interactive REPL and "
        "are refused in unattended channel/supervisor contexts, where by default "
        "only read-only commands (ls, cat, grep, git status, …) are allowed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "A single shell command line, run through the shell so pipes, "
                    "&&, redirects and globs work. For multi-line logic or non-bash "
                    "code, use 'script' instead."
                ),
            },
            "script": {
                "type": "string",
                "description": (
                    "Multi-line code written to a temp file and executed with "
                    "'interpreter' (default bash) without a shell, so quotes and "
                    "backticks are not re-parsed. Use for multi-line logic, "
                    "Python/Node/etc., or any command with quotes, backticks, or "
                    "AWS/jq/SQL filters."
                ),
            },
            "interpreter": {
                "type": "string",
                "description": "Interpreter for 'script'. Default: bash. Options: bash, sh, python3, python, node, ruby, perl.",
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

# Destructive binaries — checked as the leading command of EACH sub-command
# (split on shell operators) so chaining like `echo ok && rm -rf ~` is caught,
# not just the first token.
_DESTRUCTIVE_CMDS = {
    "rm", "rmdir", "dd", "mkfs", "fdisk", "parted",
    "shutdown", "reboot", "halt", "poweroff",
    "kill", "killall", "pkill",
    "chown", "chgrp", "mv", "shred", "wipe",
    "userdel", "groupdel", "passwd",
}
# Split a command line into sub-commands at shell control operators / substitutions.
_SPLIT_RE = re.compile(r"\$\(|\|\||&&|;|\||&|\n|`|\(|\)")
# Wrapper commands that run another command — peeled off the front of each
# segment so `sudo rm`, `env rm`, `nohup rm &`, `xargs rm` etc. expose the real
# command as the leading token instead of evading the check.
_WRAPPER_CMDS = {
    "sudo", "doas", "env", "nohup", "time", "command", "xargs",
    "stdbuf", "setsid", "nice", "ionice",
}
# Shell binaries whose `-c '…'` payload is a nested script — recursed into.
_SHELL_BINS = {"bash", "sh", "zsh", "dash", "ksh"}
# Download-and-run pipelines: network fetch piped into an interpreter.
_DOWNLOAD_RUN_RE = re.compile(
    r"(?ix)\b(?:curl|wget)\b[^\n;&|]*\|\s*"
    r"(?:sudo\s+|doas\s+)?(?:env\s+)?"
    r"(?:bash|sh|zsh|dash|ksh|python3?|perl|ruby|node)\b"
)
# High-confidence destructive patterns matched anywhere in the text (covers
# bash and common Python/Node forms that the per-segment leading-token check
# can't see).
_EXTRA_DESTRUCTIVE_RE = re.compile(
    r"(?ix)"
    r"\bfind\b[^\n]*?(?:-delete|-exec\s+rm)\b"
    r"|\bgit\s+clean\s+-[a-z]*f"
    r"|\bgit\s+reset\s+--hard\b"
    r"|>\s*/dev/sd|of=/dev/"
    r"|\btruncate\s+-s\s*0"
    r"|:\(\)\s*\{\s*:\s*\|\s*:"                       # fork bomb
    r"|\bshutil\.rmtree\b|\bos\.remove\b|\bos\.unlink\b"
    r"|\bchmod\s+-R\b"
)
# Sensitive paths whose mere appearance in a command is a red flag (SSH keys,
# cloud credentials, Aria's own secrets, PEM private keys).
_SECRET_PATH_RE = re.compile(
    r"(?ix)"
    r"\.ssh/|/\.ssh\b|\bid_rsa\b|\bid_ed25519\b|\bid_ecdsa\b|\bid_dsa\b"
    r"|\.aws/credentials|\.aws/config\b"
    r"|\.config/gcloud|\.config/gh\b"
    r"|\.kube/config|\.netrc\b|\.pgpass\b|\.docker/config\.json"
    r"|\.aria/\.env\b"
    r"|-----BEGIN[A-Z0-9\s]*PRIVATE\sKEY-----"
)

# Interactive-only Python invocations (no args = REPL)
_PYTHON_REPL_RE = re.compile(r"^\s*python3?\s*$")


def _unattended_policy() -> str:
    """Shell policy for non-interactive contexts (channels/supervisor).
    safe (default) = only read-only allowlisted commands run;
    off = no shell at all; full = legacy blacklist (destructive + secret-path
    blocked, everything else allowed)."""
    val = os.environ.get("ARIA_SHELL_UNATTENDED", "safe").strip().lower()
    return val if val in ("safe", "off", "full") else "safe"


def _strip_wrappers(s: str) -> tuple[str, bool]:
    """Peel FOO=bar assignments, wrapper commands (sudo, env, nohup, time,
    xargs, …) and the wrappers' own option flags off the front of a segment so
    the wrapped command becomes the leading token. Returns (stripped, True if
    any wrapper was peeled)."""
    wrapped = False
    while True:
        s = re.sub(r"^(?:\w+=\S*\s+)+", "", s).lstrip()   # FOO=bar env prefixes
        m = re.match(r"[\"']?([\w./-]+)", s)
        if not m or os.path.basename(m.group(1)).lower() not in _WRAPPER_CMDS:
            return s, wrapped
        wrapped = True
        s = s[m.end():].lstrip()
        while True:                       # the wrapper's own flags (-u, -oL, -n 10 …)
            f = re.match(r"-\S+\s*", s)
            if not f:
                break
            s = s[f.end():]


def _inline_shell_payload(s: str) -> str | None:
    """Extract the script argument of `bash -c '…'` / `sh -lc "…"` so it can be
    re-scanned for destructive ops. Returns None when there is no -c flag."""
    m = re.match(r"[\"']?[\w./-]+\s+((?:-[\w-]+\s+)*)(.*)$", s, re.S)
    if not m or "c" not in m.group(1).replace("-", ""):
        return None
    rest = m.group(2).strip()
    if rest[:1] in ("'", '"'):
        quote = rest[0]
        end = rest.rfind(quote)
        rest = rest[1:end] if end > 0 else rest[1:]
    return rest.strip() or None


def _is_destructive(text: str) -> str | None:
    """Return a reason string if the command performs a destructive operation."""
    for seg in _SPLIT_RE.split(text):
        s = seg.strip()
        if not s:
            continue
        s, wrapped = _strip_wrappers(s)
        m = re.match(r"[\"']?([\w./-]+)", s)
        if not m:
            continue
        cmd = os.path.basename(m.group(1)).lower()
        if cmd in _DESTRUCTIVE_CMDS:
            return f"destructive command '{cmd}'"
        if cmd in _SHELL_BINS:
            # `bash -c 'rm -rf ~'` — recurse into the quoted payload
            payload = _inline_shell_payload(s)
            if payload:
                hit = _is_destructive(payload)
                if hit:
                    return hit
        if wrapped:
            # a wrapper hid the real command behind a flag argument
            # (`sudo -u root rm`, `xargs -I{} rm {}`) — scan every token
            for tok in re.findall(r"[\w./-]+", s):
                tok = os.path.basename(tok).lower()
                if tok in _DESTRUCTIVE_CMDS:
                    return f"destructive command '{tok}'"
    if _DOWNLOAD_RUN_RE.search(text):
        return "download-and-execute pipeline (network fetch piped into an interpreter)"
    if _EXTRA_DESTRUCTIVE_RE.search(text):
        return "destructive operation"
    return None


def _touches_secret(text: str) -> bool:
    return bool(_SECRET_PATH_RE.search(text))


def _is_interactive() -> bool:
    """Interactive means a real TTY on stdin AND no active channel turn. A bot
    launched from a terminal still has a TTY, but a Telegram/WhatsApp/supervisor
    turn must never trigger a blocking input() prompt for a remote user — an
    active channel context always takes the unattended policy branch."""
    try:
        from aria import context
        if context.current() is not None:
            return False
    except Exception:
        pass
    return os.isatty(0)


def _command_prefix(command: str) -> str:
    """The learnable unit: first two tokens of the command (e.g. 'git push',
    'npm install', 'rm -rf'). Matching is on a token boundary, so approving
    'git push' covers 'git push origin main' but not 'gitfoo'."""
    toks = command.strip().split()
    return " ".join(toks[:2]) if toks else ""


def _load_allowlist() -> list[str]:
    try:
        data = json.loads(_ALLOWLIST_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _persist_allow(prefix: str) -> None:
    if not prefix:
        return
    allow = _load_allowlist()
    if prefix in allow:
        return
    allow.append(prefix)
    try:
        _ALLOWLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ALLOWLIST_FILE.write_text(json.dumps(allow), encoding="utf-8")
        _ALLOWLIST_FILE.chmod(0o600)
    except OSError:
        pass


def _is_allowlisted(command: str) -> bool:
    cmd = command.strip()
    for prefix in _load_allowlist():
        if cmd == prefix or cmd.startswith(prefix + " "):
            return True
    return False


def _tty_write(text: str) -> None:
    """Write straight to the real terminal, bypassing any stdout redirection the
    caller may have installed (e.g. a rich live spinner replaces sys.stdout with
    a markup-parsing proxy that eats the '[y]'/'[a]' bracket tokens out of the
    prompt). Falls back to builtin print if the original stream is unavailable."""
    stream = sys.__stdout__ or sys.stdout
    try:
        stream.write(text)
        stream.flush()
    except Exception:
        print(text, end="", flush=True)


def _ask(prompt: str) -> str:
    """Show `prompt` on the real terminal (unmangled) and read one line. The read
    goes through builtin input(), so line-editing works and tests can patch it."""
    _tty_write(prompt)
    try:
        return input("").strip().lower()
    except EOFError:
        return ""


def _confirm(command: str, reason: str = "") -> bool:
    if not _is_interactive():
        return False
    why = f"  ⚠️  {reason}\n" if reason else ""
    prefix = _command_prefix(command)
    _tty_write(f"\n⚠️  Shell command requested:\n{why}  $ {command}\n")
    answer = _ask(f"  Run? [y]es / [N]o / [a]lways ('{prefix}') ")
    if answer in ("a", "always"):
        _persist_allow(prefix)
        _tty_write(f"  ✓ Will not ask again for commands starting with '{prefix}'.\n")
        return True
    return answer in ("y", "yes")


# Read-only commands allowed to run unattended under ARIA_SHELL_UNATTENDED=safe.
# Deliberately excluded: sed/awk (write files / spawn commands), python/node/…
# (arbitrary code, incl. `python -c`), curl/wget (exfiltration + download-run),
# env/xargs/sudo and other wrappers (command smuggling), tee/chmod (writes).
_SAFE_UNATTENDED_CMDS = {
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "echo", "printf",
    "pwd", "whoami", "date", "ps", "df", "du", "stat", "file", "which",
    "uname", "hostname", "id", "uptime", "free", "printenv", "sort", "uniq",
    "cut", "tr", "diff", "cmp", "md5sum", "sha1sum", "sha256sum", "basename",
    "dirname", "readlink", "realpath", "tree", "true", "false", "git",
}
# git is allowed only with a read-only subcommand.
_GIT_SAFE_SUBCMDS = {
    "status", "log", "diff", "show", "blame", "shortlog", "ls-files",
    "rev-parse", "describe",
}
# Output redirections write files; only /dev/null and fd-dups (2>&1) are benign.
_WRITE_REDIRECT_RE = re.compile(r"\d?>{1,2}(?!\s*(?:&\d|/dev/null\b))")


def _safe_allowlist() -> set[str]:
    """Base read-only allowlist plus ARIA_SHELL_SAFE_EXTRA (comma-separated
    extra leading tokens the user explicitly trusts unattended)."""
    raw = os.environ.get("ARIA_SHELL_SAFE_EXTRA", "")
    extra = {t.strip().lower() for t in raw.split(",") if t.strip()}
    return _SAFE_UNATTENDED_CMDS | extra


def _safe_refusal(what: str) -> str:
    return (
        f"[shell_run] Refused — '{what}' is not on the unattended-safe allowlist "
        "(ARIA_SHELL_UNATTENDED=safe runs only read-only commands like ls, cat, "
        "grep, git status). Run it yourself in a terminal, add the command to "
        "ARIA_SHELL_SAFE_EXTRA, or set ARIA_SHELL_UNATTENDED=full."
    )


def _git_subcommand(s: str) -> str:
    """First non-flag token after `git`, skipping arg-taking globals (-C, -c)."""
    toks = s.split()[1:]
    skip_next = False
    for t in toks:
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            if t in ("-C", "-c"):
                skip_next = True
            continue
        return t.lower()
    return ""


def _check_safe_unattended(payload: str) -> str | None:
    """Allowlist gate for non-interactive `safe` mode: every sub-command's
    leading token must be a known read-only verb. Wrappers (sudo, env, nohup,
    xargs, …) are NOT peeled here — they are simply not on the list, so they
    are refused rather than letting them smuggle a command through. Fails
    closed on anything unparseable."""
    if _WRITE_REDIRECT_RE.search(payload):
        return _safe_refusal("output redirection (>)")
    allow = _safe_allowlist()
    # Drop benign fd-dups (2>&1) BEFORE splitting — the '&' splitter would
    # otherwise leave a bogus '1' segment that fails the allowlist.
    payload = re.sub(r"\d?>{1,2}\s*&\s*\d+", " ", payload)
    for seg in _SPLIT_RE.split(payload):
        s = seg.strip()
        if not s or s.startswith("#"):
            continue
        s = re.sub(r"^(?:\w+=\S*\s+)+", "", s)          # strip FOO=bar env prefixes
        m = re.match(r"[\"']?([\w./-]+)", s)
        if not m:
            return _safe_refusal(s.split()[0] if s.split() else s)
        cmd = os.path.basename(m.group(1)).lower()
        if cmd not in allow:
            return _safe_refusal(cmd)
        if cmd == "git":
            sub = _git_subcommand(s)
            if sub not in _GIT_SAFE_SUBCMDS:
                return _safe_refusal(f"git {sub}".strip())
        if cmd == "find" and re.search(r"-(?:exec|execdir|ok|okdir|delete)\b", s):
            return _safe_refusal("find -exec/-delete")
    return None


def _gate(payload: str) -> str | None:
    """
    Apply the shell safety policy to a command or script BEFORE it runs.
    Returns a rejection string to abort, or None to proceed.

    - Interactive REPL: destructive/secret-touching ops prompt for confirmation;
      everything else runs (no friction for normal dev work).
    - Non-interactive (Telegram/WhatsApp/supervisor), per ARIA_SHELL_UNATTENDED:
        off  → all shell rejected
        safe → only the read-only allowlist runs (_check_safe_unattended);
               destructive AND secret-path always rejected
        full → destructive rejected; secret-path allowed; ordinary commands
               allowed (legacy blacklist behavior)
    """
    interactive = _is_interactive()
    policy      = _unattended_policy()

    if not interactive and policy == "off":
        return ("[shell_run] Shell is disabled outside the interactive REPL "
                "(ARIA_SHELL_UNATTENDED=off).")

    reasons: list[str] = []
    dest = _is_destructive(payload)
    if dest:
        reasons.append(dest)
    if _touches_secret(payload):
        reasons.append("references a sensitive path (SSH keys / cloud credentials / Aria secrets)")

    if not reasons:
        if interactive or policy == "full":
            return None  # ordinary command — allowed
        # Non-interactive 'safe': only the read-only allowlist runs.
        return _check_safe_unattended(payload)

    if interactive:
        # A command the user previously approved with "always" runs without a
        # repeat prompt (learnable trust). Everything else still confirms.
        if _is_allowlisted(payload):
            return None
        return None if _confirm(payload, "; ".join(reasons)) else "[shell_run] Cancelled by user."

    # Non-interactive + risky: destructive is always refused; secret-path is
    # refused under 'safe' but permitted under 'full'.
    if dest or policy == "safe":
        return (f"[shell_run] Refused — {'; '.join(reasons)} — in non-interactive mode "
                f"(ARIA_SHELL_UNATTENDED={policy}). Run it yourself in a terminal if intended.")
    return None


# Interpreters allowed for the script field — no shell metacharacters possible
# since we pass [interpreter, tmp_path] as a list, bypassing the shell entirely.
_ALLOWED_INTERPRETERS = {
    "bash", "sh", "python3", "python", "node", "ruby", "perl", "raku",
}


def execute(args: dict) -> str:
    script_content = args.get("script", "").strip()
    command        = args.get("command", "").strip()
    interpreter    = args.get("interpreter", "bash").strip()
    stdin_text     = args.get("stdin")
    cwd            = args.get("cwd")
    timeout        = int(args.get("timeout", 60))

    # ── Auto-redirect: command with quotes → script mode ──────────────────
    # A command containing quotes/backticks runs more predictably as a script
    # (shell=False, no shell re-parsing of the quotes). Same result, safer exec.
    if command and not script_content and ('"' in command or "'" in command or '`' in command):
        script_content = command
        command = ""

    # ── Script mode ───────────────────────────────────────────────────────
    if script_content:
        # Whitelist interpreter — prevent injection via metacharacters
        interp_bin = interpreter.split()[0]  # e.g. "python3" from "python3 -u"
        if interp_bin not in _ALLOWED_INTERPRETERS:
            return (
                f"[shell_run] Interpreter not allowed: '{interp_bin}'. "
                f"Allowed: {', '.join(sorted(_ALLOWED_INTERPRETERS))}"
            )
        # Safety policy applies to script content too — script mode used to skip
        # every check, so a destructive script ran unguarded in any context.
        gate = _gate(script_content)
        if gate:
            return gate
        suffix = ".py" if "python" in interp_bin else ".sh"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(script_content)
            tmp_path = tmp.name
        try:
            # Pass as a list — shell=False, no metacharacter risk
            return _run_script([interpreter, tmp_path],
                               stdin_text=stdin_text, cwd=cwd, timeout=timeout)
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

    # Safety policy: confirm (REPL) or reject (channels/supervisor) destructive
    # and secret-touching commands; ordinary commands run unimpeded.
    gate = _gate(command)
    if gate:
        return gate

    return _run_shell(command, stdin_text=stdin_text, cwd=cwd, timeout=timeout)


def _run_script(
    argv: list[str],
    stdin_text: str | None,
    cwd: str | None,
    timeout: int,
) -> str:
    """Run a script via an explicit argv list — shell=False, no injection risk.
    Prefixed with the sandbox wrapper when one is configured."""
    argv = [*_sandbox_prefix(), *argv]
    try:
        result = subprocess.run(
            argv,
            shell=False,
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
        return (f"[shell_run error] Script timed out after {timeout}s and was "
                "killed. It did NOT complete. Do not just re-run it — raise the "
                "`timeout` argument if it legitimately needs longer, or run it in "
                "the background and poll.")
    except Exception as exc:
        return f"[shell_run error] {exc}"


def _sandbox_prefix() -> list[str]:
    """Optional real isolation. ARIA_SHELL_SANDBOX is a command prefix the shell
    invocation is wrapped in (e.g. 'firejail --quiet --private-tmp' or a bwrap
    line). Empty/unset → no wrapping. Honoured only if the wrapper binary exists,
    so a misconfigured value can't silently break every command."""
    import shlex
    raw = os.environ.get("ARIA_SHELL_SANDBOX", "").strip()
    if not raw:
        return []
    parts = shlex.split(raw)
    if parts and Path(parts[0]).name and _which(parts[0]):
        return parts
    return []


def _which(binary: str) -> bool:
    import shutil
    return shutil.which(binary) is not None


def _run_shell(
    command: str,
    stdin_text: str | None,
    cwd: str | None,
    timeout: int,
) -> str:
    """Run a shell command string — shell=True intentional for pipes, &&, redirects.
    Under a configured sandbox, run it as `<sandbox> bash -c <command>` instead."""
    sandbox = _sandbox_prefix()
    try:
        if sandbox:
            result = subprocess.run(
                [*sandbox, "bash", "-c", command],
                shell=False, capture_output=True, text=True,
                timeout=timeout, cwd=cwd, env=build_env(), input=stdin_text,
            )
        else:
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
        return (f"[shell_run error] Command timed out after {timeout}s and was "
                "killed. It did NOT complete. Do not just re-run the same command "
                "— raise the `timeout` argument if it legitimately needs longer, "
                "or run it in the background (append ` &` / use nohup) and poll.")
    except Exception as exc:
        return f"[shell_run error] {exc}"
