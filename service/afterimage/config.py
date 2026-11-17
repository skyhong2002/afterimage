"""Settings, read once from the environment.

AFTERIMAGE_DATA            data directory (database, originals, thumbs, view). Default ./data
AFTERIMAGE_FRONTEND        built frontend (site/dist). Unset = API only
AFTERIMAGE_PUBLIC_URL      public base URL, e.g. https://afterimage.skyhong.tw
AFTERIMAGE_ADMIN_PASSWORD_HASH   scrypt hash made by `python -m afterimage.auth`
AFTERIMAGE_SESSION_SECRET  random string used to sign the session cookie
AFTERIMAGE_API_TOKEN       bearer token for scripts and Hermes (empty = disabled)
INFOVORE_MCP_URL           infovore MCP endpoint used to find wallpaper gaps
AFTERIMAGE_SCANNER         0 turns the background scan off. Default on
TMDB_TOKEN                 TMDB v4 "API Read Access Token" (empty = TMDB source off)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    frontend_dir: Path | None
    public_url: str
    admin_password_hash: str
    session_secret: str
    api_token: str
    infovore_mcp: str
    tmdb_token: str
    scanner: bool = False   # background scan of the owner's whole history (on in production)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "afterimage.sqlite"

    @property
    def originals_dir(self) -> Path:
        return self.data_dir / "originals"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def view_dir(self) -> Path:
        return self.data_dir / "view"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.originals_dir, self.thumbs_dir, self.view_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)


def load() -> Settings:
    frontend = os.environ.get("AFTERIMAGE_FRONTEND")
    return Settings(
        data_dir=Path(os.environ.get("AFTERIMAGE_DATA", "./data")).resolve(),
        frontend_dir=Path(frontend).resolve() if frontend else None,
        public_url=os.environ.get("AFTERIMAGE_PUBLIC_URL", "http://localhost:8000").rstrip("/"),
        admin_password_hash=os.environ.get("AFTERIMAGE_ADMIN_PASSWORD_HASH", ""),
        session_secret=os.environ.get("AFTERIMAGE_SESSION_SECRET", ""),
        api_token=os.environ.get("AFTERIMAGE_API_TOKEN", ""),
        infovore_mcp=os.environ.get("INFOVORE_MCP_URL", "https://infovore.skyhong.tw/mcp"),
        tmdb_token=os.environ.get("TMDB_TOKEN", ""),
        scanner=os.environ.get("AFTERIMAGE_SCANNER", "1") != "0",
    )
