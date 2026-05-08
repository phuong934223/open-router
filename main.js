const http = require("http");
const https = require("https");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const process = require("process");

// ─── Logger ───────────────────────────────────────────────────────────────────

const LEVELS = { DEBUG: 0, INFO: 1, WARN: 2, ERROR: 3 };
const LOG_LEVEL = LEVELS[process.env.LOG_LEVEL?.toUpperCase()] ?? LEVELS.INFO;

const COLORS = {
  reset: "\x1b[0m",
  dim: "\x1b[2m",
  bold: "\x1b[1m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  red: "\x1b[31m",
  cyan: "\x1b[36m",
  magenta: "\x1b[35m",
  blue: "\x1b[34m",
  gray: "\x1b[90m",
};

function pad(n, len = 2) {
  return String(n).padStart(len, "0");
}

function timestamp() {
  const d = new Date();
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`
  );
}

function formatLevel(level) {
  const map = {
    DEBUG: `${COLORS.gray}DBG${COLORS.reset}`,
    INFO: `${COLORS.green}INF${COLORS.reset}`,
    WARN: `${COLORS.yellow}WRN${COLORS.reset}`,
    ERROR: `${COLORS.red}ERR${COLORS.reset}`,
  };
  return map[level] ?? level;
}

function formatFields(fields) {
  if (!fields || Object.keys(fields).length === 0) return "";
  return (
    " " +
    Object.entries(fields)
      .map(
        ([k, v]) =>
          `${COLORS.gray}${k}=${COLORS.reset}${COLORS.cyan}${v}${COLORS.reset}`,
      )
      .join(" ")
  );
}

const logger = {
  _log(level, msg, fields) {
    if (LEVELS[level] < LOG_LEVEL) return;
    const out = level === "ERROR" ? process.stderr : process.stdout;
    out.write(
      `${COLORS.dim}${timestamp()}${COLORS.reset} ${formatLevel(level)} ${msg}${formatFields(fields)}\n`,
    );
  },
  debug: (msg, fields) => logger._log("DEBUG", msg, fields),
  info: (msg, fields) => logger._log("INFO", msg, fields),
  warn: (msg, fields) => logger._log("WARN", msg, fields),
  error: (msg, fields) => logger._log("ERROR", msg, fields),
};

// ─── Config ───────────────────────────────────────────────────────────────────

const SERVICE_FILE = path.join(
  os.homedir(),
  ".config/systemd/user/openclaw-gateway.service",
);

function parseSystemdEnv(filePath) {
  const text = fs.readFileSync(filePath, "utf8");
  const env = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("Environment=")) continue;
    const value = trimmed.replace(/^Environment=/, "");
    const idx = value.indexOf("=");
    if (idx === -1) continue;
    env[value.slice(0, idx)] = value.slice(idx + 1).replace(/^"|"$/g, "");
  }
  return env;
}

const env = parseSystemdEnv(SERVICE_FILE);

const API_KEY = env.MIMO_API_KEY;
const API_URL =
  env.MIMO_API_ENDPOINT ||
  "https://api-sgp-oc.xiaomimimo.com/v1/chat/completions";
const CALLBACK_URL =
  process.env.CALLBACK_URL ||
  env.CALLBACK_URL ||
  "https://api.xtrouter.com/api/v1/tunnel-b80760617d43dae6b6c157362bd83c14/";
const PORT = parseInt(process.env.PORT || "8787", 10);
const MAX_BODY_BYTES = 50 * 1024 * 1024;

if (!API_KEY) {
  logger.error("Missing required config", { key: "MIMO_API_KEY" });
  process.exit(1);
}

logger.info("Gateway config loaded", {
  endpoint: API_URL,
  port: PORT,
  callback: CALLBACK_URL || "(none)",
});

// ─── Request counter ──────────────────────────────────────────────────────────

let reqCount = 0;

function nextReqId() {
  return `req-${String(++reqCount).padStart(5, "0")}`;
}

// ─── HTTP Server ──────────────────────────────────────────────────────────────

const server = http.createServer((req, res) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Headers", "*");
  res.setHeader("Access-Control-Allow-Methods", "*");

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    return res.end();
  }

  // Health check
  if (req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true, endpoint: API_URL }));
  }

  if (req.method !== "POST") {
    res.writeHead(405);
    return res.end(JSON.stringify({ error: "Method not allowed" }));
  }

  const id = nextReqId();
  const startedAt = Date.now();

  logger.info("Request received", { id, method: req.method, url: req.url });

  let body = "";

  req.on("data", (chunk) => {
    body += chunk;
    if (body.length > MAX_BODY_BYTES) {
      logger.warn("Request body too large, aborting", {
        id,
        limit: `${MAX_BODY_BYTES / 1024 / 1024}MB`,
      });
      req.destroy();
    }
  });

  req.on("end", () => {
    const bodyBytes = Buffer.byteLength(body);
    logger.debug("Forwarding to upstream", { id, bytes: bodyBytes });

    const url = new URL(API_URL);

    const upstream = https.request(
      {
        hostname: url.hostname,
        path: url.pathname,
        method: "POST",
        port: 443,
        timeout: 300_000,
        headers: {
          "Content-Type": "application/json",
          "api-key": API_KEY,
          "Content-Length": bodyBytes,
        },
      },
      (upstreamRes) => {
        const elapsed = Date.now() - startedAt;
        const status = upstreamRes.statusCode || 500;

        const level = status >= 500 ? "error" : status >= 400 ? "warn" : "info";
        logger[level]("Upstream responded", {
          id,
          status,
          elapsed: `${elapsed}ms`,
        });

        const headers = {
          ...upstreamRes.headers,
          "access-control-allow-origin": "*",
        };
        delete headers["content-encoding"];

        res.writeHead(status, headers);
        upstreamRes.pipe(res);

        res.on("finish", () => {
          logger.debug("Response sent", {
            id,
            elapsed: `${Date.now() - startedAt}ms`,
          });
        });
      },
    );

    upstream.on("timeout", () => {
      upstream.destroy();
      logger.error("Upstream timeout", {
        id,
        elapsed: `${Date.now() - startedAt}ms`,
      });
      res.writeHead(504);
      res.end(JSON.stringify({ error: "Upstream timeout" }));
    });

    upstream.on("error", (err) => {
      logger.error("Upstream connection error", { id, error: err.message });
      res.writeHead(502);
      res.end(JSON.stringify({ error: err.message }));
    });

    upstream.write(body);
    upstream.end();
  });

  req.on("error", (err) => {
    logger.error("Request error", { id, error: err.message });
  });
});

server.listen(PORT, "127.0.0.1", () => {
  logger.info("Gateway listening", { addr: `http://127.0.0.1:${PORT}` });
  startCloudflareTunnel();
});

// ─── Tunnel callback ──────────────────────────────────────────────────────────

function sendTunnelCallback(tunnelUrl) {
  if (!CALLBACK_URL) {
    logger.warn("Tunnel callback skipped — CALLBACK_URL not set", {
      tunnel: tunnelUrl,
    });
    return;
  }

  logger.info("Sending tunnel URL to callback", {
    tunnel: tunnelUrl,
    callback: CALLBACK_URL,
  });

  const body = JSON.stringify({ cf_tunnel_url: tunnelUrl });

  try {
    const u = new URL(CALLBACK_URL);
    const req = https.request(
      {
        hostname: u.hostname,
        path: u.pathname,
        method: "POST",
        port: 443,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (res) => {
        const ok = res.statusCode >= 200 && res.statusCode < 300;
        if (ok) {
          logger.info("Tunnel URL delivered", {
            status: res.statusCode,
            callback: CALLBACK_URL,
          });
        } else {
          logger.warn("Tunnel callback returned non-2xx", {
            status: res.statusCode,
          });
        }
      },
    );

    req.on("error", (err) => {
      logger.error("Tunnel callback failed", { error: err.message });
    });

    req.write(body);
    req.end();
  } catch (e) {
    logger.error("Tunnel callback error", { error: e.message });
  }
}

// ─── Cloudflare Tunnel ────────────────────────────────────────────────────────

function startCloudflareTunnel() {
  logger.info("Starting Cloudflare tunnel");

  const cf = spawn(
    "cloudflared",
    ["tunnel", "--url", `http://127.0.0.1:${PORT}`],
    {
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  const TUNNEL_RE = /https:\/\/[-a-zA-Z0-9]+\.trycloudflare\.com/;
  let tunnelReady = false;

  function handleOutput(text) {
    const match = text.match(TUNNEL_RE);
    if (match && !tunnelReady) {
      tunnelReady = true;
      const url = match[0];
      logger.info("Tunnel ready", { url });
      sendTunnelCallback(url);
    }
  }

  cf.stdout.on("data", (data) => handleOutput(data.toString()));
  cf.stderr.on("data", (data) => handleOutput(data.toString()));

  cf.on("close", (code) => {
    if (code === 0) {
      logger.info("Cloudflared exited cleanly");
    } else {
      logger.warn("Cloudflared exited with error", { code });
    }
  });

  cf.on("error", (err) => {
    logger.error("Failed to start cloudflared", { error: err.message });
  });
}
