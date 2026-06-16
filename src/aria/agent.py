"""
aria/agent.py — ReAct-style agentic loop for any local LLM.

Tools are invoked via a plain-text protocol:

    TOOL: tool_name
    INPUT: {"arg": "value"}

Memory is saved via:

    REMEMBER: some fact

Session continuity: agent.close() summarises the conversation and saves it
to workspace/memory/last_session.md. Next session, that summary is loaded
into the system prompt for lightweight continuity without replaying history.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

from aria import tools
from aria.workspace import Workspace

_TOOL_RE = re.compile(
    r"TOOL:\s*(?P<tool_name>\w+)\s*\nINPUT:\s*(?P<args>\{.*?\})",
    re.DOTALL,
)
_REMEMBER_RE = re.compile(r"REMEMBER:\s*(?P<note>[^\n]+)")
_LEARN_RE    = re.compile(r"LEARN:\s*(?P<note>[^\n]+)")

# Persists the last-used model profile across sessions
_PROFILE_STATE = Path.home() / ".aria" / ".last_profile"

# Markdown detection — patterns that only appear in intentional markdown
_MD_PATTERNS = re.compile(
    r"(\*\*[^*]+\*\*)"          # **bold**
    r"|(__[^_]+__)"              # __bold__
    r"|(\*[^*\n]+\*)"           # *italic*
    r"|(^#{1,6}\s)",             # # heading
    re.MULTILINE
)
# Code blocks checked separately since they span lines
_MD_CODE = re.compile(r"```|`[^`]+`")
# List items — only if there are multiple (single - could be a dash)
_MD_LIST = re.compile(r"^(\s*[-*]\s.+\n){2,}", re.MULTILINE)
# GFM table — match the delimiter row (|---|:--:|), the unambiguous signature.
# A line made only of pipes/dashes/colons/spaces with at least one '|' and '-'.
_MD_TABLE = re.compile(r"^\s*\|?[ :|-]*-[ :|-]*\|[ :|-]*$", re.MULTILINE)


def _has_markdown(text: str) -> bool:
    """Return True if text contains intentional markdown syntax worth rendering."""
    clean = re.sub(r"REMEMBER:[^\n]*\n?", "", text)
    clean = re.sub(r"LEARN:[^\n]*\n?",    "", clean)
    return bool(
        _MD_PATTERNS.search(clean)
        or _MD_CODE.search(clean)
        or _MD_LIST.search(clean)
        or _MD_TABLE.search(clean)
    )


# Heading styles for streamed Markdown. rich centers headings and draws
# decorative rules by default; we want plain left-aligned coloured text so the
# REPL reads like a chat, not a document.
def _md_theme():
    from rich.theme import Theme
    return Theme({
        "markdown.h1":        "bold green",
        "markdown.h1.border": "none",
        "markdown.h2":        "bold cyan",
        "markdown.h2.border": "none",
        "markdown.h3":        "bold",
        "markdown.h4":        "bold dim",
        "markdown.h5":        "dim",
        "markdown.h6":        "dim",
    })


_CHAT_MD_CLASS = None


def _chat_markdown(body: str):
    """
    Markdown renderable with left-aligned headings. rich centers headings by
    default (LEVEL_ALIGN), which looks like a printed document; a chat REPL
    wants them flush-left. The subclass is built once and cached.
    """
    global _CHAT_MD_CLASS
    if _CHAT_MD_CLASS is None:
        from rich.markdown import Markdown, Heading

        class _LeftHeading(Heading):
            def __rich_console__(self, console, options):
                text = self.text.copy()
                text.justify = "left"
                yield text

        class _ChatMarkdown(Markdown):
            elements = {**Markdown.elements, "heading_open": _LeftHeading}

        _CHAT_MD_CLASS = _ChatMarkdown
    return _CHAT_MD_CLASS(body)
# Both configurable via ~/.aria/.env
_MAX_LOOPS         = int(os.environ.get("ARIA_MAX_LOOPS",         "20"))
_BROWSER_MAX_LOOPS = int(os.environ.get("ARIA_BROWSER_MAX_LOOPS", "50"))
_MAX_HISTORY       = int(os.environ.get("ARIA_MAX_HISTORY",       "60"))


class Agent:
    def __init__(self, output_callback=None, window_key: str | None = None) -> None:
        # Default output: plain print for streaming tokens.
        # Rich is used for status lines (tool calls, memory saves) via console.print.
        self._output = output_callback or (lambda t: print(t, end="", flush=True))
        self._is_terminal = output_callback is None
        self.client = OpenAI(
            base_url=os.environ["LLM_BASE_URL"],
            api_key=os.environ.get("LLM_API_KEY", "local"),
        )
        self.model: str = os.environ.get("LLM_MODEL", "llama3.2")
        self.name: str  = os.environ.get("AGENT_NAME", "Agent")
        # Terminal Markdown rendering. Default from ARIA_REPL_MARKDOWN (on);
        # toggled at runtime via the REPL `/markdown` command. When off, terminal
        # responses stream as plain wrapped text (raw markdown shown literally).
        self.markdown_enabled = os.environ.get(
            "ARIA_REPL_MARKDOWN", "on"
        ).strip().lower() not in ("off", "0", "false", "no")

        from aria import config
        self.ws = Workspace(config.workspace_dir())
        # Select this agent's per-channel conversation window. REPL/single-shot
        # pass nothing (defaults to "repl"); channels pass "<channel>:<user_id>";
        # the supervisor passes "supervisor" so background tasks never inherit a
        # user's chat context.
        self.window_key = window_key or "repl"
        self.ws.set_window_key(self.window_key)
        self.tool_schemas = tools.load_all(config.tools_dir())
        self.ws.update_tools_registry(self.tool_schemas)

        self.system_prompt = self._build_system_prompt()
        # Protocol examples live in the system prompt (_protocol_examples_block),
        # NOT in history: seeding them as real turns made the model recall them
        # as the user's messages and adopt their placeholders ("Hello, <name>!").
        # Seed stays empty so history holds only genuine conversation.
        self._seed: list[dict[str, Any]] = []
        # Resume the prior conversation as real history turns so a restarted
        # REPL/Telegram session continues with genuine immediate context.
        prior = self.ws.load_conversation_window_messages()
        self.history: list[dict[str, Any]] = list(self._seed) + prior
        self.session_log    = self.ws.new_session_path()
        self._last_response   = ""  # last clean text response
        self._active_profile  = "default"
        self._responses:    list[str] = []  # all clean text responses this turn

        # Restore last used profile (persisted across sessions)
        if _PROFILE_STATE.exists():
            try:
                saved = _PROFILE_STATE.read_text().strip()
                if saved and saved != "default":
                    self.switch_profile(saved)
            except Exception:
                pass

        # Background reflection — runs once per day for REPL users who don't
        # have the supervisor running. Fires silently in a daemon thread so it
        # never blocks the conversation. If the supervisor is running it will
        # have already advanced the watermark, so this exits instantly.
        self._maybe_reflect_background()

    def _maybe_reflect_background(self) -> None:
        """
        Fire a background reflection pass if it's been long enough since the
        last one. Uses a daemon thread so it never blocks the conversation and
        dies cleanly if the process exits.

        Only runs if ARIA_REFLECT_EVERY > 0. Works alongside the supervisor —
        if the supervisor already advanced the watermark, reflect.run() finds
        no new sessions and returns immediately (idempotent, negligible cost).
        """
        reflect_every = int(os.environ.get("ARIA_REFLECT_EVERY", "86400"))
        if reflect_every <= 0:
            return

        # Check time since last reflection via watermark file mtime
        watermark = self.ws.root / "memory" / "reflect_watermark"
        if watermark.exists():
            import time
            age = time.time() - watermark.stat().st_mtime
            if age < reflect_every:
                return  # Not due yet

        import threading

        def _run() -> None:
            try:
                from aria import reflect as _reflect
                _reflect.run(notify=False)
            except Exception:
                pass  # Never surface errors from background reflection

        t = threading.Thread(target=_run, daemon=True, name="aria-reflect-bg")
        t.start()

    # ── Model profiles ───────────────────────────────────────────────────────

    def list_profiles(self) -> list[dict]:
        """
        Return all configured model profiles.
        Scans LLM_PROFILE1_MODEL through LLM_PROFILE9_MODEL in .env.
        Each profile inherits LLM_BASE_URL and LLM_API_KEY if not overridden.
        The default profile always appears first.
        """
        profiles = [{
            "key":     "default",
            "name":    "default",
            "model":   os.environ.get("LLM_MODEL", "llama3.2"),
            "base_url": os.environ.get("LLM_BASE_URL", ""),
            "active":  self._active_profile == "default",
        }]
        for i in range(1, 10):
            model = os.environ.get(f"LLM_PROFILE{i}_MODEL", "")
            if not model:
                continue
            name = os.environ.get(f"LLM_PROFILE{i}_NAME", f"profile{i}").lower().strip()
            profiles.append({
                "key":     name,
                "name":    name,
                "model":   model,
                "base_url": os.environ.get(f"LLM_PROFILE{i}_BASE_URL",
                                            os.environ.get("LLM_BASE_URL", "")),
                "active":  self._active_profile == name,
            })
        return profiles

    def switch_profile(self, name: str) -> str:
        """
        Switch to a named model profile. Rebuilds the OpenAI client and
        updates self.model. History and memory are unaffected.
        Returns a confirmation string.
        """
        name = name.strip().lower()

        if name == "default":
            self.client = OpenAI(
                base_url=os.environ["LLM_BASE_URL"],
                api_key=os.environ.get("LLM_API_KEY", "local"),
            )
            self.model = os.environ.get("LLM_MODEL", "llama3.2")
            self._active_profile = "default"
            try:
                _PROFILE_STATE.write_text("default")
            except Exception:
                pass
            return f"Switched to default ({self.model})"

        for i in range(1, 10):
            model = os.environ.get(f"LLM_PROFILE{i}_MODEL", "")
            if not model:
                continue
            profile_name = os.environ.get(f"LLM_PROFILE{i}_NAME",
                                           f"profile{i}").lower().strip()
            if profile_name == name:
                base_url = os.environ.get(f"LLM_PROFILE{i}_BASE_URL",
                                           os.environ.get("LLM_BASE_URL", ""))
                api_key  = os.environ.get(f"LLM_PROFILE{i}_API_KEY",
                                           os.environ.get("LLM_API_KEY", "local"))
                self.client = OpenAI(base_url=base_url, api_key=api_key)
                self.model  = model
                self._active_profile = name
                try:
                    _PROFILE_STATE.parent.mkdir(parents=True, exist_ok=True)
                    _PROFILE_STATE.write_text(name)
                except Exception:
                    pass
                return f"Switched to {name} ({model})"

        available = [p["name"] for p in self.list_profiles()]
        return f"Profile '{name}' not found. Available: {', '.join(available)}"

    # ── System prompt ────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        soul         = self.ws.load_soul()
        memory       = self.ws.load_memory()
        tool_docs    = self._build_tool_docs()
        # The recent conversation is now resumed as real history turns in
        # __init__ (load_conversation_window_messages), so it is intentionally
        # NOT injected here — duplicating it caused the model to read back the
        # memory block instead of the live turns.
        onboard_block = (
            "## First Contact\n"
            "Core memory is empty — you have not met this user yet. In your FIRST "
            "reply this session, briefly introduce yourself and ask for the basics "
            "you need to be useful: their name (at minimum), and if it feels "
            "natural their timezone and preferred language. Ask in one short, "
            "friendly sentence — don't interrogate. The moment they tell you, save "
            "it with REMEMBER:. Once you know their name, don't ask again.\n\n"
            if self.ws.core_is_empty() else "")
        notify_feed  = self.ws.load_notify_feed()
        notify_block = (f"## Recent Proactive Messages\n{notify_feed}\n\n"
                        if notify_feed else "")
        ops_mem      = self.ws.load_operational_memory()
        ops_block    = (
            "## Operational Memory (suggestions from past sessions)\n"
            "These are procedures and shortcuts learned from experience. "
            "Use them as a starting point but verify if results seem wrong — "
            "they may be outdated. If you find a better approach, update with LEARN:\n\n"
            f"{ops_mem}\n\n"
            if ops_mem else "")

        return (
            f"{soul}\n\n"
            "## Core Memory\n"
            f"{memory}\n\n"
            f"{onboard_block}"            f"{ops_block}"            f"{notify_block}"
            "## Tool Protocol\n"
            "To call a tool, output EXACTLY these two lines with no other text before them:\n\n"
            "TOOL: <tool_name>\n"
            'INPUT: {"key": "value"}\n\n'
            "The system runs it and replies:\n\n"
            "RESULT: <o>\n\n"
            "After RESULT, write your final answer in plain text.\n\n"
            f"{tool_docs}\n\n"
            "## Memory System\n"
            "You have two memory files that tailor you to this user. Use them proactively.\n\n"
            "REMEMBER: <fact>\n"
            "  → Core memory. Permanent facts about the user: name, role, timezone, language,\n"
            "    preferences, recurring contacts. Use when you learn something always true.\n\n"
            "LEARN: <procedure>\n"
            "  → Operational memory. How to be useful in this user's specific context:\n"
            "    which accounts/tools to use for tasks, Jira project keys, calendar IDs,\n"
            "    recurring task patterns, shortcuts discovered during tool use.\n"
            "    The more you LEARN, the less you have to figure out from scratch each session.\n\n"
            "Both markers can appear anywhere in your response. Write to them often.\n\n"
            f"{self._protocol_examples_block()}\n\n"
            "## Rules\n"
            "- Use TOOL:/INPUT: for tool calls. No other syntax works.\n"
            "- Use REMEMBER: to save user facts. Use LEARN: to save operational knowledge and "
            "keep anything you would need to improve future session interactions.\n"
            "- Never narrate before a tool call. Emit TOOL: immediately.\n"
            "- After RESULT, answer in plain text.\n"
            "- You know your available tools from the list above — never call a tool to look them up.\n"
            "- File authorization flow: if file_access returns an authorization request,\n"
            "  ask the user naturally (e.g. 'I need read access to /path — shall I grant that?').\n"
            "  When the user agrees, call file_access with action=authorize, path, and level\n"
            "  (read or write — infer from context or user's words).\n"
            "  Then retry the original operation automatically. Never self-authorize.\n"
            "- Be concise.\n"
        )

    def _build_tool_docs(self) -> str:
        """Build tool docs dynamically — never hardcodes tool names."""
        if not self.tool_schemas:
            return "_No tools available._"
        lines = ["### Available Tools\n"]
        for t in self.tool_schemas:
            fn       = t["function"]
            props    = fn.get("parameters", {}).get("properties", {})
            required = fn.get("parameters", {}).get("required", [])
            args     = ", ".join(f"{k}{'*' if k in required else '?'}" for k in props)
            lines.append(f"#### `{fn['name']}`({args})")
            lines.append(fn["description"] + "\n")
        lines.append("_* required, ? optional_")
        return "\n".join(lines)

    def _protocol_examples_block(self) -> str:
        """
        Render the protocol examples as a labelled transcript for the system
        prompt. They teach the TOOL/INPUT/RESULT/REMEMBER/LEARN format WITHOUT
        living in self.history — where the model mistakes them for real
        conversation, reads them back ("your last messages were… My name is
        <name>"), and adopts their placeholders ("Hello, <name>!"). Fenced and
        explicitly marked as not-real so the model never quotes them.
        """
        labels = {"user": "User", "assistant": self.name}
        lines = [
            "## Protocol Examples",
            "The transcript below is ILLUSTRATIVE ONLY — it demonstrates the exact "
            "TOOL/INPUT/RESULT/REMEMBER/LEARN format. It is NOT part of your "
            "conversation with the user. Never quote it, never treat its names, "
            "URLs, or tasks as real, and never list it when asked about earlier "
            "messages.",
            "",
            "```",
        ]
        lines += [f"{labels[m['role']]}: {m['content']}" for m in self._few_shot_examples()]
        lines.append("```")
        return "\n".join(lines)

    def _few_shot_examples(self) -> list[dict]:
        """
        Protocol examples — tool-agnostic except for the file_access demo
        which is needed to show the TOOL:/INPUT: format concretely.
        Rendered into the system prompt by _protocol_examples_block(); these are
        NOT seeded into self.history (that caused few-shot leakage into recall).
        """
        return [
            {"role": "user", "content": "List the files in /tmp"},
            {"role": "assistant", "content": 'TOOL: file_access\nINPUT: {"action": "list", "path": "/tmp"}'},
            {"role": "user", "content": "RESULT: notes.txt\nreport.pdf"},
            {"role": "assistant", "content": "/tmp contains: notes.txt, report.pdf."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4."},
            {"role": "user",      "content": "My name is <name>."},
            {"role": "assistant", "content": "REMEMBER: User's name is <name>.\nNice to meet you, <name>!"},
            {"role": "user",      "content": "My project tracker is at <url>."},
            {"role": "assistant", "content": "LEARN: User's project tracker is at <url>.\nGot it, I'll use that for your projects."},
            {"role": "user",      "content": "scheduled task: summarise top stories from <news-url>"},
            {"role": "assistant", "content": 'TOOL: web_fetch\nINPUT: {"url": "<news-url>", "max_chars": 2000}'},
            {"role": "user",      "content": "RESULT: 1. Story A\n2. Story B\n3. Story C"},
            {"role": "assistant", "content": 'TOOL: notify\nINPUT: {"message": "Top stories:\\n1. Story A\\n2. Story B\\n3. Story C"}'},
            {"role": "user",      "content": "RESULT: [notify] Message sent."},
            {"role": "assistant", "content": "Done. Summary sent."},
        ]

    # ── Public interface ─────────────────────────────────────────────────────

    def chat(self, user_input: str) -> None:
        """Send a message; output goes to self._output callback."""
        self.history.append({"role": "user", "content": user_input})
        self.ws.log_session(self.session_log, "user", user_input)
        self.ws.append_conversation_window("user", user_input, self.name)
        self._trim_history()
        self._run_loop()

    def chat_collect(self, user_input: str) -> str:
        """
        Run a chat turn and return all clean text responses joined.
        Used by supervisor tasks where a single string result is needed.
        For Telegram/WhatsApp use chat_yield() instead.
        """
        buf: list[str] = []
        orig = self._output
        self._output = buf.append
        orig_is_terminal  = self._is_terminal
        self._is_terminal = False
        self._last_response = ""
        self._responses     = []
        try:
            self.chat(user_input)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "chat_collect exception: %s", exc, exc_info=True
            )
            return f"[{self.name}] Error: {exc}"
        finally:
            self._output      = orig
            self._is_terminal = orig_is_terminal

        if self._responses:
            return "\n\n".join(self._responses)
        return self._last_response or f"[{self.name}] No response generated."

    def chat_yield(self, user_input: str) -> list[str]:
        """
        Run a chat turn and return all clean text responses in order.
        Each entry should be sent as a separate message — no joining.
        Used by Telegram/WhatsApp so each response arrives immediately
        as the agent produces it, with natural timing between messages.
        """
        buf: list[str] = []
        orig = self._output
        self._output = buf.append
        orig_is_terminal  = self._is_terminal
        self._is_terminal = False
        self._last_response = ""
        self._responses     = []
        try:
            self.chat(user_input)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error(
                "chat_yield exception: %s", exc, exc_info=True
            )
            return [f"[{self.name}] Error: {exc}"]
        finally:
            self._output      = orig
            self._is_terminal = orig_is_terminal

        return self._responses or [self._last_response] or [f"[{self.name}] No response generated."]

    def _trim_history(self) -> None:
        """Compress old RESULT blocks and drop oldest turns to stay within limits."""
        seed_len = len(self._seed)
        real = self.history[seed_len:]

        last_asst_idx = None
        for i in range(len(real) - 1, -1, -1):
            if real[i]["role"] == "assistant":
                last_asst_idx = i
                break

        for i, msg in enumerate(real):
            if msg["role"] == "user" and msg["content"].startswith("RESULT:"):
                if i == last_asst_idx:
                    continue
                if len(msg["content"]) > 400:
                    real[i] = {**msg, "content": "RESULT: [output truncated — already processed]"}
                continue
            if msg["role"] != "assistant" or i == last_asst_idx:
                continue

        excess = len(real) - _MAX_HISTORY
        if excess > 0:
            real = real[excess:]

        self.history = self._seed + real

    def _classify_side_effect_tools(self) -> set[str]:
        """
        Classify loaded tools as side-effect or data tools based on their
        descriptions. No LLM call, no user metadata — the agent decides.

        Side-effect tools: send, notify, deliver, push, post, publish,
                           schedule, queue, remind, alert, message, email (send).
        Data tools: everything else — read, search, fetch, list, get, create
                    (files/issues/events), analyse.

        This runs once per _run_loop call and is O(n tools) — negligible cost.
        Custom tools in ~/.aria/tools/ are classified automatically by the same
        rules — no configuration needed from the tool author.
        """
        # Keywords that indicate a tool delivers output externally or schedules work.
        # Checked against the tool name and first sentence of its description.
        _SIDE_EFFECT_KEYWORDS = {
            "send", "notify", "deliver", "push", "post", "publish",
            "schedule", "queue", "remind", "alert", "dispatch", "broadcast",
        }

        side_effects: set[str] = set()
        for t in self.tool_schemas:
            fn   = t["function"]
            name = fn["name"].lower()
            desc = fn.get("description", "").lower()
            # Take only the first sentence of the description to avoid
            # false positives (e.g. "search and send results" is a data tool)
            first_sentence = desc.split(".")[0]

            if any(kw in name or kw in first_sentence for kw in _SIDE_EFFECT_KEYWORDS):
                side_effects.add(fn["name"])

        return side_effects

    # ── ReAct loop ───────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """
        ReAct loop. Collects all clean text responses into self._responses.
        Tool call syntax and RESULT: blocks are never included.
        Callers receive every piece of text the agent produced for the user.
        """
        seen_calls: list[str] = []
        # Use a higher loop limit when browser tool is involved — browser tasks
        # require many sequential steps (navigate, snapshot, click, type...).
        browser_task = any(
            "browser" in (msg.get("content") or "")
            for msg in self.history[-3:]
            if msg["role"] == "user"
        )
        loop_limit = _BROWSER_MAX_LOOPS if browser_task else _MAX_LOOPS

        for _ in range(loop_limit):
            response = self._stream_response()

            # Network/API errors return an [error] sentinel — surface and stop
            if response.startswith("[error]"):
                self._responses.append(response)
                self._last_response = response
                return

            for m in _REMEMBER_RE.finditer(response):
                note = m.group("note").strip()
                if note:
                    self.ws.append_memory(f"- {note}")
                    if self._is_terminal:
                        from rich.console import Console
                        Console().print(f"  [dim]💾 remembered: {note}[/dim]")
                    else:
                        self._output(f"  💾 remembered: {note}\n")

            for m in _LEARN_RE.finditer(response):
                note = m.group("note").strip()
                if note:
                    self.ws.append_operational_memory(f"- {note}")
                    if self._is_terminal:
                        from rich.console import Console
                        Console().print(f"  [dim]📖 learned: {note}[/dim]")
                    else:
                        self._output(f"  📖 learned: {note}\n")

            tool_match = _TOOL_RE.search(response)

            if not tool_match:
                # Pure text response — no tool call — collect it.
                # Strip BOTH memory markers: they're intercepted above and must
                # never reach the user, the window, or history.
                clean   = re.sub(r"REMEMBER:[^\n]*\n?", "", response)
                clean   = re.sub(r"LEARN:[^\n]*\n?",    "", clean).strip()
                display = clean or response.strip() or "(no response)"
                self.ws.log_session(self.session_log, self.name, display)
                if self.history and self.history[-1]["role"] == "assistant":
                    self.history[-1]["content"] = display
                self._responses.append(display)
                self._last_response = display
                self.ws.append_conversation_window("assistant", display, self.name)
                return

            # Text before TOOL: marker — log it for analysis but never deliver.
            # Pre-tool text is internal reasoning ("Now I have enough...",
            # "Let me check that...") — useful in session logs for reflection
            # but not user-facing content. Only final answers go in _responses.
            pre_tool = response[:tool_match.start()].strip()
            pre_tool = re.sub(r"REMEMBER:[^\n]*\n?", "", pre_tool).strip()
            pre_tool = re.sub(r"LEARN:[^\n]*\n?",    "", pre_tool).strip()
            if pre_tool:
                self.ws.log_session(self.session_log, self.name, pre_tool)

            tool_name = tool_match.group("tool_name")
            raw_args  = tool_match.group("args")

            call_sig = f"{tool_name}:{raw_args}"
            if call_sig in seen_calls:
                msg = f"(tool {tool_name} called repeatedly with same args — stopping)"
                self._output(f"  \u26a0\ufe0f  {msg}\n")
                self.ws.log_session(self.session_log, self.name, msg)
                if self.history and self.history[-1]["role"] == "assistant":
                    self.history[-1]["content"] = msg
                return
            seen_calls.append(call_sig)

            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                result = f"[agent] Invalid JSON: {raw_args}"
            else:
                result = self._execute_tool(tool_name, args)

            self.ws.log_session(
                self.session_log,
                f"tool:{tool_name}",
                f"**Input:** `{raw_args}`\n\n**Output:**\n```\n{result}\n```",
            )

            if not (self.history and self.history[-1]["role"] == "assistant"):
                self.history.append({"role": "assistant", "content": response})
            self.history.append({"role": "user", "content": f"RESULT: {result}"})

        if self._is_terminal:
            from rich.console import Console
            Console().print(f"\n  [yellow]⚠ Hit loop limit ({loop_limit}).[/yellow]")
        else:
            self._output(f"[{self.name}] Hit loop limit ({loop_limit}).\n")

    # ── Streaming ────────────────────────────────────────────────────────────

    def _stream_response(self) -> str:
        now        = datetime.now(timezone.utc).astimezone()
        time_ctx   = f"Current date and time: {now.strftime('%A, %Y-%m-%d %H:%M %Z')}"
        from aria import __version__
        sys_prompt = self.system_prompt + f"\n\n## Context\n{time_ctx}\nVersion: {__version__}\n"
        messages   = [{"role": "system", "content": sys_prompt}] + self.history

        while messages and messages[-1]["role"] == "assistant":
            messages = messages[:-1]

        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
            )
        except Exception as exc:
            # Network down, timeout, API error — surface clearly, don't crash
            err_type = type(exc).__name__
            msg = str(exc)
            # Simplify common cases
            if "Connection" in err_type or "connect" in msg.lower():
                friendly = "No connection to LLM — check your network and LLM_BASE_URL."
            elif "timeout" in msg.lower() or "Timeout" in err_type:
                friendly = "LLM request timed out — the server may be overloaded."
            elif "401" in msg or "403" in msg or "Unauthorized" in msg:
                friendly = "LLM authentication failed — check LLM_API_KEY in ~/.aria/.env."
            else:
                friendly = f"LLM error ({err_type}): {msg}"
            if self._is_terminal:
                from rich.console import Console
                Console().print(f"\n  [red]⚠ {friendly}[/red]")
            else:
                self._output(f"\n⚠ {friendly}\n")
            return f"[error] {friendly}"

        full_text    = ""
        line_buf     = ""
        in_tool_call = False
        # Terminal mode streams through a single rich.Live region: rich owns
        # word-wrapping and redraws in place, so there is no manual cursor math
        # and no erase-then-reprint flash. Markdown renders incrementally as the
        # response grows. Non-terminal callers (Telegram, chat_collect) keep
        # writing to self._output line by line, exactly as before.
        display_lines: list[str] = []

        live = None
        con  = None
        if self._is_terminal:
            from rich.console import Console
            from rich.live import Live
            con = Console(highlight=False, theme=_md_theme())
            con.print(f"\n  [bold green]{self.name}[/bold green]")
            live = Live(console=con, refresh_per_second=12,
                        vertical_overflow="visible", auto_refresh=True)
            live.start()
        else:
            self._output(f"\n{self.name}: ")

        def _renderable():
            from rich.text import Text
            body = "\n".join(display_lines)
            partial = "" if line_buf.startswith(("TOOL:", "REMEMBER:", "LEARN:")) else line_buf
            if partial:
                body = f"{body}\n{partial}" if body else partial
            if not body:
                return Text("")
            if self.markdown_enabled and _has_markdown(body):
                return _chat_markdown(body)
            return Text(body)

        try:
            for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not (delta and delta.content):
                    continue
                token      = delta.content
                full_text += token

                if in_tool_call:
                    continue

                line_buf += token

                while "\n" in line_buf:
                    line, line_buf = line_buf.split("\n", 1)
                    if line.startswith("TOOL:"):
                        in_tool_call = True
                        line_buf = ""
                        break
                    if line.startswith("REMEMBER:") or line.startswith("LEARN:"):
                        continue  # suppress from display
                    if self._is_terminal:
                        display_lines.append(line)
                    else:
                        self._output(line + "\n")

                if live is not None and not in_tool_call:
                    live.update(_renderable())

            # Flush the trailing partial line (no newline terminator)
            if line_buf and not in_tool_call and not line_buf.startswith(
                ("REMEMBER:", "TOOL:", "LEARN:")
            ):
                if self._is_terminal:
                    display_lines.append(line_buf)
                else:
                    self._output(line_buf)
                line_buf = ""
        finally:
            if live is not None:
                live.update(_renderable())
                live.stop()

        tool_match = _TOOL_RE.search(full_text)

        if tool_match:
            if self._is_terminal:
                from rich.console import Console
                Console().print(f"  [dim]⚙ calling [bold]{tool_match.group('tool_name')}[/bold]...[/dim]")
            else:
                self._output(f"⚙ calling {tool_match.group('tool_name')}...\n")
        elif con is not None:
            con.print()       # trailing blank line for spacing before next prompt
        else:
            self._output("\n")

        self.history.append({"role": "assistant", "content": full_text})
        return full_text

    # ── Session continuity ────────────────────────────────────────────────────

    def close(self) -> None:
        """
        Trim the conversation window to the last ARIA_WINDOW_MESSAGES entries.
        No LLM call — fast, works offline, safe to call on any exit path.
        """
        self.ws.trim_conversation_window()

    # ── Tool execution ────────────────────────────────────────────────────────

    def _execute_tool(self, name: str, args: dict) -> str:
        result = tools.dispatch(name, args, self.tool_schemas)
        if len(result) > 6000:
            result = result[:6000] + "\n\u2026 [truncated]"
        return result
