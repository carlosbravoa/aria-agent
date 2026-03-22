"""
aria/main.py — CLI entry point.

Usage:
  aria                        # interactive REPL
  aria "how do I list files"  # single-shot, then exit
"""

from __future__ import annotations

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
    agent = Agent()
    if len(sys.argv) > 1:
        agent.chat(" ".join(sys.argv[1:]))
    else:
        repl(agent)


if __name__ == "__main__":
    main()
