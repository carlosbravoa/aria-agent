"""
aria/channels/commands.py — Slash commands shared by every channel.

A channel passes any message starting with "/" to host.run_command(); a known
command returns its reply text (light Markdown: **bold**, `code`, _italic_ —
Telegram converts it to HTML, WhatsApp shows it as-is), anything else returns
None so the channel can treat it as a normal message. Custom channel plugins
get the whole set for free.

  /help /start     what the bot is and the command list
  /stop            stop the running turn after its current step
  /clear           forget this conversation (window + plan)
  /memory          show long-term memory
  /tools           list tools
  /models          list model profiles      /model <name>  switch profile
  /save <note>     append a note to memory
  /version         version
"""

from __future__ import annotations

NAMES = frozenset({"help", "start", "stop", "clear", "memory", "tools", "models",
                   "model", "save", "version"})
# Commands that never wait behind a running turn (they don't touch history).
INSTANT = frozenset({"stop", "help", "start", "version", "memory", "tools", "models"})

HELP = ("/stop · /clear · /memory · /tools · /models · /model <name> · "
        "/save <note> · /version · /help")


def parse(text: str) -> tuple[str, str] | None:
    """("name", "args") for a slash command, else None. Accepts Telegram's
    /cmd@BotName form."""
    text = (text or "").strip()
    if not text.startswith("/") or len(text) < 2:
        return None
    head, _, rest = text[1:].partition(" ")
    name = head.split("@", 1)[0].lower()
    return (name, rest.strip()) if name.replace("_", "").isalnum() else None


def run(agent, text: str) -> str | None:
    """Execute a shared command against `agent`; None if it isn't one."""
    parsed = parse(text)
    if parsed is None:
        return None
    name, args = parsed
    from aria import __version__

    if name in ("help", "start"):
        return f"👋 I'm **{agent.name}** v{__version__}.\nCommands: {HELP}"
    if name == "stop":
        return ("⏹ Stopping after the current step…" if agent.request_stop()
                else "Nothing is running.")
    # Commands that change the conversation or its model must not race a
    # running turn (it is appending to the history right now).
    mutating = name == "clear" or (name == "model" and bool(args))
    if mutating and getattr(agent, "_busy", False):
        return ("A reply is still running — send /stop first, or try again "
                "when it's done.")
    if name == "clear":
        agent.clear_session()
        return "History cleared."
    if name == "memory":
        return agent.ws.load_memory() or "_Nothing stored yet._"
    if name == "tools":
        lines = [f"• **{t['function']['name']}** — {t['function']['description'][:60]}"
                 for t in agent.tool_schemas]
        return "\n".join(lines) or "No tools loaded."
    if name == "models" or (name == "model" and not args):
        return "\n".join(f"`{p['name']}` {p['model']}{' ✓' if p['active'] else ''}"
                         for p in agent.list_profiles())
    if name == "model":
        return agent.switch_profile(args.split()[0])
    if name == "save":
        if not args:
            return "Usage: /save <note>"
        agent.ws.append_memory(args)
        return f"Saved: {args}"
    if name == "version":
        return f"{agent.name} v{__version__}"
    return None
