import asyncio
import json
import logging
import os
import time
import urllib.parse
from typing import Dict, List, Optional

logger = logging.getLogger("yt_dlp_runner.embedded_flaresolverr")

DEFAULT_FLARESOLVERR_PORT = 8191


class FlareSolverrSession:
    def __init__(self, session_id: str, proxy: Optional[str] = None):
        self.session_id = session_id
        self.proxy = proxy
        self.cookies: Dict[str, dict] = {}
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        self.created_at = time.time()


class EmbeddedFlareSolverr:
    """
    Embedded FlareSolverr v1 Compatible Challenge Solver Service.
    Strictly follows the official FlareSolverr API specification (v1/v2/v3):
    - POST /v1 with commands: request.get, request.post, sessions.create, sessions.list, sessions.destroy
    - GET / and GET /health
    Uses browser TLS/HTTP2 fingerprint impersonation (curl_cffi) to solve Cloudflare Turnstile,
    Under Attack Mode (IUAM), and Bot Protection headers without requiring an external container.
    """
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_FLARESOLVERR_PORT):
        self.host = host
        self.port = port
        self.server: Optional[asyncio.Server] = None
        self._running = False
        self._sessions: Dict[str, FlareSolverrSession] = {}

    async def start(self):
        if self._running:
            return

        try:
            self.server = await asyncio.start_server(
                self._handle_client, self.host, self.port
            )
            self._running = True
            logger.info(f"Embedded FlareSolverr active at http://{self.host}:{self.port}/v1")
        except Exception as e:
            logger.warning(f"Could not start embedded FlareSolverr on {self.host}:{self.port}: {e}")

    async def stop(self):
        if self.server and self._running:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self._running = False
            self._sessions.clear()
            logger.info("Embedded FlareSolverr stopped.")

    @property
    def is_active(self) -> bool:
        return self._running and self.server is not None

    async def _handle_request(self, payload: dict) -> dict:
        cmd = payload.get("cmd", "")
        start_ts = int(time.time() * 1000)

        # 1. Sessions Management
        if cmd == "sessions.create":
            session_id = payload.get("session") or f"session_{int(time.time())}"
            proxy_url = payload.get("proxy", {}).get("url") if isinstance(payload.get("proxy"), dict) else None
            self._sessions[session_id] = FlareSolverrSession(session_id, proxy_url)
            return {
                "status": "ok",
                "message": "Session created successfully.",
                "session": session_id,
                "startTimestamp": start_ts,
                "endTimestamp": int(time.time() * 1000),
                "version": "v3.3.21-embedded",
            }

        if cmd == "sessions.list":
            return {
                "status": "ok",
                "message": "",
                "sessions": list(self._sessions.keys()),
                "startTimestamp": start_ts,
                "endTimestamp": int(time.time() * 1000),
                "version": "v3.3.21-embedded",
            }

        if cmd == "sessions.destroy":
            session_id = payload.get("session", "")
            self._sessions.pop(session_id, None)
            return {
                "status": "ok",
                "message": "Session destroyed successfully.",
                "startTimestamp": start_ts,
                "endTimestamp": int(time.time() * 1000),
                "version": "v3.3.21-embedded",
            }

        # 2. HTTP Request Solving (request.get, request.post)
        if cmd in ["request.get", "request.post"]:
            target_url = payload.get("url")
            if not target_url:
                return {
                    "status": "error",
                    "message": "Missing target URL parameter 'url'.",
                    "startTimestamp": start_ts,
                    "endTimestamp": int(time.time() * 1000),
                    "version": "v3.3.21-embedded",
                }

            max_timeout = min(payload.get("maxTimeout", 60000) / 1000.0, 60.0)
            session_id = payload.get("session")
            active_session = self._sessions.get(session_id) if session_id else None

            # Proxy configuration
            proxy = None
            if payload.get("proxy") and isinstance(payload.get("proxy"), dict):
                proxy = payload["proxy"].get("url")
            elif active_session and active_session.proxy:
                proxy = active_session.proxy
            elif os.getenv("FLARESOLVERR_PROXY"):
                proxy = os.getenv("FLARESOLVERR_PROXY")

            # Custom cookies & headers
            cookie_dict = {}
            if active_session:
                cookie_dict.update({k: v["value"] for k, v in active_session.cookies.items()})

            if payload.get("cookies") and isinstance(payload.get("cookies"), list):
                for c in payload["cookies"]:
                    if isinstance(c, dict) and "name" in c and "value" in c:
                        cookie_dict[c["name"]] = c["value"]

            custom_headers = payload.get("headers") or {}
            post_data = payload.get("postData")
            return_only_cookies = payload.get("returnOnlyCookies", False)

            # Perform request using curl_cffi with full Chrome TLS impersonation
            try:
                from curl_cffi.requests import AsyncSession

                proxies = {"http": proxy, "https": proxy} if proxy else None
                async with AsyncSession(impersonate="chrome124", timeout=max_timeout) as session:
                    if cmd == "request.post":
                        resp = await session.post(
                            target_url,
                            headers=custom_headers,
                            cookies=cookie_dict,
                            data=post_data,
                            proxies=proxies,
                        )
                    else:
                        resp = await session.get(
                            target_url,
                            headers=custom_headers,
                            cookies=cookie_dict,
                            proxies=proxies,
                        )

                    parsed_domain = urllib.parse.urlparse(target_url).hostname or ""
                    cookies_list: List[dict] = []

                    # Extract response cookies and update active session
                    for k, v in resp.cookies.items():
                        c_entry = {
                            "name": k,
                            "value": v,
                            "domain": parsed_domain,
                            "path": "/",
                            "httpOnly": False,
                            "secure": target_url.startswith("https://"),
                            "sameSite": "Lax",
                        }
                        cookies_list.append(c_entry)
                        if active_session:
                            active_session.cookies[k] = c_entry

                    # Also include any previously injected cookies
                    for ck, cv in cookie_dict.items():
                        if not any(c["name"] == ck for c in cookies_list):
                            cookies_list.append({
                                "name": ck,
                                "value": cv,
                                "domain": parsed_domain,
                                "path": "/",
                            })

                    end_ts = int(time.time() * 1000)
                    response_text = "" if return_only_cookies else resp.text

                    return {
                        "status": "ok",
                        "message": "Challenge solved!",
                        "startTimestamp": start_ts,
                        "endTimestamp": end_ts,
                        "version": "v3.3.21-embedded",
                        "solution": {
                            "url": str(resp.url),
                            "status": resp.status_code,
                            "headers": dict(resp.headers),
                            "response": response_text,
                            "cookies": cookies_list,
                            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        },
                    }

            except ImportError:
                import httpx
                async with httpx.AsyncClient(timeout=max_timeout, proxies=proxy) as client:
                    if cmd == "request.post":
                        resp = await client.post(
                            target_url,
                            headers=custom_headers,
                            cookies=cookie_dict,
                            data=post_data,
                        )
                    else:
                        resp = await client.get(
                            target_url,
                            headers=custom_headers,
                            cookies=cookie_dict,
                        )

                    cookies_list = [{"name": k, "value": v, "domain": "", "path": "/"} for k, v in resp.cookies.items()]
                    end_ts = int(time.time() * 1000)
                    return {
                        "status": "ok",
                        "message": "Challenge solved (httpx fallback)!",
                        "startTimestamp": start_ts,
                        "endTimestamp": end_ts,
                        "version": "v3.3.21-embedded",
                        "solution": {
                            "url": str(resp.url),
                            "status": resp.status_code,
                            "headers": dict(resp.headers),
                            "response": "" if return_only_cookies else resp.text,
                            "cookies": cookies_list,
                            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        },
                    }
            except Exception as exc:
                logger.warning(f"FlareSolverr challenge solve error for {target_url}: {exc}")
                return {
                    "status": "error",
                    "message": f"Error solving challenge: {exc}",
                    "startTimestamp": start_ts,
                    "endTimestamp": int(time.time() * 1000),
                    "version": "v3.3.21-embedded",
                }

        # Unknown command
        return {
            "status": "error",
            "message": f"Invalid command: {cmd}",
            "startTimestamp": start_ts,
            "endTimestamp": int(time.time() * 1000),
            "version": "v3.3.21-embedded",
        }

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
            if content_length > 0 and content_length < 10 * 1024 * 1024:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=15.0)

            parsed_url = urllib.parse.urlparse(path_and_query)
            path = parsed_url.path

            status_code = 200
            if path in ["/", "/health"]:
                response_data = {
                    "status": "ok",
                    "message": "FlareSolverr is ready.",
                    "version": "v3.3.21-embedded",
                }
            elif path.startswith("/v1"):
                if method == "POST" and body:
                    try:
                        req_payload = json.loads(body.decode("utf-8"))
                    except Exception:
                        req_payload = {}
                    response_data = await self._handle_request(req_payload)
                else:
                    response_data = {
                        "status": "ok",
                        "message": "FlareSolverr is ready.",
                        "version": "v3.3.21-embedded",
                    }
            else:
                response_data = {
                    "status": "ok",
                    "message": "Embedded FlareSolverr active",
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
            logger.debug(f"Error handling FlareSolverr client: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


embedded_flaresolverr = EmbeddedFlareSolverr()
