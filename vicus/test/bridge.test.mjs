// Tests for vicus/bridge.mjs, with a fake Vicus client, MLS crate, transport and
// Cognito. Run with `node --test vicus/test/`. No network, no Vicus checkout needed.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  mkdtempSync, readFileSync, writeFileSync, existsSync, readdirSync, statSync, rmSync, mkdirSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { createBridge, loadVicusFrom, claimsOf, refreshDelayMs, newDeviceId } from '../bridge.mjs';

const BOT = 'bot@example.org';
const NOW = 1_800_000_000_000;

function jwt(claims) {
  const enc = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
  return `${enc({ alg: 'RS256' })}.${enc(claims)}.c2lnbmF0dXJl`;
}
const tokenFor = (n = 1, extra = {}) =>
  jwt({ tenantId: 'acme', accountId: BOT, exp: Math.floor(NOW / 1000) + 3600, n, ...extra });

// ── fakes ────────────────────────────────────────────────────────────────────

function makeWorld() {
  const world = {
    groups: {
      c1: [BOT, 'ana@example.org'],                       // 1:1 with ana
      c2: [BOT, 'ana@example.org', 'bob@example.org'],    // group
      c3: [BOT, 'bob@example.org', 'bob@example.org'],    // bob, two devices: still 1:1
    },
    clients: [],
    transports: [],
    sent: [],
    files: [],
    reads: [],
    seq: 100,
    failSend: null,
    restoreFails: false,
    connectError: null,
  };

  class MlsClient {
    constructor(acct) { this.acct = acct; this.state = `fresh:${acct}`; this.ops = 0; }
    static restoreState(s) {
      if (world.restoreFails) throw 'restore: bad snapshot'; // the crate throws strings
      const m = new MlsClient('restored');
      m.state = s;
      return m;
    }
    exportState() { return this.state; }
    groups() { return Object.keys(world.groups); }
    members(c) { return world.groups[c]; }
    bump() { this.ops++; this.state = `${this.state.split('#')[0]}#${this.ops}`; }
  }

  class MqttTransport {
    constructor(o) { this.o = o; this.connected = false; this.reconnects = 0; this.tokens = []; world.transports.push(this); }
    async connect() {
      if (world.connectError) {
        const e = world.connectError;
        world.connectError = null;
        throw new Error(`${e} wss://x.iot/mqtt?x-amz-customauthorizer-name=a&enclave-token=${this.o.token()}`);
      }
      this.tokens.push(this.o.token());
      this.connected = true;
    }
    async reconnect() { this.reconnects++; this.tokens.push(this.o.token()); this.connected = true; }
    isConnected() { return this.connected; }
    close() { this.connected = false; this.closed = true; }
  }

  class VicusClient {
    constructor(o) {
      this.o = o;
      this.imported = null;
      this.readMarks = {};
      this.resumes = 0;
      this.closed = false;
      this.name = undefined;
      world.clients.push(this);
    }
    importMarks(m) { this.imported = m; Object.assign(this.readMarks, m.read ?? {}); }
    exportMarks() { return { read: { ...this.readMarks }, ops: this.o.mls.ops }; }
    async start() { this.started = true; await world.onStart?.(this); }
    async resume() { this.resumes++; }
    close() { this.closed = true; }
    accounts(c) { return [...new Set(this.o.mls.members(c))].sort(); }
    joinedConversations() { return Object.keys(world.groups); }
    isBlocked(a) { return a === 'blocked@example.org'; }
    marks() { return { c1: 5, c3: 9 }; }
    async send(convId, text) {
      const id = `${convId}:${Date.now()}:${Math.random()}`;
      await this.o.outbox.add({ id, convId, text, createdAt: Date.now() });
      if (world.failSend) throw new Error(world.failSend);
      const seq = ++world.seq;
      this.o.mls.bump();
      world.sent.push({ convId, text, seq });
      await this.o.outbox.remove(id);
      this.o.onStateChange?.(true, convId);
      return seq;
    }
    async sendFile(convId, file, caption) {
      const bytes = Buffer.from(await new Response(file.bytes).arrayBuffer());
      const seq = ++world.seq;
      world.files.push({ convId, name: file.name, mime: file.mime, caption, bytes: bytes.toString() });
      this.o.onStateChange?.(true, convId);
      return { seq, attachment: {} };
    }
    async fetchAttachment(convId, a) {
      if (a.fail) throw new Error('fetch failed');
      return new TextEncoder().encode(`plain:${a.id}`).buffer;
    }
    async markRead(convId, seq) { world.reads.push([convId, seq]); this.readMarks[convId] = seq; }
    async setDisplayName(name) { this.name = name; this.o.onStateChange?.(false); }
    /** Test helper: what sync() does on decrypt — bump the ratchet, hand it over, mark. */
    deliver(m) {
      this.o.mls.bump();
      this.o.onMessage({ createdAt: NOW, text: '', ...m });
      this.o.onStateChange?.(true, m.convId);
    }
  }

  world.loadVicus = async () => ({ VicusClient, MqttTransport, MlsClient });
  return world;
}

function harness(opts = {}) {
  const world = opts.world ?? makeWorld();
  const stateDir = opts.stateDir ?? mkdtempSync(join(tmpdir(), 'vicus-bridge-'));
  const lines = [];
  const logs = [];
  const exits = [];
  const waiters = [];
  const auth = { signIns: 0, refreshes: 0 };
  const signIn = opts.signIn ?? (async () => {
    auth.signIns++;
    return {
      idToken: opts.token ?? tokenFor(1),
      refresh: async () => { auth.refreshes++; return tokenFor(2 + auth.refreshes); },
    };
  });
  const bridge = createBridge({
    env: {
      site: 'https://vicus.example.org',
      email: BOT,
      password: 'hunter2-secret',
      sourceDir: '/nonexistent/vicus',
      stateDir,
      displayName: opts.displayName,
      ...(opts.env ?? {}),
    },
    loadVicus: opts.loadVicus ?? world.loadVicus,
    fetchConfig: async () => opts.config ?? ({
      apiUrl: 'https://api.example.org', userPoolId: 'eu-west-1_x', userPoolClientId: 'cid',
      iotEndpoint: 'x-ats.iot.eu-west-1.amazonaws.com', authorizerName: 'auth',
    }),
    signIn,
    registerDevice: opts.registerDevice ?? (async () => { world.registered = (world.registered ?? 0) + 1; }),
    write: (line) => {
      assert.equal(typeof line, 'string');
      assert.ok(!line.includes('\n'), 'one line per event');
      const ev = JSON.parse(line);
      lines.push(ev);
      opts.onEvent?.(ev, stateDir);
      for (const w of [...waiters]) if (w.pred(ev)) { waiters.splice(waiters.indexOf(w), 1); w.resolve(ev); }
    },
    log: (t) => logs.push(t),
    exit: (code) => exits.push(code),
    now: () => NOW,
    timers: opts.timers,
    pid: opts.pid ?? 424242,
    isAlive: opts.isAlive ?? (() => false),
  });
  const waitFor = (pred, ms = 3000) => {
    const hit = lines.find(pred);
    if (hit) return Promise.resolve(hit);
    return new Promise((resolve, reject) => {
      const w = { pred, resolve };
      waiters.push(w);
      setTimeout(() => reject(new Error(`timed out; events: ${JSON.stringify(lines)}`)), ms).unref();
    });
  };
  const send = (obj) => bridge.handleLine(JSON.stringify(obj));
  const state = () => JSON.parse(readFileSync(join(stateDir, 'state.json'), 'utf8'));
  const client = () => world.clients.at(-1);
  return { world, stateDir, lines, logs, exits, bridge, waitFor, send, state, client, auth };
}

async function started(opts) {
  const h = harness(opts);
  await h.bridge.start();
  await h.waitFor((e) => e.type === 'ready');
  return h;
}

const tick = () => new Promise((r) => setImmediate(r));

// ── tests ────────────────────────────────────────────────────────────────────

test('helpers: claims, refresh delay, device id', () => {
  const t = tokenFor(1);
  assert.equal(claimsOf(t).accountId, BOT);
  assert.equal(refreshDelayMs(t, NOW), (3600 - 300) * 1000);
  assert.equal(refreshDelayMs(jwt({ exp: Math.floor(NOW / 1000) + 10 }), NOW), 60_000);
  const id = newDeviceId();
  assert.match(id, /^cli-aria-[0-9a-z]{8,}$/);
  assert.match(id, /^[A-Za-z0-9_-]{4,64}$/);
});

test('ready: registers, connects with the bare device id, persists the device id', async () => {
  const h = await started();
  const ready = h.lines[0];
  assert.deepEqual(Object.keys(ready).sort(), ['account', 'deviceId', 'tenant', 'type']);
  assert.equal(ready.account, BOT);
  assert.equal(ready.tenant, 'acme');
  assert.match(ready.deviceId, /^cli-aria-/);
  assert.equal(h.world.registered, 1);
  const tr = h.world.transports[0];
  assert.equal(tr.o.clientId, ready.deviceId);
  assert.equal(tr.o.tokenKeyName, 'enclave-token');
  assert.equal(h.client().o.platform, 'cli');
  const s = h.state();
  assert.equal(s.deviceId, ready.deviceId);
  assert.equal(s.snapshot, `fresh:${BOT}`);
  const mode = statSync(join(h.stateDir, 'state.json')).mode & 0o777;
  assert.equal(mode, 0o600);
  assert.equal(statSync(h.stateDir).mode & 0o777, 0o700);
  await h.bridge.shutdown();
  assert.deepEqual(h.exits, [0]);
});

test('message: persisted to the inbox before it is emitted', async () => {
  let onDiskAtEmit = null;
  const h = await started({
    onEvent: (ev, dir) => {
      if (ev.type === 'message') onDiskAtEmit = JSON.parse(readFileSync(join(dir, 'state.json'), 'utf8'));
    },
  });
  h.client().deliver({ convId: 'c1', seq: 7, from: 'ana@example.org', text: 'hola' });
  const msg = await h.waitFor((e) => e.type === 'message');
  assert.deepEqual(msg, {
    type: 'message', id: 'c1:7', convId: 'c1', seq: 7, from: 'ana@example.org', text: 'hola',
    createdAt: NOW, members: 2, attachment: null,
  });
  assert.ok(onDiskAtEmit.inbox.some((e) => e.id === 'c1:7'), 'inbox entry was on disk at emit time');
  // The MLS snapshot and the marks on disk agree (same instant).
  assert.equal(onDiskAtEmit.marks.ops, Number(onDiskAtEmit.snapshot.split('#')[1]));
  await h.bridge.shutdown();
});

test('group message reports member count; own-account and blocked messages are dropped', async () => {
  const h = await started();
  h.client().deliver({ convId: 'c2', seq: 1, from: BOT, text: 'from my laptop' });
  h.client().deliver({ convId: 'c2', seq: 2, from: 'blocked@example.org', text: 'spam' });
  h.client().deliver({ convId: 'c2', seq: 3, from: 'bob@example.org', text: 'hi all' });
  const msg = await h.waitFor((e) => e.type === 'message');
  assert.equal(msg.id, 'c2:3');
  assert.equal(msg.members, 3);
  await tick();
  assert.equal(h.lines.filter((e) => e.type === 'message').length, 1);
  assert.deepEqual(h.state().inbox.map((e) => e.id), ['c2:3']);
  await h.bridge.shutdown();
});

test('attachment: decrypted to incoming/ (0600) before emit; failures are reported', async () => {
  const h = await started();
  h.client().deliver({
    convId: 'c1', seq: 8, from: 'ana@example.org', text: 'see this',
    attachment: { id: 'obj1', k: 'k', iv: 'iv', name: '../photo.jpg', mime: 'image/jpeg', size: 40 },
  });
  const msg = await h.waitFor((e) => e.type === 'message');
  assert.equal(msg.attachment.name, '../photo.jpg');
  assert.equal(msg.attachment.mime, 'image/jpeg');
  assert.ok(msg.attachment.path.startsWith(join(h.stateDir, 'incoming')));
  assert.equal(readFileSync(msg.attachment.path, 'utf8'), 'plain:obj1');
  assert.equal(msg.attachment.size, 'plain:obj1'.length);
  assert.equal(statSync(msg.attachment.path).mode & 0o777, 0o600);
  assert.equal(h.state().inbox[0].attachment.path, msg.attachment.path);

  h.client().deliver({
    convId: 'c1', seq: 9, from: 'ana@example.org',
    attachment: { id: 'big', k: 'k', iv: 'iv', name: 'huge.bin', mime: 'application/octet-stream', size: 26 * 1024 * 1024 },
  });
  const big = await h.waitFor((e) => e.type === 'message' && e.seq === 9);
  assert.equal(big.attachment.path, null);
  assert.match(big.attachment.error, /too large/);
  await h.bridge.shutdown();
});

test('restart: unacked inbox entries are re-emitted; acked ones are not', async () => {
  const world = makeWorld();
  const h1 = await started({ world });
  h1.client().deliver({ convId: 'c1', seq: 1, from: 'ana@example.org', text: 'one' });
  h1.client().deliver({ convId: 'c1', seq: 2, from: 'ana@example.org', text: 'two' });
  await h1.waitFor((e) => e.type === 'message' && e.seq === 2);
  h1.send({ type: 'ack', id: 'c1:1' });
  await tick(); await tick();
  // Crash-like stop: no further acks.
  await h1.bridge.shutdown();

  const h2 = await started({ world, stateDir: h1.stateDir });
  const again = await h2.waitFor((e) => e.type === 'message');
  assert.equal(again.id, 'c1:2');
  assert.equal(again.text, 'two');
  assert.equal(h2.lines.filter((e) => e.type === 'message').length, 1);
  // The same device came back: restored, not recreated, and marks imported.
  assert.equal(h2.lines[0].deviceId, h1.lines[0].deviceId);
  assert.ok(h2.client().imported, 'marks imported before start');
  assert.match(h2.client().o.mls.state, /^fresh:/);
  await h2.bridge.shutdown();
});

test('ack removes from the inbox, marks read, and saves', async () => {
  const h = await started();
  h.client().deliver({ convId: 'c1', seq: 4, from: 'ana@example.org', text: 'x' });
  await h.waitFor((e) => e.type === 'message');
  h.send({ type: 'ack', id: 'c1:4', reqId: 'a1' });
  const r = await h.waitFor((e) => e.type === 'result' && e.reqId === 'a1');
  assert.equal(r.ok, true);
  assert.deepEqual(h.world.reads, [['c1', 4]]);
  const s = h.state();
  assert.deepEqual(s.inbox, []);
  assert.equal(s.marks.read.c1, 4);
  await h.bridge.shutdown();
});

test('send: result with seq; failures report the error and whether it is queued', async () => {
  const h = await started();
  h.send({ type: 'send', reqId: 's1', convId: 'c1', text: 'hello' });
  const r1 = await h.waitFor((e) => e.reqId === 's1');
  assert.deepEqual(r1, { type: 'result', reqId: 's1', ok: true, seq: 101 });
  assert.deepEqual(h.state().outbox, []);

  h.send({ type: 'send', reqId: 's2', convId: 'nope', text: 'hello' });
  const r2 = await h.waitFor((e) => e.reqId === 's2');
  assert.equal(r2.ok, false);
  assert.match(r2.error, /not a member/);

  h.world.failSend = 'POST /conversations/c1/messages -> 503';
  h.send({ type: 'send', reqId: 's3', convId: 'c1', text: 'later' });
  const r3 = await h.waitFor((e) => e.reqId === 's3');
  assert.equal(r3.ok, false);
  assert.equal(r3.queued, true);
  assert.match(r3.error, /503/);
  // The outbox (the client's Outbox interface) is in the state file.
  assert.deepEqual(h.state().outbox.map((i) => i.text), ['later']);
  await h.bridge.shutdown();
});

test('commands are serialised in arrival order', async () => {
  const h = await started();
  for (let i = 0; i < 5; i++) h.send({ type: 'send', reqId: `q${i}`, convId: 'c1', text: `m${i}` });
  await h.waitFor((e) => e.reqId === 'q4');
  assert.deepEqual(h.world.sent.map((s) => s.text), ['m0', 'm1', 'm2', 'm3', 'm4']);
  assert.deepEqual(h.lines.filter((e) => e.type === 'result').map((e) => e.reqId), ['q0', 'q1', 'q2', 'q3', 'q4']);
  await h.bridge.shutdown();
});

test('sendfile: sends the file with name, mime and caption; errors are results', async () => {
  const h = await started();
  const path = join(h.stateDir, 'report.txt');
  writeFileSync(path, 'file body');
  h.send({ type: 'sendfile', reqId: 'f1', convId: 'c1', path, name: 'Report.txt', mime: 'text/plain', caption: 'here' });
  const r1 = await h.waitFor((e) => e.reqId === 'f1');
  assert.equal(r1.ok, true);
  assert.equal(typeof r1.seq, 'number');
  assert.deepEqual(h.world.files[0], { convId: 'c1', name: 'Report.txt', mime: 'text/plain', caption: 'here', bytes: 'file body' });

  h.send({ type: 'sendfile', reqId: 'f2', convId: 'c1', path: join(h.stateDir, 'missing.bin'), name: 'm', mime: 'x/y' });
  const r2 = await h.waitFor((e) => e.reqId === 'f2');
  assert.equal(r2.ok, false);
  assert.match(r2.error, /cannot read/);
  await h.bridge.shutdown();
});

test('notify: only existing one-to-one conversations, never groups; ok:false if none', async () => {
  const h = await started();
  h.send({ type: 'notify', reqId: 'n1', accounts: ['Ana@Example.org', 'bob@example.org', 'zed@example.org'], text: 'ping' });
  const r1 = await h.waitFor((e) => e.reqId === 'n1');
  assert.equal(r1.ok, true);
  assert.deepEqual(r1.reached, ['ana@example.org', 'bob@example.org']);
  assert.deepEqual(r1.skipped, ['zed@example.org']);
  assert.deepEqual(h.world.sent.map((s) => s.convId), ['c1', 'c3']);

  delete h.world.groups.c1;
  h.send({ type: 'notify', reqId: 'n2', accounts: ['ana@example.org'], text: 'ping' });
  const r2 = await h.waitFor((e) => e.reqId === 'n2');
  assert.equal(r2.ok, false);
  assert.deepEqual(r2.skipped, ['ana@example.org']);
  assert.ok(r2.error);
  assert.equal(h.world.sent.length, 2, 'nothing sent into the group c2');
  await h.bridge.shutdown();
});

test('atomic save: snapshot and marks written together, no temp files left', async () => {
  const h = await started();
  for (let i = 0; i < 5; i++) {
    h.client().o.mls.bump();
    h.client().o.onStateChange(true, 'c1');
    const s = h.state();
    assert.equal(s.snapshot, h.client().o.mls.exportState());
    assert.equal(s.marks.ops, h.client().o.mls.ops);
  }
  assert.deepEqual(readdirSync(h.stateDir).filter((f) => f.endsWith('.tmp')), []);
  await h.bridge.shutdown();
});

test('non-urgent changes are debounced but saved by shutdown', async () => {
  const h = await started();
  h.client().o.mls.bump();
  h.client().o.onStateChange(false, 'c1');
  const expected = h.client().o.mls.exportState();
  assert.notEqual(h.state().snapshot, expected, 'not written synchronously');
  h.send({ type: 'shutdown' });
  await h.bridge.done;
  assert.equal(h.state().snapshot, expected);
  assert.equal(h.client().closed, true);
  assert.deepEqual(h.exits, [0]);
  assert.ok(!existsSync(join(h.stateDir, 'bridge.lock')), 'lock released');
});

test('stdin EOF shuts down cleanly', async () => {
  const h = await started();
  await h.bridge.end();
  assert.deepEqual(h.exits, [0]);
  assert.equal(h.client().closed, true);
});

test('token refresh is scheduled 5 min before expiry, then reconnect + resume with the new token', async () => {
  const scheduled = [];
  const timers = {
    setTimeout: (fn, ms) => { const t = { fn, ms, cleared: false }; scheduled.push(t); return t; },
    clearTimeout: (t) => { if (t) t.cleared = true; },
    setInterval: () => ({}),
    clearInterval: () => {},
  };
  const h = await started({ timers });
  const refresh = scheduled.find((t) => t.ms === (3600 - 300) * 1000 && !t.cleared);
  assert.ok(refresh, `a refresh timer at 55 min; got ${scheduled.map((t) => t.ms)}`);
  const tr = h.world.transports[0];
  const resumesBefore = h.client().resumes;
  refresh.fn();
  for (let i = 0; i < 10; i++) await tick();
  assert.equal(h.auth.refreshes, 1);
  assert.equal(tr.reconnects, 1);
  assert.equal(h.client().resumes, resumesBefore + 1);
  assert.equal(claimsOf(tr.tokens.at(-1)).n, 3, 'reconnected with the refreshed token');
  assert.equal(claimsOf(h.client().o.token()).n, 3, 'API calls use the refreshed token');
  assert.ok(scheduled.filter((t) => t.ms === (3600 - 300) * 1000).length >= 2, 'next refresh scheduled');
  // shutdown with fake timers: the grace sleep never fires, but the chain is idle
  await h.bridge.shutdown();
});

test('fatal: token without tenant claims', async () => {
  const h = harness({ token: jwt({ accountId: BOT, exp: Math.floor(NOW / 1000) + 3600 }) });
  await h.bridge.start();
  const f = h.lines.find((e) => e.type === 'fatal');
  assert.ok(f);
  assert.match(f.error, /not part of any group yet/);
  assert.deepEqual(h.exits, [1]);
});

test('fatal: bad credentials', async () => {
  const h = harness({
    signIn: async () => { throw Object.assign(new Error('Incorrect username or password.'), { code: 'NotAuthorizedException' }); },
  });
  await h.bridge.start();
  assert.match(h.lines.find((e) => e.type === 'fatal').error, /rejected/);
  assert.deepEqual(h.exits, [1]);
  assert.ok(!h.logs.join('\n').includes('hunter2-secret'));
});

test('fatal: missing Vicus build artefacts, with a build hint', async () => {
  const src = mkdtempSync(join(tmpdir(), 'vicus-src-'));
  mkdirSync(join(src, 'packages', 'client'), { recursive: true });
  const h = harness({ loadVicus: loadVicusFrom, env: { sourceDir: src } });
  await h.bridge.start();
  const f = h.lines.find((e) => e.type === 'fatal');
  assert.ok(f);
  assert.match(f.error, /dist\/index\.js/);
  assert.match(f.error, /wasm-pack build --target nodejs/);
  assert.deepEqual(h.exits, [1]);
  rmSync(src, { recursive: true, force: true });
});

test('fatal: missing env', async () => {
  const h = harness({ env: { sourceDir: '' } });
  await h.bridge.start();
  assert.match(h.lines.find((e) => e.type === 'fatal').error, /VICUS_SOURCE_DIR/);
});

test('fatal: another bridge holds the lock (and the lock is left alone)', async () => {
  const stateDir = mkdtempSync(join(tmpdir(), 'vicus-bridge-'));
  writeFileSync(join(stateDir, 'bridge.lock'), '999');
  const h = harness({ stateDir, isAlive: (p) => p === 999 });
  await h.bridge.start();
  assert.match(h.lines.find((e) => e.type === 'fatal').error, /already running/);
  assert.equal(readFileSync(join(stateDir, 'bridge.lock'), 'utf8'), '999');
  assert.equal(h.world.clients.length, 0, 'never connected');
});

test('a stale lock (dead pid) is taken over', async () => {
  const stateDir = mkdtempSync(join(tmpdir(), 'vicus-bridge-'));
  writeFileSync(join(stateDir, 'bridge.lock'), '999');
  const h = await started({ stateDir, isAlive: () => false });
  assert.equal(readFileSync(join(stateDir, 'bridge.lock'), 'utf8'), '424242');
  await h.bridge.shutdown();
});

test('fatal: a snapshot that will not restore is never replaced by a fresh device', async () => {
  const world = makeWorld();
  const stateDir = mkdtempSync(join(tmpdir(), 'vicus-bridge-'));
  const saved = { deviceId: 'cli-aria-abcdefgh', account: BOT, snapshot: 'OLD', marks: { read: {} }, outbox: [], inbox: [] };
  writeFileSync(join(stateDir, 'state.json'), JSON.stringify(saved));
  world.restoreFails = true;
  const h = harness({ world, stateDir });
  await h.bridge.start();
  assert.match(h.lines.find((e) => e.type === 'fatal').error, /could not restore/);
  assert.deepEqual(JSON.parse(readFileSync(join(stateDir, 'state.json'), 'utf8')), saved);
  assert.equal(world.clients.length, 0);
});

test('fatal: state belongs to another account', async () => {
  const stateDir = mkdtempSync(join(tmpdir(), 'vicus-bridge-'));
  writeFileSync(join(stateDir, 'state.json'), JSON.stringify({ deviceId: 'cli-aria-abcdefgh', account: 'other@example.org', snapshot: 'S' }));
  const h = harness({ stateDir });
  await h.bridge.start();
  assert.match(h.lines.find((e) => e.type === 'fatal').error, /belongs to other@example.org/);
});

test('connect failures retry, and never leak the token or MQTT URL to stderr', async () => {
  const world = makeWorld();
  world.connectError = 'connack timeout';
  const h = harness({ world });
  // Real timers: the first retry waits 1 s.
  await h.bridge.start();
  await h.waitFor((e) => e.type === 'ready', 5000);
  const all = h.logs.join('\n');
  assert.match(all, /connecting failed/);
  assert.ok(!all.includes(tokenFor(1)), 'token not logged');
  assert.ok(!/enclave-token=eyJ/.test(all), 'MQTT URL token not logged');
  assert.ok(h.lines.every((e) => ['ready', 'message', 'result', 'fatal'].includes(e.type)));
  await h.bridge.shutdown();
});

test('display name is set once and remembered', async () => {
  const world = makeWorld();
  const h1 = await started({ world, displayName: 'Aria' });
  assert.equal(h1.client().name, 'Aria');
  assert.equal(h1.state().displayNameSent, 'Aria');
  await h1.bridge.shutdown();
  const h2 = await started({ world, stateDir: h1.stateDir, displayName: 'Aria' });
  assert.equal(h2.client().name, undefined, 'not re-sent');
  await h2.bridge.shutdown();
});

test('commands sent before ready wait for it; unknown/garbage input is harmless', async () => {
  const world = makeWorld();
  let release;
  world.onStart = () => new Promise((r) => { release = r; });
  const h = harness({ world });
  const startP = h.bridge.start();
  h.bridge.handleLine('not json');
  h.send({ type: 'send', reqId: 'early', convId: 'c1', text: 'queued before ready' });
  h.send({ type: 'bogus', reqId: 'b1' });
  while (!release) await tick();
  assert.equal(h.lines.length, 0);
  release();
  await startP;
  const r = await h.waitFor((e) => e.reqId === 'early');
  assert.equal(r.ok, true);
  const b = await h.waitFor((e) => e.reqId === 'b1');
  assert.equal(b.ok, false);
  assert.equal(h.lines[0].type, 'ready');
  await h.bridge.shutdown();
});
