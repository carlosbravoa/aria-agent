"""
Characterization tests pinning channel-helper behaviour across the
no-behaviour-change refactor (shared feed recording, allow-list parsing,
/model rendering, side-effect-free import of aria.main).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"

# Exercises whitespace, empties, '+' prefixes, negatives, duplicates, order.
_RAW = " 34600111222 , ,+34600333444,34600111222,-100123,  7 ,abc,, "


# ── 1. notify feed recording ────────────────────────────────────────────────

@pytest.mark.parametrize("modname", ["aria.telegram_notify", "aria.whatsapp_notify"])
def test_record_feed_appends_to_notify_feed(minimal_env, modname):
    import importlib
    mod = importlib.import_module(modname)
    mod._record_feed("first push")
    mod._record_feed("second push")
    text = (minimal_env / "memory" / "notify_feed.md").read_text()
    assert "first push" in text and "second push" in text
    assert text.index("first push") < text.index("second push")


@pytest.mark.parametrize("modname", ["aria.telegram_notify", "aria.whatsapp_notify"])
def test_record_feed_swallows_errors(minimal_env, monkeypatch, modname):
    import importlib
    mod = importlib.import_module(modname)
    import aria.workspace

    def boom(self, text):
        raise OSError("disk full")

    monkeypatch.setattr(aria.workspace.Workspace, "append_notify_feed", boom)
    assert mod._record_feed("x") is None


def test_record_feed_same_file_for_both_channels(minimal_env):
    from aria import telegram_notify, whatsapp_notify
    telegram_notify._record_feed("from-tg")
    whatsapp_notify._record_feed("from-wa")
    text = (minimal_env / "memory" / "notify_feed.md").read_text()
    assert text.index("from-tg") < text.index("from-wa")


# ── 2. allow-list parsing ───────────────────────────────────────────────────

def test_telegram_notify_chat_ids(monkeypatch):
    from aria import telegram_notify as tn
    monkeypatch.setenv("TELEGRAM_ALLOWED", _RAW)
    # isdigit() filter: '+', '-' and non-numeric entries dropped; dups kept.
    assert tn._chat_ids() == [34600111222, 34600111222, 7]


@pytest.mark.parametrize("raw", ["", " , ,", "abc,-5,+1"])
def test_telegram_notify_chat_ids_empty_raises(monkeypatch, raw):
    from aria import telegram_notify as tn
    monkeypatch.setenv("TELEGRAM_ALLOWED", raw)
    with pytest.raises(RuntimeError, match="TELEGRAM_ALLOWED not set. Add chat IDs to ~/.aria/.env"):
        tn._chat_ids()


def test_telegram_notify_chat_ids_unset_raises(monkeypatch):
    from aria import telegram_notify as tn
    monkeypatch.delenv("TELEGRAM_ALLOWED", raising=False)
    with pytest.raises(RuntimeError):
        tn._chat_ids()


def test_whatsapp_notify_allowed(monkeypatch):
    from aria import whatsapp_notify as wn
    monkeypatch.setenv("WHATSAPP_ALLOWED", _RAW)
    assert wn._allowed() == ["34600111222", "+34600333444", "34600111222",
                             "-100123", "7", "abc"]


@pytest.mark.parametrize("raw", ["", " , , "])
def test_whatsapp_notify_allowed_empty_raises(monkeypatch, raw):
    from aria import whatsapp_notify as wn
    monkeypatch.setenv("WHATSAPP_ALLOWED", raw)
    with pytest.raises(RuntimeError, match="WHATSAPP_ALLOWED not set. Add numbers to ~/.aria/.env"):
        wn._allowed()


def test_whatsapp_bridge_allowed(monkeypatch):
    from aria import whatsapp_bridge as wa
    monkeypatch.setenv("WHATSAPP_ALLOWED", _RAW)
    got = wa._allowed()
    assert isinstance(got, set)
    assert got == {"34600111222", "+34600333444", "-100123", "7", "abc"}
    monkeypatch.setenv("WHATSAPP_ALLOWED", " , ")
    assert wa._allowed() == set()
    monkeypatch.delenv("WHATSAPP_ALLOWED")
    assert wa._allowed() == set()


@pytest.mark.parametrize("chat_id, expected", [
    (34600111222, True), ("+34600333444", True), (-100123, True), (7, True),
    ("abc", True), (34600333444, False), (8, False), ("", False), (" 7", False),
])
def test_telegram_bot_is_allowed(monkeypatch, chat_id, expected):
    from aria import telegram_bot as tb
    monkeypatch.setenv("TELEGRAM_ALLOWED", _RAW)
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id))
    assert tb._is_allowed(upd) is expected  # type: ignore[arg-type]


def test_telegram_bot_is_allowed_unset(monkeypatch):
    from aria import telegram_bot as tb
    monkeypatch.delenv("TELEGRAM_ALLOWED", raising=False)
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=""))
    assert tb._is_allowed(upd) is False  # type: ignore[arg-type]


# ── 3. /model rendering per channel ─────────────────────────────────────────

class _FakeAgent:
    name = "Aria"

    def __init__(self):
        self.switched: list[str] = []
        self.closed = False

    def list_profiles(self):
        return [
            {"name": "default", "model": "m-default", "active": False},
            {"name": "fast", "model": "m-fast", "active": True},
            {"name": "averyveryverylongname", "model": "m-long", "active": False},
        ]

    def switch_profile(self, name):
        self.switched.append(name)
        return f"Switched to {name!r}"

    def close(self):
        self.closed = True


def _run_repl(monkeypatch, inputs):
    from rich.console import Console
    from aria import main as M
    buf = io.StringIO()
    con = Console(theme=M._THEME, highlight=False, file=buf, width=100,
                  force_terminal=False, color_system=None)
    monkeypatch.setattr(M, "console", con)
    monkeypatch.setattr(M, "_make_prompt_session", lambda agent=None: None)
    monkeypatch.setattr(M, "_print_banner", lambda agent: None)
    it = iter(inputs)

    def fake_prompt(session, waker=None):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(M, "_prompt", fake_prompt)
    agent = _FakeAgent()
    M.repl(agent)  # type: ignore[arg-type]
    return buf.getvalue(), agent


def test_repl_models_listing(monkeypatch):
    out, _ = _run_repl(monkeypatch, ["/models"])
    lines = out.splitlines()
    assert "  default      m-default" in lines
    assert "  fast         m-fast ← active" in lines
    assert "  averyveryverylongname m-long" in lines
    assert "Model profiles" in lines[0]


def test_repl_model_show_current_and_switch(monkeypatch):
    out, agent = _run_repl(monkeypatch, ["/model", "/MODEL   Fast  "])
    lines = out.splitlines()
    assert lines[0] == "  fast m-fast"
    assert lines[1] == "  Switched to 'Fast'"
    assert agent.switched == ["Fast"]


def _tg_update(chat_id):
    sent: list[tuple[str, str | None]] = []

    async def reply_text(text, parse_mode=None):
        sent.append((text, parse_mode))

    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id),
                          message=SimpleNamespace(reply_text=reply_text))
    return upd, sent


def test_telegram_cmd_model_list_and_switch(monkeypatch):
    from aria import telegram_bot as tb
    monkeypatch.setenv("TELEGRAM_ALLOWED", "42")
    agent = _FakeAgent()
    monkeypatch.setattr(tb, "get_session", lambda ch, cid: agent)

    upd, sent = _tg_update(42)
    asyncio.run(tb.cmd_model(upd, SimpleNamespace(args=[])))  # type: ignore[arg-type]
    assert sent == [(
        "<code>default     </code> m-default\n"
        "<code>fast        </code> m-fast ✓\n"
        "<code>averyveryverylongname</code> m-long",
        "HTML",
    )]

    upd, sent = _tg_update(42)
    asyncio.run(tb.cmd_model(upd, SimpleNamespace(args=["Fast", "x"])))  # type: ignore[arg-type]
    assert agent.switched == ["Fast"]
    assert sent == [("Switched to &#x27;Fast&#x27;", "HTML")]  # _md_to_html escapes

    upd, sent = _tg_update(99)  # not allowed → silent
    asyncio.run(tb.cmd_model(upd, SimpleNamespace(args=[])))  # type: ignore[arg-type]
    assert sent == []


def _wa_post(monkeypatch, text):
    from aria import whatsapp_bridge as wa
    import aria.channel
    agent = _FakeAgent()
    monkeypatch.setattr(aria.channel, "get_session", lambda ch, s: agent)
    monkeypatch.setenv("ARIA_WA_SECRET", "s")
    monkeypatch.setenv("WHATSAPP_ALLOWED", "34600111222")
    h = wa._Handler.__new__(wa._Handler)
    h.path = "/message"
    body = json.dumps({"from": "34600111222", "text": text}).encode()
    h.headers = {"X-Aria-Secret": "s", "Content-Length": str(len(body))}  # type: ignore[assignment]
    h.rfile = io.BytesIO(body)
    replies: list[dict] = []
    monkeypatch.setattr(h, "_respond", lambda data: replies.append(data))
    monkeypatch.setattr(h, "_reject", lambda code, msg: replies.append({"reject": code}))
    h.do_POST()
    return replies, agent


@pytest.mark.parametrize("cmd", ["/models", " /MODEL "])
def test_whatsapp_model_list(monkeypatch, cmd):
    replies, _ = _wa_post(monkeypatch, cmd)
    assert replies == [{"reply":
        "*default*      m-default\n"
        "*fast*         m-fast ✓\n"
        "*averyveryverylongname* m-long"}]


def test_whatsapp_model_switch(monkeypatch):
    replies, agent = _wa_post(monkeypatch, "/Model   Fast  x ")
    assert agent.switched == ["Fast  x"]
    assert replies == [{"reply": "Switched to 'Fast  x'"}]


# ── 4. importing aria.main has no side effects ──────────────────────────────

def test_import_main_has_no_side_effects(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "ARIA_ENV"}
    env.update(HOME=str(home), PYTHONPATH=str(_SRC))
    proc = subprocess.run(
        [sys.executable, "-c", "import aria.main; print('imported')"],
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "imported"
    assert list(home.iterdir()) == []
    # Nothing else that env-reads at import is pulled in before config.load().
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, aria.main; print('aria.agent' in sys.modules)"],
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.stdout.strip() == "False", proc.stderr


def test_cli_first_run_wizard_still_runs(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "ARIA_ENV"}
    env.update(HOME=str(home), PYTHONPATH=str(_SRC))
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; sys.argv=['aria','--version'];"
         "from aria.main import main; main()"],
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )
    # Wizard runs before argparse: .env is created and --version never prints.
    assert (home / ".aria" / ".env").exists()
    assert not any(ln.startswith("aria ") for ln in proc.stdout.splitlines())


def test_cli_loads_env_before_agent_import(tmp_path):
    """ARIA_* values from .env must be visible to aria.agent's import-time constants."""
    home = tmp_path / "home"
    home.mkdir()
    envf = tmp_path / "aria.env"
    envf.write_text("LLM_BASE_URL=x\nLLM_API_KEY=x\nLLM_MODEL=x\nARIA_MAX_LOOPS=7\n")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LLM_", "ARIA_"))}
    env.update(HOME=str(home), PYTHONPATH=str(_SRC), ARIA_ENV=str(envf),
               ARIA_WORKSPACE=str(tmp_path / "ws"), ARIA_REFLECT_EVERY="0")
    code = (
        "import sys, aria.main as M\n"
        "sys.argv = ['aria', '--usage']\n"
        "M.main()\n"
        "import aria.agent as ag\n"
        "print('LOOPS', ag._MAX_LOOPS)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=60)
    assert "LOOPS 7" in proc.stdout, proc.stdout + proc.stderr
