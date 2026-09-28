import os
from pathlib import Path
from typing import Optional
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_downloads_dir() -> str:
    if os.getenv("DOWNLOADS_DIR"):
        return os.getenv("DOWNLOADS_DIR")
    if os.path.exists("/app") or os.getenv("IN_DOCKER"):
        return "/app/downloads"
    return "./downloads"


def _default_cookies_dir() -> str:
    if os.getenv("COOKIES_DIR"):
        return os.getenv("COOKIES_DIR")
    if os.path.exists("/app") or os.getenv("IN_DOCKER"):
        return "/app/cookies"
    return "./cookies"


class RunnerSettings(BaseSettings):
    APP_NAME: str = "yt-dlp Runner Supervisor"
    APP_VERSION: str = "0.1.0"
    HOST: str = "0.0.0.0"
    PORT: int = 8080

    DOWNLOADS_DIR: str = _default_downloads_dir()
    COOKIES_DIR: str = _default_cookies_dir()

    # FlareSolverr & Proxy configuration
    # Traffic flow: yt-dlp -> FlareSolverr -> User defined proxy (if defined)
    HTTP_PROXY: Optional[str] = os.getenv(
        "HTTP_PROXY",
        os.getenv("http_proxy", os.getenv("FLARESOLVERR_PROXY", os.getenv("PROXY_URL", None)))
    )
    FLARESOLVERR_URL: str = os.getenv("FLARESOLVERR_URL", "http://127.0.0.1:8191/v1")
    FLARESOLVERR_PROXY: Optional[str] = os.getenv(
        "FLARESOLVERR_PROXY",
        os.getenv("HTTP_PROXY", os.getenv("PROXY_URL", None))
    )
    BGUTIL_POT_PROVIDER_URL: Optional[str] = os.getenv(
        "BGUTIL_POT_PROVIDER_URL",
        os.getenv("POT_PROVIDER_URL", "http://127.0.0.1:4416")
    )
    MITMPROXY_PORT: int = 8192
    USE_FLARESOLVERR_PROXY: bool = True

    @model_validator(mode="after")
    def sync_proxy_settings(self) -> "RunnerSettings":
        if self.HTTP_PROXY and not self.FLARESOLVERR_PROXY:
            self.FLARESOLVERR_PROXY = self.HTTP_PROXY
        elif self.FLARESOLVERR_PROXY and not self.HTTP_PROXY:
            self.HTTP_PROXY = self.FLARESOLVERR_PROXY
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def ensure_directories(self):
        try:
            Path(self.DOWNLOADS_DIR).mkdir(parents=True, exist_ok=True)
            Path(self.COOKIES_DIR).mkdir(parents=True, exist_ok=True)
        except OSError:
            # Fallback for restricted local test environments
            self.DOWNLOADS_DIR = "./downloads"
            self.COOKIES_DIR = "./cookies"
            Path(self.DOWNLOADS_DIR).mkdir(parents=True, exist_ok=True)
            Path(self.COOKIES_DIR).mkdir(parents=True, exist_ok=True)


settings = RunnerSettings()
settings.ensure_directories()
