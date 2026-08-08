"""
aria/tools/memory_search.py — Search across all of Aria's memory stores.

Core/operational/pattern memory is injected into the system prompt in full every
turn, which does not scale as memory grows. This tool lets the model look up a
specific fact on demand (a name, a project key, a past decision) without the
whole memory being resident in context — the first step toward retrieval-gated
memory. Read-only, so it is PARALLEL_SAFE.
"""

from __future__ import annotations

PARALLEL_SAFE = True

DEFINITION = {
    "name": "memory_search",
    "description": (
        "Search across all stored memory (core facts, operational notes, "
        "observed patterns, project notes, recent proactive messages) for a "
        "phrase and return the matching entries with their source. Use it to "
        "recall a specific detail you saved before instead of assuming — "
        "especially for facts that may not be in the current context window."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Phrase to search for (case-insensitive substring).",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum matches to return (default 20).",
            },
        },
        "required": ["query"],
    },
}


def execute(args: dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "[memory_search] No query provided."
    try:
        max_results = int(args.get("max_results") or 20)
    except (TypeError, ValueError):
        max_results = 20
    try:
        from aria import config
        from aria.workspace import Workspace

        ws = Workspace(config.workspace_dir())
        hits = ws.search_memory(query, max_results=max_results)
        if not hits:
            return f"[memory_search] No memory matched {query!r}."
        lines = [f"- ({source}) {line}" for source, line in hits]
        return f"[memory_search] {len(hits)} match(es) for {query!r}:\n" + "\n".join(lines)
    except Exception as e:
        return f"[memory_search error] {e}"
