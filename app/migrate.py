"""Run Alembic migrations from code (the web app and ingest both call this at start)."""

from alembic import command
from alembic.config import Config

from app.config import ROOT


def upgrade(database_url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    command.upgrade(cfg, "head")
