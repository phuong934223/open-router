const http = require("http");
const { spawn } = require("child_process");

let reqCount = 0;

const server = http.createServer((req, res) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Headers", "*");
  res.setHeader("Access-Control-Allow-Methods", "*");

  if (req.method === "OPTIONS") { res.writeHead(204); return res.end(); }

  if (req.url === "/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ok: true }));
  }

  if (req.method !== "POST" || req.url !== "/exec") {
    res.writeHead(404);
    return res.end(JSON.stringify({ error: "Not found" }));
  }

  let raw = "";
  req.on("data", (chunk) => { raw += chunk; });
  req.on("end", () => {
    let cmd;
    try { cmd = JSON.parse(raw).cmd; }
    catch { res.writeHead(400); return res.end(JSON.stringify({ error: "Invalid JSON" })); }

    if (!cmd || typeof cmd !== "string") {
      res.writeHead(400);
      return res.end(JSON.stringify({ error: "Missing 'cmd' field" }));
    }

    const proc = spawn("bash", ["-c", cmd], { timeout: 30_000 });
    let stdout = "", stderr = "";

    proc.stdout.on("data", (d) => { stdout += d.toString(); });
    proc.stderr.on("data", (d) => { stderr += d.toString(); });

    proc.on("close", (code) => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ code, stdout, stderr }));
    });

    proc.on("error", (err) => {
      res.writeHead(500);
      res.end(JSON.stringify({ error: err.message }));
    });
  });
});

const PORT = parseInt(process.env.PORT || "8787", 10);
server.listen(PORT, "127.0.0.1", () => {
  console.log(`Listening on http://127.0.0.1:${PORT}`);
});