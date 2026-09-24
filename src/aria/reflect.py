"""
aria/reflect.py — Autonomous memory reflection engine.

Three-phase process:
  1. Extraction  — analyse only NEW session logs (watermark-gated), extract
                   raw observations per batch
  2. Consolidation of patterns — merge raw observations with existing patterns
                   into a single pruned output capped at MAX_PATTERN_LINES
  3. Consolidation of operational memory — dedupe/prune operational_memory.md
                   against the new observations (only if it has content)

Serialised by a file lock so the supervisor and a REPL background thread can't
run it concurrently. Keeps patterns.md lean regardless of history length.

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

# Tuning knobs. Read at USE time via _cfg(), never at import: aria-reflect's
# main() imports this module before config.load() applies ~/.aria/.env, so
# import-time reads silently ignored every .env override.
_DEFAULTS = {
    "ARIA_REFLECT_BATCH":         10,
    "ARIA_REFLECT_SESSION_CHARS": 3000,
    "ARIA_REFLECT_MAX_LINES":     40,
    "ARIA_OPSMEM_MAX_LINES":      40,
    "ARIA_CORE_MAX_LINES":        80,
    # Friction phase: analyse memory/friction_log.md (high-friction turns flagged
    # by the agent harness) once one tool has this many events, or the log holds
    # twice as many events overall.
    "ARIA_FRICTION_REFLECT_MIN":  3,
    # Sessions modified within this many minutes may still be receiving turns;
    # they're left for a later pass (the watermark never skips past them).
    "ARIA_REFLECT_SETTLE_MIN":    10,
}


def _cfg(name: str) -> int:
    default = _DEFAULTS[name]
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _new_lines(snapshot: str | None, current: str | None) -> list[str]:
    """Entry lines present in `current` but not in `snapshot` — i.e. what other
    processes appended while a reflection LLM call was in flight."""
    before = {l.strip() for l in (snapshot or "").splitlines()}
    return [l for l in (current or "").splitlines()
            if l.strip() and not l.strip().startswith(("#", "<!--"))
            and l.strip() not in before]


def _read_session(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    cap = _cfg("ARIA_REFLECT_SESSION_CHARS")
    if len(text) > cap:
        # Keep BOTH ends: the opening (what the session was about) and the close
        # (where corrections, outcomes, and "that was wrong, do X" live). Keeping
        # only the head — the old behaviour — meant reflection never saw the
        # conclusions of any long session.
        head = cap * 2 // 3
        tail = cap - head
        text = text[:head] + "\n… [middle truncated] …\n" + text[-tail:]
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
        "- Technical context (languages, tools, systems)\n"
        "- Systemic friction: repeated similar tool errors, or repeated "
        "workarounds for the same tool problem (e.g. quoting issues) — tasks "
        "that took far more steps than they should suggest something is broken\n\n"
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
        f"1. Hard limit: {_cfg('ARIA_REFLECT_MAX_LINES')} bullet points total across all categories.\n"
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


def _ops_consolidation_prompt(current_ops: str, new_observations: str) -> str:
    """
    Prompt for Phase 3: consolidate operational_memory.md.
    Deduplicates entries covering the same topic, keeps most recent/accurate,
    removes entries contradicted or superseded by recent sessions.
    """
    return (
        "You are consolidating an AI assistant's operational memory — a list of "
        "procedures and shortcuts learned from past sessions with a specific user.\n\n"
        "## Current operational memory entries\n"
        f"{current_ops}\n\n"
        "## Recent session observations\n"
        f"{new_observations}\n\n"
        "## Task\n"
        "Produce a clean, deduplicated operational memory list following these rules:\n"
        f"1. Hard limit: {_cfg('ARIA_OPSMEM_MAX_LINES')} entries total.\n"
        "2. Deduplicate — if two entries cover the same topic (e.g. both mention Jira project), "
        "keep only the most recent or most accurate one.\n"
        "3. Correct — if a recent session shows that an entry was wrong or has changed "
        "(e.g. an attempt failed, a new value was used successfully), update or remove it.\n"
        "4. Prune — remove entries that are too vague to be actionable, or that describe "
        "something the agent should figure out each time rather than memorise.\n"
        "5. Keep entries that are specific, verified by successful use, and save meaningful "
        "time or reduce errors in future sessions.\n"
        "6. Preserve entries not touched by recent sessions exactly as-is.\n\n"
        "Output only the bullet list — one entry per line, starting with '- '. "
        "No headings, no preamble, no explanation."
    )


def _core_consolidation_prompt(current_core: str) -> str:
    """
    Prompt for Phase 4: consolidate core.md (permanent user facts). core.md has
    no line cap by design (facts are permanent, so we can't blind-drop the
    oldest) — but repeated/near-duplicate remember() calls make it the biggest
    long-run context-cost growth. This is CONSERVATIVE: dedup and fix
    contradictions only; never prune a genuine, still-true fact.
    """
    return (
        "You are cleaning up an AI assistant's core memory — a list of PERMANENT "
        "facts about one user (name, role, timezone, language, preferences, "
        "recurring contacts).\n\n"
        "## Current core memory\n"
        f"{current_core}\n\n"
        "## Task\n"
        "Return a cleaned fact list following these rules:\n"
        "1. Merge exact and near-duplicate facts into one.\n"
        "2. If two facts about the same attribute contradict (e.g. two different "
        "timezones), keep only the most recent/most specific one.\n"
        "3. Do NOT invent, infer, or drop any genuine distinct fact — this is a "
        "conservative dedup, not a summary. When unsure, keep the fact.\n"
        f"4. Soft cap: aim for at most {_cfg('ARIA_CORE_MAX_LINES')} bullet points.\n\n"
        "Output only the bullet list — one fact per line, starting with '- '. "
        "No headings, no preamble, no explanation."
    )


def _friction_counts(text: str) -> dict[str, int]:
    """Per-tool event counts from friction_log.md lines (the `worst=tool(n/m)`
    field written by agent._flag_friction)."""
    import re
    counts: dict[str, int] = {}
    for m in re.finditer(r"worst=([\w.-]+)\(", text or ""):
        counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def _friction_is_hot(text: str) -> bool:
    """Enough accumulated friction to be worth an LLM diagnosis: one tool with
    >= _FRICTION_REFLECT_MIN events, or twice that many events overall (turns
    without a dominant failing tool still count toward the total)."""
    threshold = _cfg("ARIA_FRICTION_REFLECT_MIN")
    if threshold <= 0 or not text:
        return False
    total = len([l for l in text.splitlines() if l.startswith("- ")])
    counts = _friction_counts(text)
    return (any(n >= threshold for n in counts.values())
            or total >= 2 * threshold)


def _friction_prompt(friction_log: str, ops: str) -> str:
    ops_block = (f"## Current operational memory (context)\n{ops}\n\n"
                 if ops else "")
    return (
        "An AI assistant's harness automatically logged these HIGH-FRICTION "
        "turns — turns with many failing tool calls, repeated calls, or hard "
        "stops. The assistant itself tends to work around problems without "
        "reporting them, so your job is to spot the systemic issue it didn't.\n\n"
        "## Friction log (one line per struggling turn)\n"
        f"{friction_log}\n\n"
        f"{ops_block}"
        "## Task\n"
        "Identify recurring failure modes or recurring workaround patterns. "
        "Name the tool and the most likely root cause (e.g. 'shell_run: shell "
        "quoting breaks on nested quotes'). Be concrete and evidence-based.\n"
        "- If there IS a recurring issue: output at most 3 terse bullets.\n"
        "- If the events look unrelated one-offs: output exactly NONE."
    )


def _phase_friction(ws, client, model: str, notify: bool) -> str:
    """Phase 5: diagnose accumulated high-friction turns. Consumes (clears) the
    friction log so stale events never re-alert. Returns a status suffix."""
    friction_raw = ws.load_friction_log() or ""
    if not _friction_is_hot(friction_raw):
        return ""
    log.info("Analysing %d friction events...",
             len([l for l in friction_raw.splitlines() if l.startswith("- ")]))
    ops = ws.load_operational_memory() or ""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user",
                       "content": _friction_prompt(friction_raw, ops)}],
            stream=False,
        )
        diagnosis = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.warning("Friction analysis failed: %s", exc)
        return ""            # keep the log — retry next pass
    # Consumed either way (no stale re-alerts) — but only the entries that were
    # actually analysed; events logged during the LLM call stay for next pass.
    ws.clear_friction_log(friction_raw)
    if not diagnosis or diagnosis.upper().startswith("NONE"):
        return ", friction events reviewed (no systemic issue)"
    # Make the finding visible in BOTH directions: to the agent (ops memory is
    # injected into every session's system prompt) and to the user (notify).
    flat = " ".join(diagnosis.split())[:400]
    try:
        ws.append_operational_memory(f"[suspected issue] {flat}")
    except Exception:
        pass
    if notify:
        try:
            from aria.telegram_notify import send
            send(f"⚠ Reflection found a possible systemic issue:\n{diagnosis[:800]}")
        except Exception as exc:
            log.warning("Friction notify failed: %s", exc)
    log.info("Friction diagnosis: %s", flat)
    return ", friction: possible systemic issue flagged"


def run(notify: bool = False, *, base_url: str | None = None,
        api_key: str | None = None, model: str | None = None) -> str:
    """Run the reflection pass. Returns a status string.

    Serialised with a file lock so the supervisor's periodic job and a REPL
    background thread can't run it at the same time (which would double the LLM
    cost and clobber patterns.md / operational_memory.md).

    `base_url`/`api_key`/`model` override the default `LLM_*` env profile. The
    REPL background pass passes its active profile so reflection uses the same
    endpoint the user is actually on — the default profile may be down (which is
    often why they switched). Foreground `aria-reflect` passes nothing → env."""
    from aria import config
    from aria.workspace import Workspace

    config.load()
    ws = Workspace(config.workspace_dir())

    lock = _acquire_reflect_lock(ws)
    if lock is False:
        return "Reflection: another pass is already running — skipped."
    try:
        return _run_locked(ws, notify, base_url=base_url,
                           api_key=api_key, model=model)
    finally:
        _release_reflect_lock(lock)


def _acquire_reflect_lock(ws):
    """Exclusive non-blocking lock. Returns the open file handle, False if
    another pass holds it, or None where fcntl is unavailable (no enforcement)."""
    try:
        import fcntl
    except ImportError:
        return None
    fh = open(ws.root / "memory" / "reflect.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return False


def _release_reflect_lock(lock) -> None:
    if not lock:                       # None (no fcntl) or False (never acquired)
        return
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock.close()
    except Exception:
        pass


def _run_locked(ws, notify: bool, *, base_url: str | None = None,
                api_key: str | None = None, model: str | None = None) -> str:
    from aria.agent import _make_client

    unanalysed = ws.unanalysed_sessions(
        settle_seconds=max(0, _cfg("ARIA_REFLECT_SETTLE_MIN")) * 60)
    if not unanalysed:
        # No new sessions — but accumulated friction events alone are still
        # worth a diagnosis pass (the whole point is surfacing issues the
        # conversations themselves never mention).
        if _friction_is_hot(ws.load_friction_log() or ""):
            client = _make_client(
                base_url or os.environ["LLM_BASE_URL"],
                api_key or os.environ.get("LLM_API_KEY", "local"),
            )
            model = model or os.environ.get("LLM_MODEL", "llama3.2")
            fr_status = _phase_friction(ws, client, model, notify)
            msg = f"Reflection: no new sessions{fr_status or ''}."
            log.info(msg)
            return msg
        msg = "Reflection: no new sessions to analyse."
        log.info(msg)
        return msg

    batch_size = max(1, _cfg("ARIA_REFLECT_BATCH"))
    log.info("Reflection: %d new sessions, batches of %d", len(unanalysed), batch_size)

    client = _make_client(
        base_url or os.environ["LLM_BASE_URL"],
        api_key or os.environ.get("LLM_API_KEY", "local"),
    )
    model = model or os.environ.get("LLM_MODEL", "llama3.2")

    # ── Phase 1: extract raw observations from each batch ────────────────────
    all_observations: list[str] = []
    analysed: list[Path]        = []
    total_analysed              = 0

    for i in range(0, len(unanalysed), batch_size):
        batch    = unanalysed[i : i + batch_size]
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

        analysed.extend(batch)
        total_analysed += len(batch)

    if not all_observations:
        return "Reflection: extraction failed — no patterns updated."

    # ── Phase 2: consolidate new observations with existing patterns ──────────
    new_observations    = "\n\n".join(all_observations)
    existing_patterns   = ws.load_patterns()

    log.info("Consolidating patterns (max %d lines)...", _cfg("ARIA_REFLECT_MAX_LINES"))
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": _consolidation_prompt(new_observations, existing_patterns),
            }],
            stream=False,
        )
        consolidated = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.error("Consolidation failed: %s", exc)
        consolidated = ""
    if not consolidated:
        # Save raw observations rather than losing them (or blanking patterns.md)
        consolidated = new_observations

    ws.save_patterns(consolidated)

    if analysed:
        # Advances the watermark only across contiguous analysed sessions, so a
        # still-active (unsettled) session in between is picked up next run.
        ws.mark_sessions_analysed(analysed)

    line_count = len([l for l in consolidated.splitlines() if l.strip()])

    # ── Phase 3: consolidate operational_memory.md ────────────────────────────
    ops_status = ""
    ops_path = ws.root / "memory" / "operational_memory.md"
    ops_snapshot = ops_path.read_text(encoding="utf-8") if ops_path.exists() else ""
    # Derived from the same snapshot the merge-at-write compares against.
    current_ops = "\n".join(l for l in ops_snapshot.strip().splitlines()
                            if not l.startswith("#")).strip() or None
    if current_ops:
        log.info("Consolidating operational memory...")
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{
                    "role": "user",
                    "content": _ops_consolidation_prompt(current_ops, new_observations),
                }],
                stream=False,
            )
            consolidated_ops = (resp.choices[0].message.content or "").strip()
            # Guard: an empty completion must never wipe operational memory
            # down to a bare header (same guard as Phase 4).
            if consolidated_ops:
                from aria.workspace import _secure_write, file_lock
                with file_lock(ops_path):
                    # Re-read at write time: LEARN: entries appended by another
                    # process during the LLM call are kept, not overwritten.
                    current = ops_path.read_text(encoding="utf-8") if ops_path.exists() else ""
                    added = _new_lines(ops_snapshot, current)
                    merged = "\n".join([consolidated_ops] + added)
                    _secure_write(ops_path, "# Operational Memory\n" + merged + "\n")
                ops_lines = len([l for l in merged.splitlines() if l.strip()])
                ops_status = f", operational memory consolidated to {ops_lines} entries"
                log.info("Operational memory consolidated to %d entries.", ops_lines)
            else:
                log.warning("Operational memory consolidation returned nothing — left as-is.")
        except Exception as exc:
            log.warning("Operational memory consolidation failed: %s", exc)
    else:
        log.info("No operational memory to consolidate.")

    # ── Phase 4: consolidate core memory (conservative dedup) ─────────────────
    core_status = ""
    core_path = ws.root / "memory" / "core.md"
    core_snapshot = core_path.read_text(encoding="utf-8") if core_path.exists() else ""
    current_core = "\n".join(l for l in core_snapshot.splitlines()
                             if not l.strip().startswith("#")).strip() or None
    if current_core and not ws.core_is_empty():
        log.info("Consolidating core memory...")
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{
                    "role": "user",
                    "content": _core_consolidation_prompt(current_core),
                }],
                stream=False,
            )
            consolidated_core = (resp.choices[0].message.content or "").strip()
            # Guard: only overwrite if we got a non-empty result back, so a
            # transient error or an empty completion never wipes permanent facts.
            if consolidated_core:
                from aria.workspace import file_lock
                with file_lock(core_path):
                    # Re-read under the lock: facts remembered by another
                    # process during the LLM call are appended, not lost.
                    current = core_path.read_text(encoding="utf-8") if core_path.exists() else ""
                    added = _new_lines(core_snapshot, current)
                    consolidated_core = "\n".join([consolidated_core] + added)
                    ws.save_core_memory(consolidated_core)   # re-entrant lock
                core_lines = len([l for l in consolidated_core.splitlines() if l.strip()])
                core_status = f", core memory consolidated to {core_lines} facts"
                log.info("Core memory consolidated to %d facts.", core_lines)
        except Exception as exc:
            log.warning("Core memory consolidation failed: %s", exc)
    else:
        log.info("No core memory to consolidate.")

    # ── Phase 5: friction diagnosis (systemic-issue detection) ────────────────
    friction_status = _phase_friction(ws, client, model, notify)

    msg = (
        f"Reflection complete: {total_analysed} sessions analysed, "
        f"patterns consolidated to {line_count} lines"
        f"{ops_status}{core_status}{friction_status}."
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
