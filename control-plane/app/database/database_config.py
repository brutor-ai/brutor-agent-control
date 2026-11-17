# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Database Configuration Module
The control plane's PostgreSQL engine: DATABASE_URL, the pool size and the
statement timeout from the environment, everything else fixed here.

The control plane only supports PostgreSQL at runtime. Tests that need
an in-memory database construct their own `create_engine("sqlite:///:memory:")`
directly (e.g. `tests/test_admin_rbac_endpoints.py`) — they do not pass
through this module. Do not re-introduce a SQLite branch here; a stray
`sqlite://` URL would silently downsize the connection pool.
"""

from typing import Dict, Any
from dataclasses import dataclass

from sqlalchemy.engine import make_url

from app import env_vars


# The PostgreSQL driver this image ships (pyproject: psycopg2-binary).
POSTGRES_DRIVER = "psycopg2"


def normalize_database_url(url: str) -> str:
    """Pin a driverless ``postgresql://`` URL to the driver we install.

    A bare ``postgresql://`` lets SQLAlchemy pick the DBAPI, and the pick
    changed under us: SQLAlchemy 2.1 made psycopg (v3) the default where
    2.0 used psycopg2. An unpinned rebuild (0.11.12) pulled 2.1, every
    worker died on ``No module named 'psycopg'`` and the control plane
    never came up. Naming the driver makes the URL mean the same thing
    on every SQLAlchemy version. A URL that already names a driver
    (``postgresql+psycopg://``, ``postgresql+asyncpg://``) is left alone.
    """
    scheme, sep, rest = url.partition("://")
    if sep and scheme.lower() == "postgresql":
        return f"postgresql+{POSTGRES_DRIVER}://{rest}"
    return url


# The engine's fixed settings. Only the pool size and the statement timeout
# are variables (`app/env_vars.py`); TLS is set in DATABASE_URL.
POOL_TIMEOUT_SECS = 30      # wait for a free pooled connection
POOL_RECYCLE_SECS = 1800    # below the idle timeout of a typical load balancer or PgBouncer
CONNECT_TIMEOUT_SECS = 10   # unless DATABASE_URL states `connect_timeout`


@dataclass
class DatabaseConfig:
    """The engine's settings: the URL, the pool size and the statement
    timeout. Everything else is fixed: the overflow equals the pool size,
    a pooled connection is tested before use, and the transaction
    isolation is PostgreSQL's own (READ COMMITTED), which the code assumes.
    TLS is what the URL's libpq parameters say (`sslmode`, `sslrootcert`,
    `sslcert`, `sslkey`): nothing here replaces them."""

    url: str
    pool_size: int = 20
    command_timeout: int = 60  # statement_timeout, seconds

    @property
    def max_overflow(self) -> int:
        return self.pool_size

    @property
    def is_postgresql(self) -> bool:
        """Check if using PostgreSQL (the only supported runtime backend)."""
        return "postgresql" in self.url.lower()

    def get_engine_config(self) -> Dict[str, Any]:
        """Build the SQLAlchemy engine kwargs for a PostgreSQL DSN.

        Raises ValueError on a non-PostgreSQL URL — a stray `sqlite://`
        or other backend would silently fall back to SQLAlchemy's
        default 5-connection pool and cause exhaustion under load. Fail
        fast at startup instead.
        """
        if not self.is_postgresql:
            raise ValueError(
                f"Unsupported DATABASE_URL: {self.url!r}. The control "
                f"plane requires PostgreSQL (postgresql:// or "
                f"postgresql+driver://). Tests that need an in-memory "
                f"DB construct their own engine directly."
            )

        config = {
            "pool_pre_ping": True,
            "pool_size": self.pool_size,
            "max_overflow": self.max_overflow,
            "pool_timeout": POOL_TIMEOUT_SECS,
            "pool_recycle": POOL_RECYCLE_SECS,
        }

        # What the URL's query states. The driver gets `connect_args` after
        # the URL, so a key set here REPLACES the URL's: the URL's own
        # libpq parameters are therefore never set here.
        url_query = dict(make_url(self.url).query)
        connect_args = {}
        if "connect_timeout" not in url_query:
            connect_args["connect_timeout"] = CONNECT_TIMEOUT_SECS
        if self.command_timeout:
            timeout = f"-c statement_timeout={self.command_timeout * 1000}"
            url_options = url_query.get("options")
            if isinstance(url_options, tuple):
                url_options = " ".join(url_options)
            connect_args["options"] = f"{url_options} {timeout}" if url_options else timeout
        if connect_args:
            config["connect_args"] = connect_args
        return config


def load_database_config() -> DatabaseConfig:
    """The engine's settings, read from the environment: `DATABASE_URL`
    (required), `BRUTOR_CONTROL_PLANE_DB_POOL_SIZE` and
    `BRUTOR_DB_COMMAND_TIMEOUT_SECS`, each defined in `app/env_vars.py`."""
    database_url = env_vars.DATABASE_URL.exact()
    if not database_url:
        raise ValueError(
            "DATABASE_URL environment variable is required. "
            "Example: postgresql://user:password@host:port/database"
        )
    return DatabaseConfig(
        url=normalize_database_url(database_url),
        pool_size=env_vars.BRUTOR_CONTROL_PLANE_DB_POOL_SIZE.integer(),
        command_timeout=env_vars.BRUTOR_DB_COMMAND_TIMEOUT_SECS.integer(),
    )


def get_database_info(config: DatabaseConfig) -> Dict[str, Any]:
    """
    Get database connection information for logging (without sensitive data)

    Args:
        config: DatabaseConfig instance

    Returns:
        Dictionary with safe connection info
    """
    # Parse URL to extract components (hide password)
    url = config.url
    if "@" in url:
        # Format: postgresql://user:password@host:port/database
        protocol = url.split("://")[0]
        rest = url.split("://")[1]
        if "@" in rest:
            credentials, location = rest.split("@", 1)
            user = credentials.split(":")[0]
            masked_url = f"{protocol}://{user}:***@{location}"
        else:
            masked_url = url
    else:
        masked_url = url

    return {
        "url": masked_url,
        "type": "PostgreSQL",
        "pool_size": config.pool_size,
        "max_overflow": config.max_overflow,
        "command_timeout": config.command_timeout,
        "ssl_mode": dict(make_url(config.url).query).get("sslmode", "libpq default (prefer)"),
    }


# Module-level configuration
db_config = load_database_config()