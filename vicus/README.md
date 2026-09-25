# Vicus bridge for Aria

`bridge.mjs` is the Node side of Aria's Vicus channel. Vicus is an MLS
end-to-end-encrypted messenger. The bridge runs one Vicus device for the bot's
account: it signs in, keeps the device's MLS state, receives and decrypts
messages, and sends replies. Aria's Python channel (`src/aria/channels/vicus/`)
starts it as a child process and talks to it over stdio in JSON lines.

This repository does not contain any Vicus code. At runtime the bridge loads
the Vicus client and its crypto crate from a Vicus checkout that you provide
and build yourself.

## Prerequisites

- Node.js 20 or newer.
- A built Vicus checkout, pointed to by `VICUS_SOURCE_DIR`:

  ```bash
  cd /path/to/vicus
  npm --prefix packages/client ci
  npm --prefix packages/client run build          # -> packages/client/dist/index.js

  # the MLS crate, built for Node (needs Rust: https://rustup.rs)
  cargo install wasm-pack
  (cd packages/core-crypto && wasm-pack build --target nodejs)   # -> packages/core-crypto/pkg/
  ```

  If either artefact is missing, the bridge exits with a `fatal` event that
  explains how to build it.
- The bridge's own dependency (`amazon-cognito-identity-js`, for SRP sign-in).
  `aria-install` copies `bridge.mjs`, `package.json` and `package-lock.json` to
  `~/.aria/vicus-bridge/` and tells you to run `npm ci` there. To install it by
  hand, run `npm ci` in this directory.
- A Vicus account for the bot that is already a member of a tenant. An admin
  invites it, and you sign in once on the web to set its password. If the
  account's token has no `tenantId`/`accountId` claims, the bridge stops with
  "not part of any group yet".

## Environment

| variable | meaning |
|---|---|
| `VICUS_SITE` | the deployment's web origin, e.g. `https://vicus.example.org`. The bridge reads `<site>/config.json` from it. |
| `VICUS_EMAIL`, `VICUS_PASSWORD` | the bot account's credentials |
| `VICUS_SOURCE_DIR` | the built Vicus checkout (see above) |
| `VICUS_STATE_DIR` | where the device lives (default `~/.aria/vicus`) |
| `VICUS_DISPLAY_NAME` | optional. The bridge sends it as the bot's display name when it changes. |

The state directory has mode 0700 and holds:

- `state.json` (mode 0600). This is the device: its id, the MLS snapshot, the
  sync marks, the outbox, and every message Aria has not yet acknowledged. All
  of it is written in one atomic write. Back it up like a private key. If you
  lose it, the bot rejoins every conversation as a new device.
- `incoming/`: decrypted attachments that are waiting for Aria (0600, up to 25 MiB each).
- `bridge.lock`: stops a second bridge from running the same device.

## Stdio protocol

The bridge reads commands on stdin and writes events on stdout, one JSON object
per line. stdout carries nothing else. Diagnostics go to stderr, and the bridge
redacts tokens, the MQTT URL and the password before writing them.

Events:

```json
{"type":"ready","account":"bot@x","tenant":"acme","deviceId":"cli-aria-…"}
{"type":"message","id":"<convId>:<seq>","convId":"…","seq":7,"from":"ana@x","text":"hola",
 "createdAt":1727000000000,"members":2,"attachment":null}
{"type":"result","reqId":"…","ok":true,"seq":12}
{"type":"result","reqId":"…","ok":false,"error":"…","queued":true}
{"type":"fatal","error":"…"}
```

- `members` is the number of distinct accounts in the conversation, counting the bot.
  A value of 2 means a one-to-one conversation.
- `attachment` is either `null` or `{"path","name","mime","size"}`. The file at
  `path` is already decrypted. If the bridge could not fetch the file, or it is
  over the size cap, `path` is `null` and an `error` field explains why.
- `queued: true` on a failed `send` means the text is still in the outbox and
  the client will send it again on the next resume.
- The bridge writes `fatal` and then exits non-zero. Causes include bad
  credentials, a token without claims, missing artefacts, a held lock, and a
  saved state that cannot be restored. The bridge never replaces a saved device
  with a new one.

Commands:

```json
{"type":"ack","id":"<convId>:<seq>"}
{"type":"send","reqId":"…","convId":"…","text":"…"}
{"type":"sendfile","reqId":"…","convId":"…","path":"/abs/file","name":"a.pdf","mime":"application/pdf","caption":"…"}
{"type":"notify","reqId":"…","accounts":["ana@x"],"text":"…"}
{"type":"shutdown"}
```

- The bridge handles commands one at a time, in the order they arrive.
  Commands that arrive before `ready` wait for it.
- `ack` removes the message from the saved inbox, marks it read, and saves.
  Until Aria acks a message, the bridge sends it again every time it starts.
  So a crash between decrypting a message and handling it cannot lose the message.
- `notify` sends into the existing one-to-one conversation with each listed
  account and never creates a conversation. Its result is
  `{"ok","reached":[…],"skipped":[…],"failed":[{account,error}]}`. `ok` is
  false only if no account was reached.
- `shutdown`, EOF on stdin, SIGTERM and SIGINT all make the bridge save, close
  the connection and exit 0.

## How it behaves

- **Sign-in.** It uses Cognito `USER_SRP_AUTH` with the pool that `config.json`
  names. It refreshes the ID token with the refresh token at least 5 minutes
  before expiry, then reconnects MQTT and resumes (PROTOCOL §1.2, §3.4). If the
  refresh fails, it signs in again with the password.
- **Device.** The device id is `cli-aria-<12 base36>`, created once and stored
  in `state.json`, with platform `cli`. The bridge registers the device with
  `POST /devices` before it opens the socket. The account-bound MQTT client id
  is derived by the Vicus `MqttTransport`.
- **Reconnects.** After a dropped connection the Vicus client reconnects with
  its own backoff. If the connection has been down for about 3 minutes, a
  watchdog steps in.
- **Messages the bridge does not pass on.** It drops rows written by another
  device of the bot's own account, and rows from accounts the bot has blocked.
  The client never emits control envelopes, such as renames, profiles and
  retractions.

## Tests

```bash
node --check vicus/bridge.mjs
node --test vicus/test/
```

The tests use a fake Vicus client, MLS crate, transport and Cognito. They need
no network and no Vicus checkout.
