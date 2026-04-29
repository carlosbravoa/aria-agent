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
from typing import Any

from openai import OpenAI

from aria import tools
from aria.workspace import Workspace

_TOOL_RE = re.compile(
    r"TOOL:\s*(?P<tool_name>\w+)\s*\nINPUT:\s*(?P<args>\{.*?\})",
    re.DOTALL,
)
_REMEMBER_RE = re.compile(r"REMEMBER:\s*(?P<note>[^\n]+)")
# Both configurable via ~/.aria/.env
_MAX_LOOPS   = int(os.environ.get("ARIA_MAX_LOOPS",   "20"))
_MAX_HISTORY = int(os.environ.get("ARIA_MAX_HISTORY", "60"))


class Agent:
    def __init__(self, output_callback=None) -> None:
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

        from aria import config
        self.ws = Workspace(config.workspace_dir())
        self.tool_schemas = tools.load_all(config.tools_dir())
        self.ws.update_tools_registry(self.tool_schemas)

        self.system_prompt = self._build_system_prompt()
        self._seed = self._few_shot_examples()
        self.history: list[dict[str, Any]] = list(self._seed)
        self.session_log = self.ws.new_session_path()

    # ── System prompt ────────────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        soul         = self.ws.load_soul()
        memory       = self.ws.load_memory()
        tool_docs    = self._build_tool_docs()
        last_session = self.ws.last_session_summary()
        prev_block   = last_session if last_session else "_No previous session._"

        return (
            f"{soul}\n\n"
            "## Memory\n"
            f"{memory}\n\n"
            "## Previous Session\n"
            f"{prev_block}\n\n"
            "## Tool Protocol\n"
            "To call a tool, output EXACTLY these two lines with no other text before them:\n\n"
            "TOOL: <tool_name>\n"
            'INPUT: {"key": "value"}\n\n'
            "The system runs it and replies:\n\n"
            "RESULT: <o>\n\n"
            "After RESULT, write your final answer in plain text.\n\n"
            f"{tool_docs}\n\n"
            "## Saving to Memory\n"
            "To save a fact, output this anywhere in your response:\n\n"
            "REMEMBER: <the fact>\n\n"
            "## Rules\n"
            "- Use TOOL:/INPUT: for tool calls. No other syntax works.\n"
            "- Use REMEMBER: to persist facts.\n"
            "- Never narrate before a tool call. Emit TOOL: immediately.\n"
            "- After RESULT, answer in plain text.\n"
            "- You know your available tools from the list above — never call a tool to look them up.\n"
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

    def _few_shot_examples(self) -> list[dict]:
        """
        Protocol examples — tool-agnostic except for the file_access demo
        which is needed to show the TOOL:/INPUT: format concretely.
        """
        return [
            {"role": "user", "content": "List the files in /tmp"},
            {"role": "assistant", "content": 'TOOL: file_access\nINPUT: {"action": "list", "path": "/tmp"}'},
            {"role": "user", "content": "RESULT: notes.txt\nreport.pdf"},
            {"role": "assistant", "content": "/tmp contains: notes.txt, report.pdf."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4."},
            {"role": "user", "content": "My name is <username>."},
            {"role": "assistant", "content": "REMEMBER: User name is <username>.\nNice to meet you, <username>!"},
            {"role": "user", "content": "scheduled task: summarise top stories from https://news.ycombinator.com"},
            {"role": "assistant", "content": 'TOOL: web_fetch\nINPUT: {"url": "https://news.ycombinator.com", "max_chars": 2000}'},
            {"role": "user", "content": "RESULT: 1. Story A\n2. Story B\n3. Story C"},
            {"role": "assistant", "content": 'TOOL: notify\nINPUT: {"message": "Top HN stories:\\n1. Story A\\n2. Story B\\n3. Story C"}'},
            {"role": "user", "content": "RESULT: [notify] Message sent."},
            {"role": "assistant", "content": "Done. Summary sent."},
        ]

    # ── Public interface ─────────────────────────────────────────────────────

    def chat(self, user_input: str) -> None:
        """Send a message; output goes to self._output callback."""
        self.history.append({"role": "user", "content": user_input})
        self.ws.log_session(self.session_log, "user", user_input)
        self._trim_history()
        self._run_loop()

    def chat_collect(self, user_input: str) -> str:
        """
        Run a chat turn and return only the final response text.
        Suppresses all status output (tool calls, memory saves, name prefix)
        — safe for Telegram, WhatsApp, supervisor tasks.
        """
        # Capture everything but discard it — we only want the final answer
        # which _run_loop writes to self.history[-1]["content"].
        buf: list[str] = []
        orig = self._output
        self._output = buf.append
        try:
            self.chat(user_input)
        finally:
            self._output = orig
        # Extract the clean final answer directly from history,
        # bypassing all the status noise captured in buf.
        seed_len = len(self._seed)
        real = self.history[seed_len:]
        # Find the last assistant message that isn't a tool call
        for msg in reversed(real):
            if msg["role"] == "assistant":
                content = msg.get("content", "").strip()
                # Skip raw tool call lines — the final answer never starts with TOOL:
                if not content.startswith("TOOL:"):
                    return content
        return ""

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

    # ── ReAct loop ───────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        seen_calls: list[str] = []

        for _ in range(_MAX_LOOPS):
            response = self._stream_response()

            for m in _REMEMBER_RE.finditer(response):
                note = m.group("note").strip()
                if note:
                    self.ws.append_memory(f"- {note}")
                    if self._is_terminal:
                        from rich.console import Console
                        Console().print(f"  [dim]💾 saved: {note}[/dim]")
                    else:
                        self._output(f"  💾 saved: {note}\n")

            tool_match = _TOOL_RE.search(response)

            if not tool_match:
                clean   = re.sub(r"TOOL:.*", "", response, flags=re.DOTALL).strip()
                display = clean or response.strip() or "(no response)"
                self.ws.log_session(self.session_log, self.name, display)
                if self.history and self.history[-1]["role"] == "assistant":
                    self.history[-1]["content"] = display
                return

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
            Console().print(f"\n  [yellow]⚠ Hit loop limit ({_MAX_LOOPS}).[/yellow]")
        else:
            self._output(f"[{self.name}] Hit loop limit ({_MAX_LOOPS}).\n")

    # ── Streaming ────────────────────────────────────────────────────────────

    def _stream_response(self) -> str:
        now        = datetime.now(timezone.utc).astimezone()
        time_ctx   = f"Current date and time: {now.strftime('%A, %Y-%m-%d %H:%M %Z')}"
        from aria import __version__
        sys_prompt = self.system_prompt + f"\n\n## Context\n{time_ctx}\nVersion: {__version__}\n"
        messages   = [{"role": "system", "content": sys_prompt}] + self.history

        while messages and messages[-1]["role"] == "assistant":
            messages = messages[:-1]

        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
        )

        full_text    = ""
        line_buf     = ""
        in_tool_call = False
        streamed_lines: list[str] = []  # track what we printed for terminal erase

        if self._is_terminal:
            from rich.console import Console
            _con = Console(highlight=False)
            _con.print(f"\n  [bold green]{self.name}[/bold green] ", end="")
        else:
            self._output(f"\n{self.name}: ")

        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
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
                    if line.startswith("REMEMBER:"):
                        pass  # suppress
                    else:
                        self._output(line + "\n")
                        if self._is_terminal:
                            streamed_lines.append(line)

        # Flush remaining partial line
        if line_buf and not in_tool_call:
            if not line_buf.startswith("REMEMBER:") and not line_buf.startswith("TOOL:"):
                self._output(line_buf)
                if self._is_terminal:
                    streamed_lines.append(line_buf)

        tool_match = _TOOL_RE.search(full_text)

        if tool_match:
            if self._is_terminal:
                from rich.console import Console
                Console().print(f"  [dim]⚙ calling [bold]{tool_match.group('tool_name')}[/bold]...[/dim]")
            else:
                self._output(f"⚙ calling {tool_match.group('tool_name')}...\n")
        else:
            self._output("\n")
            # ── Option A: erase streamed text and re-render as Markdown ──────
            if self._is_terminal and streamed_lines:
                self._render_markdown_replace(streamed_lines, full_text)

        self.history.append({"role": "assistant", "content": full_text})
        return full_text

    def _render_markdown_replace(self, streamed_lines: list[str], full_text: str) -> None:
        """
        Erase the raw streamed text and re-render as rich Markdown (Option A).

        Moves the cursor up by the number of lines we printed, clears them,
        then renders the full response through rich's Markdown renderer.

        Only called in terminal mode after a non-tool response.
        """
        from rich.console import Console
        from rich.markdown import Markdown
        from rich.theme import Theme

        # Override default heading styles — rich centers headings and adds
        # decorative rules by default. We want plain left-aligned bold text.
        _MD_THEME = Theme({
            "markdown.h1":         "bold green",
            "markdown.h1.border":  "none",
            "markdown.h2":         "bold cyan",
            "markdown.h2.border":  "none",
            "markdown.h3":         "bold",
            "markdown.h4":         "bold dim",
            "markdown.h5":         "dim",
            "markdown.h6":         "dim",
        })

        # Clean the response — strip REMEMBER: lines before rendering
        clean = re.sub(r"REMEMBER:[^\n]*\n?", "", full_text).strip()
        if not clean:
            return

        # Count how many terminal lines the streamed text occupied.
        # Each streamed line may wrap if wider than the terminal.
        con = Console(highlight=False, theme=_MD_THEME)
        terminal_width = con.width or 80
        # +2 for the "  Name " prefix on the first line
        lines_to_erase = 1  # the "Name " prefix line
        for i, line in enumerate(streamed_lines):
            visible_len = len(line) + (len(self.name) + 3 if i == 0 else 0)
            lines_to_erase += max(1, (visible_len + terminal_width - 1) // terminal_width)

        # Move cursor up and clear each line
        import sys
        for _ in range(lines_to_erase):
            sys.stdout.write("\033[1A\033[2K")
        sys.stdout.flush()

        # Re-render with name prefix + Markdown
        con.print(f"  [bold green]{self.name}[/bold green]")
        con.print(Markdown(clean), soft_wrap=True)

    # ── Session continuity ────────────────────────────────────────────────────

    def summarise_session(self) -> str | None:
        """
        Summarise the current session for continuity in the next one.

        Strategy:
        - 0 real turns → None (nothing happened)
        - 1-2 turns → save last user message + last assistant answer verbatim
          (cheap, no LLM call, preserves actual content)
        - 3+ turns → LLM summarises into bullet points

        Never returns None for a session that had real exchanges — always
        saves something so last_session.md stays current.
        """
        seed_len = len(self._seed)
        real = self.history[seed_len:]
        if not real:
            return None

        # Build transcript — skip RESULT: blocks (tool output noise)
        turns: list[tuple[str, str]] = []  # (role, content)
        for msg in real:
            role    = msg["role"]
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            if role == "user" and content.startswith("RESULT:"):
                continue
            if role in ("user", "assistant") and not content.startswith("TOOL:"):
                turns.append((role, content))

        if not turns:
            return None

        # ── Short session: save verbatim snippet, no LLM call ─────────────────
        if len(turns) <= 2:
            lines = []
            for role, content in turns:
                label   = "User asked" if role == "user" else f"{self.name} answered"
                snippet = content[:400] + ("…" if len(content) > 400 else "")
                lines.append(f"- {label}: {snippet}")
            return "\n".join(lines)

        # ── Longer session: LLM summary ───────────────────────────────────────
        transcript = "\n".join(
            f"{'User' if r == 'user' else self.name}: {c[:300]}{'…' if len(c) > 300 else ''}"
            for r, c in turns
        )
        prompt = (
            "Summarise this conversation in 3-5 bullet points for use as context "
            "in the next session. Include: what the user asked, what was found or "
            "done, and any key content or facts from the answers. Be specific — "
            "include actual content, not just meta-descriptions like 'user asked about X'.\n\n"
            + transcript
        )

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                stream=False,
            )
            return resp.choices[0].message.content.strip() or None
        except Exception:
            # Fallback: save verbatim snippet rather than losing the session
            lines = []
            for role, content in turns[-4:]:  # last 2 exchanges
                label   = "User asked" if role == "user" else f"{self.name} answered"
                snippet = content[:400] + ("…" if len(content) > 400 else "")
                lines.append(f"- {label}: {snippet}")
            return "\n".join(lines)

    def close(self) -> None:
        """Summarise this session and persist it to last_session.md.
        Always writes — never leaves a stale older summary in place.
        """
        summary = self.summarise_session()
        if summary:
            self.ws.save_session_summary(summary)
        else:
            # No real exchanges this session — record a minimal timestamp
            # marker so the model knows the file is current.
            ts = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")
            self.ws.save_session_summary(f"- No exchange recorded ({ts}).")

    # ── Tool execution ────────────────────────────────────────────────────────

    def _execute_tool(self, name: str, args: dict) -> str:
        result = tools.dispatch(name, args, self.tool_schemas)
        if len(result) > 6000:
            result = result[:6000] + "\n\u2026 [truncated]"
        return result
