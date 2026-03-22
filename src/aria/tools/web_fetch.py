"""
tools/web_fetch.py — Fetch and extract readable text from a URL.
"""

import httpx
import re

DEFINITION = {
    "name": "web_fetch",
    "description": "Fetch the text content of a web page. Use for research, reading docs, or checking URLs.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch."},
            "max_chars": {
                "type": "integer",
                "description": "Max characters to return (default 4000).",
                "default": 4000,
            },
        },
        "required": ["url"],
    },
}


def _strip_html(html: str) -> str:
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def execute(args: dict) -> str:
    url: str = args["url"]
    max_chars: int = args.get("max_chars", 4000)
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; AgentBot/1.0)"})
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "html" in ct:
            text = _strip_html(resp.text)
        else:
            text = resp.text
        return text[:max_chars]
    except Exception as exc:
        return f"[web_fetch error] {exc}"
