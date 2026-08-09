"""
Tests for the .env backup-before-overwrite behavior in aria/install.py.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def cfg_dir(tmp_path):
    # A dedicated dir so we don't collide with minimal_env's own tmp .env.
    d = tmp_path / "cfg"
    d.mkdir()
    return d


def test_write_env_no_backup_when_absent(minimal_env, cfg_dir):
    from aria import install
    target = cfg_dir / ".env"
    backup = install._write_env(target, {"LLM_MODEL": "m", "AGENT_NAME": "A"})
    assert backup is None
    assert target.exists() and "LLM_MODEL=m" in target.read_text()


def test_write_env_backs_up_existing(minimal_env, cfg_dir):
    from aria import install
    target = cfg_dir / ".env"
    target.write_text("LLM_MODEL=old\nTELEGRAM_ALLOWED=111\n")
    target.chmod(0o600)

    backup = install._write_env(target, {"LLM_MODEL": "new", "AGENT_NAME": "A"})

    assert backup is not None and backup.exists()
    # backup holds the OLD content; target holds the NEW value
    assert "LLM_MODEL=old" in backup.read_text()
    assert "TELEGRAM_ALLOWED=111" in backup.read_text()
    assert "LLM_MODEL=new" in target.read_text()
    # secret-bearing file: perms preserved on the backup
    assert (backup.stat().st_mode & 0o777) == 0o600
    assert backup.name.startswith(".env.bak-")


def test_backup_does_not_clobber_on_rapid_reruns(minimal_env, cfg_dir):
    from aria import install
    target = cfg_dir / ".env"
    target.write_text("v=1\n")
    # Two consecutive backups must never overwrite each other, even within the
    # same second (the collision-suffix guards that).
    b1 = install._backup_env(target)
    b2 = install._backup_env(target)
    assert b1 != b2 and b1.exists() and b2.exists()
