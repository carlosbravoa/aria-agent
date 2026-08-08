"""
Tests for aria/whatsapp_deploy.py — copying the Node bridge files into
~/.aria/whatsapp/ without disturbing node_modules/ or the WhatsApp login state.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def fake_checkout(tmp_path, monkeypatch):
    """A fake aria-agent checkout with a whatsapp/ source dir, wired via
    ARIA_SOURCE_DIR."""
    src = tmp_path / "checkout"
    (src / "whatsapp").mkdir(parents=True)
    (src / "whatsapp" / "bridge.js").write_text("// bridge v1\n")
    (src / "whatsapp" / "package.json").write_text('{"name":"aria-wa","version":"1"}\n')
    monkeypatch.setenv("ARIA_SOURCE_DIR", str(src))
    return src


def test_deploy_copies_both_files(fake_checkout, tmp_path):
    from aria import whatsapp_deploy
    dest = tmp_path / "dest"
    res = whatsapp_deploy.deploy(dest=dest)
    assert res["error"] is None
    assert set(res["copied"]) == {"bridge.js", "package.json"}
    assert res["package_changed"] is True
    assert (dest / "bridge.js").read_text() == "// bridge v1\n"


def test_deploy_skips_identical(fake_checkout, tmp_path):
    from aria import whatsapp_deploy
    dest = tmp_path / "dest"
    whatsapp_deploy.deploy(dest=dest)
    res = whatsapp_deploy.deploy(dest=dest)   # second run: nothing changed
    assert res["copied"] == []
    assert res["package_changed"] is False


def test_deploy_detects_bridge_change_only(fake_checkout, tmp_path):
    from aria import whatsapp_deploy
    dest = tmp_path / "dest"
    whatsapp_deploy.deploy(dest=dest)
    (fake_checkout / "whatsapp" / "bridge.js").write_text("// bridge v2 with /send\n")
    res = whatsapp_deploy.deploy(dest=dest)
    assert res["copied"] == ["bridge.js"]
    assert res["package_changed"] is False
    assert (dest / "bridge.js").read_text() == "// bridge v2 with /send\n"


def test_deploy_never_touches_node_modules_or_auth(fake_checkout, tmp_path):
    from aria import whatsapp_deploy
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "node_modules").mkdir()
    (dest / "node_modules" / "dep.js").write_text("keep me")
    (dest / ".wwebjs_auth").mkdir()
    (dest / ".wwebjs_auth" / "session").write_text("logged in")
    whatsapp_deploy.deploy(dest=dest)
    assert (dest / "node_modules" / "dep.js").read_text() == "keep me"
    assert (dest / ".wwebjs_auth" / "session").read_text() == "logged in"


def test_deploy_reports_missing_source(tmp_path, monkeypatch):
    from aria import whatsapp_deploy
    monkeypatch.setattr(whatsapp_deploy, "source_dir", lambda: None)
    res = whatsapp_deploy.deploy(dest=tmp_path / "dest")
    assert res["copied"] == [] and res["error"] and "source not found" in res["error"]
