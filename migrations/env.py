from alembic import context
from sqlalchemy import pool

from app import models  # noqa: F401  (registers the tables)
from app.config import load_settings
from app.db import Base, make_engine

config = context.config
url = config.attributes.get("database_url") or load_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(url)
    with engine.connect() as connection:
        # render_as_batch lets later migrations alter columns on SQLite.
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
