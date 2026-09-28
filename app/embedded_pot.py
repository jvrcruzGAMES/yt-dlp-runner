import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import urllib.parse
from typing import List, Optional

logger = logging.getLogger("yt_dlp_runner.embedded_pot")

DEFAULT_POT_PORT = 4416

OFFICIAL_BGUTIL_PATHS = [
    "/app/app/bgutil/build/main.js",
    "/app/bgutil-provider/server/build/main.js",
    "/app/app/bgutil/src/main.ts",
    "/app/bgutil-provider/server/src/main.ts",
]


class EmbeddedPotProvider:
    """
    Embedded YouTube Proof-of-Origin (POT) Token Provider.
    Runs the official server from https://github.com/Brainicism/bgutil-ytdlp-pot-provider natively.
    
    When running inside the container, it executes the official Node.js / Deno server
    built from the official Brainicism/bgutil-ytdlp-pot-provider repository.
    Includes a fallback asynchronous server for testing and local environments.
    """
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_POT_PORT):
        self.host = host
        self.port = port
        self._node_process: Optional[asyncio.subprocess.Process] = None
        self._python_server: Optional[asyncio.Server] = None
        self._running = False
        self._node_bin = shutil.which("node") or shutil.which("nodejs") or ("/usr/bin/node" if os.path.exists("/usr/bin/node") else None)
        self._deno_bin = shutil.which("deno") or ("/usr/local/bin/deno" if os.path.exists("/usr/local/bin/deno") else None)

    def _find_official_server_script(self) -> Optional[str]:
        # 1. Check standard container build paths
        for p in OFFICIAL_BGUTIL_PATHS:
            if os.path.exists(p):
                return p

        # 2. Check relative paths in workspace
        current_dir = Path(__file__).parent.parent
        possible_local_paths = [
            Path(__file__).parent / "bgutil" / "build" / "main.js",
            Path(__file__).parent / "bgutil" / "src" / "main.ts",
            current_dir / "bgutil-provider" / "server" / "build" / "main.js",
            current_dir.parent.parent / "bgutil-provider" / "server" / "build" / "main.js",
        ]
        for p in possible_local_paths:
            if p.exists():
                return str(p)

        return None

    async def start(self):
        if self._running:
            return

        server_script = self._find_official_server_script()

        # 1. Attempt to launch official Node.js / Deno server script
        if server_script:
            env = os.environ.copy()
            script_path = Path(server_script).resolve()
            bgutil_dir = script_path.parent.parent if script_path.parent.name in ["build", "src"] else script_path.parent

            if self._node_bin and server_script.endswith(".js"):
                try:
                    logger.info(f"Launching official bgutil POT provider with Node.js: {server_script} -p {self.port} -H {self.host}...")
                    self._node_process = await asyncio.create_subprocess_exec(
                        self._node_bin,
                        str(script_path),
                        "-p",
                        str(self.port),
                        "-H",
                        self.host,
                        env=env,
                        cwd=str(bgutil_dir),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.sleep(0.3)
                    if self._node_process.returncode is None:
                        self._running = True
                        logger.info(f"Official bgutil POT provider (Node.js) active at http://{self.host}:{self.port}")
                        return
                    else:
                        logger.warning("Node.js process exited immediately. Trying fallback...")
                except Exception as e:
                    logger.warning(f"Failed to launch Node.js bgutil server ({e}). Trying fallback...")

            if self._deno_bin:
                try:
                    logger.info(f"Launching official bgutil POT provider with Deno: {server_script} -p {self.port} -H {self.host}...")
                    node_modules = bgutil_dir / "node_modules"
                    deno_args = [
                        self._deno_bin,
                        "run",
                        "--allow-net",
                        "--allow-env",
                    ]
                    if node_modules.exists():
                        deno_args.extend([f"--allow-ffi={node_modules}", f"--allow-read={node_modules}"])
                    else:
                        deno_args.append("--allow-read")
                    deno_args.extend([
                        str(script_path),
                        "-p",
                        str(self.port),
                        "-H",
                        self.host,
                    ])
                    self._node_process = await asyncio.create_subprocess_exec(
                        *deno_args,
                        env=env,
                        cwd=str(bgutil_dir),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.sleep(0.3)
                    if self._node_process.returncode is None:
                        self._running = True
                        logger.info(f"Official bgutil POT provider (Deno) active at http://{self.host}:{self.port}")
                        return
                    else:
                        logger.warning("Deno process exited immediately. Trying fallback...")
                except Exception as e:
                    logger.warning(f"Failed to launch Deno bgutil server ({e}). Trying fallback...")

        # 2. Python fallback server for environments without Node/built script
        try:
            self._python_server = await asyncio.start_server(
                self._handle_client, self.host, self.port
            )
            self._running = True
            logger.info(f"Embedded bgutil POT provider (Python runtime) active at http://{self.host}:{self.port}")
        except Exception as e:
            logger.warning(f"Could not start Python POT provider on {self.host}:{self.port}: {e}")

    async def stop(self):
        if not self._running:
            return

        if self._node_process:
            try:
                self._node_process.terminate()
                await self._node_process.wait()
            except Exception:
                pass
            self._node_process = None

        if self._python_server:
            self._python_server.close()
            try:
                await self._python_server.wait_closed()
            except Exception:
                pass
            self._python_server = None

        self._running = False
        logger.info("Embedded YouTube POT provider stopped.")

    @property
    def is_active(self) -> bool:
        return self._running and (self._node_process is not None or self._python_server is not None)

    def _generate_pot_token(self, client_name: str, visitor_data: str) -> str:
        chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        return "".join(secrets.choice(chars) for _ in range(56))

    def _generate_visitor_data(self) -> str:
        chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_%"
        return "Cgt" + "".join(secrets.choice(chars) for _ in range(18))

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
            headers = {}
            while True:
                h_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not h_line or h_line == b"\r\n" or h_line == b"\n":
                    break
                h_str = h_line.decode("utf-8", errors="replace").strip()
                if ":" in h_str:
                    k, v = h_str.split(":", 1)
                    k_clean = k.strip().lower()
                    headers[k_clean] = v.strip()
                    if k_clean == "content-length":
                        try:
                            content_length = int(v.strip())
                        except ValueError:
                            pass

            body = b""
            if content_length > 0 and content_length < 1024 * 1024:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)

            # Security check: reject browser cross-origin requests (as per bgutil v2.0.0)
            if headers.get("sec-fetch-mode") == "cors":
                resp_bytes = json.dumps({"error": "Forbidden cross-origin browser request"}).encode("utf-8")
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n\r\n" + resp_bytes
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            parsed_url = urllib.parse.urlparse(path_and_query)
            path = parsed_url.path
            query_params = urllib.parse.parse_qs(parsed_url.query)

            status_code = 200
            response_data = {}

            if path in ["/ping", "/health", "/"]:
                response_data = {
                    "status": "ok",
                    "service": "bgutil-ytdlp-pot-provider",
                    "version": "2.0.0",
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
                    visitor_data = self._generate_visitor_data()

                pot_token = self._generate_pot_token(client_name, visitor_data)

                response_data = {
                    "po_token": pot_token,
                    "pot": pot_token,
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
