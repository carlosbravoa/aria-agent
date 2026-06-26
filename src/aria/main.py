"""
aria/main.py — CLI entry point.

Usage:
  aria                          # interactive REPL
  aria "query"                  # single-shot, prints to stdout
  aria --notify "query"         # single-shot, sends result to Telegram
  aria --notify --chat 123 "q"  # single-shot, sends to a specific chat ID
  aria --version                # print version and exit
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# ── First-run check (before anything else) ───────────────────────────────────
from aria.setup import is_first_run, run as _setup_run
if is_first_run():
    _setup_run()

# ── Normal startup ────────────────────────────────────────────────────────────
from aria import config, __version__
config.load()

from aria.agent import Agent  # noqa: E402
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme
from rich import print as rprint

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

console = Console(theme=_THEME, highlight=False)


# ── Input: prompt_toolkit session ─────────────────────────────────────────────

_COMMANDS = [
    "/help", "/memory", "/tools", "/clear", "/compact", "/retry", "/copy",
    "/save ", "/markdown ", "/version", "/cost", "/trust", "/models", "/model ",
    "/discard", "/quit", "/exit",
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
[cmd]/trust[/] [meta][clear][/]  Show/clear auto-approved shell commands
[cmd]/version[/]     Show version
[cmd]/quit[/]        Exit  [meta](or Ctrl+D)[/]

[meta]Tips: [cmd]!cmd[/] runs a shell command · [cmd]@path/to/file[/] attaches a file · [cmd]Esc[/]/[cmd]Ctrl+C[/] interrupts a reply (keeps context) · [cmd]Alt+Enter[/] newline[/]
"""


_MENTION_RE = re.compile(r"(?<![\w@])@([^\s@]+)")
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


def _prompt(session) -> str:
    """
    Read a line of input. Uses the prompt_toolkit session when available
    (history, autosuggest, completion, multi-line); falls back to a plain
    coloured input() prompt when prompt_toolkit isn't installed.

    Raises EOFError on Ctrl+D and KeyboardInterrupt on Ctrl+C, which the REPL
    loop treats as "exit" and "cancel line" respectively.
    """
    if session is not None:
        return session.prompt([("class:prompt", "  You › ")]).strip()

    # Fallback: ANSI-coloured input(); \001..\002 mark non-printing width.
    CYAN_BOLD = "\001\033[1;36m\002"
    RESET     = "\001\033[0m\002"
    return input(f"  {CYAN_BOLD}You ›{RESET} ").strip()


def repl(agent: Agent) -> None:
    session = _make_prompt_session(agent)
    _print_banner(agent)

    while True:
        try:
            user = _prompt(session)
        except EOFError:
            console.print("\n  [meta]Bye.[/]")
            break
        except KeyboardInterrupt:
            # Ctrl+C at the prompt cancels the current line, doesn't exit.
            console.print()
            continue

        if not user:
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

        elif cmd == "/models":
            console.rule("[meta]Model profiles[/]")
            for p in agent.list_profiles():
                active = " ← active" if p["active"] else ""
                console.print(
                    f"  [cmd]{p['name']:12}[/] [meta]{p['model']}[/][success]{active}[/]"
                )
            console.rule()

        elif cmd == "/model":
            if not rest:
                # Show current model
                active = next(p for p in agent.list_profiles() if p["active"])
                console.print(f"  [agent]{active['name']}[/] [meta]{active['model']}[/]")
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

        elif cmd.startswith("/"):
            console.print(f"  [error]Unknown command: {cmd}[/]  Type /help for commands.")

        else:
            try:
                agent.chat(_expand_mentions(user))
            except KeyboardInterrupt:
                # Ctrl+C mid-response — cancel this turn, keep the session
                console.print("\n  [meta](interrupted)[/]")
            except Exception as exc:
                console.print(f"\n  [error]⚠ Unexpected error: {exc}[/]")
                console.print("  [meta]Session is intact — you can keep chatting.[/]")

    # Summarise and save session on exit — always, even after errors
    console.print("  [meta]Saving conversation window...[/]", end=" ")
    try:
        agent.close()
        console.print("[success]done.[/]")
    except Exception:
        console.print("[meta]skipped.[/]")


def main() -> None:
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
        help="Run single-shot and send result to Telegram",
    )
    parser.add_argument(
        "--chat", "-c",
        type=int,
        default=None,
        metavar="CHAT_ID",
        help="Telegram chat ID to notify",
    )
    parser.add_argument(
        "query",
        nargs="*",
        help="Query to run in single-shot mode",
    )
    args   = parser.parse_args()
    query  = " ".join(args.query).strip()

    if args.notify:
        if not query:
            parser.error("--notify requires a query")

        from aria.telegram_notify import send
        agent = Agent(window_key="notify")
        try:
            result = agent.chat_collect(query)
            agent.close()
            send(result, chat_id=args.chat)
            console.print(f"[success]Sent:[/] {result[:120]}{'...' if len(result) > 120 else ''}")
        except Exception as e:
            error_msg = f"⚠️ {agent.name} task failed: {e}"
            try:
                send(error_msg, chat_id=args.chat)
            except Exception:
                pass
            console.print(f"[error]{error_msg}[/]", file=sys.stderr)
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
