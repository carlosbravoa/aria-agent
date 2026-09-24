/**
 * ~/.aria/whatsapp/bridge.js
 *
 * Connects to WhatsApp via whatsapp-web.js and forwards messages to the
 * Aria Python bridge (aria-whatsapp), then sends the reply back.
 *
 * It ALSO runs a small local push listener so Python can PUSH a message to
 * WhatsApp out-of-band (the `notify` tool on a WhatsApp turn, scheduled tasks):
 *   POST http://127.0.0.1:ARIA_WA_PUSH_PORT/send
 *     header X-Aria-Secret: <ARIA_WA_SECRET>
 *     body   {"to": "1234567890", "text": "..."}
 *   → {"ok": true}  |  {"error": "..."}
 * The listener binds to 127.0.0.1 only and fails closed when no secret is set.
 *
 * Setup:
 *   mkdir -p ~/.aria/whatsapp && cd ~/.aria/whatsapp
 *   npm init -y
 *   npm install whatsapp-web.js qrcode-terminal
 *   node bridge.js
 *
 * Config (read from ~/.aria/.env via process.env or direct assignment):
 *   ARIA_WA_PORT=7532          (Python bridge this client POSTs inbound to)
 *   ARIA_WA_PUSH_PORT=7533     (local push listener Python POSTs outbound to)
 *   ARIA_WA_SECRET=<same secret as in ~/.aria/.env>
 *   WHATSAPP_ALLOWED=1234567890,0987654321   (international format, no +)
 *   ARIA_WA_TIMEOUT=600        (seconds to wait for an agent turn; keep in sync
 *                               with the Python bridge, which pushes the reply
 *                               out-of-band if a turn outlives this)
 */

const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const http = require("http");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

// ── Config ────────────────────────────────────────────────────────────────────

function loadEnv() {
  const envPath = path.join(process.env.HOME, ".aria", ".env");
  if (!fs.existsSync(envPath)) return;
  fs.readFileSync(envPath, "utf8")
    .split("\n")
    .forEach((line) => {
      line = line.trim();
      if (!line || line.startsWith("#") || !line.includes("=")) return;
      const [key, ...rest] = line.split("=");
      const value = rest.join("=").trim().replace(/^['"]|['"]$/g, "");
      if (!(key.trim() in process.env)) {
        process.env[key.trim()] = value;
      }
    });
}

loadEnv();

const PORT      = parseInt(process.env.ARIA_WA_PORT      || "7532");
const PUSH_PORT = parseInt(process.env.ARIA_WA_PUSH_PORT || "7533");
const SECRET  = process.env.ARIA_WA_SECRET           || "";
const ALLOWED = (process.env.WHATSAPP_ALLOWED || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);
// Agent turns (tool marathons, browser tasks) routinely exceed two minutes.
const TIMEOUT_MS = (parseInt(process.env.ARIA_WA_TIMEOUT || "600") || 600) * 1000;

// Constant-time secret comparison; length mismatch is a plain (safe) reject
// because timingSafeEqual throws on unequal lengths.
function secretMatches(given) {
  const a = Buffer.from(String(given || ""));
  const b = Buffer.from(SECRET);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// ── WhatsApp client ───────────────────────────────────────────────────────────

const client = new Client({
  authStrategy: new LocalAuth({
    dataPath: path.join(process.env.HOME, ".aria", "whatsapp", ".wwebjs_auth"),
  }),
  puppeteer: {
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

client.on("qr", (qr) => {
  console.log("\nScan this QR code with WhatsApp:\n");
  qrcode.generate(qr, { small: true });
});

let clientReady = false;
client.on("authenticated", () => console.log("WhatsApp authenticated."));
client.on("ready",         () => { clientReady = true; console.log("WhatsApp client ready."); });

client.on("disconnected", (reason) => {
  console.error("WhatsApp disconnected:", reason);
  process.exit(1);
});

client.on("message", async (msg) => {
  // Only handle plain text from real users (not groups, status, etc.)
  if (msg.isGroupMsg || msg.type !== "chat" || msg.fromMe) return;

  // Strip @c.us suffix WhatsApp appends to numbers
  const sender = msg.from.replace(/@c\.us$/, "");

  // FAIL CLOSED like the Python side: an empty allowlist accepts nobody.
  if (!ALLOWED.includes(sender)) {
    console.log(`Ignored message from non-allowed sender: ${sender}`);
    return;
  }

  console.log(`[${sender}]: ${msg.body.slice(0, 80)}`);

  // Everything awaits inside try: a rejected getChat()/sendStateTyping() in an
  // async event handler would otherwise be an unhandled rejection (node exit).
  let chat = null;
  try {
    chat = await msg.getChat();
    await chat.sendStateTyping();
    const reply = await callBridge(sender, msg.body);
    await chat.clearState();
    await msg.reply(reply);
  } catch (err) {
    console.error("Bridge error:", err.message);
    try {
      if (chat) await chat.clearState();
      await msg.reply(err.timedOut
        ? "⏳ Still working on that — I'll send the answer when it's ready."
        : "⚠️ Something went wrong. Please try again.");
    } catch (err2) {
      console.error("Error reply failed:", err2.message);
    }
  }
});

// ── HTTP call to Python bridge ────────────────────────────────────────────────

function callBridge(from, text) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ from, text });
    const options = {
      hostname: "127.0.0.1",
      port: PORT,
      path: "/message",
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
        ...(SECRET ? { "X-Aria-Secret": SECRET } : {}),
      },
    };

    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try {
          const parsed = JSON.parse(data);
          if (parsed.reply) resolve(parsed.reply);
          else reject(new Error(parsed.error || "Empty reply from bridge"));
        } catch {
          reject(new Error(`Invalid JSON from bridge: ${data}`));
        }
      });
    });

    req.on("error", reject);
    req.setTimeout(TIMEOUT_MS, () => {
      req.destroy();
      const err = new Error("Bridge request timed out");
      err.timedOut = true;
      reject(err);
    });

    req.write(body);
    req.end();
  });
}

// ── Push listener (Python → WhatsApp) ───────────────────────────────────────────
//
// A tiny stdlib HTTP server so the Python side can push a message out-of-band.
// Bound to 127.0.0.1 only; fails closed when ARIA_WA_SECRET is unset.

function startPushServer() {
  const server = http.createServer((req, res) => {
    const reply = (code, obj) => {
      const out = JSON.stringify(obj);
      res.writeHead(code, {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(out),
      });
      res.end(out);
    };

    if (req.method !== "POST" || req.url !== "/send") {
      reply(404, { error: "not found" });
      return;
    }

    // Auth via shared secret — FAIL CLOSED, mirroring the Python side.
    if (!SECRET) {
      reply(403, { error: "bridge not configured: set ARIA_WA_SECRET" });
      return;
    }
    if (!secretMatches(req.headers["x-aria-secret"])) {
      reply(403, { error: "forbidden" });
      return;
    }

    let data = "";
    req.on("data", (chunk) => (data += chunk));
    req.on("end", async () => {
      let payload;
      try {
        payload = JSON.parse(data);
      } catch {
        reply(400, { error: "invalid JSON" });
        return;
      }

      const to   = (payload.to   || "").toString().trim();
      const text = (payload.text || "").toString();
      if (!to || !text.trim()) {
        reply(400, { error: "missing 'to' or 'text'" });
        return;
      }

      if (!clientReady) {
        reply(503, { error: "WhatsApp client not ready" });
        return;
      }

      try {
        await client.sendMessage(`${to}@c.us`, text);
        reply(200, { ok: true });
      } catch (err) {
        console.error("Push send error:", err.message);
        reply(500, { error: err.message });
      }
    });
  });

  server.listen(PUSH_PORT, "127.0.0.1", () => {
    console.log(`Push listener on http://127.0.0.1:${PUSH_PORT}/send`);
  });
}

// ── Start ─────────────────────────────────────────────────────────────────────

console.log(`Connecting to Aria bridge at http://127.0.0.1:${PORT}`);
startPushServer();
client.initialize();
