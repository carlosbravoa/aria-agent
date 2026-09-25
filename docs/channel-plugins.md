# Channel plugins

A **channel** connects Aria to a messaging surface. Telegram and WhatsApp are
built-in channels; you can add your own (Discord, Matrix, Signal, Slack, email,
a webhook for Home Assistant or n8n, …) by dropping one Python file into
`~/.aria/channels/`.

A channel only moves text in and out. Aria's host provides everything else:
- one session per `(channel, user)`, with its own conversation window and plan
- long-term memory shared across every channel
- all the tools
- idle timeouts and clean shutdown
- the delivery context, so the agent's `notify` / `send_file` tools reply on
  your channel

## Quick start

```bash
mkdir -p ~/.aria/channels
cp docs/examples/channels/webhook.py ~/.aria/channels/
aria-channel --list          # webhook shows up
aria-install                 # select it, answer its settings, get a systemd unit
# or by hand: add ARIA_CHANNELS=telegram,webhook to ~/.aria/.env and run
aria-channel webhook
```

## Writing a plugin

```python
# ~/.aria/channels/mychat.py
from aria.channels import ChannelPlugin, ConfigField


class MyChat(ChannelPlugin):
    name = "mychat"                              # [a-z][a-z0-9_-]*, unique
    description = "My chat service"
    config_fields = (                            # prompted by aria-install
        ConfigField("MYCHAT_TOKEN", prompt="Bot token", secret=True, required=True),
        ConfigField("MYCHAT_ALLOWED", prompt="Allowed user ids (comma-separated)",
                    required=True),
    )

    def run(self) -> None:                       # long-running receive loop
        from aria.channels import host
        import mychat_sdk                        # import SDKs here, not at top level
        allowed = set(host.parse_allowed("MYCHAT_ALLOWED"))
        client = mychat_sdk.Client(os.environ["MYCHAT_TOKEN"])
        try:
            for msg in client.messages():
                if msg.user not in allowed:
                    continue                     # fail closed
                for reply in host.handle_message(self.name, msg.user, msg.text):
                    client.send(msg.user, reply)
        finally:
            host.shutdown()

    def send(self, text: str, to: str | None = None) -> None:
        """Push (notify tool, supervisor results, aria --notify). `to=None`
        means: every allowed user. Raise RuntimeError with a readable reason."""
        ...


PLUGIN = MyChat()
```

### The contract (`aria.channels.base.ChannelPlugin`)

| Member | Required | Purpose |
|---|---|---|
| `name` | yes | Channel id. It's also the prefix of each conversation's window key (`mychat:<user>`). |
| `run()` | yes | Blocking receive loop. Call `host.handle_message()` for each message. |
| `send(text, to=None)` | for push | Deliver text. Without it, `notify` explains that the channel can't push. |
| `send_file(path, caption, to)` + `supports_files = True` | no | Enables the `send_file` tool on this channel. |
| `config_fields` | no | Settings the installer prompts for. By default the channel counts as "configured" when all its `required` fields are set. |
| `legacy_keys` | no | Env keys that auto-enable the channel when `ARIA_CHANNELS` is unset. |
| `services()` | no | systemd units. Default: one `aria-channel-<name>` unit running `aria-channel <name>`. |
| `install(dry_run)` | no | Pre-install hook, e.g. deploy helper files. Return notes to print. |
| `start(stop_event)` + `supports_attached = True` | no | Lets the channel run in attached mode (see below). |

### Host API (`aria.channels.host`)

| Function | Purpose |
|---|---|
| `handle_message(channel, user_id, text, response_cb=None, activity_cb=None)` | Runs one agent turn and returns the list of replies. The callbacks stream replies and tool progress mid-turn. It blocks, so call it from a worker thread in async code. |
| `get_agent(channel, user_id)` | The live Agent, for your own commands such as `/clear` → `agent.clear_session()`. |
| `shutdown()` | Close all sessions. Call it when `run()` exits. |
| `parse_allowed(var)` | Parse a comma-separated allow-list env var. |
| `record_feed(text)` | Log an outbound push so the agent has context when the user replies. |

### Rules
- **Keep the module top level cheap.** The registry imports every plugin
  whenever it's consulted: the notify tool, the supervisor, the installer.
  Import SDKs inside `run()` / `send()`.
- **Fail closed.** Only accept messages from configured users.
- **A broken plugin is skipped with a warning.** It never takes other
  channels down, and each channel runs as its own service.
- **Replacing a built-in needs `override = True`.** Without it, a user plugin
  named `telegram` or `whatsapp` is skipped. With it, your plugin takes over
  everywhere, and `aria-install` rewrites the existing unit (for example
  `aria-telegram`) to run your plugin, so two bots never poll the same token.
- **`ARIA_CHANNELS=none`** disables every channel, which is what the installer
  writes when you deselect them all. An empty value would fall back to legacy
  auto-enable.

## Attached mode: online only while Aria is open

By default a channel runs as a **service**, a background systemd unit that's
always online. A channel can instead run **attached**: it starts inside the
`aria` CLI when you open it and goes offline when you quit. Nothing runs in the
background and the system configuration isn't modified.

```ini
ARIA_CHANNEL_MODE_TELEGRAM=attached     # or answer "yes" in aria-install
```

```text
$ aria
  📱 telegram attached — messages reach Aria while this window is open
  You › /remote            # status
  You › /remote off        # go offline without quitting
  You › /remote on         # back online (works in service mode too, if no service is running)
```

- **Telegram supports attached mode. WhatsApp is service-only**, because it
  needs its separate Node process.
- **Conversations from the phone get their own session**, as with the
  service. They share long-term memory with the REPL. They also take the
  unattended tool policy, so `shell_run` never prompts your terminal on behalf
  of a remote message.
- **Messages sent while Aria is closed are dropped on start.** Stale requests
  never run unexpectedly.
- **Only one receiver at a time.** A per-channel run lock
  (`~/.aria/run/<name>.lock`) keeps the CLI and a service from both polling.
  - If a service is running, the CLI doesn't attach and tells you so.
  - A service started while the CLI is attached waits and takes over when you
    quit.
  - Switching a channel to attached in `aria-install` removes its old unit.
- **Logs go to `~/.aria/logs/attached.log`**, with secrets redacted, never to
  the terminal.

Plugin side: set `supports_attached = True` and implement `start(stop)`, a
receive loop that runs in a background thread and returns once `stop` is set.
It must not install signal handlers or exit the process.

### Remote control: drive the terminal session from your phone

With **control**, an attached channel's messages run in the terminal's own
session instead of a separate one. That means the same history, plan, working
directory and project context, so you continue from your phone exactly where
the terminal left off.

```ini
ARIA_CHANNEL_MODE_TELEGRAM=control      # attached + controls the session at startup
```
```text
  You › /remote control     # hand the session to the phone (attaches if needed)
  You › /remote release     # phone chats get their own session again (stays online)
  You › /remote off         # release and go offline
```

- **A phone message interrupts the prompt without losing what you were
  typing.** The prompt comes back afterwards with your text restored. If you
  were in the middle of a turn, the message waits until the turn finishes.
- **Phone turns render in the terminal** as `📱 telegram › …`, and the replies
  stream to the phone. Ctrl+C at the terminal interrupts them too.
- **Your local turns are mirrored to the phone**: first `💻 <your message>`,
  then each reply.
- **Phone turns keep the channel's delivery context.** `notify` replies on the
  phone, and `shell_run` uses the unattended policy, so nothing waits on a
  terminal prompt nobody is at.
- **Phone commands act on the terminal session.** Telegram's `/clear` and
  `/model` apply to it, because they act on "the conversation" and the
  conversation is the terminal's.
- **Every allowed user of the channel can drive the session.** Keep the
  allow-list to yourself.
- **When you quit `aria`, a message still waiting gets a reply** saying the
  session was closed.
- **Without prompt_toolkit**, which only happens on a minimal Windows install,
  phone messages run after you next press Enter.

No plugin changes are needed. The host routes a controlled channel's messages
to the terminal.

## Configuration

| Setting | Meaning |
|---|---|
| `ARIA_CHANNELS=telegram,mychat` | Enabled channels. Written by `aria-install`. |
| *(unset)* | Legacy mode: every channel whose settings are present is enabled. For example, `TELEGRAM_TOKEN` enables Telegram and `WHATSAPP_ALLOWED` enables WhatsApp. Pre-plugin installs keep working unchanged. |
| `ARIA_NOTIFY_CHANNEL=mychat` | Where pushes go outside a conversation. Default: `telegram` when enabled, otherwise the first enabled channel that can push. |
| `ARIA_CHANNELS_DIR` | Plugin directory. Default `~/.aria/channels`. |
| `ARIA_CHANNEL_MODE_<NAME>` | `service` (default), `attached`, or `control` (attached + remote control of the terminal session). |

## Commands

```bash
aria-channel --list        # discovered channels, which are enabled, push default
aria-channel <name>        # run one channel (what its systemd unit executes)
aria --notify --channel mychat "query"   # push a single-shot answer via a channel
```

## Compatibility
- `aria-telegram` and `aria-whatsapp` still work, and their systemd units are
  unchanged.
- The old modules (`aria.telegram_bot`, `aria.telegram_notify`,
  `aria.whatsapp_bridge`, `aria.whatsapp_notify`, `aria.whatsapp_deploy`) are
  aliases of the new ones under `aria.channels.telegram` /
  `aria.channels.whatsapp`, so custom tools importing them keep working.
