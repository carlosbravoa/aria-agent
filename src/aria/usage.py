"""
aria/usage.py — Read and summarize persisted token usage.

Every model call appends a JSON line to ~/.aria/usage.jsonl (see
Agent._persist_usage): timestamp, model, profile, channel, in/out tokens.
Previously these counts lived only in a per-process counter and were lost at
exit, so per-model / per-channel cost was unknowable. This module aggregates the
log for the `aria --usage` CLI flag and the `/usage` REPL command.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def usage_path() -> Path:
    return Path.home() / ".aria" / "usage.jsonl"


def load_usage(path: str | Path | None = None) -> list[dict]:
    """Load usage records, skipping any malformed lines."""
    p = Path(path) if path else usage_path()
    if not p.exists():
        return []
    records: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                records.append(rec)
        except (ValueError, TypeError):
            continue
    return records


def summarize(records: list[dict]) -> dict:
    """Aggregate records into totals and per-model / per-channel breakdowns."""
    out: dict[str, Any] = {"calls": len(records), "in": 0, "out": 0,
           "by_model": {}, "by_channel": {}}
    for r in records:
        tin  = int(r.get("in", 0) or 0)
        tout = int(r.get("out", 0) or 0)
        out["in"]  += tin
        out["out"] += tout
        for key, dim in (("model", "by_model"), ("channel", "by_channel")):
            name = str(r.get(key, "?"))
            slot = out[dim].setdefault(name, {"calls": 0, "in": 0, "out": 0})
            slot["calls"] += 1
            slot["in"]    += tin
            slot["out"]   += tout
    return out


def format_report(records: list[dict] | None = None) -> str:
    """A plain-text usage report suitable for the REPL or stdout."""
    if records is None:
        records = load_usage()
    if not records:
        return "No usage recorded yet (~/.aria/usage.jsonl is empty)."
    s = summarize(records)
    total = s["in"] + s["out"]
    lines = [
        f"Lifetime usage — {s['calls']:,} calls · "
        f"{s['in']:,} in / {s['out']:,} out / {total:,} total tokens",
    ]

    def _section(title: str, dim: dict) -> None:
        lines.append("")
        lines.append(f"{title}:")
        for name, v in sorted(dim.items(),
                              key=lambda kv: -(kv[1]["in"] + kv[1]["out"])):
            lines.append(f"  {name:24.24} {v['calls']:>6,} calls  "
                         f"{v['in'] + v['out']:>12,} tok")

    _section("By model", s["by_model"])
    _section("By channel", s["by_channel"])
    return "\n".join(lines)
