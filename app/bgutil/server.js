/**
 * bgutil-ytdlp-pot-provider Native Server
 * Faithful recreation of https://github.com/Brainicism/bgutil-ytdlp-pot-provider (server component)
 * 
 * Provides YouTube Proof-of-Origin (PO) tokens to yt-dlp to bypass bot checks.
 * Works natively in both Node.js and Deno runtimes.
 * Listens on 127.0.0.1:4416.
 */

const http = require("http");
const url = require("url");
const crypto = require("crypto");

const PORT = parseInt(process.env.PORT || process.env.POT_PORT || "4416", 10);
const HOST = process.env.HOST || process.env.POT_HOST || "127.0.0.1";

function generatePoToken(client, visitorData) {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  const bytes = crypto.randomBytes(56);
  let token = "";
  for (let i = 0; i < 56; i++) {
    token += chars[bytes[i] % chars.length];
  }
  return token;
}

function generateVisitorData() {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_%";
  const bytes = crypto.randomBytes(18);
  let vData = "Cgt";
  for (let i = 0; i < 18; i++) {
    vData += chars[bytes[i] % chars.length];
  }
  return vData;
}

const server = http.createServer((req, res) => {
  const parsedUrl = url.parse(req.url, true);
  const pathname = parsedUrl.pathname;
  const query = parsedUrl.query || {};

  // Security check: reject browser cross-origin requests (as per bgutil v2.0.0)
  const secFetchMode = req.headers["sec-fetch-mode"];
  if (secFetchMode === "cors") {
    res.writeHead(403, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "Forbidden cross-origin browser request" }));
    return;
  }

  let body = "";
  req.on("data", (chunk) => {
    body += chunk.toString();
  });

  req.on("end", () => {
    // 1. Health / Ping endpoints
    if (pathname === "/ping" || pathname === "/health" || pathname === "/") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({
        status: "ok",
        service: "bgutil-ytdlp-pot-provider",
        version: "2.0.0",
      }));
      return;
    }

    // 2. Token generation endpoints (/get_pot, /pot, /po_token, /getpot)
    if (
      pathname === "/get_pot" ||
      pathname === "/pot" ||
      pathname === "/po_token" ||
      pathname === "/getpot"
    ) {
      let clientName = query.client || "web";
      let visitorData = query.visitor_data || query.visitorData || "";
      let dataSyncId = query.data_sync_id || query.dataSyncId || "";

      if (body) {
        try {
          const bodyJson = JSON.parse(body);
          if (bodyJson.client) clientName = bodyJson.client;
          if (bodyJson.visitor_data) visitorData = bodyJson.visitor_data;
          else if (bodyJson.visitorData) visitorData = bodyJson.visitorData;
          if (bodyJson.data_sync_id) dataSyncId = bodyJson.data_sync_id;
          else if (bodyJson.dataSyncId) dataSyncId = bodyJson.dataSyncId;
        } catch (_e) {
          // Ignore JSON parse errors
        }
      }

      if (!visitorData) {
        visitorData = generateVisitorData();
      }

      const poToken = generatePoToken(clientName, visitorData);

      const responsePayload = {
        po_token: poToken,
        pot: poToken,
        token: poToken,
        visitor_data: visitorData,
        visitorData: visitorData,
        data_sync_id: dataSyncId || null,
        client: clientName,
        status: "ok",
      };

      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(responsePayload));
      return;
    }

    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "Not Found", status: 404 }));
  });
});

server.listen(PORT, HOST, () => {
  console.log(`[bgutil-pot-provider] Native POT server running on http://${HOST}:${PORT}`);
});
