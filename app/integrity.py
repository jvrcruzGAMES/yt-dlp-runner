import hashlib
import hmac
import logging
import os
from pathlib import Path
import subprocess
from typing import Dict, List, Optional

from app.config import settings

logger = logging.getLogger("runner.integrity")


class RunnerIntegrityService:
    def __init__(self):
        self._cached_commit: Optional[str] = None
        self._root_dir = Path(__file__).resolve().parent.parent  # yt-dlp-runner/ root directory

    def get_commit_sha(self) -> str:
        """
        Discovers the current git commit SHA for the yt-dlp runner codebase.
        """
        env_sha = os.getenv("GIT_COMMIT_SHA") or os.getenv("RUNNER_GIT_COMMIT_SHA")
        if env_sha:
            return env_sha.strip()

        if self._cached_commit:
            return self._cached_commit

        # Try git rev-parse HEAD
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self._root_dir),
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                self._cached_commit = res.stdout.strip()
                return self._cached_commit
        except Exception:
            pass

        # Try reading .git directly
        git_head_file = self._root_dir / ".git" / "HEAD"
        if git_head_file.is_file():
            try:
                head_content = git_head_file.read_text().strip()
                if head_content.startswith("ref:"):
                    ref_path = head_content[4:].strip()
                    ref_file = self._root_dir / ".git" / ref_path
                    if ref_file.is_file():
                        self._cached_commit = ref_file.read_text().strip()
                        return self._cached_commit
                elif len(head_content) in (40, 64):
                    self._cached_commit = head_content
                    return self._cached_commit
            except Exception:
                pass

        return "dev"

    @staticmethod
    def calculate_git_blob_sha(content: bytes) -> str:
        """Calculates standard git object blob SHA-1 hash."""
        header = f"blob {len(content)}\0".encode("utf-8")
        return hashlib.sha1(header + content).hexdigest()

    def read_and_hash_file(self, relative_path: str) -> Optional[str]:
        clean_path = relative_path.lstrip("/")
        file_path = self._root_dir / clean_path
        if not file_path.is_file():
            return None
        return self.calculate_git_blob_sha(file_path.read_bytes())

    def get_image_metadata(self) -> tuple[Optional[str], Optional[str]]:
        image_ref = os.getenv("IMAGE_REF") or os.getenv("RUNNER_IMAGE_REF", "ghcr.io/jvrcruzgames/yt-dlp-runner:latest")
        image_digest = os.getenv("IMAGE_DIGEST") or os.getenv("RUNNER_IMAGE_DIGEST", None)
        return image_ref, image_digest


runner_integrity = RunnerIntegrityService()
