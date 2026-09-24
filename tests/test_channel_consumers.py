"""
Consumers of the channel registry: the notify / send_file tools, supervisor and
reflection pushes, `aria --notify`, the self-updater and the installer.

The contract: every outbound push goes through aria.channels, and existing
deployments behave exactly as before — outside a channel pushes go to
Telegram, a WhatsApp turn replies on WhatsApp, an unknown channel gets the
"[notify error] Push notifications are not wired…" message, and the systemd
units aria-install writes for telegram/whatsapp/supervisor are byte-identical
to the pre-plugin ones.

User plugins are exercised as drop-in files in a temp ARIA_CHANNELS_DIR.
No network, no systemctl: senders and subprocess.run are stubbed.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from aria import channels, context


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def chan_env(minimal_env, tmp_path, monkeypatch):
    """Isolated registry: an empty user plugin dir, no ARIA_CHANNELS /
    ARIA_NOTIFY_CHANNEL / legacy keys leaking in from the developer's env."""
    d = tmp_path / "user_channels"
    d.mkdir()
    monkeypatch.setenv("ARIA_CHANNELS_DIR", str(d))
    for k in ("ARIA_CHANNELS", "ARIA_NOTIFY_CHANNEL", "TELEGRAM_TOKEN",
              "TELEGRAM_ALLOWED", "WHATSAPP_ALLOWED", "ARIA_WA_SECRET"):
        monkeypatch.delenv(k, raising=False)
    channels.reset_cache()
    context.clear()
    yield d
    context.clear()
    channels.reset_cache()


_FAKE = '''
from aria.channels.base import ChannelPlugin, ServiceSpec
SENT = []

class Fake(ChannelPlugin):
    name = "{name}"
    description = "{name} test channel"
    legacy_keys = ("{name_up}_TOKEN",)
    supports_files = {files}

    def send(self, text, to=None):
        SENT.append(("text", text, to))

    def send_file(self, path, caption="", to=None):
        SENT.append(("file", str(path), caption, to))
        return path.name

PLUGIN = Fake()
'''

_MUTE = '''
from aria.channels.base import ChannelPlugin

class Mute(ChannelPlugin):
    name = "mute"
    description = "receive-only"

PLUGIN = Mute()
'''


def _add_plugin(d: Path, name: str, files: bool = False) -> None:
    (d / f"{name}.py").write_text(
        _FAKE.format(name=name, name_up=name.upper(), files=files), encoding="utf-8")
    channels.reset_cache()


def _sent(name: str) -> list:
    return sys.modules[f"_aria_user_channel_{name}"].SENT


@pytest.fixture
def tg_sent(monkeypatch):
    calls = []
    import aria.telegram_notify as tn

    def fake_send(text, chat_id=None):
        calls.append({"text": text, "chat_id": chat_id})

    monkeypatch.setattr(tn, "send", fake_send)
    return calls


@pytest.fixture
def tg_docs(monkeypatch):
    calls = []
    import aria.telegram_notify as tn

    def fake_doc(path, caption="", chat_id=None):
        calls.append({"path": Path(path), "caption": caption, "chat_id": chat_id})
        return Path(path).name

    monkeypatch.setattr(tn, "send_document", fake_doc)
    return calls


@pytest.fixture
def wa_sent(monkeypatch):
    calls = []
    import aria.whatsapp_notify as wn

    def fake_send(text, to=None):
        calls.append({"text": text, "to": to})

    monkeypatch.setattr(wn, "send", fake_send)
    return calls


# ── notify tool ───────────────────────────────────────────────────────────────

def test_notify_no_channel_goes_to_telegram(chan_env, tg_sent, wa_sent):
    from aria.tools import notify
    assert notify.execute({"message": "done"}) == "[notify] Message sent."
    assert tg_sent == [{"text": "done", "chat_id": None}]
    assert not wa_sent


def test_notify_whatsapp_turn_stays_on_whatsapp(chan_env, tg_sent, wa_sent):
    from aria.tools import notify
    tok = context.set_active("whatsapp", "346")
    try:
        assert notify.execute({"message": "hi"}) == "[notify] Message sent."
    finally:
        context.reset(tok)
    assert not tg_sent
    assert [c["text"] for c in wa_sent] == ["hi"]


def test_notify_unknown_channel_error_text(chan_env, tg_sent):
    from aria.tools import notify
    tok = context.set_active("matrix", "@u:x")
    try:
        out = notify.execute({"message": "hello"})
    finally:
        context.reset(tok)
    assert out == ("[notify error] Push notifications are not wired for the "
                   "'matrix' channel. Put the message in your normal reply instead.")
    assert not tg_sent


def test_notify_error_surfaces(chan_env, monkeypatch):
    from aria.tools import notify
    import aria.telegram_notify as tn

    def boom(text, chat_id=None):
        raise RuntimeError("TELEGRAM_TOKEN not set")

    monkeypatch.setattr(tn, "send", boom)
    assert notify.execute({"message": "x"}) == "[notify error] TELEGRAM_TOKEN not set"


def test_notify_user_plugin_turn(chan_env, tg_sent):
    _add_plugin(chan_env, "mine")
    from aria.tools import notify
    tok = context.set_active("mine", "u1")
    try:
        assert notify.execute({"message": "yo"}) == "[notify] Message sent."
    finally:
        context.reset(tok)
    assert _sent("mine") == [("text", "yo", None)]
    assert not tg_sent


def test_notify_receive_only_plugin_is_not_wired(chan_env, tg_sent):
    (chan_env / "mute.py").write_text(_MUTE, encoding="utf-8")
    channels.reset_cache()
    from aria.tools import notify
    tok = context.set_active("mute", "u1")
    try:
        out = notify.execute({"message": "x"})
    finally:
        context.reset(tok)
    assert "not wired for the 'mute' channel" in out
    assert not tg_sent


def test_notify_explicit_notify_channel(chan_env, monkeypatch, tg_sent):
    _add_plugin(chan_env, "mine")
    monkeypatch.setenv("ARIA_NOTIFY_CHANNEL", "mine")
    from aria.tools import notify
    assert notify.execute({"message": "bcast"}) == "[notify] Message sent."
    assert _sent("mine") == [("text", "bcast", None)]
    assert not tg_sent


def test_notify_description_is_not_telegram_only(chan_env):
    from aria.tools import notify
    assert "notif" in notify.DEFINITION["description"].lower()


# ── send_file tool ────────────────────────────────────────────────────────────

def _readable(tmp_path, monkeypatch) -> Path:
    from aria.tools import file_access
    f = tmp_path / "report.txt"
    f.write_text("data")
    monkeypatch.setattr(file_access, "resolve_readable", lambda raw: (f, None))
    return f


def test_send_file_no_channel_uses_telegram(chan_env, tmp_path, monkeypatch, tg_docs):
    from aria.tools import send_file
    f = _readable(tmp_path, monkeypatch)
    out = send_file.execute({"path": str(f), "caption": "c"})
    assert out.startswith("[send_file] Sent report.txt")
    assert tg_docs[0]["path"] == f and tg_docs[0]["caption"] == "c"


def test_send_file_refused_on_whatsapp(chan_env, tmp_path, monkeypatch, tg_docs):
    from aria.tools import send_file
    f = _readable(tmp_path, monkeypatch)
    tok = context.set_active("whatsapp", "346")
    try:
        out = send_file.execute({"path": str(f)})
    finally:
        context.reset(tok)
    assert out == (f"[send_file] Sending files is only supported on Telegram, "
                   f"not whatsapp. The file is at {f}.")
    assert not tg_docs


def test_send_file_user_plugin_with_files(chan_env, tmp_path, monkeypatch, tg_docs):
    _add_plugin(chan_env, "mine", files=True)
    from aria.tools import send_file
    f = _readable(tmp_path, monkeypatch)
    tok = context.set_active("mine", "u1")
    try:
        out = send_file.execute({"path": str(f), "caption": "hi"})
    finally:
        context.reset(tok)
    assert out.startswith("[send_file] Sent report.txt")
    assert _sent("mine") == [("file", str(f), "hi", None)]
    assert not tg_docs


# ── supervisor / reflect / aria --notify ──────────────────────────────────────

def test_supervisor_task_result_pushed(chan_env, monkeypatch, tg_sent):
    import aria.supervisor as sup
    from aria.task import Task
    monkeypatch.setattr(sup, "run_with_timeout", lambda t, a, timeout: "result!")
    assert sup._execute(Task(prompt="p", notify=True)) == "result!"
    assert [c["text"] for c in tg_sent] == ["result!"]


def test_supervisor_push_failure_is_logged(chan_env, monkeypatch, caplog):
    import aria.supervisor as sup
    import aria.telegram_notify as tn
    from aria.task import Task

    def boom(text, chat_id=None):
        raise RuntimeError("no token")

    monkeypatch.setattr(tn, "send", boom)
    monkeypatch.setattr(sup, "run_with_timeout", lambda t, a, timeout: "r")
    assert sup._execute(Task(prompt="p", notify=True)) == "r"
    assert "Telegram notify failed" in caplog.text


def test_supervisor_push_follows_notify_channel(chan_env, monkeypatch, tg_sent):
    _add_plugin(chan_env, "mine")
    monkeypatch.setenv("ARIA_NOTIFY_CHANNEL", "mine")
    import aria.supervisor as sup
    from aria.task import Task
    monkeypatch.setattr(sup, "run_with_timeout", lambda t, a, timeout: "r2")
    sup._execute(Task(prompt="p", notify=True))
    assert _sent("mine") == [("text", "r2", None)]
    assert not tg_sent


def test_reflect_friction_push(chan_env, tg_sent):
    from aria import reflect

    class WS:
        def load_friction_log(self):
            return "\n".join(f"- event {i}" for i in range(50))
        def load_operational_memory(self):
            return ""
        def clear_friction_log(self, raw):
            pass
        def append_operational_memory(self, s):
            pass

    class Msg:
        content = "The shell tool keeps failing."

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return type("R", (), {"choices": [type("C", (), {"message": Msg})]})

    if not reflect._friction_is_hot(WS().load_friction_log()):
        pytest.skip("friction threshold not met by the synthetic log")
    out = reflect._phase_friction(WS(), Client, "m", notify=True)
    assert "systemic issue" in out
    assert tg_sent and tg_sent[0]["text"].startswith("⚠ Reflection found")


def _run_main(monkeypatch, argv):
    import aria.main as m

    class FakeAgent:
        name = "Aria"

        def __init__(self, *a, **k):
            pass

        def chat_collect(self, q):
            return f"answer to {q}"

        def close(self):
            pass

    monkeypatch.setattr(m, "Agent", FakeAgent, raising=False)
    monkeypatch.setattr(sys, "argv", ["aria", *argv])
    m.main()


def test_main_notify_default_telegram(chan_env, monkeypatch, tg_sent):
    _run_main(monkeypatch, ["--notify", "q1"])
    assert tg_sent == [{"text": "answer to q1", "chat_id": None}]


def test_main_notify_chat_id(chan_env, monkeypatch, tg_sent):
    _run_main(monkeypatch, ["--notify", "--chat", "123", "q"])
    assert tg_sent == [{"text": "answer to q", "chat_id": 123}]


def test_main_notify_channel_flag(chan_env, monkeypatch, tg_sent):
    _add_plugin(chan_env, "mine")
    _run_main(monkeypatch, ["--notify", "--channel", "mine", "--chat", "u7", "q"])
    assert _sent("mine") == [("text", "answer to q", "u7")]
    assert not tg_sent


# ── update tool ───────────────────────────────────────────────────────────────

def test_update_service_names_legacy_plus_enabled(chan_env, monkeypatch):
    from aria.tools import update
    _add_plugin(chan_env, "mine")
    monkeypatch.setenv("MINE_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    names = update._service_names()
    assert names[:4] == ["aria-telegram", "aria-supervisor", "aria-whatsapp",
                         "aria-whatsapp-node"]
    assert names[4:] == ["aria-channel-mine"]
    assert len(names) == len(set(names))


def test_update_service_names_survive_registry_failure(chan_env, monkeypatch):
    from aria.tools import update

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(channels, "enabled", boom)
    assert update._service_names() == list(update._SERVICES)


def test_update_refresh_runs_enabled_plugin_install(chan_env, monkeypatch):
    from aria.tools import update
    (chan_env / "inst.py").write_text(textwrap.dedent('''
        from aria.channels.base import ChannelPlugin
        class P(ChannelPlugin):
            name = "inst"
            legacy_keys = ("INST_KEY",)
            def install(self, dry_run=False):
                return [("ok", "Deployed helper: x.js"), ("info", "already fine"), "a hint"]
        PLUGIN = P()
    '''), encoding="utf-8")
    channels.reset_cache()
    monkeypatch.setenv("INST_KEY", "1")
    lines: list[str] = []
    update._refresh_channel_files(lines)
    assert any("Deployed helper: x.js" in ln for ln in lines)
    assert not any("already fine" in ln or "a hint" in ln for ln in lines)   # only real changes


# ── installer: systemd unit files ─────────────────────────────────────────────

def _unit(desc: str, exec_start: str, env_file: str, deps: str = "") -> str:
    """The exact pre-plugin unit text (captured from install.py before the
    refactor) — do NOT derive this from install._service."""
    return (
        "[Unit]\n"
        f"Description={desc}\n"
        f"{deps}"
        "StartLimitIntervalSec=300\n"
        "StartLimitBurst=5\n"
        "OnFailure=aria-rollback@%n.service\n"
        "\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f"EnvironmentFile={env_file}\n"
        "PassEnvironment=DBUS_SESSION_BUS_ADDRESS GNOME_KEYRING_CONTROL SSH_AUTH_SOCK\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


@pytest.fixture
def fake_system(chan_env, tmp_path, monkeypatch):
    """tmp HOME with a .env, fake aria-* binaries, a fake node, no systemctl."""
    import shutil as _shutil
    from aria import install
    import aria.channels.whatsapp.deploy as wdeploy

    home = Path.home()
    (home / ".aria" / "whatsapp").mkdir(parents=True)
    (home / ".aria" / "whatsapp" / "bridge.js").write_text("//")
    env = home / ".aria" / ".env"
    env.write_text("TELEGRAM_TOKEN=t\nTELEGRAM_ALLOWED=1\nWHATSAPP_ALLOWED=346\n")
    node = tmp_path / "bin" / "node"
    node.parent.mkdir()
    node.write_text("")
    node.chmod(0o755)

    real_which = _shutil.which
    monkeypatch.setattr(_shutil, "which",
                        lambda n, *a, **k: str(node) if n in ("node", "nodejs")
                        else (None if n.startswith("aria-") else real_which(n, *a, **k)))
    monkeypatch.setattr(install, "_aria_bin", lambda n: f"/opt/bin/{n}")
    monkeypatch.setattr(install, "_systemd_available", lambda: True)
    monkeypatch.setattr(install, "_linger_enabled", lambda: True)
    monkeypatch.setattr(wdeploy, "deploy", lambda *a, **k: {
        "source": None, "dest": home / ".aria" / "whatsapp", "copied": [],
        "package_changed": False, "error": None})

    calls: list[list[str]] = []

    class R:
        returncode = 0
        stdout = "active"
        stderr = ""

    def fake_run(argv, *a, **k):
        calls.append(list(argv))
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return {"home": home, "env": env, "node": node, "calls": calls,
            "units": home / ".config" / "systemd" / "user"}


def test_units_byte_identical_services_path(fake_system):
    from aria import install
    install.install_services(features=None)
    units, env = fake_system["units"], str(fake_system["env"])
    bridge = fake_system["home"] / ".aria" / "whatsapp" / "bridge.js"
    assert (units / "aria-telegram.service").read_text() == \
        _unit("Aria Telegram Bot", "/opt/bin/aria-telegram", env)
    assert (units / "aria-supervisor.service").read_text() == \
        _unit("Aria Task Supervisor", "/opt/bin/aria-supervisor", env)
    assert (units / "aria-whatsapp.service").read_text() == \
        _unit("Aria WhatsApp Python Bridge", "/opt/bin/aria-whatsapp", env)
    assert (units / "aria-whatsapp-node.service").read_text() == _unit(
        "Aria WhatsApp Node.js Bridge", f"{fake_system['node']} {bridge}", env,
        deps="After=aria-whatsapp.service\nRequires=aria-whatsapp.service\n")
    written = sorted(p.name for p in units.iterdir())
    assert written == ["aria-rollback@.service", "aria-supervisor.service",
                       "aria-telegram.service", "aria-whatsapp-node.service",
                       "aria-whatsapp.service"]


def test_units_explicit_features(fake_system):
    from aria import install
    install.install_services(features={"telegram", "supervisor"})
    names = sorted(p.name for p in fake_system["units"].iterdir())
    assert names == ["aria-rollback@.service", "aria-supervisor.service",
                     "aria-telegram.service"]


def test_node_unit_skipped_without_node(fake_system, monkeypatch, capsys):
    import shutil as _shutil
    from aria import install
    monkeypatch.setattr(_shutil, "which", lambda n, *a, **k: None)
    install.install_services(features={"whatsapp", "supervisor"})
    names = sorted(p.name for p in fake_system["units"].iterdir())
    assert "aria-whatsapp.service" in names
    assert "aria-whatsapp-node.service" not in names
    assert "node not found — aria-whatsapp-node skipped" in capsys.readouterr().out


def test_services_path_honours_aria_channels(fake_system):
    from aria import install
    fake_system["env"].write_text(fake_system["env"].read_text() + "ARIA_CHANNELS=whatsapp\n")
    install.install_services(features=None)
    names = sorted(p.name for p in fake_system["units"].iterdir())
    assert "aria-telegram.service" not in names
    assert "aria-whatsapp.service" in names


def test_user_plugin_default_unit(fake_system):
    from aria import install
    _add_plugin(fake_system["home"].parent / "user_channels", "mine")
    install.install_services(features={"mine", "supervisor"})
    text = (fake_system["units"] / "aria-channel-mine.service").read_text()
    assert text == _unit("Aria mine channel", "/opt/bin/aria-channel mine",
                         str(fake_system["env"]))


def test_uninstall_covers_legacy_and_plugins(fake_system):
    from aria import install
    _add_plugin(fake_system["home"].parent / "user_channels", "mine")
    install.uninstall()
    disabled = {c[-1] for c in fake_system["calls"] if "disable" in c}
    assert {"aria-telegram", "aria-supervisor", "aria-whatsapp",
            "aria-whatsapp-node", "aria-channel-mine"} <= disabled


# ── installer: wizard ─────────────────────────────────────────────────────────

def _run_wizard(monkeypatch, capsys, answers: dict[str, str], feats: dict[str, str]):
    import builtins
    from aria import install
    prompts: list[str] = []

    def fake_input(p):
        prompts.append(p)
        if p.rstrip().endswith(("[Y/n]:", "[y/N]:")):
            for k, v in feats.items():
                if k in p:
                    return v
            return ""
        key = p.strip().split(" ")[0].rstrip(":")
        return answers.get(key, "")

    monkeypatch.setattr(builtins, "input", fake_input)
    values, features = install.configure_env()
    return values, features, prompts, capsys.readouterr().out


def test_wizard_channel_prompts_unchanged(fake_system, monkeypatch, capsys):
    fake_system["env"].write_text("TELEGRAM_TOKEN=oldtok\nTELEGRAM_ALLOWED=42\n"
                                  "LLM_MODEL=m\nLLM_BASE_URL=u\n")
    values, features, prompts, out = _run_wizard(
        monkeypatch, capsys,
        answers={"WHATSAPP_ALLOWED": "3461", "ARIA_WA_SECRET": "sec"},
        feats={"Telegram": "", "WhatsApp": "y", "supervisor": "", "Gmail": "n"})
    assert {"telegram", "whatsapp", "supervisor"} <= features
    assert "gmail" not in features
    # Same prompt lines, same order, as the pre-plugin wizard.
    tail = [p for p in prompts if not p.rstrip().endswith(("[Y/n]:", "[y/N]:"))]
    assert tail[4:10] == [
        "  TELEGRAM_TOKEN [****]: ", "  TELEGRAM_ALLOWED [42]: ",
        "  ARIA_WA_PORT [7532]: ", "  ARIA_WA_PUSH_PORT [7533]: ",
        "  ARIA_WA_SECRET: ", "  WHATSAPP_ALLOWED: ",
    ]
    assert tail[10] == "  ARIA_SUPERVISOR_INTERVAL [30]: "
    # Section headers + hints as before.
    tg = out.index("── Telegram ")
    wa = out.index("── WhatsApp ")
    assert tg < out.index("Get token from @BotFather — get your chat ID from @userinfobot") < wa
    assert tg < out.index("Comma-separated chat IDs allowed to use the bot") < wa
    assert wa < out.index("Your number in international format, no + (e.g. 34612345678)")
    assert values["TELEGRAM_TOKEN"] == "oldtok"
    assert values["WHATSAPP_ALLOWED"] == "3461"
    env = fake_system["env"].read_text()
    assert "ARIA_CHANNELS=telegram,whatsapp" in env
    # Preselection reflects the existing .env: Telegram on, WhatsApp off.
    menu = [p for p in prompts if p.rstrip().endswith(("[Y/n]:", "[y/N]:"))]
    assert any("Telegram" in p and "[Y/n]" in p for p in menu)
    assert any("WhatsApp" in p and "[y/N]" in p for p in menu)


def test_wizard_unselected_channel_keeps_values(fake_system, monkeypatch, capsys):
    fake_system["env"].write_text("TELEGRAM_TOKEN=oldtok\nTELEGRAM_ALLOWED=42\n"
                                  "LLM_MODEL=m\nLLM_BASE_URL=u\n")
    values, features, prompts, out = _run_wizard(
        monkeypatch, capsys, answers={},
        feats={"Telegram": "n", "WhatsApp": "", "supervisor": "", "Gmail": ""})
    assert "telegram" not in features
    assert values["TELEGRAM_TOKEN"] == "oldtok"
    assert "── Telegram " not in out
