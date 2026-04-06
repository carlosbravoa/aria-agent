/**
 * ~/.aria/whatsapp/bridge.js
 *
 * Connects to WhatsApp via whatsapp-web.js and forwards messages to the
 * Aria Python bridge (aria-whatsapp), then sends the reply back.
 *
 * Setup:
 *   mkdir -p ~/.aria/whatsapp && cd ~/.aria/whatsapp
 *   npm init -y
 *   npm install whatsapp-web.js qrcode-terminal
 *   node bridge.js
 *
 * Config (read from ~/.aria/.env via process.env or direct assignment):
 *   ARIA_WA_PORT=7532
 *   ARIA_WA_SECRET=<same secret as in ~/.aria/.env>
 *   WHATSAPP_ALLOWED=1234567890,0987654321   (international format, no +)
 */

const { Client, LocalAuth } = require("whatsapp-web.js");
const qrcode = require("qrcode-terminal");
const http = require("http");
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

const PORT    = parseInt(process.env.ARIA_WA_PORT   || "7532");
const SECRET  = process.env.ARIA_WA_SECRET           || "";
const ALLOWED = (process.env.WHATSAPP_ALLOWED || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

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

client.on("authenticated", () => console.log("WhatsApp authenticated."));
client.on("ready",         () => console.log("WhatsApp client ready."));

client.on("disconnected", (reason) => {
  console.error("WhatsApp disconnected:", reason);
  process.exit(1);
});

client.on("message", async (msg) => {
  // Only handle plain text from real users (not groups, status, etc.)
  if (msg.isGroupMsg || msg.type !== "chat" || msg.fromMe) return;

  // Strip @c.us suffix WhatsApp appends to numbers
  const sender = msg.from.replace(/@c\.us$/, "");

  if (ALLOWED.length && !ALLOWED.includes(sender)) {
    console.log(`Ignored message from non-allowed sender: ${sender}`);
    return;
  }

  console.log(`[${sender}]: ${msg.body.slice(0, 80)}`);

  // Show "typing..." indicator
  const chat = await msg.getChat();
  await chat.sendStateTyping();

  try {
    const reply = await callBridge(sender, msg.body);
    await chat.clearState();
    await msg.reply(reply);
  } catch (err) {
    await chat.clearState();
    console.error("Bridge error:", err.message);
    await msg.reply("⚠️ Something went wrong. Please try again.");
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
    req.setTimeout(120000, () => {
      req.destroy();
      reject(new Error("Bridge request timed out"));
    });

    req.write(body);
    req.end();
  });
}

// ── Start ─────────────────────────────────────────────────────────────────────

console.log(`Connecting to Aria bridge at http://127.0.0.1:${PORT}`);
client.initialize();
