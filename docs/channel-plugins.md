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
| `output = ChannelFormat(...)` | no | What your channel can show: `markdown="commonmark" \| "basic" \| "whatsapp" \| "plain"`, `tables`, `headings`, `long_reply_chars`, `surface`. The default is basic Markdown, no tables or headings, 3000 characters. |
| `send_approval(code, summary, to, expires_min)` | no | How approval requests look. The default is a text message answered with `yes <code>`. |
| `config_fields` | no | Settings the installer prompts for. By default the channel counts as "configured" when all its `required` fields are set. |
| `legacy_keys` | no | Env keys that auto-enable the channel when `ARIA_CHANNELS` is unset. |
| `services()` | no | systemd units. Default: one `aria-channel-<name>` unit running `aria-channel <name>`. |
| `install(dry_run)` | no | Pre-install hook, e.g. deploy helper files. Return notes to print. |
| `start(stop_event)` + `supports_attached = True` | no | Lets the channel run in attached mode (see below). |

### What every channel gets from the host

- **Shared commands.** `/stop`, `/clear`, `/memory`, `/tools`, `/models`,
  `/model <name>`, `/save <note>`, `/version`, `/help`.
  `host.handle_message()` runs them without starting a turn. If your transport
  processes messages concurrently, call `host.run_command()` directly, so that
  `/stop` doesn't wait behind the running turn.
- **Approvals.** When a turn on your channel needs approval for a risky
  action, Aria calls `send_approval(code, summary, to)`. The default sends a
  text asking the user to reply `yes <code>` / `no <code>`; `handle_message()`
  or `host.answer_approval()` records the reply. Override `send_approval` for
  buttons, as Telegram does.
  - Handle approval answers **before** any per-chat lock or queue. The turn
    waiting for the answer is holding them.
  - A transport that handles one message at a time can't deliver the answer
    while the turn waits. Set `answers_approvals = False` so approvals fail
    fast instead of timing out.

### Output: fitting replies to your channel

A terminal renders anything; a chat app renders a small subset, usually on a
phone. Declare what yours can show with `output = ChannelFormat(...)` and Aria
handles both sides:

1. **The model is told, on every turn,** where its reply will be read and what
   that surface shows ("You are replying on Mychat, a chat app on a phone.
   It shows bold, italic, code and lists. It does NOT show tables or
   headings…"). This includes remote-control turns from the phone and
   scheduled tasks whose result is pushed to you.
2. **Whatever still doesn't fit is converted before your channel sees it:**
   - Tables become bullet lists ("• **Alice** — Age: 30, City: Madrid").
   - Headings become bold.
   - Markdown becomes your dialect (`whatsapp`: `*bold*`, `_italic_`;
     `plain`: stripped).
   - A reply longer than `long_reply_chars` goes out as its first part, with
     the full text attached as a `.md` file, if you support `send_file`.

   Code blocks are never touched.

| Built-in | `output` |
|---|---|
| Telegram | `basic`, no tables or headings, 3500 characters |
| WhatsApp | `whatsapp`, no tables or headings, 3000 characters |
| Vicus | `commonmark` with tables and headings, 6000 characters |

### Host API (`aria.channels.host`)

| Function | Purpose |
|---|---|
| `handle_message(channel, user_id, text, response_cb=None, activity_cb=None)` | Runs one agent turn and returns the list of replies. The callbacks stream replies and tool progress mid-turn. It blocks, so call it from a worker thread in async code. |
| `run_command(channel, user_id, text)` | Run a shared slash command. Returns the reply text (light Markdown), or `None` if the text isn't a command. |
| `answer_approval(channel, user_id, text)` | Handle a `yes 1234` / `no 1234` reply. Returns the reply, or `None`. |
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
  📱 telegram is online in this window
  You › /channel                 # status of every channel
  You › /channel off telegram    # offline
  You › /channel on telegram     # back online in this window (pauses its service if one runs)
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
  You › /channel control telegram   # hand the session to the phone (brings it online if needed)
  You › /channel release telegram   # phone chats get their own session again (stays online)
  You › /channel off telegram       # release and go offline
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
| *(entry points)* | Installed packages can provide plugins through the `aria.channels` entry-point group: `[project.entry-points."aria.channels"] mychat = "aria_mychat"`. |
| `ARIA_CHANNEL_MODE_<NAME>` | `service` (default), `attached`, or `control` (attached + remote control of the terminal session). |

## Commands

```bash
aria-channel --list        # discovered channels, which are enabled, push default
# in the aria REPL:
/channel                   # every channel, in plain words
/channel on <name> [--always]   # this window, or the background (service)
/channel off <name>        # offline everywhere   (also: control, release, setup, restart, logs)
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
