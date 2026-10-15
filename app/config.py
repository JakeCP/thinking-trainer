"""Settings, read once from the environment."""

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    app_password: str
    session_secret: str
    new_per_day: int
    tz: ZoneInfo
    prompts_dir: Path = ROOT / "prompts"
    content_dir: Path = ROOT / "content"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", ROOT / "data"))
    return Settings(
        data_dir=data_dir,
        database_url=os.environ.get("DATABASE_URL", f"sqlite:///{data_dir / 'app.db'}"),
        app_password=os.environ.get("APP_PASSWORD", ""),
        # Without a configured secret, sessions don't survive a restart. Fine locally.
        session_secret=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
        new_per_day=int(os.environ.get("NEW_PER_DAY", "3")),
        tz=ZoneInfo(os.environ.get("APP_TZ", "UTC")),
    )
