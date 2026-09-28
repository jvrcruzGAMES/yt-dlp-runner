import asyncio
import datetime
import hashlib
import logging
import mimetypes
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from app.args_sanitizer import sanitize_yt_dlp_args
from app.config import settings
from app.flaresolverr_proxy import flaresolverr_proxy
from app.plugins import plugin_manager
from app.schemas import (
    DownloadRequest,
    DownloadTaskResponse,
    FileInfo,
    PluginInstallRequest,
)

logger = logging.getLogger("yt_dlp_runner.downloader")

SIZE_MULTIPLIERS = {
    "b": 1,
    "kib": 1024,
    "mib": 1024 ** 2,
    "gib": 1024 ** 3,
    "tib": 1024 ** 4,
    "kb": 1000,
    "mb": 1000 ** 2,
    "gb": 1000 ** 3,
    "tb": 1000 ** 4,
}

PROGRESS_REGEX = re.compile(
    r"\[download\]\s+([\d\.]+)%\s+of\s+(?:~)?([\d\.]+[a-zA-Z]+)(?:\s+at\s+([\d\.]+[a-zA-Z]+/s))?(?:\s+ETA\s+(\S+))?"
)
COMPLETE_PROGRESS_REGEX = re.compile(
    r"\[download\]\s+100(?:\.0)?%\s+of\s+(?:~)?([\d\.]+[a-zA-Z]+)"
)
DESTINATION_PATTERNS: List[re.Pattern] = [
    re.compile(r"\[(?:download|ExtractAudio|VideoConvertor|Fixup[a-zA-Z0-9]+|Embed[a-zA-Z0-9]+|ModifyChapters|Thumbnail)\]\s+Destination:\s+(.+)$"),
    re.compile(r"\[(?:Merger|Fixup[a-zA-Z0-9]+|ModifyChapters)\]\s+(?:Merging formats into|Correcting container in|Writing chapters to)\s+[\"']?([^\"'\n]+)[\"']?"),
    re.compile(r"\[download\]\s+(.+?)\s+has already been downloaded"),
    re.compile(r"\[(?:info|download)\]\s+Writing .*? to:\s+(.+)$"),
    re.compile(r"\[MoveFiles\]\s+Moving file .*? to\s+[\"']?([^\"'\n]+)[\"']?"),
    re.compile(r"\[ffmpeg\]\s+(?:Merging formats into|Destination:)\s+[\"']?([^\"'\n]+)[\"']?"),
]

MEDIA_EXTENSIONS: Set[str] = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv",
    ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".flac", ".aac"
}
TEMP_EXTENSIONS: Set[str] = {".part", ".ytdl", ".temp", ".tmp"}


def parse_size_to_bytes(size_str: str) -> Optional[int]:
    try:
        clean = size_str.strip().replace("~", "")
        match = re.match(r"^([\d\.]+)\s*([a-zA-Z]+)?$", clean)
        if match:
            val = float(match.group(1))
            unit = (match.group(2) or "b").lower()
            mult = SIZE_MULTIPLIERS.get(unit, 1)
            return int(val * mult)
    except Exception:
        pass
    return None


def parse_eta_to_seconds(eta_str: str) -> Optional[int]:
    try:
        parts = eta_str.strip().split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        pass
    return None


def parse_speed_to_bytes_per_sec(speed_str: str) -> Optional[float]:
    try:
        clean = speed_str.strip().replace("/s", "")
        match = re.match(r"^([\d\.]+)\s*([a-zA-Z]+)?$", clean)
        if match:
            val = float(match.group(1))
            unit = (match.group(2) or "b").lower()
            mult = SIZE_MULTIPLIERS.get(unit, 1)
            return float(val * mult)
    except Exception:
        pass
    return None


async def stream_subprocess_lines(stream: asyncio.StreamReader, chunk_size: int = 4096):
    """
    Asynchronously yields lines from a subprocess StreamReader.
    Safely handles arbitrary length streams, carriage returns (\r), and newlines (\n, \r\n)
    without triggering asyncio.exceptions.LimitOverrunError.
    """
    buffer = ""
    while True:
        try:
            chunk = await stream.read(chunk_size)
        except Exception:
            break
        if not chunk:
            break

        buffer += chunk.decode("utf-8", errors="replace")

        while True:
            nl_pos = buffer.find("\n")
            cr_pos = buffer.find("\r")

            if nl_pos == -1 and cr_pos == -1:
                # No delimiter in buffer; if buffer grows beyond 10MB, yield chunk to prevent unbounded memory
                if len(buffer) > 10 * 1024 * 1024:
                    yield buffer
                    buffer = ""
                break

            if nl_pos != -1 and (cr_pos == -1 or nl_pos < cr_pos):
                line = buffer[:nl_pos]
                buffer = buffer[nl_pos + 1:]
                yield line
            elif cr_pos != -1:
                if cr_pos + 1 < len(buffer) and buffer[cr_pos + 1] == "\n":
                    line = buffer[:cr_pos]
                    buffer = buffer[cr_pos + 2:]
                elif cr_pos + 1 == len(buffer):
                    # \r is at the very end of chunk, wait for next chunk
                    break
                else:
                    line = buffer[:cr_pos]
                    buffer = buffer[cr_pos + 1:]
                yield line

    if buffer:
        yield buffer


def generate_file_hex_id(filename: str, file_path: Optional[str] = None) -> str:
    """Generates a unique, deterministic 16-character hex identifier for a file."""
    salt = ""
    if file_path and os.path.exists(file_path):
        salt = f":{os.path.getmtime(file_path)}:{os.path.getsize(file_path)}"
    raw = f"{filename}{salt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def create_file_info(filepath: Path, base_url_prefix: str = "/files") -> FileInfo:
    stat = filepath.stat()
    file_id = generate_file_hex_id(filepath.name, str(filepath.resolve()))
    mime_type, _ = mimetypes.guess_type(filepath.name)
    return FileInfo(
        file_id=file_id,
        filename=filepath.name,
        size_bytes=stat.st_size,
        mime_type=mime_type or "application/octet-stream",
        download_url=f"{base_url_prefix}/{file_id}/download",
        modified_at=datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.timezone.utc),
    )


def build_yt_dlp_command(
    request: DownloadRequest,
    downloads_dir: Path,
    cookie_path: Optional[str] = None
) -> Tuple[List[str], List[str]]:
    """
    Builds the complete CLI arguments list for running yt-dlp as a subprocess.
    Returns (cmd_args, stripped_args).
    """
    abs_downloads = str(downloads_dir.resolve())
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-check-certificates",
        "--no-interactive",
        "--force-overwrites",
        "--newline",
        "--no-playlist",
        "--no-mtime",
        "--paths", abs_downloads,
        "--paths", f"home:{abs_downloads}",
        "--paths", f"temp:{abs_downloads}",
    ]

    # Use available JS runtime (Node / Deno) for YouTube extraction
    if shutil.which("node") or os.path.exists("/usr/bin/node"):
        cmd.extend(["--js-runtimes", "node"])
    elif shutil.which("deno") or os.path.exists("/usr/local/bin/deno"):
        cmd.extend(["--js-runtimes", "deno"])

    output_template = request.output_template or "%(title)s [%(id)s].%(ext)s"
    cmd.extend(["-o", output_template])

    if request.format_selection:
        cmd.extend(["-f", request.format_selection])

    if cookie_path and os.path.exists(cookie_path):
        cmd.extend(["--cookies", cookie_path])

    pot_url = (
        getattr(settings, "BGUTIL_POT_PROVIDER_URL", None)
        or os.getenv("BGUTIL_POT_PROVIDER_URL")
        or os.getenv("POT_PROVIDER_URL")
    )
    if pot_url:
        cmd.extend([
            "--extractor-args", f"youtubepot-bgutilhttp:base_url={pot_url}",
            "--extractor-args", f"youtubepot-bgutil:base_url={pot_url}",
            "--extractor-args", f"youtubepot:base_url={pot_url}",
            "--extractor-args", f"youtube:getpot_bgutil_baseurl={pot_url}",
        ])

    stripped_args: List[str] = []
    if request.custom_args:
        sanitized_args, stripped_args = sanitize_yt_dlp_args(request.custom_args)
        cmd.extend(sanitized_args)

    cmd.append(request.url)
    return cmd, stripped_args


class DownloadTaskManager:
    def __init__(self):
        self.tasks: Dict[str, DownloadTaskResponse] = {}
        self._async_tasks: Dict[str, asyncio.Task] = {}
        self._subprocesses: Dict[str, asyncio.subprocess.Process] = {}
        self.file_registry: Dict[str, Path] = {}  # file_id or filename -> Path
        self.last_activity_time: float = time.time()
        self.total_completed_downloads: int = 0
        self._refresh_file_registry()

    def touch_activity(self):
        self.last_activity_time = time.time()

    def get_active_tasks_count(self) -> int:
        return sum(
            1 for t in self.tasks.values()
            if t.status in ["pending", "installing_plugins", "downloading", "processing"]
        )

    def _refresh_file_registry(self) -> List[FileInfo]:
        downloads_path = Path(settings.DOWNLOADS_DIR)
        file_infos: List[FileInfo] = []
        if downloads_path.exists():
            for f in downloads_path.rglob("*"):
                if f.is_file() and f.suffix.lower() not in TEMP_EXTENSIONS:
                    try:
                        info = create_file_info(f)
                        self.file_registry[info.file_id] = f
                        self.file_registry[f.name] = f
                        file_infos.append(info)
                    except OSError:
                        continue
        return file_infos

    def get_file_by_id_or_name(self, identifier: str) -> Optional[Path]:
        self.touch_activity()
        if identifier in self.file_registry:
            path = self.file_registry[identifier]
            if path.is_file() and path.exists():
                return path

        self._refresh_file_registry()
        return self.file_registry.get(identifier)

    def list_all_files(self) -> List[FileInfo]:
        self.touch_activity()
        return self._refresh_file_registry()

    async def start_download(self, request: DownloadRequest) -> DownloadTaskResponse:
        self.touch_activity()
        task_id = str(uuid.uuid4())
        
        task_record = DownloadTaskResponse(
            task_id=task_id,
            url=request.url,
            status="pending",
            started_at=datetime.datetime.now(datetime.timezone.utc),
            logs=[f"Task queued at {datetime.datetime.now(datetime.timezone.utc).isoformat()}"],
        )
        self.tasks[task_id] = task_record

        async_task = asyncio.create_task(self._execute_download(task_id, request))
        self._async_tasks[task_id] = async_task
        return task_record

    async def _execute_download(self, task_id: str, request: DownloadRequest):
        task = self.tasks[task_id]
        cookie_path: Optional[str] = None
        start_timestamp = time.time()

        settings.ensure_directories()
        downloads_dir = Path(settings.DOWNLOADS_DIR)
        cwd_dir = Path.cwd().resolve()

        # Snapshot existing files before download (path -> (mtime, size))
        before_files: Dict[Path, Tuple[float, int]] = {}
        if downloads_dir.exists():
            for f in downloads_dir.rglob("*"):
                if f.is_file():
                    try:
                        before_files[f.resolve()] = (f.stat().st_mtime, f.stat().st_size)
                    except OSError:
                        pass
        if cwd_dir != downloads_dir.resolve() and cwd_dir.exists():
            for f in cwd_dir.rglob("*"):
                if f.is_file():
                    try:
                        before_files[f.resolve()] = (f.stat().st_mtime, f.stat().st_size)
                    except OSError:
                        pass

        try:
            self.touch_activity()

            # 0. Ensure client-requested plugins are installed via pip if specified
            if request.plugins:
                task.status = "installing_plugins"
                task.logs.append(f"Installing plugins via pip: {request.plugins}")
                plugin_resp = await plugin_manager.install_plugins(
                    PluginInstallRequest(packages=request.plugins, upgrade=False)
                )
                if plugin_resp.success:
                    task.logs.append(f"Successfully installed plugins: {request.plugins}")
                else:
                    task.logs.append(f"Plugin install warning: {plugin_resp.stderr or plugin_resp.stdout}")

            task.status = "downloading"
            task.logs.append(f"Starting yt-dlp CLI download for URL: {request.url}")

            # 1. Handle cookie file if provided
            if request.cookie_content:
                cookie_filename = f"cookie_{task_id}.txt"
                cookie_path = os.path.join(settings.COOKIES_DIR, cookie_filename)
                with open(cookie_path, "w", encoding="utf-8") as f:
                    f.write(request.cookie_content)
                task.logs.append(f"Wrote cookie file to isolated storage at: {cookie_path}")

            # 2. Build CLI arguments
            cmd, stripped_args = build_yt_dlp_command(request, downloads_dir, cookie_path)
            if stripped_args:
                task.logs.append(f"Stripped worker-reserved arguments: {stripped_args}")
            task.logs.append(f"Executing CLI command: {' '.join(cmd)}")

            # 3. Spawn subprocess with DEVNULL stdin to prevent hanging
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(downloads_dir.resolve()),
                limit=10 * 1024 * 1024,
            )
            self._subprocesses[task_id] = process

            explicit_paths: List[Path] = []
            last_error_message: Optional[str] = None

            async for line_text in stream_subprocess_lines(process.stdout):
                raw_line = line_text.strip()
                if not raw_line:
                    continue

                self.touch_activity()

                if "ERROR:" in raw_line or "error:" in raw_line.lower():
                    last_error_message = raw_line

                # Parse progress percentage, size, speed, ETA
                prog_match = PROGRESS_REGEX.search(raw_line)
                if prog_match:
                    try:
                        pct = float(prog_match.group(1))
                        task.progress_percent = pct
                        total_str = prog_match.group(2)
                        total_bytes = parse_size_to_bytes(total_str)
                        if total_bytes:
                            task.total_bytes = total_bytes
                            task.downloaded_bytes = int(total_bytes * (pct / 100.0))

                        speed_str = prog_match.group(3)
                        if speed_str:
                            task.speed_bytes_per_sec = parse_speed_to_bytes_per_sec(speed_str)

                        eta_str = prog_match.group(4)
                        if eta_str:
                            task.eta_seconds = parse_eta_to_seconds(eta_str)
                    except Exception:
                        pass
                elif COMPLETE_PROGRESS_REGEX.search(raw_line):
                    task.progress_percent = 100.0
                    task.status = "processing"

                # Parse destination and written file paths from all patterns
                for pat in DESTINATION_PATTERNS:
                    m = pat.search(raw_line)
                    if m:
                        p_str = m.group(1).strip().strip("'\"")
                        dest_p = Path(p_str)
                        if not dest_p.is_absolute():
                            dest_p = downloads_dir / dest_p
                        explicit_paths.append(dest_p)

                # Keep logs manageable: append non-progress lines or milestones
                if not raw_line.startswith("[download]") or "%" not in raw_line or "100%" in raw_line:
                    task.logs.append(raw_line)

            return_code = await process.wait()
            self._subprocesses.pop(task_id, None)

            # 4. Discover all output files generated during this run (media and non-media)
            discovered_files: List[FileInfo] = []
            candidate_paths: List[Path] = []

            for ep in explicit_paths:
                if ep.is_file() and ep.exists() and ep.suffix.lower() not in TEMP_EXTENSIONS:
                    if ep not in candidate_paths:
                        candidate_paths.append(ep)

            # Search downloads_dir
            if downloads_dir.exists():
                for f in downloads_dir.rglob("*"):
                    if not f.is_file() or f.suffix.lower() in TEMP_EXTENSIONS:
                        continue
                    if f not in candidate_paths:
                        candidate_paths.append(f)

            # Prioritize media files, then sort by file size descending
            def sort_key(p: Path):
                is_media = p.suffix.lower() in MEDIA_EXTENSIONS
                try:
                    size = p.stat().st_size
                except Exception:
                    size = 0
                return (0 if is_media else 1, -size)

            candidate_paths.sort(key=sort_key)

            for cp in candidate_paths:
                try:
                    info = create_file_info(cp)
                    if not any(df.file_id == info.file_id for df in discovered_files):
                        self.file_registry[info.file_id] = cp
                        self.file_registry[cp.name] = cp
                        discovered_files.append(info)
                except Exception as ex:
                    logger.warning(f"Error creating file info for {cp}: {ex}")

            if discovered_files:
                task.filepath = str(self.file_registry[discovered_files[0].file_id])
                task.filename = discovered_files[0].filename
                task.files = discovered_files
                task.status = "completed"
                task.progress_percent = 100.0
                task.completed_at = datetime.datetime.now(datetime.timezone.utc)
                file_names = [f.filename for f in discovered_files]
                task.logs.append(f"Task completed successfully. Generated {len(discovered_files)} file(s): {file_names}")
                self.total_completed_downloads += 1
                self.touch_activity()
            elif return_code == 0:
                task.status = "completed"
                task.progress_percent = 100.0
                task.completed_at = datetime.datetime.now(datetime.timezone.utc)
                task.logs.append("Task completed successfully.")
                self.total_completed_downloads += 1
                self.touch_activity()
            else:
                task.status = "failed"
                if last_error_message:
                    task.error = last_error_message
                elif return_code != 0:
                    task.error = f"yt-dlp process exited with error code {return_code}"
                else:
                    task.error = "Download completed without generating any output files."
                task.completed_at = datetime.datetime.now(datetime.timezone.utc)
                task.logs.append(f"ERROR: {task.error}")
                self.touch_activity()

        except asyncio.CancelledError:
            task.status = "cancelled"
            task.error = "Download task was cancelled."
            task.completed_at = datetime.datetime.now(datetime.timezone.utc)
            task.logs.append("Download task was cancelled.")
        except Exception as e:
            logger.error(f"Download failed for task {task_id}: {e}", exc_info=True)
            task.status = "failed"
            task.error = str(e)
            task.completed_at = datetime.datetime.now(datetime.timezone.utc)
            task.logs.append(f"Error: {e}")
        finally:
            if task_id in self._subprocesses:
                p = self._subprocesses.pop(task_id)
                try:
                    p.kill()
                except Exception:
                    pass
            if cookie_path and os.path.exists(cookie_path):
                try:
                    os.remove(cookie_path)
                except Exception as ex:
                    logger.warning(f"Could not remove cookie file {cookie_path}: {ex}")

    def cancel_task(self, task_id: str) -> bool:
        cancelled = False
        if task_id in self._subprocesses:
            try:
                self._subprocesses[task_id].kill()
                cancelled = True
            except Exception:
                pass
        if task_id in self._async_tasks and not self._async_tasks[task_id].done():
            self._async_tasks[task_id].cancel()
            cancelled = True
        if cancelled:
            self.touch_activity()
        return cancelled

    def get_task(self, task_id: str) -> Optional[DownloadTaskResponse]:
        self.touch_activity()
        return self.tasks.get(task_id)


downloader_manager = DownloadTaskManager()
