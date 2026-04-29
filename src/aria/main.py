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


# ── Readline setup ────────────────────────────────────────────────────────────

def _setup_readline() -> None:
    """
    Enable arrow-key navigation, history, and line editing in the REPL.
    Uses the built-in readline module (Linux/macOS). No-ops gracefully on
    Windows where readline is not available.
    """
    try:
        import readline
        import atexit
        from pathlib import Path

        history_file = Path.home() / ".aria" / ".repl_history"
        history_file.parent.mkdir(parents=True, exist_ok=True)

        # Load previous history
        try:
            readline.read_history_file(str(history_file))
        except FileNotFoundError:
            pass

        readline.set_history_length(500)

        # Save history on exit
        atexit.register(readline.write_history_file, str(history_file))

        # Tab completion for slash commands
        _commands = [
            "/help", "/memory", "/tools", "/clear",
            "/save ", "/version", "/quit", "/exit",
        ]

        def _completer(text: str, state: int) -> str | None:
            matches = [c for c in _commands if c.startswith(text)]
            return matches[state] if state < len(matches) else None

        readline.set_completer(_completer)
        readline.parse_and_bind("tab: complete")

    except ImportError:
        pass  # Windows — degrade gracefully


# ── REPL ──────────────────────────────────────────────────────────────────────

_HELP_TEXT = """
[cmd]/memory[/]      Print current memory
[cmd]/tools[/]       List available tools
[cmd]/clear[/]       Clear conversation history
[cmd]/save[/] [meta]<note>[/]  Append a note to memory
[cmd]/version[/]     Show version
[cmd]/quit[/]        Exit  [meta](or Ctrl+D)[/]
"""


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


def _prompt(agent: Agent) -> str:
    """Read a line with readline support (arrow keys, history, tab completion)."""
    try:
        return input("  You › ").strip()
    except EOFError:
        raise


def repl(agent: Agent) -> None:
    _setup_readline()
    _print_banner(agent)

    while True:
        try:
            user = _prompt(agent)
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [meta]Bye.[/]")
            break

        if not user:
            continue

        parts = user.split(maxsplit=1)
        cmd   = parts[0].lower()
        rest  = parts[1] if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit"):
            console.print("  [meta]Bye.[/]")
            break

        elif cmd == "/help":
            console.print(_HELP_TEXT)

        elif cmd == "/version":
            console.print(f"  [agent]{agent.name}[/] [meta]v{__version__}[/]")

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
            agent.history = agent._few_shot_examples()
            console.print("  [success]History cleared.[/]")

        elif cmd == "/save":
            if not rest:
                console.print("  [error]Usage: /save <note>[/]")
            else:
                agent.ws.append_memory(rest)
                console.print("  [success]Saved to memory.[/]")

        elif cmd.startswith("/"):
            console.print(f"  [error]Unknown command: {cmd}[/]  Type /help for commands.")

        else:
            agent.chat(user)

    # Summarise and save session on exit
    console.print("  [meta]Saving session summary...[/]", end=" ")
    agent.close()
    console.print("[success]done.[/]")


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
        agent = Agent()
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
        agent = Agent()
        agent.chat(query)
        agent.close()

    else:
        agent = Agent()
        repl(agent)


if __name__ == "__main__":
    main()
