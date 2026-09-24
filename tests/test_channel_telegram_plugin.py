"""Telegram as the built-in channel plugin (aria.channels.telegram)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import aria.channels as channels
from aria.channels.base import ServiceSpec

_SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_CHANNELS_DIR", str(tmp_path / "no-user-channels"))
    for k in ("ARIA_CHANNELS", "ARIA_NOTIFY_CHANNEL", "TELEGRAM_TOKEN", "TELEGRAM_ALLOWED"):
        monkeypatch.delenv(k, raising=False)
    channels.reset_cache()
    yield
    channels.reset_cache()


def _enabled_names() -> list[str]:
    return [p.name for p in channels.enabled()]


def test_registry_discovers_telegram_as_builtin():
    p = channels.get("telegram")
    assert p is not None
    assert p.builtin and p.source == "aria.channels.telegram"
    assert p.description == "Telegram bot  (aria-telegram + aria --notify)"
    assert p.supports_push and p.supports_files


def test_config_fields():
    p = channels.get("telegram")
    assert p is not None
    fields = {f.key: f for f in p.config_fields}
    assert set(fields) == {"TELEGRAM_TOKEN", "TELEGRAM_ALLOWED"}
    assert fields["TELEGRAM_TOKEN"].secret and fields["TELEGRAM_TOKEN"].required
    assert fields["TELEGRAM_ALLOWED"].required and not fields["TELEGRAM_ALLOWED"].secret


def test_legacy_enablement_by_token(monkeypatch):
    assert "telegram" not in _enabled_names()
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    assert "telegram" in _enabled_names()


def test_explicit_empty_list_enables_nothing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("ARIA_CHANNELS", "")
    assert _enabled_names() == []


def test_explicit_list_enables_telegram(monkeypatch):
    monkeypatch.setenv("ARIA_CHANNELS", "telegram")
    assert _enabled_names() == ["telegram"]


def test_legacy_modules_are_aliases():
    pytest.importorskip("telegram")
    import aria.channels.telegram.bot as bot_real
    import aria.telegram_bot
    assert aria.telegram_bot is bot_real
    assert aria.telegram_bot.main is bot_real.main


def test_legacy_notify_is_alias():
    import aria.channels.telegram.notify as notify_real
    import aria.telegram_notify
    assert aria.telegram_notify is notify_real
    assert aria.telegram_notify._md_to_html is notify_real._md_to_html


def test_plugin_import_is_cheap():
    code = ("import sys, aria.channels.telegram as t; t.PLUGIN.services(); "
            "bad = [m for m in ('telegram', 'httpx') if m in sys.modules]; "
            "print(','.join(bad))")
    out = subprocess.run([sys.executable, "-c", code], cwd=_SRC, capture_output=True,
                         text=True, check=True, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_SRC)})
    assert out.stdout.strip() == ""


def test_send_delegates(monkeypatch):
    from aria.channels.telegram import notify
    calls = []
    monkeypatch.setattr(notify, "send", lambda text, chat_id=None: calls.append((text, chat_id)))
    p = channels.get("telegram")
    assert p is not None
    p.send("hi")
    p.send("yo", to="42")
    p.send("grp", to="-100123")
    assert calls == [("hi", None), ("yo", 42), ("grp", -100123)]
    with pytest.raises(RuntimeError):
        p.send("bad", to="not-a-chat")


def test_push_goes_to_telegram(monkeypatch):
    from aria.channels.telegram import notify
    calls = []
    monkeypatch.setattr(notify, "send", lambda text, chat_id=None: calls.append((text, chat_id)))
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    assert channels.push("hello") == "telegram"
    assert calls == [("hello", None)]


def test_send_file_delegates(monkeypatch, tmp_path):
    from aria.channels.telegram import notify
    calls = []

    def fake(path, caption="", chat_id=None):
        calls.append((path, caption, chat_id))
        return Path(path).name

    monkeypatch.setattr(notify, "send_document", fake)
    p = channels.get("telegram")
    assert p is not None
    f = tmp_path / "r.txt"
    assert p.send_file(f, caption="cap") == "r.txt"
    assert p.send_file(f, to="7") == "r.txt"
    assert calls == [(f, "cap", None), (f, "", 7)]


def test_services_match_legacy_unit():
    p = channels.get("telegram")
    assert p is not None
    assert p.services() == [ServiceSpec(unit="aria-telegram", description="Aria Telegram Bot",
                                        exec_start=("aria-telegram",))]
