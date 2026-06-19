"""
aria/tools/browser.py — Browser automation via Chrome DevTools Protocol (CDP).

Connects directly to Chrome/Chromium using CDP over HTTP + WebSocket.
No Playwright, no Node.js, no extra binary — just httpx (already a
dependency) and websockets (pure Python, ~50KB).

Dependencies:
  pip install websockets   # pure Python, no native code

Setup:
  # Start your browser with the debug port enabled:
  chromium --remote-debugging-port=9222 --remote-allow-origins=http://localhost
  # or Google Chrome:
  google-chrome --remote-debugging-port=9222 --remote-allow-origins=http://localhost

Optional in ~/.aria/.env:
  CHROME_PROFILE_DIR=~/snap/chromium/current/.config/chromium
  CHROME_DEBUG_PORT=9222
  ARIA_BROWSER_MAX_LOOPS=50
"""

from __future__ import annotations

import json
import os
import random
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
        "click (click an element by role/name/text), type (fill a field; set submit=true to press Enter), "
        "eval (run JavaScript directly in the page), scroll (scroll the page), "
        "back (go back), resume (continue a paused browser task), "
        "close_tab (close current tab). "
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
            "value":     {"type": "string",  "description": "Text to type into a field (for type)."},
            "submit":    {"type": "boolean", "default": False, "description": "For type: press Enter after typing to submit the field/form."},
            "direction": {"type": "string",  "enum": ["down", "up"], "default": "down", "description": "Scroll direction for the scroll action (default down)."},
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
# Scope the CDP allow-list to a fixed origin our client sends, instead of "*".
# A malicious web page can't forge its Origin header, so Chrome rejects it (403),
# while our raw client connects by sending this exact value. Verified against
# Chrome 149: evil origins are rejected, this one connects.
_CDP_ORIGIN = "http://localhost"
_PROFILE   = os.environ.get("CHROME_PROFILE_DIR", "").strip()

# Human-like interaction: real pointer paths, typing cadence, wheel scrolling,
# and occasional idle motion. On by default; ARIA_BROWSER_HUMANIZE=off reverts to
# fast direct dispatch. Kept lean so it never feels sluggish.
_HUMANIZE = os.environ.get("ARIA_BROWSER_HUMANIZE", "on").strip().lower() not in (
    "off", "0", "false", "no")
# Process-level cursor position so motion is continuous across actions.
_last_mouse = [300.0, 300.0]
# Above this length, typing skips per-char cadence (stays snappy on long values).
_FAST_TYPE_THRESHOLD = 80


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
        f"--remote-allow-origins={_CDP_ORIGIN}",
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
        f"  chromium --remote-debugging-port={_PORT} --remote-allow-origins=http://localhost"
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
        self._ws = ws_connect(self._ws_url, origin=_CDP_ORIGIN)

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
            f"  chromium --remote-debugging-port={_PORT} --remote-allow-origins=http://localhost"
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


# ── Human-like interaction ─────────────────────────────────────────────────
#
# The CDP-dispatch helpers (_move_to/_human_click_at/_type_text/_wheel_scroll)
# need a live browser. The *planners* below (_ease/_mouse_path/_target_point/
# _type_plan/_scroll_plan) are pure and unit-tested — they hold the timing and
# geometry logic so the dispatch layer stays trivial.

def _ease(t: float) -> float:
    """easeInOutQuad on [0,1] — slow-fast-slow, like a real hand."""
    return 2 * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2


def _mouse_path(start, end, steps: int | None = None):
    """Eased list of (x,y) points from start to end with small perpendicular
    jitter (max mid-path). First point steps off `start`; last point == `end`."""
    sx, sy = start
    ex, ey = end
    dist = ((ex - sx) ** 2 + (ey - sy) ** 2) ** 0.5
    if steps is None:
        steps = max(4, min(14, int(dist / 40) + 3))
    px, py = (-(ey - sy) / dist, (ex - sx) / dist) if dist else (0.0, 0.0)
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        e = _ease(t)
        x = sx + (ex - sx) * e
        y = sy + (ey - sy) * e
        if i < steps:
            j = random.uniform(-2.5, 2.5) * (1 - abs(2 * t - 1))  # 0 at ends, max mid
            x += px * j
            y += py * j
        pts.append((x, y))
    pts[-1] = (ex, ey)
    return pts


def _target_point(rect):
    """A click point inside rect — randomized off-centre when humanizing, so we
    never hit the exact pixel centre every time."""
    cx = rect["x"] + rect["width"] / 2
    cy = rect["y"] + rect["height"] / 2
    if not _HUMANIZE:
        return cx, cy
    return (cx + rect["width"] * random.uniform(-0.25, 0.25),
            cy + rect["height"] * random.uniform(-0.25, 0.25))


def _type_plan(text: str):
    """Per-char delays (seconds) for human typing cadence: a quick base with an
    occasional 'thinking' pause and a touch extra after spaces."""
    plan = []
    for ch in text:
        d = random.uniform(0.018, 0.055)
        if random.random() < 0.07:
            d += random.uniform(0.10, 0.18)
        elif ch == " ":
            d += random.uniform(0.0, 0.03)
        plan.append(d)
    return plan


def _scroll_plan(total: int):
    """Split a scroll delta into 2-4 wheel increments summing EXACTLY to total
    (single increment when not humanizing)."""
    n = random.randint(2, 4) if _HUMANIZE else 1
    if n == 1:
        return [total]
    weights = [random.uniform(0.8, 1.2) for _ in range(n)]
    s = sum(weights)
    chunks = [int(total * w / s) for w in weights]
    chunks[-1] = total - sum(chunks[:-1])   # exact remainder, no drift
    return chunks


def _move_to(session: "CDPSession", x: float, y: float) -> None:
    """Move the cursor to (x,y) — an eased, jittered path when humanizing, else a
    single hop. Updates the process-level cursor position."""
    global _last_mouse
    if _HUMANIZE:
        for px, py in _mouse_path(_last_mouse, (x, y)):
            session.send("Input.dispatchMouseEvent",
                         {"type": "mouseMoved", "x": px, "y": py, "buttons": 0})
            time.sleep(random.uniform(0.006, 0.014))
    else:
        session.send("Input.dispatchMouseEvent",
                     {"type": "mouseMoved", "x": x, "y": y, "buttons": 0})
    _last_mouse = [x, y]


def _human_click_at(session: "CDPSession", x: float, y: float) -> None:
    """Move to the point, dwell briefly, then press/release with a real gap."""
    _move_to(session, x, y)
    if _HUMANIZE:
        time.sleep(random.uniform(0.03, 0.08))
    session.send("Input.dispatchMouseEvent",
                 {"type": "mousePressed", "x": x, "y": y,
                  "button": "left", "buttons": 1, "clickCount": 1})
    time.sleep(random.uniform(0.04, 0.09) if _HUMANIZE else 0.02)
    session.send("Input.dispatchMouseEvent",
                 {"type": "mouseReleased", "x": x, "y": y,
                  "button": "left", "buttons": 0, "clickCount": 1})


def _type_text(session: "CDPSession", text: str) -> None:
    """Type via real key events. Humanized cadence for short values; a fast burst
    for long ones (and when humanizing is off) so it never feels sluggish."""
    if _HUMANIZE and len(text) <= _FAST_TYPE_THRESHOLD:
        for ch, d in zip(text, _type_plan(text)):
            session.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": ch})
            session.send("Input.dispatchKeyEvent", {"type": "char", "text": ch})
            session.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch})
            time.sleep(d)
    else:
        for ch in text:
            session.send("Input.dispatchKeyEvent", {"type": "char", "text": ch})


def _press_enter(session: "CDPSession") -> None:
    for t in ("keyDown", "keyUp"):
        session.send("Input.dispatchKeyEvent",
                     {"type": t, "key": "Enter", "code": "Enter",
                      "windowsVirtualKeyCode": 13, "text": "\r"})


def _wheel_scroll(session: "CDPSession", total_delta: int) -> None:
    x, y = _last_mouse
    for chunk in _scroll_plan(total_delta):
        session.send("Input.dispatchMouseEvent",
                     {"type": "mouseWheel", "x": x, "y": y,
                      "deltaX": 0, "deltaY": chunk})
        if _HUMANIZE:
            time.sleep(random.uniform(0.04, 0.10))


def _ambient_noise(session: "CDPSession") -> None:
    """Occasional cheap idle motion (~30%) so interaction isn't robotically
    direct — a small cursor drift or a micro-scroll. No-op when humanizing off."""
    if not _HUMANIZE or random.random() > 0.3:
        return
    if random.random() < 0.5:
        _move_to(session,
                 max(0.0, _last_mouse[0] + random.uniform(-40, 40)),
                 max(0.0, _last_mouse[1] + random.uniform(-30, 30)))
    else:
        session.send("Input.dispatchMouseEvent",
                     {"type": "mouseWheel", "x": _last_mouse[0], "y": _last_mouse[1],
                      "deltaX": 0, "deltaY": random.choice([-60, 40, 60])})
        time.sleep(0.05)


def _wait_ready(session: "CDPSession", timeout: float = 5.0) -> None:
    """Poll document.readyState until 'complete' (capped), then a small settle.
    Replaces fixed sleeps — snappier on fast pages, safer on slow ones."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = session.send("Runtime.evaluate",
                             {"expression": "document.readyState", "returnByValue": True},
                             timeout=3)
            if r.get("result", {}).get("value") == "complete":
                break
        except Exception:
            break
        time.sleep(0.1)
    time.sleep(0.2)


# Unified element finder: ranks candidates (visible, exact > startsWith >
# substring, in-viewport bonus), scrolls the winner into view, returns its rect.
# `__SPEC__` is replaced with {"selector": "..."} or {"sel": "...", "needle": "..."}.
_FINDER_JS = r"""
(function(spec){
  function vis(e){var cs=getComputedStyle(e);
    if(cs.display==='none'||cs.visibility==='hidden'||parseFloat(cs.opacity)<0.1)return false;
    var r=e.getBoundingClientRect();return r.width>=1&&r.height>=1;}
  function label(e){return ((e.getAttribute('aria-label')||e.placeholder||e.value||e.innerText||'')+'').trim().toLowerCase();}
  function rect(e){if(!e)return null;e.scrollIntoView({block:'center',inline:'center'});
    var r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height};}
  var el=null;
  if(spec.selector){ try{ el=document.querySelector(spec.selector); }catch(e){ el=null; } }
  else {
    var cands; try{ cands=Array.from(document.querySelectorAll(spec.sel)); }catch(e){ cands=[]; }
    var needle=(spec.needle||'').toLowerCase(), best=null, bs=-1;
    for(var i=0;i<cands.length;i++){var e=cands[i]; if(!vis(e))continue;
      var t=label(e); if(needle && t.indexOf(needle)<0) continue;
      var sc = t===needle?3 : (t.indexOf(needle)===0?2:1);
      var r=e.getBoundingClientRect(); if(r.top>=0&&r.bottom<=innerHeight) sc+=0.5;
      if(sc>bs){bs=sc;best=e;}}
    el=best;
  }
  return rect(el);
})(__SPEC__)
"""

# Focus an input/textbox by a fuzzy label match, scroll it in, select existing
# text (so typing replaces it). Returns true/false. `__NEEDLE__` is a JSON string.
_FOCUS_JS = r"""
(function(needle){
  var el=Array.from(document.querySelectorAll(
      'input,textarea,[contenteditable="true"],[role="textbox"],[role="searchbox"],[role="combobox"]'))
    .find(function(e){return (e.placeholder||e.name||e.id||e.getAttribute('aria-label')||'')
      .toLowerCase().includes(needle);});
  if(!el) return false;
  el.scrollIntoView({block:'center'});
  el.focus();
  try{ if(el.setSelectionRange) el.setSelectionRange(0,(el.value||'').length);
       else if(el.select) el.select(); }catch(e){}
  return true;
})(__NEEDLE__)
"""

# Fire input/change on the focused field so controlled (React/Vue) inputs commit.
_FIRE_INPUT_JS = (
    "(function(){var el=document.activeElement; if(!el) return;"
    "['input','change'].forEach(function(t){el.dispatchEvent(new Event(t,{bubbles:true}));});})()"
)


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
            # SSRF guard: always block cloud-metadata / link-local, but allow
            # loopback/private so local-dev sites still work in the browser.
            from aria.tools._net import validate_public_url, BlockedURL
            try:
                validate_public_url(url, allow_loopback=True, allow_private=True)
            except BlockedURL as exc:
                return f"[browser] Refused to open {url}: {exc}."

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

            # Wait for the page to actually finish loading (capped), not a fixed
            # sleep — faster on quick pages, safer on slow ones.
            _wait_ready(session, timeout=6)
            _ambient_noise(session)
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
                spec = {"selector": selector}
            elif role and name:
                spec = {"sel": f'[role="{role}"], {role}', "needle": name}
            elif name:
                spec = {"sel": "a,button,input,select,textarea,[role]", "needle": name}
            elif text:
                spec = {"sel": 'a,button,input,label,[role="button"],[role="link"],[role="menuitem"]',
                        "needle": text}
            else:
                return "[browser] Provide selector, role+name, name, or text to identify the element."

            # The finder ranks candidates, scrolls the winner into view, and
            # returns its (now on-screen) rect — fixes clicks on off-screen matches.
            expr = _FINDER_JS.replace("__SPEC__", json.dumps(spec))
            result = session.send("Runtime.evaluate",
                {"expression": expr, "returnByValue": True}, timeout=5)
            rect = result.get("result", {}).get("value")

            if not rect:
                snap  = _get_snapshot(session)
                label = selector or name or text or f"{role}+{name}"
                return f"[browser] Element not found: '{label}'\n\nCurrent page:\n{snap}"

            _ambient_noise(session)
            x, y = _target_point(rect)
            _human_click_at(session, x, y)
            _wait_ready(session, timeout=4)
            return _get_snapshot(session)

        case "type":
            value  = args.get("value", "")
            if not value:
                return "[browser] 'value' is required for type."
            name   = args.get("name", "")
            submit = bool(args.get("submit", False))

            if name:
                # Focus + scroll-in + select existing text (so typing replaces it).
                focus_expr = _FOCUS_JS.replace("__NEEDLE__", json.dumps(name.lower()))
                fr = session.send("Runtime.evaluate",
                    {"expression": focus_expr, "returnByValue": True}, timeout=5)
                if not fr.get("result", {}).get("value"):
                    snap = _get_snapshot(session)
                    return f"[browser] No input field matching '{name}'.\n\nCurrent page:\n{snap}"

            _type_text(session, value)
            # Commit for controlled (React/Vue) inputs that ignore raw key events.
            session.send("Runtime.evaluate", {"expression": _FIRE_INPUT_JS, "returnByValue": False})

            field_label = name or "focused field"
            if submit:
                _press_enter(session)
                _wait_ready(session, timeout=4)
                return (f"Typed {len(value)} chars into [{field_label}] and pressed Enter\n\n"
                        f"{_get_snapshot(session)}")
            return f"Typed {len(value)} chars into [{field_label}]"

        case "scroll":
            direction = args.get("direction", "down")
            amount    = int(args.get("amount", 500))
            delta     = amount if direction == "down" else -amount
            _wheel_scroll(session, delta)
            time.sleep(0.3)
            return _get_snapshot(session)

        case "back":
            session.send("Runtime.evaluate", {
                "expression": "window.history.back()",
                "returnByValue": False,
            })
            _wait_ready(session, timeout=4)
            return _get_snapshot(session)

        case "close_tab":
            url = _get_url(session)
            session.send("Runtime.evaluate",
                {"expression": "window.close()", "returnByValue": False})
            return f"[browser] Tab closed: {url}"

        case _:
            return f"[browser] Unknown action: {action}"
