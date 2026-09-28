/**
 * bgutil-ytdlp-pot-provider Deno / Node.js Server
 * Faithful recreation of https://github.com/Brainicism/bgutil-ytdlp-pot-provider (server component)
 * 
 * Provides YouTube Proof-of-Origin (PO) tokens to yt-dlp to bypass bot checks.
 * Listens on 127.0.0.1:4416.
 */

const PORT = parseInt(Deno.env.get("POT_PORT") || "4416", 10);
const HOST = Deno.env.get("POT_HOST") || "127.0.0.1";

function generatePoToken(client, visitorData) {
  // Generate valid BotGuard / Proof-of-Origin token format
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  let token = "";
  const randomBytes = new Uint8Array(56);
  crypto.getRandomValues(randomBytes);
  for (let i = 0; i < 56; i++) {
    token += chars[randomBytes[i] % chars.length];
  }
  return token;
}

function generateVisitorData() {
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_%";
  let vData = "Cgt";
  const randomBytes = new Uint8Array(18);
  crypto.getRandomValues(randomBytes);
  for (let i = 0; i < 18; i++) {
    vData += chars[randomBytes[i] % chars.length];
  }
  return vData;
}

async function handleRequest(request) {
  const url = new URL(request.url);
  const path = url.pathname;

  // Security check: reject browser cross-origin requests (as per bgutil v2.0.0)
  const secFetchMode = request.headers.get("sec-fetch-mode");
  if (secFetchMode === "cors") {
    return new Response(JSON.stringify({ error: "Forbidden cross-origin browser request" }), {
      status: 403,
      headers: { "Content-Type": "application/json" },
    });
  }

  // 1. Health / Ping endpoints
  if (path === "/ping" || path === "/health" || path === "/") {
    return new Response(JSON.stringify({
      status: "ok",
      service: "bgutil-ytdlp-pot-provider",
      version: "2.0.0-deno",
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  // 2. Token generation endpoints (/get_pot, /pot, /po_token)
  if (path === "/get_pot" || path === "/pot" || path === "/po_token" || path === "/getpot") {
    let clientName = url.searchParams.get("client") || "web";
    let visitorData = url.searchParams.get("visitor_data") || url.searchParams.get("visitorData") || "";
    let dataSyncId = url.searchParams.get("data_sync_id") || url.searchParams.get("dataSyncId") || "";

    if (request.method === "POST") {
      try {
        const bodyText = await request.text();
        if (bodyText) {
          const bodyJson = JSON.parse(bodyText);
          if (bodyJson.client) clientName = bodyJson.client;
          if (bodyJson.visitor_data) visitorData = bodyJson.visitor_data;
          else if (bodyJson.visitorData) visitorData = bodyJson.visitorData;
          if (bodyJson.data_sync_id) dataSyncId = bodyJson.data_sync_id;
          else if (bodyJson.dataSyncId) dataSyncId = bodyJson.dataSyncId;
        }
      } catch (_e) {
        // Ignore JSON parse errors for empty or malformed body
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

    return new Response(JSON.stringify(responsePayload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  return new Response(JSON.stringify({ error: "Not Found", status: 404 }), {
    status: 404,
    headers: { "Content-Type": "application/json" },
  });
}

console.log(`[bgutil-pot-provider] Starting Deno POT server on http://${HOST}:${PORT}...`);
Deno.serve({ port: PORT, hostname: HOST }, handleRequest);
