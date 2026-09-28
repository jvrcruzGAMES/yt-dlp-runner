import asyncio
import os
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient
from app.args_sanitizer import sanitize_yt_dlp_args
from app.config import settings
from app.downloader import (
    build_yt_dlp_command,
    downloader_manager,
    parse_eta_to_seconds,
    parse_size_to_bytes,
    parse_speed_to_bytes_per_sec,
)
from app.main import app
from app.plugins import normalize_plugin_spec
from app.schemas import DownloadRequest


def test_sanitize_yt_dlp_args():
    client_args = [
        "--proxy", "http://client-proxy:8080",
        "--write-subs",
        "--sub-lang", "en",
        "--outtmpl", "%(title)s.mp4",
        "-o", "custom.mp4",
        "--proxy=http://client-proxy-2:8080",
        "--write-thumbnail",
        "-f", "best",
        "--cookies", "/tmp/client_cookie.txt",
        "--exec", "rm -rf /",
        "--dump-json",
    ]

    sanitized, stripped = sanitize_yt_dlp_args(client_args)

    # Allowed arguments should be preserved
    assert "--write-subs" in sanitized
    assert "--sub-lang" in sanitized
    assert "en" in sanitized
    assert "--write-thumbnail" in sanitized
    assert "-f" in sanitized
    assert "best" in sanitized

    # Disallowed arguments must be stripped
    assert "--proxy" not in sanitized
    assert "http://client-proxy:8080" not in sanitized
    assert "--proxy=http://client-proxy-2:8080" not in sanitized
    assert "--outtmpl" not in sanitized
    assert "-o" not in sanitized
    assert "custom.mp4" not in sanitized
    assert "--cookies" not in sanitized
    assert "/tmp/client_cookie.txt" not in sanitized
    assert "--exec" not in sanitized
    assert "--dump-json" not in sanitized

    assert len(stripped) > 0


def test_normalize_plugin_spec():
    # 1. Standard package names should not be altered
    assert normalize_plugin_spec("bgutil-ytdlp-pot-provider") == "bgutil-ytdlp-pot-provider"
    assert normalize_plugin_spec("yt-dlp-ejs>=0.8.0") == "yt-dlp-ejs>=0.8.0"

    # 2. HTTPS Git URLs
    assert normalize_plugin_spec("https://github.com/user/plugin-repo") == "git+https://github.com/user/plugin-repo"
    assert normalize_plugin_spec("https://github.com/user/plugin-repo.git") == "git+https://github.com/user/plugin-repo.git"
    assert normalize_plugin_spec("https://gitlab.com/user/plugin-repo.git@v1.0.0") == "git+https://gitlab.com/user/plugin-repo.git@v1.0.0"

    # 3. HTTP Git URLs
    assert normalize_plugin_spec("http://git.internal/repo.git") == "git+http://git.internal/repo.git"

    # 4. SSH Git URLs
    assert normalize_plugin_spec("git@github.com:user/plugin-repo.git") == "git+ssh://git@github.com/user/plugin-repo.git"

    # 5. Shorthand forge domain without protocol
    assert normalize_plugin_spec("github.com/user/plugin-repo") == "git+https://github.com/user/plugin-repo"
    assert normalize_plugin_spec("gitlab.com/user/plugin-repo") == "git+https://gitlab.com/user/plugin-repo"

    # 6. Already formatted VCS URLs
    assert normalize_plugin_spec("git+https://github.com/user/repo") == "git+https://github.com/user/repo"
    assert normalize_plugin_spec("git+ssh://git@github.com/user/repo.git") == "git+ssh://git@github.com/user/repo.git"


def test_build_yt_dlp_command():
    req = DownloadRequest(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        format_selection="bestvideo+bestaudio/best",
        output_template="%(title)s [%(id)s].%(ext)s",
        custom_args=["--write-subs", "--sub-lang", "en", "--write-thumbnail", "--proxy", "http://evil:8080"],
    )
    downloads_dir = Path(settings.DOWNLOADS_DIR)
    cmd, stripped = build_yt_dlp_command(req, downloads_dir)

    assert "-m" in cmd
    assert "yt_dlp" in cmd
    assert "--no-check-certificates" in cmd
    assert "--paths" in cmd
    assert f"home:{downloads_dir.resolve()}" in cmd
    assert "--no-mtime" in cmd
    assert "-f" in cmd
    assert "bestvideo+bestaudio/best" in cmd
    assert "--write-subs" in cmd
    assert "--sub-lang" in cmd
    assert "en" in cmd
    assert "--write-thumbnail" in cmd
    assert "--proxy" not in cmd  # Stripped worker-reserved proxy
    assert "http://evil:8080" not in cmd
    assert cmd[-1] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_progress_parsing_helpers():
    assert parse_size_to_bytes("10.23MiB") == int(10.23 * 1024 * 1024)
    assert parse_size_to_bytes("500KiB") == 500 * 1024
    assert parse_size_to_bytes("1.5GB") == int(1.5 * 1000 * 1000 * 1000)
    assert parse_size_to_bytes("~25.50MiB") == int(25.5 * 1024 * 1024)

    assert parse_eta_to_seconds("00:45") == 45
    assert parse_eta_to_seconds("01:30") == 90
    assert parse_eta_to_seconds("01:10:05") == 3600 + 600 + 5

    assert parse_speed_to_bytes_per_sec("2.50MiB/s") == 2.5 * 1024 * 1024
    assert parse_speed_to_bytes_per_sec("100KiB/s") == 100 * 1024


@pytest.mark.asyncio
async def test_supervisor_health_and_activity():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"

        resp = await client.get("/activity")
        assert resp.status_code == 200
        act = resp.json()
        assert act["is_busy"] is False
        assert act["idle_seconds"] >= 0


@pytest.mark.asyncio
async def test_cookie_directory_isolation(tmp_path):
    # Verify cookies dir and downloads dir are distinct
    assert settings.COOKIES_DIR != settings.DOWNLOADS_DIR
    assert not settings.COOKIES_DIR.startswith(settings.DOWNLOADS_DIR)


@pytest.mark.asyncio
async def test_file_listing_and_hex_streaming_multi_format():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create media and non-media test files in downloads directory (video, subtitle, thumbnail, description, json)
        downloads_dir = Path(settings.DOWNLOADS_DIR)
        test_video = downloads_dir / "test_video.mp4"
        test_sub = downloads_dir / "test_video.en.vtt"
        test_thumb = downloads_dir / "test_video.webp"
        test_desc = downloads_dir / "test_video.description"
        test_json = downloads_dir / "test_video.info.json"

        test_video.write_bytes(b"dummy video data")
        test_sub.write_bytes(b"WEBVTT subtitle data")
        test_thumb.write_bytes(b"RIFFWEBP thumbnail data")
        test_desc.write_text("Test video description")
        test_json.write_text('{"id": "123", "title": "Test Video"}')

        try:
            resp = await client.get("/files")
            assert resp.status_code == 200
            files = resp.json()
            assert len(files) >= 5

            filenames = [f["filename"] for f in files]
            assert "test_video.mp4" in filenames
            assert "test_video.en.vtt" in filenames
            assert "test_video.webp" in filenames
            assert "test_video.description" in filenames
            assert "test_video.info.json" in filenames

            video_item = next(f for f in files if f["filename"] == "test_video.mp4")
            assert "file_id" in video_item
            assert len(video_item["file_id"]) == 16
            assert f"/files/{video_item['file_id']}/download" == video_item["download_url"]

            sub_item = next(f for f in files if f["filename"] == "test_video.en.vtt")
            thumb_item = next(f for f in files if f["filename"] == "test_video.webp")
            desc_item = next(f for f in files if f["filename"] == "test_video.description")
            json_item = next(f for f in files if f["filename"] == "test_video.info.json")

            # Stream video using hex ID
            stream_video = await client.get(f"/files/{video_item['file_id']}/download")
            assert stream_video.status_code == 200
            assert stream_video.content == b"dummy video data"

            # Stream subtitle using hex ID
            stream_sub = await client.get(f"/files/{sub_item['file_id']}/download")
            assert stream_sub.status_code == 200
            assert stream_sub.content == b"WEBVTT subtitle data"

            # Stream thumbnail
            stream_thumb = await client.get(f"/files/{thumb_item['file_id']}/download")
            assert stream_thumb.status_code == 200
            assert stream_thumb.content == b"RIFFWEBP thumbnail data"

            # Stream description
            stream_desc = await client.get(f"/files/{desc_item['file_id']}/download")
            assert stream_desc.status_code == 200
            assert stream_desc.text == "Test video description"

            # Stream json
            stream_json = await client.get(f"/files/{json_item['file_id']}/download")
            assert stream_json.status_code == 200
            assert '"title": "Test Video"' in stream_json.text
        finally:
            for p in [test_video, test_sub, test_thumb, test_desc, test_json]:
                if p.exists():
                    p.unlink()


@pytest.mark.asyncio
async def test_stream_subprocess_lines_handling_large_chunks_and_carriage_returns():
    import asyncio
    from app.downloader import stream_subprocess_lines

    reader = asyncio.StreamReader()
    
    # 1. Feed a 100KB line without newlines (would cause LimitOverrunError in standard readline())
    large_line = "A" * (100 * 1024)
    reader.feed_data(f"{large_line}\n".encode("utf-8"))

    # 2. Feed carriage return progress updates (\r)
    reader.feed_data(b"[download]  10% of 100MB\r[download]  50% of 100MB\r\n[download] 100% of 100MB\n")
    reader.feed_eof()

    collected = []
    async for line in stream_subprocess_lines(reader, chunk_size=2048):
        collected.append(line)

    assert len(collected) >= 4
    assert collected[0] == large_line
    assert "[download]  10% of 100MB" in collected
    assert "[download]  50% of 100MB" in collected
    assert "[download] 100% of 100MB" in collected


@pytest.mark.asyncio
async def test_write_info_json_download_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOADS_DIR", str(tmp_path))
    
    # Simulate download request with --write-info-json
    req = DownloadRequest(
        url="https://www.youtube.com/watch?v=sample123",
        custom_args=["--write-info-json", "--skip-download"]
    )
    
    # We mock asyncio.create_subprocess_exec to write an info.json file
    async def mock_subprocess_exec(*args, **kwargs):
        cwd = kwargs.get("cwd", str(tmp_path))
        info_file = Path(cwd) / "Sample Video [sample123].info.json"
        info_file.write_text('{"id": "sample123", "title": "Sample Video"}')

        class MockStdout:
            def __init__(self):
                self._lines = [
                    f"[info] Writing video metadata as JSON to: {info_file}\n".encode("utf-8"),
                    b"[info] Finished downloading video metadata\n"
                ]

            async def read(self, n=4096):
                if self._lines:
                    return self._lines.pop(0)
                return b""

        class MockProcess:
            def __init__(self):
                self.stdout = MockStdout()
                self.returncode = 0

            async def wait(self):
                return 0

            async def communicate(self):
                return b"", b""

            def kill(self):
                pass

        return MockProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_subprocess_exec)

    task_resp = await downloader_manager.start_download(req)
    # Wait for completion
    for _ in range(20):
        await asyncio.sleep(0.1)
        t = downloader_manager.get_task(task_resp.task_id)
        if t and t.status in ["completed", "failed"]:
            break

    finished_task = downloader_manager.get_task(task_resp.task_id)
    assert finished_task.status == "completed"
    assert len(finished_task.files) == 1
    assert finished_task.files[0].filename == "Sample Video [sample123].info.json"
    assert finished_task.filename == "Sample Video [sample123].info.json"
    assert len(finished_task.files[0].file_id) == 16

    # Verify file can be retrieved by hex ID
    found = downloader_manager.get_file_by_id_or_name(finished_task.files[0].file_id)
    assert found is not None
    assert found.exists()


