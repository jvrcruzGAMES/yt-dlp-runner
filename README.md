# yt-dlp Runner Image & HTTP Supervisor

A self-contained Docker container equipped with Python, FFmpeg, Deno, yt-dlp, and a FastAPI HTTP supervisor.

## Bundled Dependencies & Plugins

- **Core**: Python 3.11, FFmpeg, Deno JS runtime, `curl_cffi` (impersonation client)
- **yt-dlp Extensions**:
  - `bgutil-ytdlp-pot-provider`: PO token provider for YouTube BOT protection bypassing.
  - `yt-dlp-ejs`: Embedded JavaScript challenge solver.
  - `yt-dlp`: Latest media extractor engine.
- **Supervisor**: FastAPI HTTP API for asynchronous job dispatch, isolated cookie injection, plugin installation, and file streaming.

## Supervisor Capabilities

1. **Plugin Installation**:
   - `POST /plugins/install`
   - Installs yt-dlp plugin packages or extractor extensions dynamically via `pip`.
2. **yt-dlp Downloads with Isolated Cookie Support**:
   - `POST /download`
   - Accepts download URL, format options, custom arguments, and raw cookie content.
   - Saves cookies strictly to an isolated directory (`/app/cookies/`) separated from the visible download directory (`/app/downloads/`).
3. **File Streaming**:
   - `GET /files`: List files in `/app/downloads`.
   - `GET /files/{file_id_or_name}/download`: Stream finished audio/video files.
4. **Integrity & Activity Monitoring**:
   - `GET /health`: Supervisor health status.
   - `GET /info`: Reports supervisor version, commit SHA, and OCI image digest for Orchestrator integrity verification.
   - `GET /activity`: Reports active task count and idle duration (used by Worker for 20-minute inactivity auto-teardown).

## Standalone Build & CI/CD

This repository includes a GitHub Actions workflow (`.github/workflows/build-and-sign.yml`) that:
1. Runs the test suite via `pytest`.
2. Builds the Docker container image.
3. Signs the OCI container image using **Cosign** (Keyless Sigstore via GitHub Actions OIDC) and attests SLSA provenance.
4. Pushes to GitHub Container Registry (`ghcr.io/jvrcruzgames/yt-dlp-runner:latest`).
