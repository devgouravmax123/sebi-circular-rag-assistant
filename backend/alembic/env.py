from logging.config import fileConfig
import os
import sys
from pathlib import Path

from sqlalchemy import engine_from_config
from sqlalchemy import pool
from pgvector.sqlalchemy import Vector

from alembic import context

# Ensure backend directory is in sys.path
backend_path = Path(__file__).resolve().parent.parent
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.persistence.models import Base
from app.core.config import get_settings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
try:
    config = context.config
except AttributeError:
    config = None

# Interpret the config file for Python logging.
if config is not None and config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here for 'autogenerate' support
target_metadata = Base.metadata


def get_url():
    """Get database URL from environment settings or alembic.ini fallback."""
    settings = get_settings()
    if settings.database_url:
        return settings.database_url
    return config.get_main_option("sqlalchemy.url", "postgresql://user:pass@localhost:5432/sebi_rag")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _ensure_version_table_capacity(connection) -> None:
    """
    Ensure the alembic_version table exists with sufficient column width (VARCHAR(64))
    to support descriptive revision identifiers without StringDataRightTruncation errors.
    If the table already exists with a shorter VARCHAR(32), alter it safely to VARCHAR(64).
    """
    from sqlalchemy import text
    try:
        # Create with VARCHAR(64) if not exists
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num VARCHAR(64) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
                ");"
            )
        )
        # In case the table already existed with VARCHAR(32), expand it
        connection.execute(
            text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64);")
        )
    except Exception:
        # If dialect or permissions do not support this, allow standard alembic flow to proceed
        pass


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        _ensure_version_table_capacity(connection)
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


try:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
except NameError:
    # Occurs when env.py is imported outside of an active Alembic runner context (e.g. in tests)
    pass
