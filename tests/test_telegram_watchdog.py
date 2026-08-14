"""Tests the Telegram polling stall watchdog: a wedged getUpdates loop (no
successful poll for ARIA_TELEGRAM_STALL_MIN minutes) must trigger a restart
exit, while healthy polling and error responses must not."""

import asyncio

import pytest

pytest.importorskip("telegram")


def _watchdog(stall=600, start=1000.0):
    from aria.telegram_bot import _StallWatchdog
    clock = {"now": start}
    fired = []
    wd = _StallWatchdog(stall, clock=lambda: clock["now"],
                        on_stall=lambda: fired.append(True))
    return wd, clock, fired


def test_fresh_watchdog_is_not_stalled():
    wd, clock, fired = _watchdog()
    clock["now"] += 599
    assert wd.check() is False
    assert not fired


def test_stale_watchdog_fires():
    wd, clock, fired = _watchdog()
    clock["now"] += 601
    assert wd.check() is True
    assert fired


def test_beat_defers_the_stall():
    wd, clock, fired = _watchdog()
    clock["now"] += 500
    wd.beat()
    clock["now"] += 500          # 1000s after start, but only 500 since beat
    assert wd.check() is False
    clock["now"] += 200
    assert wd.check() is True


def test_stall_seconds_from_env(monkeypatch):
    from aria.telegram_bot import _stall_seconds
    monkeypatch.delenv("ARIA_TELEGRAM_STALL_MIN", raising=False)
    assert _stall_seconds() == 600.0                     # default 10 min
    monkeypatch.setenv("ARIA_TELEGRAM_STALL_MIN", "5")
    assert _stall_seconds() == 300.0
    monkeypatch.setenv("ARIA_TELEGRAM_STALL_MIN", "0")
    assert _stall_seconds() == 0.0                       # disabled
    monkeypatch.setenv("ARIA_TELEGRAM_STALL_MIN", "off")
    assert _stall_seconds() == 0.0
    monkeypatch.setenv("ARIA_TELEGRAM_STALL_MIN", "bogus")
    assert _stall_seconds() == 600.0                     # bad value → default


def test_request_beats_only_on_completed_roundtrip(monkeypatch):
    """do_request beats the watchdog when the HTTP round-trip completes, and
    does NOT beat when it raises — a spinning TimedOut/PoolTimeout loop must
    look stalled."""
    from aria.telegram_bot import _StallWatchdog, _WatchdogRequest
    from telegram.request import HTTPXRequest

    wd, clock, fired = _watchdog()
    req = _WatchdogRequest(wd)

    async def ok(self, *a, **k):
        return (200, b"{}")

    async def boom(self, *a, **k):
        raise RuntimeError("read error")

    monkeypatch.setattr(HTTPXRequest, "do_request", ok)
    clock["now"] += 700           # stale…
    assert asyncio.run(req.do_request("url", "POST")) == (200, b"{}")
    assert wd.check() is False    # …but the successful poll beat it

    monkeypatch.setattr(HTTPXRequest, "do_request", boom)
    clock["now"] += 700
    with pytest.raises(RuntimeError):
        asyncio.run(req.do_request("url", "POST"))
    assert wd.check() is True     # failures don't beat
