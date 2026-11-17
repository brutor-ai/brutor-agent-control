# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Alembic migration environment.

Reads DATABASE_URL through the environment-variable registry (as the app
does) and uses the app's SQLAlchemy Base metadata for autogenerate support.

Usage:
    alembic revision --autogenerate -m "add x column"
    alembic upgrade head
    alembic downgrade -1
    alembic current
    alembic history

KEEP REVISION IDS ≤ 32 CHARS. The default ``alembic_version.version_num``
column is ``VARCHAR(32)``. Production tables were created at that width
and Postgres truncates the UPDATE on apply, leaving the migration in a
half-applied state with `StringDataRightTruncation`. If you need a
longer descriptive ID, either shorten it (``aNNN_short_summary``) or
widen the column on every existing DB before the new revision is
applied:
    ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64);
This codebase keeps IDs short.
"""

import logging
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Make the app importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The environment is checked before anything of it is used (the `alembic`
# command line gets here without passing an entry point of the control
# plane): a name that is no longer read, or a value that is not of its
# variable's type, stops the migration, one line per name and never a
# value.
from app import env_registry  # noqa: E402
from app.env_vars import DATABASE_URL  # noqa: E402

env_registry.refuse_or_exit(env_registry.REGISTRY.holds_enterprise_variables())

# Load all models so Base.metadata knows about every table
from app.database import Base, models  # noqa: E402,F401

# Alembic Config object
config = context.config

# Override sqlalchemy.url with DATABASE_URL from env
# (driver pinned the same way the app pins it; see normalize_database_url)
from app.database.database_config import normalize_database_url  # noqa: E402

database_url = DATABASE_URL.exact()
if database_url:
    # `%` doubled: configparser interpolation (see app.database._alembic_config)
    config.set_main_option("sqlalchemy.url", normalize_database_url(database_url).replace("%", "%%"))

# Logging: only apply alembic.ini's logger config when run standalone
# from the CLI (where no handlers have been attached yet). Under the
# app, BrutorLogger has already configured the root logger with our
# unified format — calling fileConfig here would wipe that out and
# downgrade every log line back to alembic.ini's default
# `%(levelname)-5.5s [%(name)s] ...` layout (that's why migrations used
# to print `WARNI [unified-auth]` instead of the standard format).
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL scripts without a connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect to the DB and apply changes."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
