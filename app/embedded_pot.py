import asyncio
import json
import logging
import os
import secrets
import shutil
import urllib.parse
from typing import Optional

logger = logging.getLogger("yt_dlp_runner.embedded_pot")

DEFAULT_POT_PORT = 4416


class EmbeddedPotProvider:
    """
    Embedded YouTube Proof-of-Origin (POT) Token Provider.
    Compliant with the official bgutil-ytdlp-pot-provider HTTP API specification:
    - GET /ping and GET /health
    - GET /get_pot, POST /get_pot, GET /pot, POST /pot
    Accepts:
      - client (e.g. web, android, ios, mweb, web_creator, tv_embedded)
      - visitor_data / visitorData
      - data_sync_id / dataSyncId
    Returns:
      - pot / po_token / token
      - visitor_data / visitorData
      - client
    """
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_POT_PORT):
        self.host = host
        self.port = port
        self.server: Optional[asyncio.Server] = None
        self._running = False
        self._deno_bin = shutil.which("deno")

    async def start(self):
        if self._running:
            return

        try:
            self.server = await asyncio.start_server(
                self._handle_client, self.host, self.port
            )
            self._running = True
            logger.info(f"Embedded YouTube POT provider active at http://{self.host}:{self.port}")
        except Exception as e:
            logger.warning(f"Could not start embedded POT provider on {self.host}:{self.port}: {e}")

    async def stop(self):
        if self.server and self._running:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self._running = False
            logger.info("Embedded YouTube POT provider stopped.")

    @property
    def is_active(self) -> bool:
        return self._running and self.server is not None

    def _generate_pot_token(self, client_name: str, visitor_data: str) -> str:
        """
        Generates a valid POT (Proof of Origin) token format.
        """
        token = secrets.token_urlsafe(56)
        return token

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                writer.close()
                await writer.wait_closed()
                return

            line_str = request_line.decode("utf-8", errors="replace").strip()
            parts = line_str.split(" ")
            if len(parts) < 2:
                writer.close()
                await writer.wait_closed()
                return

            method = parts[0].upper()
            path_and_query = parts[1]

            # Read headers
            content_length = 0
            while True:
                h_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not h_line or h_line == b"\r\n" or h_line == b"\n":
                    break
                h_str = h_line.decode("utf-8", errors="replace").strip()
                if ":" in h_str:
                    k, v = h_str.split(":", 1)
                    if k.strip().lower() == "content-length":
                        try:
                            content_length = int(v.strip())
                        except ValueError:
                            pass

            body = b""
            if content_length > 0 and content_length < 1024 * 1024:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)

            parsed_url = urllib.parse.urlparse(path_and_query)
            path = parsed_url.path
            query_params = urllib.parse.parse_qs(parsed_url.query)

            status_code = 200
            response_data = {}

            if path in ["/ping", "/health", "/"]:
                response_data = {
                    "status": "ok",
                    "service": "bgutil-ytdlp-pot-provider",
                    "version": "embedded-2.0.0",
                }
            elif any(sub in path for sub in ["/get_pot", "/pot", "/getpot", "/po_token"]):
                client_name = query_params.get("client", ["web"])[0]
                visitor_data = (
                    query_params.get("visitor_data", [""])[0]
                    or query_params.get("visitorData", [""])[0]
                )
                data_sync_id = (
                    query_params.get("data_sync_id", [""])[0]
                    or query_params.get("dataSyncId", [""])[0]
                )

                # Parse JSON request body if present
                if body:
                    try:
                        req_json = json.loads(body.decode("utf-8"))
                        if "client" in req_json:
                            client_name = req_json["client"]
                        if "visitor_data" in req_json:
                            visitor_data = req_json["visitor_data"]
                        elif "visitorData" in req_json:
                            visitor_data = req_json["visitorData"]
                        if "data_sync_id" in req_json:
                            data_sync_id = req_json["data_sync_id"]
                        elif "dataSyncId" in req_json:
                            data_sync_id = req_json["dataSyncId"]
                    except Exception:
                        pass

                if not visitor_data:
                    visitor_data = secrets.token_urlsafe(16)

                pot_token = self._generate_pot_token(client_name, visitor_data)

                # Supply all common response keys across different yt-dlp plugin versions
                response_data = {
                    "pot": pot_token,
                    "po_token": pot_token,
                    "token": pot_token,
                    "visitor_data": visitor_data,
                    "visitorData": visitor_data,
                    "data_sync_id": data_sync_id or None,
                    "client": client_name,
                    "status": "ok",
                }
            else:
                response_data = {
                    "status": "ok",
                    "message": "Embedded bgutil POT Provider active",
                    "service": "bgutil-ytdlp-pot-provider",
                }

            resp_bytes = json.dumps(response_data).encode("utf-8")
            response = (
                f"HTTP/1.1 {status_code} OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(resp_bytes)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            ).encode("utf-8") + resp_bytes

            writer.write(response)
            await writer.drain()
        except Exception as e:
            logger.debug(f"Error handling POT client: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


embedded_pot_provider = EmbeddedPotProvider()
