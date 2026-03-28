"""
aria/main.py — CLI entry point.

Usage:
  aria                          # interactive REPL
  aria "query"                  # single-shot, prints to stdout
  aria --notify "query"         # single-shot, sends result to Telegram
  aria --notify --chat 123 "q"  # single-shot, sends to a specific chat ID
"""

from __future__ import annotations

import argparse
import sys

# ── First-run check (before anything else) ───────────────────────────────────
from aria.setup import is_first_run, run as _setup_run
if is_first_run():
    _setup_run()  # prints instructions and exits

# ── Normal startup ────────────────────────────────────────────────────────────
from aria import config
config.load()

from aria.agent import Agent  # noqa: E402


_HELP = """
Commands:
  /memory      Print loaded memory
  /tools       List available tools
  /clear       Clear conversation history (keeps memory)
  /save <note> Append a note to core memory
  /quit        Exit
"""


def repl(agent: Agent) -> None:
    print(f"\n✦ {agent.name} ready  (workspace: {agent.ws.root})\n"
          "  Type /help for commands.\n")
    while True:
        try:
            user = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user:
            continue

        match user.split(maxsplit=1):
            case ["/quit" | "/exit"]:
                print("Bye.")
                break
            case ["/help"]:
                print(_HELP)
            case ["/memory"]:
                print(agent.ws.load_memory())
            case ["/tools"]:
                for t in agent.tool_schemas:
                    fn = t["function"]
                    print(f"  • {fn['name']} — {fn['description'][:72]}")
            case ["/clear"]:
                agent.history = agent._few_shot_examples()
                print("History cleared.")
            case ["/save", note]:
                agent.ws.append_memory(note)
                print("Saved to memory.")
            case _:
                agent.chat(user)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="aria",
        description="Aria agent — interactive or single-shot",
        add_help=True,
    )
    parser.add_argument(
        "--notify", "-n",
        action="store_true",
        help="Run single-shot and send result to Telegram instead of stdout",
    )
    parser.add_argument(
        "--chat", "-c",
        type=int,
        default=None,
        metavar="CHAT_ID",
        help="Telegram chat ID to notify (default: all TELEGRAM_ALLOWED ids)",
    )
    parser.add_argument(
        "query",
        nargs="*",
        help="Query to run in single-shot mode",
    )
    args = parser.parse_args()
    query = " ".join(args.query).strip()

    if args.notify:
        # ── Notify mode: run query, push result to Telegram ──────────────
        if not query:
            parser.error("--notify requires a query, e.g.: aria --notify 'summarise my emails'")

        from aria.telegram_notify import send

        agent = Agent()
        try:
            result = agent.chat_collect(query)
            # Strip the "Aria: " prefix chat_collect includes
            prefix = f"\n{agent.name}: "
            if result.startswith(prefix.strip()):
                result = result[len(prefix.strip()):].strip()
            send(result, chat_id=args.chat)
            print(f"Sent to Telegram: {result[:120]}{'...' if len(result) > 120 else ''}")
        except Exception as e:
            error_msg = f"⚠️ Aria task failed: {e}"
            try:
                send(error_msg, chat_id=args.chat)
            except Exception:
                pass
            print(error_msg, file=sys.stderr)
            sys.exit(1)

    elif query:
        # ── Single-shot mode: print to stdout ────────────────────────────
        agent = Agent()
        agent.chat(query)

    else:
        # ── Interactive REPL ──────────────────────────────────────────────
        agent = Agent()
        repl(agent)


if __name__ == "__main__":
    main()
