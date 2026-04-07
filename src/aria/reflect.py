"""
aria/reflect.py — Autonomous memory reflection engine.

Two-phase process:
  1. Extraction  — analyse only NEW session logs (watermark-gated), extract
                   raw observations per batch
  2. Consolidation — merge raw observations with existing patterns into a
                   single pruned, high-signal output capped at MAX_PATTERN_LINES

This keeps patterns.md lean and signal-dense regardless of history length.

Triggered via:
  - CLI:     aria-reflect
  - Cron:    0 3 * * * aria-reflect
  - Tool:    the `reflect` tool lets the agent trigger it mid-conversation
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_BATCH_SIZE        = int(os.environ.get("ARIA_REFLECT_BATCH",         "10"))
_SESSION_CHARS     = int(os.environ.get("ARIA_REFLECT_SESSION_CHARS",  "3000"))
_MAX_PATTERN_LINES = int(os.environ.get("ARIA_REFLECT_MAX_LINES",      "40"))


def _read_session(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > _SESSION_CHARS:
        text = text[:_SESSION_CHARS] + "\n… [truncated]"
    return text


def _extraction_prompt(sessions: list[tuple[Path, str]]) -> str:
    """Prompt for Phase 1: extract raw observations from new sessions."""
    session_block = "\n\n---\n\n".join(
        f"### {path.stem}\n{content}" for path, content in sessions
    )
    return (
        "Analyse these conversation logs and extract behavioural observations "
        "about the user. Be specific and evidence-based — only include what "
        "you actually observe, not inferences.\n\n"
        "Focus on:\n"
        "- Topics and domains that came up\n"
        "- Communication preferences (length, tone, format)\n"
        "- Workflows and tool usage patterns\n"
        "- Corrections or refinements the user made\n"
        "- Technical context (languages, tools, systems)\n\n"
        "Output as concise bullet points. Omit categories with no evidence.\n\n"
        "## Sessions\n\n"
        f"{session_block}"
    )


def _consolidation_prompt(new_observations: str, existing_patterns: str | None) -> str:
    """
    Prompt for Phase 2: merge new observations with existing patterns,
    prune redundant/stale entries, cap output at MAX_PATTERN_LINES lines.
    """
    existing_block = (
        f"## Existing patterns\n{existing_patterns}\n\n"
        if existing_patterns else ""
    )
    return (
        "You are consolidating a user's behavioural pattern memory. "
        "Your output will be injected into an AI assistant's system prompt on every session, "
        "so it must be maximally signal-dense and concise.\n\n"
        f"{existing_block}"
        f"## New observations from recent sessions\n{new_observations}\n\n"
        "## Task\n"
        "Produce a single merged, pruned pattern list following these rules:\n"
        f"1. Hard limit: {_MAX_PATTERN_LINES} bullet points total across all categories.\n"
        "2. Merge duplicates — if new observations confirm existing patterns, strengthen "
        "the existing entry rather than adding a new one.\n"
        "3. Prune weak signals — remove patterns that appeared only once and haven't "
        "been confirmed by new sessions.\n"
        "4. Prioritise recency — if a new observation contradicts an existing pattern, "
        "trust the new one.\n"
        "5. Keep only high-confidence, actionable patterns. Vague generalities are noise.\n"
        "6. Group under these headings (omit empty ones):\n"
        "   - **Topics & domains**\n"
        "   - **Communication style**\n"
        "   - **Workflows & tools**\n"
        "   - **Technical context**\n"
        "   - **Preferences & corrections**\n\n"
        "Output only the bullet list — no preamble, no explanation."
    )


def run(notify: bool = False) -> str:
    """Run the reflection pass. Returns a status string."""
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

    log.info("Reflection: %d new sessions, batches of %d", len(unanalysed), _BATCH_SIZE)

    client = OpenAI(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ.get("LLM_API_KEY", "local"),
    )
    model = os.environ.get("LLM_MODEL", "llama3.2")

    # ── Phase 1: extract raw observations from each batch ────────────────────
    all_observations: list[str] = []
    last_analysed: Path | None  = None
    total_analysed              = 0

    for i in range(0, len(unanalysed), _BATCH_SIZE):
        batch    = unanalysed[i : i + _BATCH_SIZE]
        sessions = [(p, _read_session(p)) for p in batch]

        log.info("Extracting batch %d–%d...", i + 1, i + len(batch))
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": _extraction_prompt(sessions)}],
                stream=False,
            )
            all_observations.append(resp.choices[0].message.content.strip())
        except Exception as exc:
            log.error("Extraction failed for batch %d: %s", i, exc)
            break

        last_analysed   = batch[-1]
        total_analysed += len(batch)

    if not all_observations:
        return "Reflection: extraction failed — no patterns updated."

    # ── Phase 2: consolidate new observations with existing patterns ──────────
    new_observations    = "\n\n".join(all_observations)
    existing_patterns   = ws.load_patterns()

    log.info("Consolidating patterns (max %d lines)...", _MAX_PATTERN_LINES)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": _consolidation_prompt(new_observations, existing_patterns),
            }],
            stream=False,
        )
        consolidated = resp.choices[0].message.content.strip()
    except Exception as exc:
        log.error("Consolidation failed: %s", exc)
        # Save raw observations rather than losing them
        consolidated = new_observations

    ws.save_patterns(consolidated)

    if last_analysed:
        ws.update_watermark(last_analysed)

    line_count = len([l for l in consolidated.splitlines() if l.strip()])
    msg = (
        f"Reflection complete: {total_analysed} sessions analysed, "
        f"patterns consolidated to {line_count} lines."
    )
    log.info(msg)

    if notify:
        try:
            from aria.telegram_notify import send
            send(f"🧠 {msg}")
        except Exception as exc:
            log.warning("Telegram notification failed: %s", exc)

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
    parser.add_argument("--notify", "-n", action="store_true",
                        help="Send result to Telegram when done")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show debug output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    print(run(notify=args.notify))


if __name__ == "__main__":
    main()
