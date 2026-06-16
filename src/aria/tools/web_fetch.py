"""
aria/tools/web_fetch.py — Fetch and extract readable text from a URL.

Uses trafilatura for content extraction when available — the same approach
used by Firefox Reader Mode (@mozilla/readability). It identifies the main
content block (article body, documentation text) and discards navigation,
ads, footers, and other noise. Dramatically better signal-to-noise ratio
compared to plain HTML stripping for editorial and documentation content.

Falls back to regex-based HTML stripping if trafilatura is unavailable.
"""

from __future__ import annotations

import re
import httpx

DEFINITION = {
    "name": "web_fetch",
    "description": (
        "Fetch the readable text content of a web page. "
        "Extracts main article/documentation content, stripping navigation, "
        "ads, and boilerplate. Use for research, reading articles, checking docs, "
        "or summarising web pages."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Max characters to return (default 4000).",
                "default": 4000,
            },
            "include_links": {
                "type": "boolean",
                "description": "Include hyperlinks in output (default false).",
                "default": False,
            },
        },
        "required": ["url"],
    },
}


def _strip_html(html: str) -> str:
    """Fallback regex-based HTML stripper."""
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract(html: str, url: str, include_links: bool) -> str:
    """
    Extract main content using trafilatura (preferred) or regex fallback.
    trafilatura uses the same heuristics as Firefox Reader Mode:
    content density, paragraph length, link density, semantic HTML roles.
    """
    try:
        import trafilatura

        result = trafilatura.extract(
            html,
            url=url,
            include_links=include_links,
            include_images=False,
            include_tables=True,
            no_fallback=False,     # use fallback extractors if main fails
            favor_recall=True,     # prefer more content over precision
        )
        if result and len(result.strip()) > 100:
            return result.strip()
        # trafilatura returned nothing useful — fall back
    except ImportError:
        pass

    return _strip_html(html)


def execute(args: dict) -> str:
    url          = args["url"]
    max_chars    = int(args.get("max_chars", 4000))
    include_links = bool(args.get("include_links", False))

    try:
        from aria.tools._net import safe_get, BlockedURL
        try:
            resp = safe_get(
                url,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (compatible; AgentBot/1.0)"},
            )
        except BlockedURL as exc:
            return f"[web_fetch] Refused: {exc}. Only public http(s) URLs are allowed."
        resp.raise_for_status()

        ct = resp.headers.get("content-type", "")
        if "html" in ct:
            text = _extract(resp.text, url, include_links)
        else:
            # Plain text, JSON, XML — return as-is
            text = resp.text.strip()

        if not text:
            return "[web_fetch] Page returned no extractable content."

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n… [truncated — {len(text)} chars total]"

        return text

    except httpx.HTTPStatusError as exc:
        return f"[web_fetch error] HTTP {exc.response.status_code}: {url}"
    except httpx.TimeoutException:
        return f"[web_fetch error] Timeout fetching {url}"
    except Exception as exc:
        return f"[web_fetch error] {exc}"
