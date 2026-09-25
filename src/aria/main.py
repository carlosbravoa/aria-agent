"""
aria/main.py — CLI entry point.

Usage:
  aria                          # interactive REPL
  aria "query"                  # single-shot, prints to stdout
  aria --notify "query"         # single-shot, pushes the result (Telegram by default)
  aria --notify --chat 123 "q"  # single-shot, sends to a specific recipient/chat ID
  aria --notify --channel whatsapp "q"   # push via a specific channel plugin
  aria --version                # print version and exit
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import TYPE_CHECKING, Any

from aria import config, __version__
from aria.setup import is_first_run, run as _setup_run
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

if TYPE_CHECKING:
    from aria.agent import Agent

# Importing this module has no side effects. The first-run wizard,
# config.load() and the `aria.agent` import (whose module-level constants read
# os.environ) all happen in main(), in that order — see _startup().

# ── Theme ─────────────────────────────────────────────────────────────────────
_THEME = Theme({
    "prompt":    "bold cyan",
    "agent":     "bold green",
    "meta":      "dim",
    "cmd":       "bold yellow",
    "error":     "bold red",
    "success":   "bold green",
    "separator": "dim blue",
})

def _make_consoles() -> tuple[Console, Console]:
    # Console.print has no `file=` kwarg — errors go through a stderr console.
    return (Console(theme=_THEME, highlight=False),
            Console(theme=_THEME, highlight=False, stderr=True))


console, err_console = _make_consoles()


def _agent_class() -> Any:
    """`Agent`, imported on first use (after config.load()). A value already
    bound on this module — e.g. a test's monkeypatch — is returned as-is."""
    g = globals()
    if "Agent" not in g:
        from aria.agent import Agent as _Agent
        g["Agent"] = _Agent
    return g["Agent"]


def __getattr__(name: str) -> Any:
    # Keeps `aria.main.Agent` working as a module attribute.
    if name == "Agent":
        return _agent_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _startup() -> None:
    """What importing this module used to do: first-run wizard (exits), load
    .env, then import Agent — so aria.agent's import-time env reads and the
    rich consoles (which read NO_COLOR/COLUMNS/TERM... at construction) see
    the .env values."""
    global console, err_console
    if is_first_run():
        _setup_run()
    config.load()
    console, err_console = _make_consoles()
    _agent_class()


# ── Input: prompt_toolkit session ─────────────────────────────────────────────

_COMMANDS = [
    "/help", "/memory", "/tools", "/clear", "/compact", "/retry", "/copy",
    "/save ", "/markdown ", "/version", "/cost", "/usage", "/trust", "/models",
    "/model ", "/discard", "/remote", "/channel ", "/quit", "/exit",
]


def _make_prompt_session(agent=None):
    """
    Build a prompt_toolkit session for the REPL input box: persistent history,
    autosuggest from history (ghost text), fuzzy reverse-search (Ctrl+R),
    slash-command completion + highlighting, @file path completion, a status-line
    footer (model · cwd · tokens), and Alt+Enter for a newline so the user can
    compose multi-line messages while plain Enter still submits.

    `agent` feeds the status line (model/token state). Returns None if
    prompt_toolkit is unavailable (e.g. minimal Windows install) — the REPL then
    falls back to a plain input() prompt.
    """
    try:
        import glob
        from pathlib import Path
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.lexers import Lexer
        from prompt_toolkit.styles import Style
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError:
        return None

    history_file = Path.home() / ".aria" / ".repl_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    class _SlashCompleter(Completer):
        """Complete /commands at line start, and @paths anywhere; stay silent for
        ordinary prose."""
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if "\n" not in text and text.startswith("/"):
                for c in _COMMANDS:
                    if c.startswith(text) and c != text:
                        yield Completion(c, start_position=-len(text))
                return
            # @file mention → complete filesystem paths (relative to cwd).
            word = text.rsplit(" ", 1)[-1].rsplit("\n", 1)[-1]
            if word.startswith("@"):
                frag = word[1:]
                try:
                    matches = sorted(glob.glob(os.path.expanduser(frag) + "*"))
                except OSError:
                    matches = []
                for m in matches[:30]:
                    disp = m + ("/" if os.path.isdir(m) else "")
                    yield Completion(disp, start_position=-len(frag))

    class _SlashLexer(Lexer):
        """Colour a leading /command token; leave the rest as plain text."""
        def lex_document(self, document):
            def get_line(lineno):
                line = document.lines[lineno]
                if lineno == 0 and line.startswith("/"):
                    head, sep, tail = line.partition(" ")
                    frags = [("class:cmd", head)]
                    if sep:
                        frags.append(("", sep + tail))
                    return frags
                return [("", line)]
            return get_line

    kb = KeyBindings()

    @kb.add("escape", "enter")          # Alt+Enter / Esc-then-Enter → newline
    def _(event) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("tab")                      # Tab → complete /command, else accept ghost
    def _(event) -> None:
        """Deterministic Tab: cycle an open completion menu, start completion for
        a slash command, otherwise accept the history auto-suggestion (ghost
        text). We bind it explicitly because the default menu-complete behaviour
        is unreliable alongside complete_while_typing + auto-suggest."""
        buff = event.current_buffer
        if buff.complete_state:                      # menu already open → next item
            buff.complete_next()
            return
        if buff.document.text_before_cursor.lstrip().startswith("/"):
            buff.start_completion(select_first=True)  # /command → fill first match
            return
        suggestion = buff.suggestion                  # else accept ghost text
        if suggestion and suggestion.text:
            buff.insert_text(suggestion.text)

    style = Style.from_dict({
        "prompt":         "bold ansicyan",
        "cmd":            "bold ansiyellow",
        "bottom-toolbar": "fg:ansiwhite bg:ansiblack",
    })

    def _toolbar():
        """Persistent footer: agent · model · cwd · session tokens."""
        if agent is None:
            return None
        cwd = os.getcwd()
        home = os.path.expanduser("~")
        if cwd.startswith(home):
            cwd = "~" + cwd[len(home):]
        if len(cwd) > 40:
            cwd = "…" + cwd[-39:]
        tok = agent._session_tokens
        return HTML(
            f" {agent.name} · {agent.model} · {cwd} · "
            f"↑{tok['in']:,} ↓{tok['out']:,} tok"
        )

    return PromptSession(
        history=FileHistory(str(history_file)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=_SlashCompleter(),
        complete_while_typing=True,
        lexer=_SlashLexer(),
        key_bindings=kb,
        style=style,
        bottom_toolbar=_toolbar,
    )


# ── REPL ──────────────────────────────────────────────────────────────────────

_HELP_TEXT = """
[cmd]/memory[/]      Print current memory
[cmd]/tools[/]       List available tools
[cmd]/clear[/]       Clear conversation history
[cmd]/compact[/]     Summarize the conversation to reclaim context tokens
[cmd]/retry[/]       Re-run your last message
[cmd]/copy[/]        Copy the last answer to the clipboard
[cmd]/save[/] [meta]<note>[/]  Append a note to memory
[cmd]/markdown[/] [meta][on|off][/]  Toggle Markdown rendering
[cmd]/cost[/]        Show session token usage
[cmd]/usage[/]       Show lifetime token usage (all sessions, by model/channel)
[cmd]/trust[/] [meta][clear][/]  Show/clear auto-approved shell commands
[cmd]/remote[/] [meta][on|off|control|release] [channel][/]  Channels online while Aria is open; [cmd]control[/] = drive this session from the phone
[cmd]/channel[/] [meta][start|stop|restart|logs] <name>[/]  Channel background services (list with no arguments)
[cmd]/version[/]     Show version
[cmd]/quit[/]        Exit  [meta](or Ctrl+D)[/]

[meta]Tips: [cmd]!cmd[/] runs a shell command · [cmd]@path/to/file[/] attaches a file · [cmd]Esc[/]/[cmd]Ctrl+C[/] interrupts a reply (keeps context) · [cmd]Alt+Enter[/] newline[/]
"""


# \S+ rather than [^\s@]+: paths may contain '@' (/home/user@corp.com/x). The
# lookbehind still keeps email addresses (user@host) from matching.
_MENTION_RE = re.compile(r"(?<![\w@])@(\S+)")
_MENTION_MAX_BYTES = 100_000  # per-file cap; keeps a stray @bigfile from blowing context


def _expand_mentions(text: str) -> str:
    """Expand `@path` mentions into attached file contents, resolved against the
    current working directory. The original message keeps the @reference; the
    file bodies are appended in a clearly-fenced block. Missing/binary/oversized
    files are flagged inline rather than silently dropped. Terminal-only sugar."""
    seen: list[str] = []
    attachments: list[str] = []
    for m in _MENTION_RE.finditer(text):
        raw = m.group(1).rstrip(".,;:)")          # drop trailing punctuation
        if raw in seen:
            continue
        seen.append(raw)
        path = os.path.expanduser(raw)
        if not os.path.isfile(path):
            attachments.append(f"### @{raw}\n_(no such file — ignored)_")
            console.print(f"  [meta]@{raw}: no such file — sent as plain text.[/]")
            continue
        try:
            data = open(path, "rb").read(_MENTION_MAX_BYTES + 1)
        except OSError as exc:
            attachments.append(f"### @{raw}\n_(could not read: {exc})_")
            continue
        if b"\x00" in data:
            attachments.append(f"### @{raw}\n_(binary file — skipped)_")
            console.print(f"  [meta]@{raw}: binary — skipped.[/]")
            continue
        body = data[:_MENTION_MAX_BYTES].decode("utf-8", "replace")
        truncated = "\n…[truncated]" if len(data) > _MENTION_MAX_BYTES else ""
        attachments.append(f"### @{raw}\n```\n{body}{truncated}\n```")
        console.print(f"  [meta]attached @{raw} "
                      f"({len(body):,} chars)[/]")
    if not attachments:
        return text
    return text + "\n\n--- Attached files ---\n" + "\n\n".join(attachments)


def _run_shell_escape(cmd: str) -> None:
    """Run a `!command` straight in the user's shell — no LLM, output passes
    through live so interactive tools work. Ctrl+C kills the command, not the REPL."""
    if not cmd:
        return
    import subprocess
    try:
        subprocess.run(cmd, shell=True, cwd=os.getcwd())
    except KeyboardInterrupt:
        console.print("\n  [meta](command interrupted)[/]")
    except Exception as exc:
        console.print(f"  [error]{exc}[/]")


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard via the first available tool. Returns
    False if none is present (Wayland/X11/macOS/Windows all covered)."""
    import shutil
    import subprocess
    candidates = [
        (["wl-copy"], None),
        (["xclip", "-selection", "clipboard"], None),
        (["xsel", "--clipboard", "--input"], None),
        (["pbcopy"], None),
        (["clip"], None),
    ]
    for argv, _ in candidates:
        if shutil.which(argv[0]):
            try:
                subprocess.run(argv, input=text.encode("utf-8"), check=True)
                return True
            except Exception:
                continue
    return False


def _print_banner(agent: Agent) -> None:
    title = Text()
    title.append(f" {agent.name} ", style="bold green")
    title.append(f"v{__version__}", style="dim green")

    subtitle = Text()
    subtitle.append(f"workspace: {agent.ws.root}", style="meta")

    console.print()
    console.print(Panel(
        subtitle,
        title=title,
        border_style="dim blue",
        padding=(0, 1),
    ))
    console.print("  Type [cmd]/help[/] for commands, [cmd]↑[/] for history.\n",
                  style="meta")


_WAKE = "\x00aria-remote-wake\x00"   # returned by _prompt when a remote turn interrupts it


class _Waker:
    """Interrupts the prompt when a remote-control message arrives, keeping
    whatever the user had typed so the next prompt restores it. `wake()` is
    called from the channel's thread; the exit runs on prompt_toolkit's loop."""

    def __init__(self, session) -> None:
        self.session = session
        self.saved = ""

    def wake(self) -> None:
        app = self.session.app
        loop = getattr(app, "loop", None)
        if app.is_running and loop is not None:
            try:
                loop.call_soon_threadsafe(self._exit)
            except RuntimeError:
                pass              # prompt just finished — the loop picks the turn up

    def pre_run(self) -> None:
        # A message queued between the loop's check and the prompt starting.
        from aria.channels import control
        if control.pending():
            self._exit()

    def _exit(self) -> None:
        app = self.session.app
        if not app.is_running:
            return
        try:
            self.saved = self.session.default_buffer.text
            app.exit(result=_WAKE)
        except Exception:
            pass                  # already exiting (user pressed Enter)

    def take_saved(self) -> str:
        text, self.saved = self.saved, ""
        return text


def _prompt(session, waker: _Waker | None = None) -> str:
    """
    Read a line of input. Uses the prompt_toolkit session when available
    (history, autosuggest, completion, multi-line); falls back to a plain
    coloured input() prompt when prompt_toolkit isn't installed.

    Raises EOFError on Ctrl+D and KeyboardInterrupt on Ctrl+C, which the REPL
    loop treats as "exit" and "cancel line" respectively.
    """
    if session is not None:
        if waker is None:
            return session.prompt([("class:prompt", "  You › ")]).strip()
        result = session.prompt([("class:prompt", "  You › ")],
                                default=waker.take_saved(), pre_run=waker.pre_run)
        return result if result == _WAKE else result.strip()

    # Fallback: ANSI-coloured input(); \001..\002 mark non-printing width.
    CYAN_BOLD = "\001\033[1;36m\002"
    RESET     = "\001\033[0m\002"
    return input(f"  {CYAN_BOLD}You ›{RESET} ").strip()


# ── Attached channels ─────────────────────────────────────────────────────────
# A channel in attached mode (ARIA_CHANNEL_MODE_<NAME>=attached) runs inside
# this process while the REPL is open — nothing in the background.

def _start_attached_channels() -> None:
    try:
        from aria import channels
        from aria.channels import attached
        plugins = channels.attached_channels()
    except Exception as exc:
        console.print(f"  [error]Attached channels unavailable: {exc}[/]")
        return
    for p in plugins:
        ok, msg = attached.start(p)
        console.print(f"  [{'success' if ok else 'meta'}]📱 {msg}[/]")
        if ok and p.mode == "control":
            _take_control(p.name)


def _stop_attached_channels() -> None:
    try:
        from aria.channels import attached
    except Exception:
        return
    for name, _ in attached.status():
        console.print(f"  [meta]📱 {attached.stop(name, timeout=5)}[/]")


def _remote_command(rest: str) -> None:
    """/remote                     status
    /remote on|off [channel]    attach/detach (channel optional when only one fits)"""
    from aria import channels
    from aria.channels import attached
    args = rest.split()
    action = args[0].lower() if args else ""
    running = dict(attached.status())
    capable = [p for p in channels.discover().values()
               if p.supports_attached and p.is_configured()]

    if action not in ("on", "off", "control", "release"):
        if action:
            console.print("  [error]Usage: /remote [on|off|control|release] [channel][/]")
            return
        if not capable and not running:
            console.print("  [meta]No configured channel can run attached "
                          "(e.g. set TELEGRAM_TOKEN and TELEGRAM_ALLOWED).[/]")
            return
        from aria.channels import control
        ctl = control.controlled()
        for p in capable:
            state = running.get(p.name, "offline")
            if p.name in ctl:
                state += ", controls this session"
            console.print(f"  [cmd]{p.name:10}[/] [meta]{state}  (mode: {p.mode})[/]")
        console.print(f"  [meta]Logs: {attached.log_path()}[/]")
        return

    if len(args) > 1:
        name = args[1].lower()
    else:
        pool = (list(running) if action in ("off", "release")
                else [p.name for p in capable])
        if len(pool) != 1:
            console.print(f"  [error]Which channel? /remote {action} <name>"
                          f"{' — ' + ', '.join(pool) if pool else ''}[/]")
            return
        name = pool[0]

    from aria.channels import control
    if action in ("off", "release"):
        if name in control.controlled():
            control.disable(name)
            console.print(f"  [meta]📱 {name} no longer controls this session "
                          f"(its chats get their own session again)[/]")
        if action == "off":
            console.print(f"  [meta]📱 {attached.stop(name)}[/]")
        return
    plugin = channels.get(name)
    if plugin is None or not plugin.is_configured():
        console.print(f"  [error]{name}: unknown or not configured[/]")
        return
    if name not in running:
        ok, msg = attached.start(plugin)
        console.print(f"  [{'success' if ok else 'error'}]📱 {msg}[/]")
        if not ok:
            return
    if action == "control":
        _take_control(name)


def _channel_command(rest: str) -> None:
    """/channel                         every channel and its background service
    /channel start|stop|restart <name>  manage the service (systemd, else a detached process)
    /channel logs <name>              its recent log"""
    from rich.markup import escape
    from aria.channels import attached, services
    args = rest.split()
    action = args[0].lower() if args else ""
    here = dict(attached.status())

    if action in ("", "list", "status"):
        rows = services.status()
        if not rows:
            console.print("  [meta]No channels found.[/]")
            return
        for p, state in rows:
            configured = "" if p.is_configured() else "  (not configured)"
            extra = f", attached here: {here[p.name]}" if p.name in here else ""
            console.print(f"  [cmd]{p.name:10}[/] [meta]service: {state}  (mode: {p.mode})"
                          f"{extra}{configured}[/]")
        console.print("  [meta]/channel start|stop|restart|logs <name> · "
                      "/remote on <name> to run one only while this window is open[/]")
        return
    if action not in ("start", "stop", "restart", "logs") or len(args) < 2:
        console.print("  [error]Usage: /channel [start|stop|restart|logs] <name>[/]")
        return

    name = args[1].lower()
    try:
        if action == "logs":
            console.print(escape(services.logs(name)))
            return
        fn = {"start": services.start, "stop": services.stop, "restart": services.restart}[action]
        with console.status(f"[meta]{action.capitalize()}ing {name}…[/]", spinner="dots"):
            lines = fn(name)
    except services.ChannelServiceError as exc:
        console.print(f"  [error]{escape(str(exc))}[/]")
        return
    for line in lines:
        style = "error" if line.startswith("⚠") else "meta"
        console.print(f"  [{style}]{escape(line)}[/]")
    if action in ("start", "restart") and name in here:
        console.print(f"  [meta]{name} is attached to this session right now; the service "
                      f"takes over when you /remote off {name} or quit.[/]")


def _take_control(name: str) -> None:
    from aria.channels import control
    try:
        control.enable(name)
    except RuntimeError as exc:
        console.print(f"  [error]📱 {exc}[/]")
        return
    console.print(f"  [success]📱 {name} now controls this session — messages from "
                  f"your phone run here, and your replies are mirrored there[/]")


def _run_remote_turn(agent: Agent, turn) -> None:
    """Run a message that arrived from a controlling channel in THIS session:
    render it like a local turn, stream replies back to the phone, and hand
    the replies to the waiting channel thread."""
    from rich.markup import escape
    from aria import context
    console.print(f"\n  [cmd]📱 {turn.channel}[/] [meta]›[/] {escape(turn.text)}")
    token = context.set_active(turn.channel, turn.user_id)
    replies: list[str] = []
    try:
        replies = agent.chat_mirrored(turn.text, response_cb=turn.response_cb,
                                      activity_cb=turn.activity_cb)
    except KeyboardInterrupt:
        console.print("\n  [meta](interrupted)[/]")
        replies = ["(interrupted at the terminal)"]
    except Exception as exc:
        console.print(f"\n  [error]⚠ Unexpected error: {exc}[/]")
        replies = [f"Sorry, something went wrong: {exc}"]
    finally:
        context.reset(token)
        turn.finish(replies)


def _mirror_to_channels(text: str) -> None:
    """Send `text` to every controlling channel (the last user who wrote from
    it, else its allow-list). Background thread: the REPL never waits on it."""
    import threading
    from aria import channels
    from aria.channels import control
    targets = control.controlled()
    if not targets:
        return

    def _send() -> None:
        for name, user in targets.items():
            plugin = channels.get(name)
            if plugin is None or not plugin.supports_push:
                continue
            try:
                plugin.send(text, to=user)
            except Exception:
                pass          # best effort — the terminal is the primary surface

    threading.Thread(target=_send, daemon=True, name="aria-remote-mirror").start()


def _chat_local(agent: Agent, text: str) -> None:
    """A turn typed at the terminal. While a channel controls the session, the
    phone sees it too: the message, then each reply as it's produced."""
    from aria.channels import control
    if not control.controlled():
        agent.chat(text)
        return
    _mirror_to_channels(f"💻 {text}")
    agent.chat_mirrored(text, response_cb=_mirror_to_channels)


def repl(agent: Agent) -> None:
    from aria.channels import control
    session = _make_prompt_session(agent)
    waker = _Waker(session) if session is not None else None
    control.attach_repl(agent, waker.wake if waker is not None else None)
    _print_banner(agent)
    _start_attached_channels()
    try:
        _repl_loop(agent, session, waker)
    finally:
        control.detach_repl()
        _stop_attached_channels()

    # Summarise and save session on exit — always, even after errors
    console.print("  [meta]Saving conversation window...[/]", end=" ")
    try:
        agent.close()
        console.print("[success]done.[/]")
    except Exception:
        console.print("[meta]skipped.[/]")


def _repl_loop(agent: Agent, session, waker: _Waker | None = None) -> None:
    from aria.channels import control
    while True:
        turn = control.take()
        if turn is not None:
            _run_remote_turn(agent, turn)
            continue
        try:
            user = _prompt(session, waker)
        except EOFError:
            console.print("\n  [meta]Bye.[/]")
            break
        except KeyboardInterrupt:
            # Ctrl+C at the prompt cancels the current line, doesn't exit.
            console.print()
            continue

        if user == _WAKE or not user:
            continue

        # `!cmd` → run a shell command directly, no LLM, no tokens.
        if user.startswith("!"):
            _run_shell_escape(user[1:].strip())
            continue

        parts = user.split(maxsplit=1)
        cmd   = parts[0].lower()
        rest  = parts[1] if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit"):
            console.print("  [meta]Bye.[/]")
            break

        elif cmd == "/retry":
            txt = agent.retry_last()
            if not txt:
                console.print("  [meta]Nothing to retry yet.[/]")
            else:
                console.print(f"  [meta]↻ retrying:[/] {txt.splitlines()[0][:80]}")
                try:
                    agent.chat(txt)          # already-expanded text; don't re-expand
                except KeyboardInterrupt:
                    console.print("\n  [meta](interrupted)[/]")

        elif cmd == "/copy":
            last = (agent._last_response or "").strip()
            if not last:
                console.print("  [meta]Nothing to copy yet.[/]")
            elif _copy_to_clipboard(last):
                console.print(f"  [success]Copied last answer[/] [meta]({len(last)} chars).[/]")
            else:
                console.print("  [error]No clipboard tool found[/] "
                              "[meta](install wl-clipboard / xclip / xsel).[/]")

        elif cmd == "/compact":
            result = agent.compact()
            if result.startswith("[compact"):
                console.print(f"  [meta]{result}[/]")
            else:
                console.print("  [success]Context compacted.[/] [meta]Summary:[/]")
                console.print(Panel(result, border_style="dim blue", padding=(0, 1)))

        elif cmd == "/discard":
            console.print("  [meta]Bye. (session not saved)[/]")
            return  # skip the close() block below

        elif cmd == "/help":
            console.print(_HELP_TEXT)

        elif cmd == "/version":
            console.print(f"  [agent]{agent.name}[/] [meta]v{__version__}[/]")

        elif cmd == "/trust":
            from aria.tools import shell_run as _sr
            arg = rest.strip().lower()
            if arg == "clear":
                try:
                    _sr._ALLOWLIST_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                console.print("  [success]Cleared the shell approval list.[/]")
            else:
                allow = _sr._load_allowlist()
                if not allow:
                    console.print("  [meta]No shell commands auto-approved yet. "
                                  "Approve one with 'always' when prompted.[/]")
                else:
                    console.rule("[meta]Auto-approved shell prefixes[/]")
                    for p in allow:
                        console.print(f"  [cmd]{p}[/]")
                    console.print("  [meta]Clear with[/] [cmd]/trust clear[/]")
                    console.rule()

        elif cmd == "/cost":
            tok = agent._session_tokens
            total = tok["in"] + tok["out"]
            console.print(
                f"  [meta]Session tokens —[/] in [cmd]{tok['in']:,}[/]  "
                f"out [cmd]{tok['out']:,}[/]  total [cmd]{total:,}[/]"
            )

        elif cmd == "/usage":
            from aria import usage as _usage
            console.rule("[meta]Lifetime usage[/]")
            console.print(_usage.format_report())
            console.rule()

        elif cmd == "/models":
            console.rule("[meta]Model profiles[/]")
            for prof in agent.list_profiles():
                active = " ← active" if prof["active"] else ""
                console.print(
                    f"  [cmd]{prof['name']:12}[/] [meta]{prof['model']}[/][success]{active}[/]"
                )
            console.rule()

        elif cmd == "/model":
            if not rest:
                # Show current model
                current = next(p for p in agent.list_profiles() if p["active"])
                console.print(f"  [agent]{current['name']}[/] [meta]{current['model']}[/]")
            else:
                result = agent.switch_profile(rest.strip())
                console.print(f"  [success]{result}[/]")

        elif cmd == "/memory":
            console.rule("[meta]Memory[/]")
            console.print(agent.ws.load_memory())
            console.rule()

        elif cmd == "/tools":
            console.rule("[meta]Tools[/]")
            for t in agent.tool_schemas:
                fn = t["function"]
                console.print(
                    f"  [cmd]{fn['name']:16}[/] [meta]{fn['description'][:60]}[/]"
                )
            console.rule()

        elif cmd == "/clear":
            if hasattr(agent, "clear_session"):
                agent.clear_session()   # also resets the persisted window + plan
            else:
                agent.history = list(agent._seed)   # seed is empty; examples live in the system prompt
            console.print("  [success]History cleared.[/]")

        elif cmd == "/save":
            if not rest:
                console.print("  [error]Usage: /save <note>[/]")
            else:
                agent.ws.append_memory(rest)
                console.print("  [success]Saved to memory.[/]")

        elif cmd == "/markdown":
            arg = rest.strip().lower()
            if arg in ("on", "off"):
                agent.markdown_enabled = (arg == "on")
            elif arg:
                console.print("  [error]Usage: /markdown [on|off][/]")
                continue
            else:
                agent.markdown_enabled = not agent.markdown_enabled
            state = "on" if agent.markdown_enabled else "off"
            console.print(f"  [success]Markdown rendering {state}.[/]")

        elif cmd == "/remote":
            _remote_command(rest)

        elif cmd in ("/channel", "/channels"):
            _channel_command(rest)

        elif cmd.startswith("/"):
            console.print(f"  [error]Unknown command: {cmd}[/]  Type /help for commands.")

        else:
            try:
                _chat_local(agent, _expand_mentions(user))
            except KeyboardInterrupt:
                # Ctrl+C mid-response — cancel this turn, keep the session
                console.print("\n  [meta](interrupted)[/]")
            except Exception as exc:
                console.print(f"\n  [error]⚠ Unexpected error: {exc}[/]")
                console.print("  [meta]Session is intact — you can keep chatting.[/]")


def main() -> None:
    _startup()
    Agent = _agent_class()
    parser = argparse.ArgumentParser(
        prog="aria",
        description=f"{__version__} — AI agent",
        add_help=True,
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--notify", "-n",
        action="store_true",
        help="Run single-shot and push the result (ARIA_NOTIFY_CHANNEL, default Telegram)",
    )
    parser.add_argument(
        "--usage", "-u",
        action="store_true",
        help="Print lifetime token usage (by model/channel) and exit",
    )
    parser.add_argument(
        "--chat", "-c",
        default=None,
        metavar="CHAT_ID",
        help="Recipient to notify (Telegram chat ID, WhatsApp number, …)",
    )
    parser.add_argument(
        "--channel",
        default=None,
        metavar="NAME",
        help="Channel to push --notify results through (default: ARIA_NOTIFY_CHANNEL, else Telegram)",
    )
    parser.add_argument(
        "query",
        nargs="*",
        help="Query to run in single-shot mode",
    )
    args   = parser.parse_args()
    query  = " ".join(args.query).strip()

    if args.usage:
        from aria import usage as _usage
        print(_usage.format_report())
        return

    if args.notify:
        if not query:
            parser.error("--notify requires a query")

        from aria import channels
        if args.channel and channels.get(args.channel) is None:
            parser.error(f"unknown channel '{args.channel}' "
                         f"(available: {', '.join(sorted(channels.discover()))})")

        def send(text: str) -> None:
            channels.push(text, to=args.chat, channel=args.channel)

        agent = Agent(window_key="notify", terminal=False)
        try:
            result = agent.chat_collect(query)
            agent.close()
            send(result)
            console.print(f"[success]Sent:[/] {result[:120]}{'...' if len(result) > 120 else ''}")
        except Exception as e:
            error_msg = f"⚠️ {agent.name} task failed: {e}"
            try:
                send(error_msg)
            except Exception:
                pass
            err_console.print(f"[error]{error_msg}[/]")
            sys.exit(1)

    elif query:
        agent = Agent(window_key="cli")
        agent.chat(query)
        agent.close()

    else:
        agent = Agent()        # interactive REPL → default "repl" window
        repl(agent)


if __name__ == "__main__":
    main()
