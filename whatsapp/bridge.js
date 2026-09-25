/**
 * ~/.aria/whatsapp/bridge.js
 *
 * Connects to WhatsApp via whatsapp-web.js and forwards each message (text,
 * or media + caption) to the Aria Python bridge (aria-whatsapp). The Python
 * side acknowledges IMMEDIATELY: {"queued": true} for a normal message, or
 * {"reply": "..."} for a command / approval answer, which is sent here. The
 * agent's replies to normal messages arrive later, one WhatsApp message each,
 * through the push listener below — so no HTTP call waits for an agent turn.
 *
 * Push listener (Python → WhatsApp: turn replies, the `notify` tool,
 * scheduled tasks, send_file):
 *   POST http://127.0.0.1:ARIA_WA_PUSH_PORT/send
 *     header X-Aria-Secret: <ARIA_WA_SECRET>
 *     body   {"to": "1234567890", "text": "..."}                  text
 *        or  {"to": "...", "caption": "...",
 *             "media": {"mimetype": "...", "filename": "...", "data": "<base64>"}}
 *   → {"ok": true}  |  {"ok": true, "media": true}  |  {"error": "..."}
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
 *   ARIA_WA_MAX_MB=16          (max file size, both directions)
 *   ARIA_WA_TIMEOUT=600        (seconds to wait for the Python bridge's answer;
 *                               it now answers at once — the long default only
 *                               matters against an older, synchronous aria-whatsapp)
 */

const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");
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
// The current Python bridge answers at once; an older one waits for the
// whole agent turn, which routinely exceeds two minutes.
const TIMEOUT_MS = (parseInt(process.env.ARIA_WA_TIMEOUT || "600") || 600) * 1000;
const MAX_BYTES  = Math.max(0, parseFloat(process.env.ARIA_WA_MAX_MB || "16") || 16) * 1024 * 1024;
// JSON bodies carry base64 (4/3 of the file) plus framing.
const MAX_BODY   = Math.ceil(MAX_BYTES * 4 / 3) + 1024 * 1024;
// Tells aria-whatsapp this bridge understands the push-based protocol.
const BRIDGE_VERSION = "2";
const MEDIA_TYPES = new Set(["image", "video", "audio", "ptt", "document", "sticker"]);

function mb(bytes) {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

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
  // Only handle text and media from real users (not groups, status, etc.)
  if (msg.isGroupMsg || msg.fromMe) return;
  const isMedia = Boolean(msg.hasMedia) && MEDIA_TYPES.has(msg.type);
  if (msg.type !== "chat" && !isMedia) return;

  // Strip @c.us suffix WhatsApp appends to numbers
  const sender = msg.from.replace(/@c\.us$/, "");

  // FAIL CLOSED like the Python side: an empty allowlist accepts nobody.
  if (!ALLOWED.includes(sender)) {
    console.log(`Ignored message from non-allowed sender: ${sender}`);
    return;
  }

  // For media, body is the caption ("" when there is none).
  const text = msg.body || "";
  console.log(`[${sender}]: ${isMedia ? `<${msg.type}> ` : ""}${text.slice(0, 80)}`);

  // Everything awaits inside try: a rejected getChat()/sendStateTyping() in an
  // async event handler would otherwise be an unhandled rejection (node exit).
  let chat = null;
  try {
    chat = await msg.getChat();
    await chat.sendStateTyping();

    const payload = { from: sender, text };
    if (isMedia) {
      const declared = Number((msg._data || {}).size) || 0;
      if (declared > MAX_BYTES) {
        await chat.clearState();
        await msg.reply(`That file is ${mb(declared)} — I only accept files up to ` +
                        `${mb(MAX_BYTES)} on WhatsApp. Could you send a smaller version?`);
        return;
      }
      const media = await msg.downloadMedia();
      if (!media || !media.data) {
        await chat.clearState();
        await msg.reply("I couldn't download that file. Please try sending it again.");
        return;
      }
      const size = Buffer.byteLength(media.data, "base64");
      if (size > MAX_BYTES) {
        await chat.clearState();
        await msg.reply(`That file is ${mb(size)} — I only accept files up to ` +
                        `${mb(MAX_BYTES)} on WhatsApp. Could you send a smaller version?`);
        return;
      }
      payload.media = {
        mimetype: media.mimetype || "application/octet-stream",
        filename: media.filename || "",
        data: media.data,
        kind: msg.type,
      };
    }

    const res = await callBridge(payload);
    // A command / approval answer comes back here; a normal message is
    // queued and its replies arrive through the push listener (typing stays
    // on until the first one lands).
    if (res.reply) {
      await chat.clearState();
      await msg.reply(res.reply);
    }
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

function callBridge(payload) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(payload);
    const options = {
      hostname: "127.0.0.1",
      port: PORT,
      path: "/message",
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
        "X-Aria-Bridge": BRIDGE_VERSION,
        ...(SECRET ? { "X-Aria-Secret": SECRET } : {}),
      },
    };

    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        let parsed;
        try {
          parsed = JSON.parse(data);
        } catch {
          reject(new Error(`Invalid JSON from bridge: ${data.slice(0, 200)}`));
          return;
        }
        if (res.statusCode >= 400 || !parsed || parsed.error) {
          reject(new Error((parsed && parsed.error) || `HTTP ${res.statusCode}`));
          return;
        }
        resolve(parsed);
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

    const chunks = [];
    let size = 0;
    let tooBig = false;
    req.on("data", (chunk) => {
      if (tooBig) return;
      size += chunk.length;
      if (size > MAX_BODY) {
        tooBig = true;           // drain the rest, answer 413 on "end"
        chunks.length = 0;
        return;
      }
      chunks.push(chunk);
    });
    req.on("error", (err) => console.error("Push request error:", err.message));
    req.on("end", async () => {
      if (tooBig) {
        reply(413, { error: `request too large (max ${mb(MAX_BYTES)} file)` });
        return;
      }
      let payload;
      try {
        payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      } catch {
        reply(400, { error: "invalid JSON" });
        return;
      }
      if (!payload || typeof payload !== "object") {
        reply(400, { error: "invalid JSON" });
        return;
      }

      const to    = (payload.to   || "").toString().trim();
      const text  = (payload.text || "").toString();
      const media = payload.media;
      if (!to || (!media && !text.trim())) {
        reply(400, { error: "missing 'to' or 'text'" });
        return;
      }
      if (media && (typeof media !== "object" || !media.data || !media.mimetype)) {
        reply(400, { error: "invalid 'media': need mimetype and data (base64)" });
        return;
      }

      if (!clientReady) {
        reply(503, { error: "WhatsApp client not ready" });
        return;
      }

      try {
        if (media) {
          const file = new MessageMedia(String(media.mimetype), String(media.data),
                                        media.filename ? String(media.filename) : undefined);
          const caption = (payload.caption || payload.text || "").toString();
          await client.sendMessage(`${to}@c.us`, file, {
            sendMediaAsDocument: true,
            ...(caption.trim() ? { caption } : {}),
          });
          reply(200, { ok: true, media: true });
        } else {
          await client.sendMessage(`${to}@c.us`, text);
          reply(200, { ok: true });
        }
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
