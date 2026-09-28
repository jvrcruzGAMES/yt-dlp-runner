import asyncio
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.parse
from typing import Dict, List, Optional

logger = logging.getLogger("yt_dlp_runner.embedded_flaresolverr")

DEFAULT_FLARESOLVERR_PORT = 8191

OFFICIAL_FLARESOLVERR_PATHS = [
    "/app/app/flaresolverr/src/flaresolverr.py",
    "/app/flaresolverr/src/flaresolverr.py",
]

CLOUDFLARE_CHALLENGE_TITLES = [
    "Just a moment...",
    "Attention Required! | Cloudflare",
    "Checking your browser",
    "Please Wait...",
    "Cloudflare",
]

CLOUDFLARE_CHALLENGE_SELECTORS = [
    "#cf-challenge-running",
    "#challenge-running",
    "#challenge-stage",
    ".ray_id",
    "#cf-browser-verification",
    "#cf-challenge-body",
    "#turnstile-wrapper",
]


class FlareSolverrSession:
    def __init__(self, session_id: str, proxy: Optional[str] = None):
        self.session_id = session_id
        self.proxy = proxy
        self.driver = None
        self.cookies: Dict[str, dict] = {}
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        self.created_at = time.time()

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


class EmbeddedFlareSolverr:
    """
    Embedded FlareSolverr Service powered by the official FlareSolverr source code
    (https://github.com/Flaresolverr/Flaresolverr).

    When running inside the container or when dependencies are present, it executes
    the official FlareSolverr server natively from source (python src/flaresolverr.py).
    Includes a built-in asynchronous fallback solver for headless unit test suites.
    """
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_FLARESOLVERR_PORT):
        self.host = host
        self.port = port
        self.server: Optional[asyncio.Server] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._sessions: Dict[str, FlareSolverrSession] = {}
        self._default_driver = None

    def _find_official_flaresolverr_script(self) -> Optional[str]:
        # 1. Check standard container paths
        for p in OFFICIAL_FLARESOLVERR_PATHS:
            if os.path.exists(p):
                return p

        # 2. Check local relative paths
        local_candidates = [
            Path(__file__).parent / "flaresolverr" / "src" / "flaresolverr.py",
            Path(__file__).parent.parent / "flaresolverr" / "src" / "flaresolverr.py",
            Path(__file__).parent.parent.parent / "flaresolverr" / "src" / "flaresolverr.py",
        ]
        for p in local_candidates:
            if p.exists():
                return str(p)

        return None

    async def start(self):
        if self._running:
            return

        script_path = self._find_official_flaresolverr_script()

        # 1. Attempt to launch official FlareSolverr from source code
        if script_path:
            resolved_script = Path(script_path).resolve()
            src_dir = resolved_script.parent
            env = os.environ.copy()
            env["HOST"] = self.host
            env["PORT"] = str(self.port)
            env["LOG_LEVEL"] = env.get("LOG_LEVEL", "info")
            env["HEADLESS"] = env.get("HEADLESS", "true")

            proxy_url = (
                os.getenv("HTTP_PROXY")
                or os.getenv("http_proxy")
                or os.getenv("FLARESOLVERR_PROXY")
                or os.getenv("PROXY_URL")
            )
            if proxy_url:
                env["PROXY_URL"] = proxy_url
                env["HTTP_PROXY"] = proxy_url
                env["http_proxy"] = proxy_url
                env["HTTPS_PROXY"] = proxy_url
                env["https_proxy"] = proxy_url

            try:
                logger.info(f"Launching official FlareSolverr from source: {resolved_script} on {self.host}:{self.port}...")
                self._process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(resolved_script),
                    env=env,
                    cwd=str(src_dir),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )

                # Poll up to 3 seconds for the port to open
                ready = False
                for _ in range(30):
                    if self._process.returncode is not None:
                        break
                    try:
                        _, writer = await asyncio.open_connection(self.host, self.port)
                        writer.close()
                        await writer.wait_closed()
                        ready = True
                        break
                    except Exception:
                        await asyncio.sleep(0.1)

                if ready and self._process.returncode is None:
                    self._running = True
                    logger.info(f"Official FlareSolverr (from source) active at http://{self.host}:{self.port}/v1")
                    return
                else:
                    if self._process.returncode is None:
                        try:
                            self._process.terminate()
                            await self._process.wait()
                        except Exception:
                            pass
                    self._process = None
                    logger.warning("Official FlareSolverr process did not open port in time. Using embedded fallback server...")
            except Exception as e:
                logger.warning(f"Failed to launch official FlareSolverr process ({e}). Using embedded fallback server...")

        # 2. Asynchronous fallback server for testing environments without full browser/Xvfb setup
        try:
            self.server = await asyncio.start_server(
                self._handle_client, self.host, self.port
            )
            self._running = True
            logger.info(f"Embedded FlareSolverr active at http://{self.host}:{self.port}/v1 (backend: undetected-chromedriver / fallback)")
        except Exception as e:
            logger.warning(f"Could not start embedded FlareSolverr on {self.host}:{self.port}: {e}")

    async def stop(self):
        if not self._running:
            return

        if self._process:
            try:
                self._process.terminate()
                await self._process.wait()
            except Exception:
                pass
            self._process = None

        if self.server:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None

        self._running = False

        # Clean up all active browser instances
        for sess in self._sessions.values():
            sess.close()
        self._sessions.clear()

        if self._default_driver:
            try:
                self._default_driver.quit()
            except Exception:
                pass
            self._default_driver = None

        logger.info("Embedded FlareSolverr stopped.")

    @property
    def is_active(self) -> bool:
        return self._running and (self._process is not None or self.server is not None)

    def _create_uc_driver(self, proxy: Optional[str] = None):
        """
        Creates an undetected-chromedriver Chrome instance configured identically to FlareSolverr.
        """
        try:
            import undetected_chromedriver as uc

            options = uc.ChromeOptions()
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-setuid-sandbox")
            options.add_argument("--disable-extensions")
            options.add_argument("--no-first-run")
            options.add_argument("--window-size=1920,1080")

            if proxy:
                options.add_argument(f"--proxy-server={proxy}")

            for chrome_bin in ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]:
                if os.path.exists(chrome_bin):
                    options.binary_location = chrome_bin
                    break

            driver = uc.Chrome(options=options, headless=True)
            return driver
        except Exception as e:
            logger.debug(f"undetected-chromedriver not initialized or display unavailable: {e}")
            return None

    def _solve_with_uc_driver(
        self,
        driver,
        target_url: str,
        cmd: str,
        max_timeout: float,
        cookies: Optional[List[dict]] = None,
        headers: Optional[dict] = None,
        post_data: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Executes challenge resolution via undetected-chromedriver.
        Waits for Cloudflare challenges to clear and returns the solution dictionary.
        """
        try:
            driver.set_page_load_timeout(max_timeout)
            driver.get(target_url)

            # Inject custom cookies
            if cookies:
                for c in cookies:
                    if isinstance(c, dict) and "name" in c and "value" in c:
                        try:
                            driver.add_cookie({
                                "name": c["name"],
                                "value": c["value"],
                                "domain": c.get("domain") or urllib.parse.urlparse(target_url).hostname or "",
                                "path": c.get("path", "/"),
                            })
                        except Exception:
                            pass

            start_time = time.time()
            challenge_detected = False

            # Challenge polling loop
            while (time.time() - start_time) < max_timeout:
                title = driver.title or ""
                page_source = driver.page_source or ""

                is_challenge = any(t in title for t in CLOUDFLARE_CHALLENGE_TITLES) or any(
                    sel in page_source for sel in ["cf-browser-verification", "turnstile-wrapper", "challenge-running"]
                )

                if is_challenge:
                    challenge_detected = True
                    time.sleep(1.0)
                else:
                    break

            current_url = driver.current_url
            page_html = driver.page_source
            extracted_cookies = driver.get_cookies()
            user_agent = driver.execute_script("return navigator.userAgent")

            return {
                "url": current_url,
                "status": 200,
                "headers": {},
                "response": page_html,
                "cookies": extracted_cookies,
                "userAgent": user_agent,
                "challenge_detected": challenge_detected,
            }
        except Exception as e:
            logger.warning(f"Error during undetected-chromedriver execution: {e}")
            return None

    async def _solve_request(self, payload: dict) -> dict:
        cmd = payload.get("cmd", "request.get")
        target_url = payload.get("url")
        start_ts = int(time.time() * 1000)

        # 1. Sessions Management
        if cmd == "sessions.create":
            session_id = payload.get("session") or f"session_{int(time.time())}"
            proxy_url = payload.get("proxy", {}).get("url") if isinstance(payload.get("proxy"), dict) else None
            sess = FlareSolverrSession(session_id, proxy_url)
            # Pre-initialize browser driver for session in background
            loop = asyncio.get_event_loop()
            sess.driver = await loop.run_in_executor(None, self._create_uc_driver, proxy_url)
            self._sessions[session_id] = sess
            return {
                "status": "ok",
                "message": "Session created successfully.",
                "session": session_id,
                "startTimestamp": start_ts,
                "endTimestamp": int(time.time() * 1000),
                "version": "v3.3.21",
            }

        if cmd == "sessions.list":
            return {
                "status": "ok",
                "message": "",
                "sessions": list(self._sessions.keys()),
                "startTimestamp": start_ts,
                "endTimestamp": int(time.time() * 1000),
                "version": "v3.3.21",
            }

        if cmd == "sessions.destroy":
            session_id = payload.get("session", "")
            sess = self._sessions.pop(session_id, None)
            if sess:
                sess.close()
            return {
                "status": "ok",
                "message": "Session destroyed successfully.",
                "startTimestamp": start_ts,
                "endTimestamp": int(time.time() * 1000),
                "version": "v3.3.21",
            }

        # 2. HTTP Challenge Solving
        if cmd in ["request.get", "request.post"]:
            if not target_url:
                return {
                    "status": "error",
                    "message": "Missing target URL parameter 'url'.",
                    "startTimestamp": start_ts,
                    "endTimestamp": int(time.time() * 1000),
                    "version": "v3.3.21",
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
            elif os.getenv("HTTP_PROXY"):
                proxy = os.getenv("HTTP_PROXY")
            elif os.getenv("http_proxy"):
                proxy = os.getenv("http_proxy")
            elif os.getenv("FLARESOLVERR_PROXY"):
                proxy = os.getenv("FLARESOLVERR_PROXY")
            elif os.getenv("PROXY_URL"):
                proxy = os.getenv("PROXY_URL")

            cookies_param = payload.get("cookies") or []
            headers_param = payload.get("headers") or {}
            post_data = payload.get("postData")
            return_only_cookies = payload.get("returnOnlyCookies", False)

            loop = asyncio.get_event_loop()

            # Attempt 1: Use undetected-chromedriver (actual FlareSolverr backend)
            driver = active_session.driver if active_session and active_session.driver else None
            driver_created_here = False

            if not driver:
                try:
                    driver = await asyncio.wait_for(
                        loop.run_in_executor(None, self._create_uc_driver, proxy),
                        timeout=5.0,
                    )
                    driver_created_here = True
                except Exception as e:
                    logger.debug(f"undetected-chromedriver creation timed out or failed: {e}")
                    driver = None

            solution = None
            if driver:
                try:
                    solution = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            self._solve_with_uc_driver,
                            driver,
                            target_url,
                            cmd,
                            max_timeout,
                            cookies_param,
                            headers_param,
                            post_data,
                        ),
                        timeout=max_timeout + 5.0,
                    )
                except Exception as e:
                    logger.debug(f"undetected-chromedriver solve timed out or failed: {e}")
                    solution = None
                finally:
                    if driver_created_here and not active_session:
                        try:
                            driver.quit()
                        except Exception:
                            pass

            # Attempt 2: Fallback to curl_cffi / httpx with Chrome TLS impersonation if driver unavailable
            if not solution:
                try:
                    from curl_cffi.requests import AsyncSession

                    cookie_dict = {}
                    for c in cookies_param:
                        if isinstance(c, dict) and "name" in c and "value" in c:
                            cookie_dict[c["name"]] = c["value"]

                    proxies = {"http": proxy, "https": proxy} if proxy else None
                    async with AsyncSession(impersonate="chrome124", timeout=max_timeout) as session:
                        if cmd == "request.post":
                            resp = await session.post(
                                target_url,
                                headers=headers_param,
                                cookies=cookie_dict,
                                data=post_data,
                                proxies=proxies,
                            )
                        else:
                            resp = await session.get(
                                target_url,
                                headers=headers_param,
                                cookies=cookie_dict,
                                proxies=proxies,
                            )

                        parsed_domain = urllib.parse.urlparse(target_url).hostname or ""
                        cookies_list = []
                        for k, v in resp.cookies.items():
                            cookies_list.append({
                                "name": k,
                                "value": v,
                                "domain": parsed_domain,
                                "path": "/",
                            })

                        solution = {
                            "url": str(resp.url),
                            "status": resp.status_code,
                            "headers": dict(resp.headers),
                            "response": resp.text,
                            "cookies": cookies_list,
                            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        }
                except Exception as exc:
                    logger.debug(f"curl_cffi fallback error: {exc}. Attempting standard httpx client...")
                    try:
                        import httpx
                        async with httpx.AsyncClient(timeout=max_timeout, verify=False) as h_client:
                            if cmd == "request.post":
                                resp = await h_client.post(target_url, headers=headers_param, content=post_data)
                            else:
                                resp = await h_client.get(target_url, headers=headers_param)

                            parsed_domain = urllib.parse.urlparse(target_url).hostname or ""
                            cookies_list = [{"name": k, "value": v, "domain": parsed_domain, "path": "/"} for k, v in resp.cookies.items()]
                            solution = {
                                "url": str(resp.url),
                                "status": resp.status_code,
                                "headers": dict(resp.headers),
                                "response": resp.text,
                                "cookies": cookies_list,
                                "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                            }
                    except Exception as e2:
                        logger.warning(f"FlareSolverr resolution error: {e2}")

            if solution:
                end_ts = int(time.time() * 1000)
                if return_only_cookies:
                    solution["response"] = ""

                msg = "Challenge solved!" if solution.get("challenge_detected") else "Challenge not detected!"
                return {
                    "status": "ok",
                    "message": msg,
                    "startTimestamp": start_ts,
                    "endTimestamp": end_ts,
                    "version": "v3.3.21",
                    "solution": solution,
                }
            else:
                return {
                    "status": "error",
                    "message": f"Unable to solve challenge for {target_url}",
                    "startTimestamp": start_ts,
                    "endTimestamp": int(time.time() * 1000),
                    "version": "v3.3.21",
                }

        return {
            "status": "error",
            "message": f"Invalid command: {cmd}",
            "startTimestamp": start_ts,
            "endTimestamp": int(time.time() * 1000),
            "version": "v3.3.21",
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
                    "version": "v3.3.21",
                }
            elif path.startswith("/v1"):
                if method == "POST" and body:
                    try:
                        req_payload = json.loads(body.decode("utf-8"))
                    except Exception:
                        req_payload = {}
                    response_data = await self._solve_request(req_payload)
                else:
                    response_data = {
                        "status": "ok",
                        "message": "FlareSolverr is ready.",
                        "version": "v3.3.21",
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
