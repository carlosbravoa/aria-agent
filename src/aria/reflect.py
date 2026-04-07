"""
aria/reflect.py — Autonomous memory reflection engine.

Scans unanalysed session logs, extracts behavioural patterns and insights
using the LLM, and writes them to memory/patterns.md. Runs unsupervised.

Can be triggered three ways:
  1. CLI:       aria-reflect
  2. Cron:      0 3 * * * aria-reflect   (daily at 3am)
  3. On-demand: the `reflect` tool lets the agent trigger it mid-conversation

Architecture:
  sessions/session_*.md  (raw logs)
       ↓  [batch analysis, LLM call per N sessions]
  memory/patterns.md     (extracted patterns — loaded every session)
  memory/reflect_watermark (tracks which sessions have been analysed)

Patterns extracted:
  - Recurring topics and domains the user cares about
  - Preferred communication style and response format
  - Common workflows and tool usage sequences
  - Implicit preferences revealed through corrections or feedback
  - Time-of-day / day-of-week activity patterns
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# Analyse sessions in batches to keep prompts manageable
_BATCH_SIZE   = int(os.environ.get("ARIA_REFLECT_BATCH", "10"))
# Max chars to read per session (avoid huge files dominating the prompt)
_SESSION_CHARS = int(os.environ.get("ARIA_REFLECT_SESSION_CHARS", "3000"))


def _read_session(path: Path) -> str:
    """Read a session log, truncating if necessary."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > _SESSION_CHARS:
        text = text[:_SESSION_CHARS] + "\n… [truncated]"
    return text


def _build_analysis_prompt(sessions: list[tuple[Path, str]], existing_patterns: str | None) -> str:
    """Build the prompt for pattern extraction."""
    session_block = "\n\n---\n\n".join(
        f"### {path.stem}\n{content}" for path, content in sessions
    )

    existing_block = (
        f"## Existing patterns (update/extend these, do not repeat verbatim)\n{existing_patterns}\n\n"
        if existing_patterns else ""
    )

    return (
        "You are analysing conversation logs to extract behavioural patterns "
        "about the user. Your output will be stored as memory and used to make "
        "future interactions more personalised and efficient.\n\n"
        f"{existing_block}"
        "## Session logs to analyse\n\n"
        f"{session_block}\n\n"
        "## Instructions\n"
        "Extract patterns across these dimensions (only include what you actually observe):\n"
        "- **Topics & domains**: recurring subjects, projects, or areas of interest\n"
        "- **Communication style**: preferred response length, tone, format (bullets vs prose)\n"
        "- **Workflows**: common task sequences, tool combinations, repeated operations\n"
        "- **Implicit preferences**: things the user corrected, refined, or reacted positively to\n"
        "- **Temporal patterns**: when they tend to interact, urgency signals\n"
        "- **Technical context**: languages, tools, systems mentioned repeatedly\n\n"
        "Format as concise bullet points grouped by dimension. "
        "Be specific — 'prefers Python' is better than 'has technical interests'. "
        "Omit dimensions where no clear pattern exists. "
        "Do not include personal names or sensitive data."
    )


def run(notify: bool = False) -> str:
    """
    Run the reflection pass. Returns a status string.
    If notify=True, sends result to Telegram.
    """
    from aria import config
    from aria.workspace import Workspace
    from openai import OpenAI

    config.load()
    ws = Workspace(config.workspace_dir())

    unanalysed = ws.unanalysed_sessions()
    if not unanalysed:
        msg = "Reflection: no new sessions to analyse."
        log.info(msg)
        return msg

    log.info("Reflection: analysing %d sessions in batches of %d", len(unanalysed), _BATCH_SIZE)

    client = OpenAI(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ.get("LLM_API_KEY", "local"),
    )
    model = os.environ.get("LLM_MODEL", "llama3.2")

    existing_patterns = ws.load_patterns()
    last_analysed: Path | None = None
    total_analysed = 0

    # Process in batches so no single prompt gets too large
    for i in range(0, len(unanalysed), _BATCH_SIZE):
        batch = unanalysed[i : i + _BATCH_SIZE]
        sessions = [(p, _read_session(p)) for p in batch]

        prompt = _build_analysis_prompt(sessions, existing_patterns)

        log.info("Analysing batch %d-%d...", i + 1, i + len(batch))
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                stream=False,
            )
            existing_patterns = resp.choices[0].message.content.strip()
        except Exception as exc:
            log.error("LLM call failed for batch %d: %s", i, exc)
            break  # stop at failed batch, save progress so far

        last_analysed = batch[-1]
        total_analysed += len(batch)

    if existing_patterns:
        ws.save_patterns(existing_patterns)

    if last_analysed:
        ws.update_watermark(last_analysed)

    msg = f"Reflection complete: {total_analysed} sessions analysed, patterns updated."
    log.info(msg)

    if notify:
        try:
            from aria.telegram_notify import send
            send(f"🧠 {msg}")
        except Exception as exc:
            log.warning("Could not send Telegram notification: %s", exc)

    return msg


def main() -> None:
    """CLI entry point: aria-reflect"""
    import argparse

    from aria.setup import is_first_run, run as setup_run
    if is_first_run():
        setup_run()

    parser = argparse.ArgumentParser(
        prog="aria-reflect",
        description="Analyse session logs and update memory patterns.",
    )
    parser.add_argument(
        "--notify", "-n",
        action="store_true",
        help="Send result to Telegram when done",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show debug output",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    result = run(notify=args.notify)
    print(result)


if __name__ == "__main__":
    main()
