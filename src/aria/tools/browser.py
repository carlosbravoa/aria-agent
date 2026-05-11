"""
aria/tools/browser.py — Browser automation via Chrome DevTools Protocol (CDP).

Connects directly to Chrome/Chromium using CDP over HTTP + WebSocket.
No Playwright, no Node.js, no extra binary — just httpx (already a
dependency) and websockets (pure Python, ~50KB).

Dependencies:
  pip install websockets   # pure Python, no native code

Setup:
  # Start your browser with the debug port enabled:
  chromium --remote-debugging-port=9222 --remote-allow-origins=*
  # or Google Chrome:
  google-chrome --remote-debugging-port=9222 --remote-allow-origins=*

Optional in ~/.aria/.env:
  CHROME_PROFILE_DIR=~/snap/chromium/current/.config/chromium
  CHROME_DEBUG_PORT=9222
  ARIA_BROWSER_MAX_LOOPS=50
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

DEFINITION = {
    "name": "browser",
    "description": (
        "Control a Chrome/Chromium browser via CDP to perform web tasks on the user's behalf. "
        "Uses the user's real browser with existing sessions and cookies. "
        "Use this for any website — including Gmail (mail.google.com), GitHub, Jira, "
        "or any site where the user is already logged in. "
        "Prefer dedicated tools (gmail, jira) when configured, use browser as fallback or "
        "when the user explicitly asks to use the browser. "
        "Actions: open (navigate to URL), snapshot (get page structure for interaction), "
        "read (extract full readable text content — use for articles, emails, docs), "
        "click (click an element by role/name/text), type (fill a field), "
        "scroll (scroll the page), back (go back), "
        "resume (continue a paused browser task), close_tab (close current tab). "
        "Always call snapshot after navigating or clicking to see the current page state. "
        "If the page uses canvas or has no accessible content, report that to the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["open", "snapshot", "read", "eval", "click", "type",
                         "scroll", "back", "resume", "close_tab"],
                "description": (
                    "snapshot = page structure for interaction (buttons, links, inputs). "
                    "read = full readable text content (articles, emails, docs). "
                    "eval = run JavaScript directly in the page — can click, extract data, modify DOM. "
                    "Use eval for canvas apps, complex SPAs, or anything snapshot cannot reach."
                ),
            },
            "url":       {"type": "string",  "description": "URL to navigate to (for open)."},
            "role":      {"type": "string",  "description": "ARIA role of element (button, link, textbox...)."},
            "name":      {"type": "string",  "description": "Accessible name or label of the element."},
            "text":      {"type": "string",  "description": "Text content to match (alternative to role+name)."},
            "value":     {"type": "string",  "description": "Text to type into a field."},
            "direction": {"type": "string",  "enum": ["down", "up"], "default": "down"},
            "amount":    {"type": "integer", "description": "Scroll pixels (default 500).", "default": 500},
            "progress":  {"type": "string",  "description": "Task progress note (saved for resume)."},
            "selector":  {"type": "string",  "description": "CSS selector to scope snapshot to a specific part of the page."},
            "script":    {"type": "string",  "description": "JavaScript to run for eval action. Can return JSON-serialisable data, or null for side-effects (click, focus, etc.)"},
        },
        "required": ["action"],
    },
}

_PORT      = int(os.environ.get("CHROME_DEBUG_PORT", "9222"))
_CDP_HTTP  = f"http://localhost:{_PORT}"
_STATE     = Path.home() / ".aria" / "browser_state.json"
_PROFILE   = os.environ.get("CHROME_PROFILE_DIR", "").strip()


# ── Chrome detection ──────────────────────────────────────────────────────────

def _cdp_available() -> bool:
    try:
        import httpx
        r = httpx.get(f"{_CDP_HTTP}/json/version", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def _browser_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-f", "chromium|chrome"], capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


def _launch_browser() -> str | None:
    """Launch browser with CDP port. Returns error string or None on success."""
    # Build binary list — snap chromium, deb chrome, generic chromium
    candidates = ["chromium", "google-chrome", "google-chrome-stable", "chromium-browser"]
    binary = next((b for b in candidates
                   if subprocess.run(["which", b], capture_output=True).returncode == 0), None)
    if not binary:
        return (
            "No browser found. Install one:\n"
            "  snap install chromium\n"
            "  sudo apt install chromium-browser"
        )

    args = [
        binary,
        f"--remote-debugging-port={_PORT}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if _PROFILE:
        profile = Path(_PROFILE).expanduser().resolve()
        # Clear stale lock
        lock = profile / "SingletonLock"
        if lock.exists() and not _browser_running():
            lock.unlink(missing_ok=True)
        args.append(f"--user-data-dir={profile}")

    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for _ in range(20):
        time.sleep(0.5)
        if _cdp_available():
            return None

    return (
        f"Browser launched but CDP not reachable on port {_PORT}.\n"
        "Try launching manually:\n"
        f"  chromium --remote-debugging-port={_PORT} --remote-allow-origins=*"
    )


# ── CDP client ────────────────────────────────────────────────────────────────

class CDPSession:
    """
    Minimal synchronous CDP client over WebSocket.
    Uses only httpx (already a dep) and websockets (pure Python).
    """

    def __init__(self, ws_url: str):
        self._ws_url = ws_url
        self._ws     = None
        self._id     = 0

    def connect(self) -> None:
        try:
            from websockets.sync.client import connect as ws_connect
        except ImportError:
            raise RuntimeError(
                "websockets not installed.\n"
                "Install with: pip install websockets"
            )
        self._ws = ws_connect(self._ws_url)

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def send(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        self._id += 1
        msg = json.dumps({"id": self._id, "method": method, "params": params or {}})
        self._ws.send(msg)
        # Drain messages until we get our response
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._ws.socket.settimeout(deadline - time.monotonic())
                raw = self._ws.recv()
                data = json.loads(raw)
                if data.get("id") == self._id:
                    if "error" in data:
                        raise RuntimeError(f"CDP error: {data['error']}")
                    return data.get("result", {})
            except TimeoutError:
                break
        raise TimeoutError(f"CDP timeout waiting for response to {method}")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()


def _get_session() -> CDPSession:
    """Connect to the active tab in Chrome. Handles all three states."""
    # Check websockets available first
    try:
        import websockets  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "websockets not installed.\n"
            "Install with: pip install websockets"
        )

    # State 1: CDP already available
    if _cdp_available():
        import httpx
        tabs = httpx.get(f"{_CDP_HTTP}/json", timeout=3).json()
        # Pick the first non-devtools tab
        tab = next(
            (t for t in tabs if t.get("type") == "page" and "devtools" not in t.get("url", "")),
            tabs[0] if tabs else None
        )
        if not tab:
            raise RuntimeError("No tabs found. Open a tab in your browser first.")
        session = CDPSession(tab["webSocketDebuggerUrl"])
        session.connect()
        return session

    # State 2: Browser running without debug port
    if _browser_running():
        raise RuntimeError(
            f"Browser is running but CDP port {_PORT} is not available.\n"
            "Please close the browser and I will relaunch it with debugging enabled.\n"
            "Or relaunch manually:\n"
            f"  chromium --remote-debugging-port={_PORT} --remote-allow-origins=*"
        )

    # State 3: Launch browser
    err = _launch_browser()
    if err:
        raise RuntimeError(err)

    import httpx
    tabs = httpx.get(f"{_CDP_HTTP}/json", timeout=3).json()
    tab  = next((t for t in tabs if t.get("type") == "page"), tabs[0] if tabs else None)
    if not tab:
        raise RuntimeError("Browser launched but no tabs found.")
    session = CDPSession(tab["webSocketDebuggerUrl"])
    session.connect()
    return session


# ── Accessibility tree ────────────────────────────────────────────────────────

# ── Viewport-based accessibility snapshot ────────────────────────────────────
#
# Instead of the full accessibility tree (which can be thousands of nodes on
# complex SPAs like Gmail), we only look at what is currently visible in the
# viewport. This is bounded by screen size, not page complexity, and works
# the same way on any page — no site-specific code needed.
#
# Approach:
#   1. Use JS to get viewport bounds and find all elements with visible
#      bounding boxes within those bounds.
#   2. For each visible element, fetch its accessibility node via
#      Accessibility.queryAXTree (targeted, not full tree).
#   3. Format into compact readable lines.

_VISIBLE_ELEMENTS_JS = r"""
(function() {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const seen = new Set();
    const results = [];

    // Only interactive/semantic elements — avoids div/span/p which leak script content
    const all = document.querySelectorAll(
        'a[href], button, input, select, textarea, ' +
        '[role="button"], [role="link"], [role="menuitem"], [role="tab"], ' +
        '[role="checkbox"], [role="radio"], [role="combobox"], [role="searchbox"], ' +
        '[role="textbox"], [role="option"], [role="switch"], ' +
        'h1, h2, h3, h4, h5, label, th, [aria-label], [aria-labelledby]'
    );

    for (const el of all) {
        // Skip hidden elements
        const cs = window.getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden' ||
            parseFloat(cs.opacity) < 0.1) continue;

        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        if (r.bottom < 0 || r.top > vh) continue;
        if (r.right  < 0 || r.left > vw) continue;

        // Skip if already covered by an ancestor
        let skip = false;
        for (const s of seen) {
            if (s !== el && s.contains(el)) { skip = true; break; }
        }
        if (skip) continue;

        const tag  = el.tagName.toLowerCase();
        const type = el.getAttribute('type') || '';
        const aria = el.getAttribute('aria-label') || '';
        const ttl  = el.getAttribute('title') || '';
        // Use innerText (rendered text only — never includes script content)
        const inner = (el.innerText || '').trim().replace(/[ \t\n\r]+/g, ' ').slice(0, 120);
        const text  = (aria || ttl || inner).trim();

        // Skip no-label elements (except self-describing inputs)
        if (!text && !['input', 'select', 'textarea'].includes(tag)) continue;

        // Skip if text looks like leaked code
        if (/^(var |function |const |let |\{|\()/.test(text)) continue;

        const role        = el.getAttribute('role') || tag;
        const val         = (el.value !== undefined && el.value !== '') ? String(el.value).slice(0, 80) : '';
        const href        = el.href ? el.href.slice(0, 100) : '';
        const placeholder = el.placeholder || '';

        results.push({ role, tag, type, text, val, href, placeholder,
                       top: Math.round(r.top), left: Math.round(r.left) });
        seen.add(el);
        if (results.length >= 100) break;
    }
    return results;
})()
"""



def _get_snapshot(session: CDPSession, selector: str = "") -> str:
    """
    Get only the elements currently visible in the browser viewport.
    Bounded by screen size, not page complexity — works on any page.
    """
    url = _get_url(session)

    try:
        if selector:
            # Scroll selector into view first
            session.send("Runtime.evaluate", {
                "expression": f"document.querySelector({json.dumps(selector)})?.scrollIntoView()",
                "returnByValue": False,
            })
            time.sleep(0.3)

        result = session.send("Runtime.evaluate", {
            "expression": _VISIBLE_ELEMENTS_JS,
            "returnByValue": True,
            "awaitPromise": False,
        }, timeout=10)

        elements = result.get("result", {}).get("value") or []

        if not elements:
            return _diagnose_page(session)

        lines = _format_visible_elements(elements)
        text  = "\n".join(lines)

        if len(elements) >= 120:
            text += "\n… [viewport has more — use scroll to see below]"

        return f"URL: {url}\n\n{text}"

    except Exception as e:
        return f"[browser] Snapshot failed: {e}\nURL: {url}"


def _format_visible_elements(elements: list) -> list[str]:
    """Format visible DOM elements into compact readable lines."""
    # Meaningful roles/tags to surface explicitly
    interactive = {"button", "a", "input", "select", "textarea",
                   "link", "menuitem", "checkbox", "radio", "combobox",
                   "searchbox", "option", "tab", "switch"}
    heading_tags = {"h1", "h2", "h3", "h4"}

    lines = []
    seen_texts: set[str] = set()

    for el in elements:
        role = el.get("role", "")
        tag  = el.get("tag",  "")
        text = el.get("text", "").strip()
        val  = el.get("val",  "").strip()
        href = el.get("href", "")
        etype = el.get("type", "")

        if not text and not val:
            continue

        # Deduplicate identical text entries
        key = (role or tag, text[:40])
        if key in seen_texts:
            continue
        seen_texts.add(key)

        # Format by type
        if tag in heading_tags:
            lines.append(f"{'#' * int(tag[1])} {text}")
        elif tag == "a" or role == "link":
            lines.append(f"[link] {text}")
        elif tag == "button" or role == "button":
            lines.append(f"[button] {text}")
        elif tag == "input":
            if etype in ("submit", "button"):
                lines.append(f"[button] {text or val}")
            elif etype in ("checkbox", "radio"):
                lines.append(f"[{etype}] {text}")
            else:
                placeholder = text or etype or "input"
                lines.append(f"[input] {placeholder}" + (f" = {val}" if val else ""))
        elif tag == "textarea":
            lines.append(f"[textarea] {text}" + (f" = {val}" if val else ""))
        elif tag == "select":
            lines.append(f"[select] {text}" + (f" = {val}" if val else ""))
        elif role in interactive:
            lines.append(f"[{role}] {text}")
        elif text:
            lines.append(text)

    return lines



def _diagnose_page(session: CDPSession) -> str:
    url = _get_url(session)
    try:
        has_canvas  = session.send("Runtime.evaluate",
            {"expression": "document.querySelectorAll('canvas').length > 0",
             "returnByValue": True})["result"]["value"]
        has_iframes = session.send("Runtime.evaluate",
            {"expression": "document.querySelectorAll('iframe').length > 0",
             "returnByValue": True})["result"]["value"]
    except Exception:
        has_canvas = has_iframes = False

    msg = f"URL: {url}\n\n"
    if has_canvas:
        msg += (
            "⚠ This page uses canvas rendering — no accessibility tree available.\n"
            "Canvas-based UIs cannot be automated via the accessibility tree."
        )
    elif has_iframes:
        msg += (
            "⚠ This page contains iframes. Content inside may not be visible in the snapshot.\n"
            "Try scrolling or interacting with the main page elements first."
        )
    else:
        msg += "⚠ Page accessibility tree is empty or not yet loaded. Try again in a moment."
    return msg


def _get_url(session: CDPSession) -> str:
    try:
        r = session.send("Runtime.evaluate",
            {"expression": "window.location.href", "returnByValue": True})
        return r.get("result", {}).get("value", "unknown")
    except Exception:
        return "unknown"


# ── State persistence ─────────────────────────────────────────────────────────

def _save_state(url: str, progress: str) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps({
        "url": url, "progress": progress,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }), encoding="utf-8")


def _load_state() -> dict | None:
    if _STATE.exists():
        try:
            return json.loads(_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


# ── Main execute ──────────────────────────────────────────────────────────────

def execute(args: dict) -> str:
    action = args["action"]

    if action == "resume":
        state = _load_state()
        if not state:
            return "[browser] No saved browser task to resume."
        return (
            f"Resuming browser task.\n"
            f"Last URL: {state['url']}\n"
            f"Progress: {state['progress']}\n"
            f"Saved at: {state['saved_at']}\n\n"
            "Reconnecting to browser..."
        )

    try:
        session = _get_session()
    except RuntimeError as e:
        return f"[browser] {e}"

    with session:
        if args.get("progress"):
            _save_state(_get_url(session), args["progress"])

        try:
            return _execute_action(session, action, args)
        except Exception as e:
            return f"[browser] Action '{action}' failed: {e}"


def _execute_action(session: CDPSession, action: str, args: dict) -> str:

    match action:

        case "open":
            url = args.get("url", "")
            if not url:
                return "[browser] 'url' is required for open."
            if not url.startswith(("http://", "https://")):
                url = "https://" + url

            # Open in a new tab — don't clobber whatever the user has open
            try:
                import httpx
                # Use CDP /json/new endpoint to create a new tab
                r = httpx.get(f"{_CDP_HTTP}/json/new?{url}", timeout=5)
                new_tab = r.json()
                ws_url  = new_tab.get("webSocketDebuggerUrl")
                if not ws_url:
                    raise RuntimeError("No WebSocket URL in new tab response")
                # Reconnect session to the new tab
                session.close()
                session._ws_url = ws_url
                session.connect()
            except Exception as e:
                # Fallback: navigate in current tab
                session.send("Page.navigate", {"url": url}, timeout=15)

            # Wait for page to settle
            time.sleep(2)
            return _get_snapshot(session)

        case "snapshot":
            return _get_snapshot(session, selector=args.get("selector", ""))

        case "eval":
            # Run JavaScript to extract specific data.
            # The agent writes targeted JS for the specific page/task,
            # avoiding the need to parse the full accessibility tree.
            script = args.get("script", "")
            if not script:
                return "[browser] 'script' is required for eval action."
            try:
                result = session.send("Runtime.evaluate", {
                    "expression": script,
                    "returnByValue": True,
                    "awaitPromise": True,
                }, timeout=10)
                value = result.get("result", {}).get("value")
                if value is None:
                    return "[browser] Query returned no result."
                if isinstance(value, (dict, list)):
                    return json.dumps(value, ensure_ascii=False, indent=2)
                return str(value)
            except Exception as e:
                return f"[browser] Query failed: {e}"

        case "read":
            # Extract readable text from the page.
            # Uses the main content element's innerText (fast, bounded)
            # rather than full outerHTML (can be megabytes on SPAs).
            # Falls back to trafilatura on outerHTML only if innerText is thin.
            url = _get_url(session)

            # Try to get innerText of the main content area
            inner_text_js = """
(function() {
    // Try semantic content elements first
    const candidates = [
        'main', 'article', '[role="main"]', '#content',
        '.content', '.main', 'body'
    ];
    for (const sel of candidates) {
        const el = document.querySelector(sel);
        if (el) {
            const t = el.innerText;
            if (t && t.trim().length > 200) return t.trim();
        }
    }
    return document.body.innerText.trim();
})()
"""
            result = session.send("Runtime.evaluate", {
                "expression": inner_text_js,
                "returnByValue": True,
            }, timeout=10)
            text = result.get("result", {}).get("value", "").strip()

            # If innerText is thin (JS-heavy SPA with no text nodes),
            # fall back to trafilatura on outerHTML
            if len(text) < 200:
                html_result = session.send("Runtime.evaluate", {
                    "expression": "document.documentElement.outerHTML",
                    "returnByValue": True,
                }, timeout=10)
                html = html_result.get("result", {}).get("value", "")
                try:
                    import trafilatura
                    text = trafilatura.extract(
                        html, url=url,
                        include_links=False, include_tables=True,
                        no_fallback=False, favor_recall=True,
                    ) or text
                except ImportError:
                    pass

            if not text:
                return f"[browser] No readable content found.\n\n{_get_snapshot(session)}"

            if len(text) > 6000:
                text = text[:6000] + "\n… [content truncated — use scroll to read more]"

            return f"URL: {url}\n\n{text}"

        case "click":
            role     = args.get("role", "")
            name     = args.get("name", "")
            text     = args.get("text", "")
            selector = args.get("selector", "")

            if selector:
                expr = (
                    f"(function(){{"
                    f"  var el = document.querySelector({json.dumps(selector)});"
                    f"  return el ? el.getBoundingClientRect() : null;"
                    f"}})()"
                )
            elif role and name:
                expr = (
                    f"(function(){{"
                    f"  var role = {json.dumps(role)};"
                    f"  var name = {json.dumps(name.lower())};"
                    f"  var els = document.querySelectorAll('[role="' + role + '"], ' + role);"
                    f"  var el = Array.from(els).find(e => "
                    f"    (e.innerText||e.getAttribute('aria-label')||'').toLowerCase().includes(name));"
                    f"  return el ? el.getBoundingClientRect() : null;"
                    f"}})()"
                )
            elif name:
                expr = (
                    f"(function(){{"
                    f"  var name = {json.dumps(name.lower())};"
                    f"  var candidates = document.querySelectorAll("
                    f"    'a,button,input,select,textarea,[role]');"
                    f"  var el = Array.from(candidates).find(e => "
                    f"    (e.getAttribute('aria-label')||e.placeholder||"
                    f"     e.innerText||'').toLowerCase().includes(name));"
                    f"  return el ? el.getBoundingClientRect() : null;"
                    f"}})()"
                )
            elif text:
                expr = (
                    f"(function(){{"
                    f"  var txt = {json.dumps(text.lower())};"
                    f"  var candidates = document.querySelectorAll("
                    f"    'a,button,input,label,[role=\"button\"],[role=\"link\"],[role=\"menuitem\"]');"
                    f"  var el = Array.from(candidates).find(e => "
                    f"    (e.innerText||e.getAttribute('aria-label')||'').toLowerCase().includes(txt));"
                    f"  return el ? el.getBoundingClientRect() : null;"
                    f"}})()"
                )
            else:
                return "[browser] Provide selector, role+name, name, or text to identify the element."

            result = session.send("Runtime.evaluate",
                {"expression": expr, "returnByValue": True}, timeout=5)
            rect = result.get("result", {}).get("value")

            if not rect:
                snap  = _get_snapshot(session)
                label = selector or name or text or f"{role}+{name}"
                return f"[browser] Element not found: '{label}'\n\nCurrent page:\n{snap}"

            x = rect["x"] + rect["width"]  / 2
            y = rect["y"] + rect["height"] / 2

            for etype in ["mousePressed", "mouseReleased"]:
                session.send("Input.dispatchMouseEvent", {
                    "type": etype, "x": x, "y": y,
                    "button": "left", "clickCount": 1,
                })
            time.sleep(1)
            return _get_snapshot(session)

        case "type":
            value = args.get("value", "")
            if not value:
                return "[browser] 'value' is required for type."
            name = args.get("name", "")

            if name:
                # Focus the field first
                expr = (
                    f"(function(){{"
                    f"  var el = Array.from(document.querySelectorAll('input,textarea'))"
                    f"    .find(e => (e.placeholder||e.name||e.id||e.getAttribute('aria-label')||'').toLowerCase().includes({json.dumps(name.lower())}));"
                    f"  if(el) {{ el.focus(); return true; }}"
                    f"  return false;"
                    f"}})()"
                )
                session.send("Runtime.evaluate",
                    {"expression": expr, "returnByValue": True}, timeout=5)

            # Type each character
            for char in value:
                session.send("Input.dispatchKeyEvent", {
                    "type": "char", "text": char,
                })
            field_label = args.get("name", "") or "focused field"
            return f"Typed {len(value)} chars into [{field_label}]"

        case "scroll":
            direction = args.get("direction", "down")
            amount    = int(args.get("amount", 500))
            delta     = amount if direction == "down" else -amount
            session.send("Runtime.evaluate", {
                "expression": f"window.scrollBy(0, {delta})",
                "returnByValue": False,
            })
            time.sleep(0.5)
            return _get_snapshot(session)

        case "back":
            session.send("Runtime.evaluate", {
                "expression": "window.history.back()",
                "returnByValue": False,
            })
            time.sleep(1.5)
            return _get_snapshot(session)

        case "close_tab":
            url = _get_url(session)
            session.send("Runtime.evaluate",
                {"expression": "window.close()", "returnByValue": False})
            return f"[browser] Tab closed: {url}"

        case _:
            return f"[browser] Unknown action: {action}"
