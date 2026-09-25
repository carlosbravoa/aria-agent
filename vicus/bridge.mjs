#!/usr/bin/env node
/**
 * Aria <-> Vicus bridge.
 *
 * A long-running sidecar, spawned by Aria's Python Vicus channel, that runs ONE
 * Vicus device (an MLS end-to-end-encrypted messenger client) for the bot account
 * and speaks JSON lines over stdio:
 *
 *   stdin  (commands): ack · send · sendfile · notify · shutdown
 *   stdout (events):   ready · message · result · fatal
 *
 * Nothing but protocol lines is ever written to stdout; diagnostics go to stderr,
 * redacted (never the ID token, never the MQTT URL that carries it).
 *
 * The Vicus client itself is NOT part of this repository: it is loaded at runtime
 * from a user-supplied Vicus checkout (VICUS_SOURCE_DIR):
 *   packages/client/dist/index.js         VicusClient, MqttTransport
 *   packages/core-crypto/pkg/core_crypto.js   Client (MLS), `wasm-pack --target nodejs`
 *
 * Durability rules (Vicus PROTOCOL §7.1): the MLS state, the sync marks, the outbox
 * and the not-yet-handled inbox are written together, atomically, to one file. A
 * decrypted message is in that file before it is emitted, and stays there until Aria
 * acks it, so a crash anywhere between decrypting and answering never loses it.
 *
 * `createBridge(deps)` holds all the logic and takes its dependencies injected, so
 * the tests can drive it with fakes; running this file directly wires the real ones.
 */

import {
  mkdirSync, chmodSync, openSync, writeSync, fsyncSync, closeSync, renameSync,
  readFileSync, existsSync, unlinkSync, statSync, writeFileSync,
} from 'node:fs';
import * as fsp from 'node:fs/promises';
import { join, basename, resolve } from 'node:path';
import { homedir } from 'node:os';
import { randomBytes } from 'node:crypto';
import { pathToFileURL, fileURLToPath } from 'node:url';

// ── constants ────────────────────────────────────────────────────────────────

const SAVE_DEBOUNCE_MS = 300;
/** Refresh at least this long before the ID token expires (PROTOCOL §1.2). */
const REFRESH_HEADROOM_S = 300;
/** Never schedule a refresh sooner than this, so clock skew cannot spin a loop. */
const REFRESH_MIN_S = 60;
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
const SNAPSHOT_CHECK_MS = 30_000;
const WATCHDOG_MS = 60_000;
/** Consecutive watchdog ticks without a connection before we step in. The client's
 *  own recovery (≈6 attempts over ~1 min) gets the first go. */
const WATCHDOG_STRIKES = 3;
const BACKOFF_MAX_MS = 60_000;
const DOWNLOAD_ATTEMPTS = 3;
const SHUTDOWN_GRACE_MS = 5_000;

const BUILD_HINT =
  'build it in your Vicus checkout: `npm --prefix packages/client ci && ' +
  'npm --prefix packages/client run build`, and in packages/core-crypto ' +
  '`wasm-pack build --target nodejs` (needs Rust and `cargo install wasm-pack`)';

// ── small helpers ────────────────────────────────────────────────────────────

/** JWT claims, without verifying (the server verifies; we only read our own token). */
export function claimsOf(token) {
  try {
    const payload = String(token).split('.')[1] ?? '';
    return JSON.parse(Buffer.from(payload, 'base64url').toString('utf8')) ?? {};
  } catch {
    return {};
  }
}

export function secondsUntilExpiry(token, nowMs) {
  const exp = Number(claimsOf(token).exp ?? 0);
  return exp ? Math.max(0, exp - Math.floor(nowMs / 1000)) : 0;
}

/** Delay before the next refresh: ≥5 min before expiry, never under a minute. */
export function refreshDelayMs(token, nowMs) {
  return Math.max(REFRESH_MIN_S, secondsUntilExpiry(token, nowMs) - REFRESH_HEADROOM_S) * 1000;
}

export function newDeviceId() {
  // `cli-aria-` + 12 base36 characters; matches [A-Za-z0-9_-]{4,64} (§1.3).
  const alphabet = '0123456789abcdefghijklmnopqrstuvwxyz';
  const bytes = randomBytes(12);
  let s = '';
  for (const b of bytes) s += alphabet[b % 36];
  return `cli-aria-${s}`;
}

function errText(err) {
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  try { return JSON.stringify(err); } catch { return String(err); }
}

const normAccount = (a) => String(a ?? '').trim().toLowerCase();

function safeFileName(name) {
  const cleaned = String(name || 'file').replace(/[\x00-\x1f/\\]/g, '_').replace(/^\.+/, '_');
  return cleaned.slice(-120) || 'file';
}

class FatalError extends Error {}

function isAuthRejection(err) {
  const code = err?.code ?? err?.name;
  return err?.fatal === true || [
    'NotAuthorizedException', 'UserNotFoundException', 'PasswordResetRequiredException',
    'UserNotConfirmedException', 'NewPasswordRequired',
  ].includes(code);
}

// ── the bridge ───────────────────────────────────────────────────────────────

/**
 * @param {object} deps
 * @param {object} deps.env        { site, email, password, sourceDir, stateDir, displayName }
 * @param {Function} deps.loadVicus  (sourceDir) => Promise<{VicusClient, MqttTransport, MlsClient}>
 * @param {Function} deps.fetchConfig (site) => Promise<config.json>
 * @param {Function} deps.signIn   (cfg, email, password) => Promise<{idToken, refresh(): Promise<string>}>
 * @param {Function} [deps.registerDevice] (cfg, token, deviceId) => Promise<void>
 * @param {Function} deps.write    (line: string) => void   — one stdout line, no newline
 * @param {Function} [deps.log]    (text: string) => void   — stderr
 * @param {Function} [deps.exit]   (code) => void
 * @param {Function} [deps.now]
 * @param {object}   [deps.timers] { setTimeout, clearTimeout, setInterval, clearInterval }
 * @param {number}   [deps.pid]
 * @param {Function} [deps.isAlive] (pid) => boolean
 */
export function createBridge(deps) {
  const env = deps.env ?? {};
  const now = deps.now ?? Date.now;
  const timers = deps.timers ?? { setTimeout, clearTimeout, setInterval, clearInterval };
  const pid = deps.pid ?? process.pid;
  const isAlive = deps.isAlive ?? defaultIsAlive;
  const registerDevice = deps.registerDevice ?? defaultRegisterDevice;
  const exit = deps.exit ?? ((code) => process.exit(code));

  const stateDir = resolve(env.stateDir || join(homedir(), '.aria', 'vicus'));
  const statePath = join(stateDir, 'state.json');
  const lockPath = join(stateDir, 'bridge.lock');
  const incomingDir = join(stateDir, 'incoming');

  // Secrets we must never print.
  const secrets = new Set();
  if (env.password) secrets.add(env.password);
  const redact = (text) => {
    let s = String(text);
    for (const secret of secrets) if (secret && secret.length >= 4) s = s.split(secret).join('[redacted]');
    s = s.replace(/(enclave-token=)[^&\s"'`]+/gi, '$1[redacted]');
    s = s.replace(/eyJ[\w-]{4,}\.[\w-]{4,}\.[\w-]+/g, '[redacted-jwt]');
    s = s.replace(/wss:\/\/[^\s"'`]*\/mqtt\?[^\s"'`]*/gi, 'wss://[redacted-mqtt-url]');
    return s;
  };
  const log = (...parts) => {
    const text = parts.map((p) => (typeof p === 'string' ? p : errText(p))).join(' ');
    try { (deps.log ?? ((t) => process.stderr.write(t + '\n')))(redact(`[vicus-bridge] ${text}`)); } catch { /* ignore */ }
  };
  const emit = (obj) => deps.write(JSON.stringify(obj));

  // ── runtime state ──
  /** What lives in state.json. */
  let st = { deviceId: null, account: null, snapshot: null, marks: null, outbox: [], inbox: [] };
  let lockHeld = false;
  let mls = null;
  let mlsTrusted = false; // true once `mls` is ours to save (restored or freshly created)
  let client = null;
  let transport = null;
  let cfg = null;
  let session = null;
  let token = null;
  let account = null;
  let tenant = null;
  let lastSavedSnapshot = null;
  let saveTimer = null;
  let flushScheduled = false;
  let ready = false;
  let closing = false;
  let finished = false;
  let refreshTimer = null;
  let refreshing = null;
  const intervals = [];
  let readyResolve;
  const readyPromise = new Promise((r) => { readyResolve = r; });
  let doneResolve;
  const done = new Promise((r) => { doneResolve = r; });
  let closingResolve;
  const closingPromise = new Promise((r) => { closingResolve = r; });
  const markClosing = () => { closing = true; closingResolve(); };
  /** Inbox ids waiting to be emitted this run, in order. */
  const emitQueue = [];
  const emitted = new Set();
  let pumping = false;
  let commandChain = Promise.resolve();

  const sleep = (ms) => new Promise((r) => { timers.setTimeout(r, ms); });

  // ── persistence ──

  function ensureDir(dir) {
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    try { chmodSync(dir, 0o700); } catch { /* not ours to fix */ }
  }

  /** Atomic write: unique temp file, fsync, rename, fsync the directory. */
  function atomicWrite(path, text) {
    const tmp = `${path}.${pid}.${randomBytes(6).toString('hex')}.tmp`;
    let fd = openSync(tmp, 'wx', 0o600);
    try {
      const buf = Buffer.from(text, 'utf8');
      let off = 0;
      while (off < buf.length) off += writeSync(fd, buf, off);
      fsyncSync(fd);
    } catch (err) {
      try { closeSync(fd); } catch { /* ignore */ }
      fd = null;
      try { unlinkSync(tmp); } catch { /* ignore */ }
      throw err;
    } finally {
      if (fd !== null) closeSync(fd);
    }
    renameSync(tmp, path);
    try {
      const dfd = openSync(stateDir, 'r');
      try { fsyncSync(dfd); } finally { closeSync(dfd); }
    } catch { /* not every platform can fsync a directory */ }
  }

  /**
   * Writes everything, now, in one file: MLS snapshot and marks captured at the same
   * instant (they must never disagree — §7.1), plus outbox and inbox.
   */
  function saveNow() {
    if (saveTimer) { timers.clearTimeout(saveTimer); saveTimer = null; }
    if (!st.deviceId) return;
    try {
      if (mls && mlsTrusted) {
        st.snapshot = mls.exportState();
        if (client) st.marks = client.exportMarks();
      }
      atomicWrite(statePath, JSON.stringify(st));
      lastSavedSnapshot = st.snapshot;
    } catch (err) {
      log('could not save state:', errText(err));
    }
  }

  function scheduleSave(urgent) {
    if (urgent) { saveNow(); return; }
    if (saveTimer) return;
    saveTimer = timers.setTimeout(() => { saveTimer = null; saveNow(); }, SAVE_DEBOUNCE_MS);
    saveTimer?.unref?.();
  }

  function loadState() {
    if (!existsSync(statePath)) return null;
    const text = readFileSync(statePath, 'utf8');
    try {
      return JSON.parse(text);
    } catch (err) {
      throw new FatalError(`the state file ${statePath} is corrupt (${errText(err)}); ` +
        'refusing to start over it — move it aside to start as a new device');
    }
  }

  // ── lock: one bridge per state dir, one live connection per device ──

  function acquireLock() {
    for (let i = 0; i < 3; i++) {
      try {
        const fd = openSync(lockPath, 'wx', 0o600);
        try { writeSync(fd, String(pid)); } finally { closeSync(fd); }
        lockHeld = true;
        return true;
      } catch (err) {
        if (err.code !== 'EEXIST') throw err;
        let other = NaN;
        try { other = parseInt(readFileSync(lockPath, 'utf8').trim(), 10); } catch { /* vanished */ }
        if (Number.isInteger(other) && other > 0 && other !== pid && isAlive(other)) return false;
        try { unlinkSync(lockPath); } catch { /* raced */ }
      }
    }
    return false;
  }

  function releaseLock() {
    if (!lockHeld) return;
    lockHeld = false;
    try {
      if (parseInt(readFileSync(lockPath, 'utf8').trim(), 10) === pid) unlinkSync(lockPath);
    } catch { /* already gone */ }
  }

  // ── outbox (the client's Outbox interface, backed by state.json) ──

  const outbox = {
    async add(item) {
      st.outbox.push({ id: item.id, convId: item.convId, text: item.text, createdAt: item.createdAt });
      saveNow();
    },
    async remove(id) {
      const before = st.outbox.length;
      st.outbox = st.outbox.filter((i) => i.id !== id);
      if (st.outbox.length !== before) saveNow();
    },
    async all() {
      return st.outbox.map((i) => ({ ...i }));
    },
  };

  // ── inbox ──

  function membersOf(convId) {
    try { return client.accounts(convId).length; } catch { return 0; }
  }

  function onClientMessage(m) {
    // Rows from another device of the bot's own account: never answer ourselves.
    if (normAccount(m.from) === account) return;
    try { if (client?.isBlocked?.(m.from)) return; } catch { /* ignore */ }
    const id = `${m.convId}:${m.seq}`;
    if (st.inbox.some((e) => e.id === id)) return;
    const a = m.attachment;
    st.inbox.push({
      id,
      convId: m.convId,
      seq: m.seq,
      from: m.from,
      text: m.text ?? '',
      createdAt: m.createdAt,
      members: membersOf(m.convId),
      attachment: a ? { meta: a, path: null, name: a.name, mime: a.mime, size: a.len ?? a.size } : null,
    });
    emitQueue.push(id);
    // Saved once the client has finished its synchronous bookkeeping for this batch
    // (markSatisfied runs right after the handler), then emitted.
    if (!flushScheduled) {
      flushScheduled = true;
      queueMicrotask(() => {
        flushScheduled = false;
        saveNow();
        void pump();
      });
    }
  }

  async function downloadAttachment(entry) {
    const att = entry.attachment;
    const a = att.meta;
    const declared = Number(a?.len ?? a?.size ?? 0);
    if (declared > MAX_ATTACHMENT_BYTES) {
      att.error = `attachment too large (${declared} bytes; limit ${MAX_ATTACHMENT_BYTES})`;
      return;
    }
    let lastErr;
    for (let attempt = 0; attempt < DOWNLOAD_ATTEMPTS; attempt++) {
      if (closing) return;
      try {
        const bytes = Buffer.from(await client.fetchAttachment(entry.convId, a));
        if (bytes.length > MAX_ATTACHMENT_BYTES) {
          att.error = `attachment too large (${bytes.length} bytes; limit ${MAX_ATTACHMENT_BYTES})`;
          return;
        }
        ensureDir(incomingDir);
        const path = join(incomingDir, `${entry.convId}-${entry.seq}-${safeFileName(a.name)}`);
        writeFileSync(path, bytes, { mode: 0o600 });
        try { chmodSync(path, 0o600); } catch { /* ignore */ }
        att.path = path;
        att.size = bytes.length;
        delete att.error;
        return;
      } catch (err) {
        lastErr = err;
        if (attempt < DOWNLOAD_ATTEMPTS - 1) await sleep(1000 * 4 ** attempt);
      }
    }
    att.error = `could not fetch the attachment: ${errText(lastErr)}`;
  }

  function wireMessage(e) {
    const att = e.attachment;
    return {
      type: 'message',
      id: e.id,
      convId: e.convId,
      seq: e.seq,
      from: e.from,
      text: e.text,
      createdAt: e.createdAt,
      members: e.members,
      attachment: att
        ? { path: att.path, name: att.name, mime: att.mime, size: att.size, ...(att.error ? { error: att.error } : {}) }
        : null,
    };
  }

  /** Emits queued inbox entries in order, each only after it is on disk. */
  async function pump() {
    if (!ready || pumping) return;
    pumping = true;
    try {
      while (emitQueue.length && !closing) {
        const id = emitQueue.shift();
        if (emitted.has(id)) continue;
        const entry = st.inbox.find((e) => e.id === id);
        if (!entry) continue; // acked meanwhile
        const att = entry.attachment;
        if (att && att.meta && (!att.path || !existsSync(att.path))) {
          att.path = null;
          await downloadAttachment(entry);
          if (closing) break;
          saveNow();
        }
        if (!st.inbox.includes(entry)) continue;
        emitted.add(id);
        emit(wireMessage(entry));
      }
    } catch (err) {
      log('emitting messages failed:', errText(err));
    } finally {
      pumping = false;
    }
  }

  // ── auth ──

  async function refreshToken() {
    refreshing ??= (async () => {
      let fresh;
      try {
        fresh = await session.refresh();
      } catch (err) {
        log('session refresh failed, signing in again:', errText(err));
        try {
          session = await deps.signIn(cfg, env.email, env.password);
          fresh = session.idToken;
        } catch (err2) {
          if (isAuthRejection(err2)) throw new FatalError(`Vicus sign-in rejected: ${errText(err2)}`);
          throw err2;
        }
      }
      const c = claimsOf(fresh);
      if (!c.tenantId || !c.accountId) {
        throw new FatalError('the refreshed token has no tenant claims: this account is not part of any group yet');
      }
      token = fresh;
      secrets.add(fresh);
    })().finally(() => { refreshing = null; });
    return refreshing;
  }

  function scheduleRefresh() {
    if (closing) return;
    if (refreshTimer) timers.clearTimeout(refreshTimer);
    const delay = refreshDelayMs(token, now());
    refreshTimer = timers.setTimeout(() => { refreshTimer = null; void refreshAndReconnect(); }, delay);
    refreshTimer?.unref?.();
  }

  /** §1.2 + §3.4: fresh token, then a new connection with it, then resume. */
  async function refreshAndReconnect() {
    if (closing) return;
    try {
      await refreshToken();
      if (closing) return;
      await transport.reconnect();
      await client.resume();
      saveNow();
      scheduleRefresh();
    } catch (err) {
      if (err instanceof FatalError) return fatal(err.message);
      log('token refresh / reconnect failed, retrying in a minute:', errText(err));
      if (closing) return;
      refreshTimer = timers.setTimeout(() => { refreshTimer = null; void refreshAndReconnect(); }, REFRESH_MIN_S * 1000);
      refreshTimer?.unref?.();
    }
  }

  // ── start-up ──

  async function withBackoff(what, fn) {
    for (let attempt = 0; ; attempt++) {
      if (closing) throw new Error('shutting down');
      try {
        return await fn();
      } catch (err) {
        if (err instanceof FatalError) throw err;
        const delay = Math.min(BACKOFF_MAX_MS, 1000 * 2 ** attempt);
        log(`${what} failed (attempt ${attempt + 1}), retrying in ${Math.round(delay / 1000)}s:`, errText(err));
        await sleep(delay);
      }
    }
  }

  async function connectAndStart() {
    await withBackoff('connecting', async () => {
      if (secondsUntilExpiry(token, now()) < REFRESH_HEADROOM_S + 60) await refreshToken();
      // The authorizer refuses an unregistered client id, and the client's start()
      // registers only after the socket is up (§1.3), so register first.
      await registerDevice(cfg, token, st.deviceId);
      try {
        await transport.connect();
        await client.start();
      } catch (err) {
        // Start clean next time: no half-open socket, no duplicate listeners.
        try { transport.close(); } catch { /* ignore */ }
        throw err;
      }
    });
  }

  async function start() {
    try {
      for (const [name, key] of [['VICUS_SITE', 'site'], ['VICUS_EMAIL', 'email'],
        ['VICUS_PASSWORD', 'password'], ['VICUS_SOURCE_DIR', 'sourceDir']]) {
        if (!env[key]) throw new FatalError(`${name} is not set`);
      }
      ensureDir(stateDir);
      if (!acquireLock()) {
        throw new FatalError(`another Vicus bridge is already running on ${stateDir} ` +
          '(one device, one connection); stop it first');
      }
      const saved = loadState();
      if (saved) st = { ...st, ...saved, outbox: saved.outbox ?? [], inbox: saved.inbox ?? [] };
      if (!st.deviceId) {
        st.deviceId = newDeviceId();
        saveNow();
      }
      // Anything left from a previous run goes out first, once we are ready.
      for (const e of st.inbox) emitQueue.push(e.id);

      let vicus;
      try {
        vicus = await deps.loadVicus(env.sourceDir);
      } catch (err) {
        throw new FatalError(errText(err));
      }
      const { VicusClient, MqttTransport, MlsClient } = vicus;

      cfg = await withBackoff('fetching config.json', () => deps.fetchConfig(env.site));
      for (const k of ['apiUrl', 'userPoolId', 'userPoolClientId', 'iotEndpoint', 'authorizerName']) {
        if (typeof cfg?.[k] !== 'string' || !cfg[k]) {
          throw new FatalError(`${env.site}/config.json has no ${k}: is VICUS_SITE a Vicus deployment?`);
        }
      }

      session = await withBackoff('signing in', async () => {
        try {
          return await deps.signIn(cfg, env.email, env.password);
        } catch (err) {
          if (isAuthRejection(err)) throw new FatalError(`Vicus sign-in rejected for ${env.email}: ${errText(err)}`);
          throw err;
        }
      });
      token = session.idToken;
      secrets.add(token);
      const claims = claimsOf(token);
      if (!claims.tenantId || !claims.accountId) {
        throw new FatalError(`${env.email} is not part of any group yet (the token carries no ` +
          'tenantId/accountId claims) — ask an admin of the Vicus deployment to invite it');
      }
      account = normAccount(claims.accountId);
      tenant = String(claims.tenantId);
      if (st.account && st.account !== account) {
        throw new FatalError(`the state in ${stateDir} belongs to ${st.account}, not ${account}; ` +
          'use another VICUS_STATE_DIR');
      }

      if (st.snapshot) {
        try {
          mls = MlsClient.restoreState(st.snapshot);
        } catch (err) {
          // Never silently start a fresh device over a saved one: it would rejoin
          // every conversation by external commit and abandon its old leaves.
          throw new FatalError(`could not restore the saved device state (${errText(err)}); refusing ` +
            `to start a fresh device over it. Move ${statePath} aside to start over as a new device`);
        }
      } else {
        mls = new MlsClient(account);
      }
      mlsTrusted = true;
      st.account = account;
      saveNow(); // the identity must be on disk before any key package is published

      transport = new MqttTransport({
        endpoint: cfg.iotEndpoint,
        authorizerName: cfg.authorizerName,
        tokenKeyName: 'enclave-token',
        token: () => token,
        // The bare device id: MqttTransport derives the account-bound client id
        // (`${sha256(tenant\0account)[:16]}-${deviceId}`, §3.1) from the token itself.
        clientId: st.deviceId,
      });
      client = new VicusClient({
        apiUrl: cfg.apiUrl,
        token: () => token,
        accountId: account,
        tenantId: tenant,
        deviceId: st.deviceId,
        platform: 'cli',
        mls,
        transport,
        outbox,
        onStateChange: (urgent) => scheduleSave(Boolean(urgent)),
        onMessage: onClientMessage,
      });
      // Marks before start(): start() syncs, and a client that does not know where it
      // got to refetches from sequence one and fails to decrypt it.
      if (st.marks) client.importMarks(st.marks);

      await connectAndStart();
      if (closing) return;
      saveNow(); // key packages minted during start live only in the MLS state

      if (env.displayName && env.displayName !== st.displayNameSent) {
        try {
          await client.setDisplayName(env.displayName);
          st.displayNameSent = env.displayName;
          saveNow();
        } catch (err) {
          log('could not set the display name:', errText(err));
        }
      }

      scheduleRefresh();
      startBackgroundChecks();
      ready = true;
      emit({ type: 'ready', account, tenant, deviceId: st.deviceId });
      readyResolve();
      void pump();
    } catch (err) {
      if (closing && !(err instanceof FatalError)) return;
      fatal(err instanceof FatalError ? err.message : `start-up failed: ${errText(err)}`);
    }
  }

  function startBackgroundChecks() {
    // Key packages minted during a sync don't always raise onStateChange; catch any
    // MLS change that was not saved.
    const snap = timers.setInterval(() => {
      if (!ready || closing) return;
      try {
        if (mls.exportState() !== lastSavedSnapshot) saveNow();
      } catch (err) {
        log('snapshot check failed:', errText(err));
      }
    }, SNAPSHOT_CHECK_MS);
    snap?.unref?.();
    intervals.push(snap);

    // The client recovers from a dropped socket with its own backoff (§3.4) and gives
    // up after ~6 attempts; this only steps in when it has been down for a while.
    let strikes = 0;
    let busy = false;
    const dog = timers.setInterval(() => {
      if (!ready || closing || busy) return;
      const up = typeof transport.isConnected === 'function' ? transport.isConnected() : true;
      if (up) { strikes = 0; return; }
      if (++strikes < WATCHDOG_STRIKES) return;
      strikes = 0;
      busy = true;
      log('connection has been down for a while; reconnecting');
      (async () => {
        if (secondsUntilExpiry(token, now()) < REFRESH_HEADROOM_S) await refreshToken();
        await transport.reconnect();
        await client.resume();
      })().catch((err) => {
        if (err instanceof FatalError) fatal(err.message);
        else log('watchdog reconnect failed:', errText(err));
      }).finally(() => { busy = false; });
    }, WATCHDOG_MS);
    dog?.unref?.();
    intervals.push(dog);
  }

  // ── commands ──

  function holds(convId) {
    try {
      const groups = mls.groups?.();
      if (Array.isArray(groups)) return groups.includes(convId);
    } catch { /* fall through */ }
    return client.joinedConversations().includes(convId);
  }

  function heldGroups() {
    try {
      const groups = mls.groups?.();
      if (Array.isArray(groups)) return groups;
    } catch { /* fall through */ }
    return client.joinedConversations();
  }

  /** The one-to-one conversation (exactly the bot and `other`) with `other`, if any. */
  function directWith(other) {
    const marks = (() => { try { return client.marks(); } catch { return {}; } })();
    const found = heldGroups().filter((c) => {
      try {
        const accts = client.accounts(c).map(normAccount);
        return accts.length === 2 && accts.includes(account) && accts.includes(other);
      } catch { return false; }
    });
    found.sort((a, b) => (marks[b] ?? 0) - (marks[a] ?? 0));
    return found[0] ?? null;
  }

  async function sendText(convId, text) {
    const before = new Set(st.outbox.map((i) => i.id));
    try {
      return { ok: true, seq: await client.send(convId, text) };
    } catch (err) {
      // The client keeps a failed send in the outbox and replays it on the next resume.
      const queued = st.outbox.some((i) => !before.has(i.id));
      return { ok: false, error: errText(err), ...(queued ? { queued: true } : {}) };
    }
  }

  async function runCommand(cmd) {
    const reqId = cmd.reqId;
    const result = (body) => {
      if (reqId !== undefined) emit({ type: 'result', reqId, ...body });
    };
    try {
      await Promise.race([readyPromise, closingPromise]);
      if (closing) return result({ ok: false, error: 'the bridge is shutting down' });
      switch (cmd.type) {
        case 'ack': {
          const idx = st.inbox.findIndex((e) => e.id === cmd.id);
          if (idx < 0) return result({ ok: true });
          const [entry] = st.inbox.splice(idx, 1);
          const read = client.markRead(entry.convId, entry.seq);
          saveNow();
          await Promise.resolve(read).catch((err) => log('markRead failed:', errText(err)));
          saveNow(); // the read mark is part of the marks
          if (entry.attachment?.path) {
            try { unlinkSync(entry.attachment.path); } catch { /* Aria moved it */ }
          }
          return result({ ok: true });
        }
        case 'send': {
          if (typeof cmd.convId !== 'string' || typeof cmd.text !== 'string') {
            return result({ ok: false, error: 'send needs convId and text' });
          }
          if (!holds(cmd.convId)) return result({ ok: false, error: `not a member of conversation ${cmd.convId}` });
          return result(await sendText(cmd.convId, cmd.text));
        }
        case 'sendfile': {
          if (typeof cmd.convId !== 'string' || typeof cmd.path !== 'string') {
            return result({ ok: false, error: 'sendfile needs convId and path' });
          }
          if (!holds(cmd.convId)) return result({ ok: false, error: `not a member of conversation ${cmd.convId}` });
          let info;
          try { info = statSync(cmd.path); } catch (err) { return result({ ok: false, error: `cannot read ${cmd.path}: ${errText(err)}` }); }
          if (!info.isFile()) return result({ ok: false, error: `${cmd.path} is not a file` });
          const mime = cmd.mime || 'application/octet-stream';
          const bytes = typeof fsp.openAsBlob === 'function'
            ? await fsp.openAsBlob(cmd.path, { type: mime })
            : new Uint8Array(readFileSync(cmd.path));
          try {
            const { seq } = await client.sendFile(
              cmd.convId,
              { name: cmd.name || basename(cmd.path), mime, bytes },
              cmd.caption || undefined,
            );
            return result({ ok: true, seq });
          } catch (err) {
            return result({ ok: false, error: errText(err) });
          }
        }
        case 'notify': {
          const accounts = [...new Set((Array.isArray(cmd.accounts) ? cmd.accounts : []).map(normAccount).filter(Boolean))];
          if (typeof cmd.text !== 'string' || !cmd.text) return result({ ok: false, error: 'notify needs text' });
          const reached = [];
          const skipped = [];
          const failed = [];
          for (const acct of accounts) {
            if (acct === account) { skipped.push(acct); continue; }
            const convId = directWith(acct);
            if (!convId) { skipped.push(acct); continue; }
            const r = await sendText(convId, cmd.text);
            if (r.ok) reached.push(acct);
            else failed.push({ account: acct, error: r.error, ...(r.queued ? { queued: true } : {}) });
          }
          const body = { ok: reached.length > 0, reached, skipped, failed };
          if (!reached.length) {
            body.error = accounts.length
              ? 'no listed account could be reached (a one-to-one conversation with the bot is needed)'
              : 'notify needs accounts';
          }
          return result(body);
        }
        default:
          return result({ ok: false, error: `unknown command ${JSON.stringify(cmd.type)}` });
      }
    } catch (err) {
      log(`command ${cmd.type} failed:`, errText(err));
      return result({ ok: false, error: errText(err) });
    }
  }

  function handleLine(line) {
    const text = String(line).trim();
    if (!text) return;
    let cmd;
    try {
      cmd = JSON.parse(text);
    } catch {
      log('ignoring a line that is not JSON');
      return;
    }
    if (!cmd || typeof cmd !== 'object') return;
    if (cmd.type === 'shutdown') { void shutdown(0); return; }
    if (closing) {
      if (cmd.reqId !== undefined) emit({ type: 'result', reqId: cmd.reqId, ok: false, error: 'the bridge is shutting down' });
      return;
    }
    commandChain = commandChain.then(() => runCommand(cmd));
  }

  // ── shutdown / fatal ──

  function stopTimers() {
    if (refreshTimer) { timers.clearTimeout(refreshTimer); refreshTimer = null; }
    for (const i of intervals.splice(0)) timers.clearInterval(i);
  }

  function finish(code) {
    if (finished) return;
    finished = true;
    releaseLock();
    doneResolve(code);
    exit(code);
  }

  async function shutdown(code = 0) {
    if (closing) return done;
    markClosing();
    // Let a command already in flight finish (bounded), so its result is reported.
    let grace;
    await Promise.race([
      commandChain.catch(() => {}),
      new Promise((r) => { grace = timers.setTimeout(r, SHUTDOWN_GRACE_MS); }),
    ]);
    timers.clearTimeout(grace);
    stopTimers();
    if (mlsTrusted) saveNow();
    try { client?.close(); } catch (err) { log('close failed:', errText(err)); }
    try { transport?.close(); } catch { /* ignore */ }
    if (mlsTrusted) saveNow();
    finish(code);
    return done;
  }

  function fatal(message) {
    if (finished) return;
    markClosing();
    stopTimers();
    emit({ type: 'fatal', error: redact(message) });
    log('fatal:', message);
    if (mlsTrusted) saveNow();
    try { client?.close(); } catch { /* ignore */ }
    try { transport?.close(); } catch { /* ignore */ }
    finish(1);
  }

  return {
    start,
    handleLine,
    /** stdin reached EOF. */
    end: () => shutdown(0),
    shutdown,
    fatal,
    redact,
    done,
    /** For tests and diagnostics. */
    _internals: () => ({ st, client, transport, mls, token, statePath, lockPath, saveNow }),
  };
}

// ── real dependencies ────────────────────────────────────────────────────────

function defaultIsAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === 'EPERM';
  }
}

async function defaultRegisterDevice(cfg, token, deviceId) {
  const res = await fetch(`${cfg.apiUrl}/devices`, {
    method: 'POST',
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
    body: JSON.stringify({ deviceId, platform: 'cli' }),
  });
  if (!res.ok) {
    const err = new Error(`POST /devices -> ${res.status}`);
    err.status = res.status;
    throw err;
  }
}

/** Loads the Vicus client and MLS crate from a checkout, or explains how to build them. */
export async function loadVicusFrom(sourceDir) {
  const root = resolve(String(sourceDir || ''));
  const clientJs = join(root, 'packages', 'client', 'dist', 'index.js');
  const cryptoJs = join(root, 'packages', 'core-crypto', 'pkg', 'core_crypto.js');
  const missing = [clientJs, cryptoJs].filter((p) => !existsSync(p));
  if (missing.length) {
    throw new Error(`Vicus build artefacts missing under VICUS_SOURCE_DIR (${root}): ` +
      `${missing.join(', ')} — ${BUILD_HINT}`);
  }
  const clientMod = await import(pathToFileURL(clientJs).href);
  const cryptoMod = await import(pathToFileURL(cryptoJs).href);
  const MlsClient = cryptoMod.Client ?? cryptoMod.default?.Client;
  const { VicusClient, MqttTransport } = clientMod;
  if (!VicusClient || !MqttTransport || !MlsClient) {
    throw new Error(`the Vicus checkout at ${root} does not export VicusClient/MqttTransport/Client ` +
      `(wrong version, or core-crypto not built with --target nodejs) — ${BUILD_HINT}`);
  }
  return { VicusClient, MqttTransport, MlsClient };
}

/** §10.1: the deployment's discovery document. */
export async function fetchSiteConfig(site) {
  let origin = String(site).trim().replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(origin)) origin = `https://${origin}`;
  const res = await fetch(`${origin}/config.json`, { cache: 'no-store' });
  if (!res.ok) throw new Error(`GET ${origin}/config.json -> ${res.status}`);
  return res.json();
}

/**
 * USER_SRP_AUTH sign-in with amazon-cognito-identity-js (§1.2), as the web app does.
 * Returns the ID token and a `refresh()` that uses the Cognito refresh token.
 */
export async function cognitoSignIn(cfg, email, password) {
  const mod = await import('amazon-cognito-identity-js');
  const lib = mod.default ?? mod;
  const { CognitoUserPool, CognitoUser, AuthenticationDetails } = lib;
  const mem = new Map();
  const Storage = {
    getItem: (k) => (mem.has(k) ? mem.get(k) : null),
    setItem: (k, v) => { mem.set(k, String(v)); },
    removeItem: (k) => { mem.delete(k); },
    clear: () => { mem.clear(); },
  };
  const pool = new CognitoUserPool({ UserPoolId: cfg.userPoolId, ClientId: cfg.userPoolClientId, Storage });
  const user = new CognitoUser({ Username: email, Pool: pool, Storage });
  const first = await new Promise((resolveP, reject) => {
    user.authenticateUser(new AuthenticationDetails({ Username: email, Password: password }), {
      onSuccess: resolveP,
      onFailure: reject,
      newPasswordRequired: () => {
        const err = new Error('this account needs its password set before first use (sign in once on the web)');
        err.code = 'NewPasswordRequired';
        reject(err);
      },
      mfaRequired: () => reject(Object.assign(new Error('MFA is not supported for the bot account'), { fatal: true })),
      totpRequired: () => reject(Object.assign(new Error('MFA is not supported for the bot account'), { fatal: true })),
    });
  });
  let refreshTok = first.getRefreshToken();
  return {
    idToken: first.getIdToken().getJwtToken(),
    async refresh() {
      const s = await new Promise((resolveP, reject) => {
        user.refreshSession(refreshTok, (err, sess) => (err ? reject(err) : resolveP(sess)));
      });
      const rotated = s.getRefreshToken?.();
      if (rotated?.getToken?.()) refreshTok = rotated;
      return s.getIdToken().getJwtToken();
    },
  };
}

/** Writes one line to stdout synchronously (so nothing is lost on exit). */
function stdoutLine(line) {
  const buf = Buffer.from(line + '\n', 'utf8');
  let off = 0;
  while (off < buf.length) {
    try {
      off += writeSync(1, buf, off);
    } catch (err) {
      if (err.code !== 'EAGAIN') throw err;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 5);
    }
  }
}

async function main() {
  const bridge = createBridge({
    env: {
      site: process.env.VICUS_SITE,
      email: process.env.VICUS_EMAIL,
      password: process.env.VICUS_PASSWORD,
      sourceDir: process.env.VICUS_SOURCE_DIR,
      stateDir: process.env.VICUS_STATE_DIR || join(homedir(), '.aria', 'vicus'),
      displayName: (process.env.VICUS_DISPLAY_NAME || '').trim() || undefined,
    },
    loadVicus: loadVicusFrom,
    fetchConfig: fetchSiteConfig,
    signIn: cognitoSignIn,
    write: stdoutLine,
    log: (t) => process.stderr.write(t + '\n'),
  });

  // stdout belongs to the protocol: anything a library prints goes to stderr, redacted.
  const toErr = (...args) => {
    const text = args.map((a) => (typeof a === 'string' ? a : a instanceof Error ? (a.stack || a.message) : (() => {
      try { return JSON.stringify(a); } catch { return String(a); }
    })())).join(' ');
    process.stderr.write(bridge.redact(text) + '\n');
  };
  console.log = toErr;
  console.info = toErr;
  console.warn = toErr;
  console.error = toErr;
  console.debug = toErr;

  process.on('unhandledRejection', (reason) => {
    toErr('[vicus-bridge] unhandled rejection:', reason instanceof Error ? (reason.stack || reason.message) : errText(reason));
  });
  process.on('uncaughtException', (err) => {
    toErr('[vicus-bridge] uncaught exception:', err?.stack || errText(err));
    bridge.fatal(`internal error: ${errText(err)}`);
  });
  for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.on(sig, () => void bridge.shutdown(0));

  const { createInterface } = await import('node:readline');
  const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on('line', (line) => bridge.handleLine(line));
  rl.on('close', () => void bridge.end());

  await bridge.start();
}

const invokedDirectly = (() => {
  try {
    return process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
  } catch {
    return false;
  }
})();
if (invokedDirectly) {
  main().catch((err) => {
    process.stderr.write(`[vicus-bridge] ${errText(err)}\n`);
    try { stdoutLine(JSON.stringify({ type: 'fatal', error: errText(err) })); } catch { /* ignore */ }
    process.exit(1);
  });
}

