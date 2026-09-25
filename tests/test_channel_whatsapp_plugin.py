"""
Tests for the built-in WhatsApp channel plugin (aria/channels/whatsapp/) and
the legacy module aliases (aria.whatsapp_bridge / _notify / _deploy).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import aria.channels as channels
from aria.channels.base import ServiceSpec

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_CHANNELS_DIR", str(tmp_path / "no-user-channels"))
    monkeypatch.delenv("ARIA_CHANNELS", raising=False)
    channels.reset_cache()
    yield
    channels.reset_cache()


def _plugin():
    p = channels.get("whatsapp")
    assert p is not None
    return p


def test_discovered_as_builtin():
    p = _plugin()
    from aria.channels.whatsapp import PLUGIN, WhatsAppChannel
    assert p is PLUGIN and isinstance(p, WhatsAppChannel)
    assert p.builtin and p.source == "aria.channels.whatsapp"
    assert p.name == "whatsapp"
    assert p.description == "WhatsApp bridge  (aria-whatsapp, needs Node.js)"
    assert p.supports_push and p.supports_files    # 4.10: send_file works on WhatsApp
    keys = [f.key for f in p.config_fields]
    assert keys == ["ARIA_WA_PORT", "ARIA_WA_PUSH_PORT", "ARIA_WA_SECRET", "WHATSAPP_ALLOWED"]
    by_key = {f.key: f for f in p.config_fields}
    assert by_key["ARIA_WA_SECRET"].secret
    assert by_key["WHATSAPP_ALLOWED"].required
    assert by_key["ARIA_WA_PORT"].default == "7532"
    assert by_key["ARIA_WA_PUSH_PORT"].default == "7533"


def test_legacy_enablement(monkeypatch):
    monkeypatch.delenv("WHATSAPP_ALLOWED", raising=False)
    assert "whatsapp" not in [p.name for p in channels.enabled()]
    monkeypatch.setenv("WHATSAPP_ALLOWED", "34612345678")
    assert "whatsapp" in [p.name for p in channels.enabled()]
    # the secret alone doesn't enable it (installer inferred from WHATSAPP_ALLOWED)
    monkeypatch.delenv("WHATSAPP_ALLOWED")
    monkeypatch.setenv("ARIA_WA_SECRET", "s")
    assert "whatsapp" not in [p.name for p in channels.enabled()]


def test_explicit_enablement(monkeypatch):
    monkeypatch.delenv("WHATSAPP_ALLOWED", raising=False)
    monkeypatch.setenv("ARIA_CHANNELS", "whatsapp")
    assert [p.name for p in channels.enabled()] == ["whatsapp"]


def test_legacy_aliases_are_same_module():
    import aria.whatsapp_bridge as old_b
    import aria.whatsapp_deploy as old_d
    import aria.whatsapp_notify as old_n
    from aria.channels.whatsapp import bridge, deploy, notify
    assert old_b is bridge and old_n is notify and old_d is deploy
    from aria import whatsapp_bridge
    assert whatsapp_bridge is bridge
    assert callable(old_b.main)            # aria-whatsapp = aria.whatsapp_bridge:main
    assert old_b._Handler is bridge._Handler


def test_alias_monkeypatch_visible_through_new_path(monkeypatch):
    import aria.whatsapp_notify as old_n
    from aria.channels.whatsapp import notify
    monkeypatch.setattr(old_n, "_push_port", lambda: 1234)
    assert notify._push_port() == 1234


def test_import_is_cheap():
    """Importing the plugin package must not pull in the bridge/notify/deploy
    modules (http.server etc.) nor start anything."""
    code = (
        "import sys, aria.channels.whatsapp as w; "
        "bad = [m for m in ('aria.channels.whatsapp.bridge', 'aria.channels.whatsapp.notify', "
        "'aria.channels.whatsapp.deploy', 'http.server', 'aria.channel', 'aria.agent') "
        "if m in sys.modules]; "
        "print(','.join(bad))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(REPO), env={"PATH": "/usr/bin:/bin", "ARIA_ENV": "/dev/null",
                                             "PYTHONPATH": str(REPO / "src")},
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


def test_send_delegates(monkeypatch):
    from aria.channels.whatsapp import notify
    calls = []
    monkeypatch.setattr(notify, "send", lambda text, to=None: calls.append((text, to)))
    _plugin().send("hi", to="34600")
    _plugin().send("all")
    assert calls == [("hi", "34600"), ("all", None)]


def test_push_via_registry(monkeypatch):
    from aria.channels.whatsapp import notify
    calls = []
    monkeypatch.setattr(notify, "send", lambda text, to=None: calls.append((text, to)))
    assert channels.push("x", to="1", channel="whatsapp") == "whatsapp"
    assert calls == [("x", "1")]


def test_run_calls_bridge_main(monkeypatch):
    from aria.channels.whatsapp import bridge
    ran = []
    monkeypatch.setattr(bridge, "main", lambda: ran.append(True))
    _plugin().run()
    assert ran == [True]


@pytest.mark.parametrize("found", [True, False])
def test_services_match_legacy(monkeypatch, tmp_path, found):
    import aria.channels.whatsapp as w
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(w.shutil, "which",
                        lambda c: f"/usr/bin/{c}" if found and c == "node" else None)
    node = "/usr/bin/node" if found else "node"
    assert _plugin().services() == [
        ServiceSpec("aria-whatsapp", "Aria WhatsApp Python Bridge", ("aria-whatsapp",)),
        ServiceSpec("aria-whatsapp-node", "Aria WhatsApp Node.js Bridge",
                    (node, str(tmp_path / ".aria" / "whatsapp" / "bridge.js")),
                    requires="aria-whatsapp.service", optional=True),
    ]


def test_services_nodejs_fallback(monkeypatch):
    import aria.channels.whatsapp as w
    monkeypatch.setattr(w.shutil, "which", lambda c: "/usr/bin/nodejs" if c == "nodejs" else None)
    assert _plugin().services()[1].exec_start[0] == "/usr/bin/nodejs"


def _fake_deploy(monkeypatch, result, dest):
    from aria.channels.whatsapp import deploy
    calls = []

    def fake():
        calls.append(True)
        return {"source": dest.parent, "dest": dest, "copied": [],
                "package_changed": False, "error": None, **result}
    monkeypatch.setattr(deploy, "deploy", fake)
    return calls


def test_install_deploys_and_reports(monkeypatch, tmp_path):
    dest = tmp_path / "wa"
    dest.mkdir()
    (dest / "package-lock.json").write_text("{}")
    calls = _fake_deploy(monkeypatch, {"copied": ["bridge.js", "package.json"],
                                       "package_changed": True}, dest)
    notes = _plugin().install()
    assert calls == [True]
    assert ("ok", f"Deployed bridge files: bridge.js, package.json → {dest}") in notes
    assert ("warn", f"Run: cd {dest} && npm ci") in notes   # package changed → actionable


def test_install_up_to_date(monkeypatch, tmp_path):
    dest = tmp_path / "wa"
    (dest / "node_modules").mkdir(parents=True)
    _fake_deploy(monkeypatch, {}, dest)
    notes = _plugin().install()
    assert ("info", "WhatsApp bridge files already up to date.") in notes
    assert not any(t.startswith("Run:") for _, t in notes)


def test_install_npm_install_without_lock(monkeypatch, tmp_path):
    dest = tmp_path / "wa"
    dest.mkdir()
    _fake_deploy(monkeypatch, {}, dest)               # no node_modules yet
    assert ("info", f"Run: cd {dest} && npm install") in _plugin().install()


def test_install_error(monkeypatch, tmp_path):
    dest = tmp_path / "wa"
    _fake_deploy(monkeypatch, {"source": None, "error": "source not found"}, dest)
    notes = _plugin().install()
    assert ("warn", "source not found") in notes
    assert not any(t.startswith("Run:") for _, t in notes)


def test_install_dry_run_does_not_deploy(monkeypatch, tmp_path):
    calls = _fake_deploy(monkeypatch, {}, tmp_path / "wa")
    notes = _plugin().install(dry_run=True)
    assert calls == []
    assert any("dry-run" in t or "not found" in t for _, t in notes)


def test_deploy_source_dir_resolves_repo(monkeypatch, tmp_path):
    from aria.channels.whatsapp import deploy
    monkeypatch.delenv("ARIA_SOURCE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)                        # ./whatsapp absent
    if not Path(deploy.__file__).resolve().is_relative_to(REPO / "src"):
        pytest.skip("aria imported from an installed copy, not this checkout")
    assert (REPO / "whatsapp" / "bridge.js").is_file()
    assert deploy.source_dir() == REPO / "whatsapp"
