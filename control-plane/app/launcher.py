# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
The command line of the control plane server, without an edition.

An entry point chooses the edition by the application factory it hands over,
and this module asks nobody which edition it is:

    main.py / app/cli.py        `launch(Edition(create_app=app.app.create_app))`
    enterprise/main.py /        the Enterprise Edition: registers the Enterprise
    brutor_enterprise/cli.py    extension and hands over `brutor_enterprise.app.create_app`

`run()` is `python <entry point> [options]`, `init()` is
`python <entry point> init`. The `edition-reset` command is dispatched by the
entry points themselves (it is reached by its name on the command line and by
nothing else).
"""

import sys
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

import click
import uvicorn

from app import env_vars
from app.services.config_manager import ConfigManager
from app.database import init_database
from app.core.logging_system import BrutorLogger, setup_logging


@dataclass(frozen=True)
class Edition:
    """What an entry point hands over: the application factory of its
    edition, and what `init` reports beyond the Community install (lines
    under "Created", and blocks of lines under "Default Credentials")."""

    create_app: Callable[..., object]
    created: Sequence[str] = ()
    credentials: Sequence[Sequence[str]] = ()


_EDITION: Optional[Edition] = None


def launch(edition: Edition) -> None:
    """Set the edition this process runs. Called once, by the entry point."""
    global _EDITION
    _EDITION = edition


def _edition() -> Edition:
    if _EDITION is None:
        raise RuntimeError("no edition: the entry point must call app.launcher.launch(...) first")
    return _EDITION


def initialize_database_system():
    """Initialize database tables and seed default data"""
    try:
        click.echo("Initializing database tables...")

        # Initialize all database tables
        init_database()

        click.echo(click.style("Database tables initialized", fg='green'))
        click.echo(click.style("Default tenant and admin users created", fg='green'))
        return True

    except Exception as e:
        click.echo(click.style(
            f"Failed to initialize database: {e}",
            fg='red'
        ))
        return False


def log_settings(log_level: Optional[str], json_logs: bool) -> tuple:
    """`(level, json)` of the log: the command-line options where they are
    given, else `BRUTOR_LOG_LEVEL` and `BRUTOR_LOG_JSON_ENABLED`."""
    return (
        env_vars.log_level_name(log_level or env_vars.BRUTOR_LOG_LEVEL.require()),
        bool(json_logs) or env_vars.BRUTOR_LOG_JSON_ENABLED.flag(),
    )


def enable_sql_echo() -> None:
    """What SQLAlchemy's `echo` and `echo_pool` do, for the development
    server only: every statement with its parameters (secrets and personal
    data included) and the pool's checkouts go to the log. Not an
    operator setting."""
    import logging

    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.pool").setLevel(logging.DEBUG)


def load_and_show_config(logger: Optional[BrutorLogger] = None):
    """Display the gateway configuration"""
    try:
        click.echo(click.style("Configuration:", fg='cyan', bold=True))
        # What is set, by name. A secret is never shown.
        for variable in (
            env_vars.BRUTOR_CONTROL_PLANE_LISTEN_ADDR, env_vars.JWT_SECRET, env_vars.DATABASE_URL, env_vars.BRUTOR_LOG_LEVEL,
        ):
            if variable.is_set():
                click.echo(f"  {variable.name}={'***SET***' if variable.secret else variable.string()}")
    except Exception as e:
        click.echo(click.style(f"Failed to display configuration: {e}", fg='red'))


@click.command()
@click.option(
    '--listen-addr',
    default=None,
    help='host:port to bind (overrides BRUTOR_CONTROL_PLANE_LISTEN_ADDR)'
)
@click.option(
    '--log-level',
    type=click.Choice(['DEBUG', 'INFO', 'WARN', 'WARNING', 'ERROR', 'CRITICAL'], case_sensitive=False),
    help='Log level (overrides BRUTOR_LOG_LEVEL)'
)
@click.option(
    '--json-logs',
    is_flag=True,
    help='Write the log as JSON (as BRUTOR_LOG_JSON_ENABLED=true does)'
)
@click.option(
    '--jwt-secret',
    help='JWT secret (overrides JWT_SECRET)'
)
@click.option(
    '--database-url',
    help='Database URL (overrides DATABASE_URL)'
)
@click.option(
    '--sql-echo',
    is_flag=True,
    help='Development only: log every SQL statement, with its parameters, and the pool\'s checkouts'
)
@click.option(
    '--reload',
    is_flag=True,
    help='Enable auto-reload for development'
)
@click.option(
    '--init-db',
    is_flag=True,
    help='Initialize database on startup'
)
@click.option(
    '--show-config',
    is_flag=True,
    help='Show loaded configuration and exit'
)
def run(
    listen_addr,
    log_level,
    json_logs,
    jwt_secret,
    database_url,
    sql_echo,
    reload,
    init_db,
    show_config
):
    """
    Start the Brutor Agent Control Plane Server with Multi-Tenancy Support
    """

    # An option stands for its variable, and takes precedence over it.
    if listen_addr:
        env_vars.BRUTOR_CONTROL_PLANE_LISTEN_ADDR.override(listen_addr)
    if log_level:
        env_vars.BRUTOR_LOG_LEVEL.override(log_level)
    if jwt_secret:
        env_vars.JWT_SECRET.override(jwt_secret)
    if database_url:
        env_vars.DATABASE_URL.override(database_url)

    # Initialize logging
    effective_log_level, use_json = log_settings(log_level, json_logs)
    logger = setup_logging(effective_log_level, use_json)
    if sql_echo:
        enable_sql_echo()

    # Show configuration if requested
    if show_config:
        load_and_show_config(logger)
        return

    try:
        # Create the temporary config manager to read values for server binding
        temp_config = ConfigManager(logger=logger, validate=False)

        # Show the startup banner
        show_startup_banner(temp_config)

        # Initialize database if requested
        if init_db:
            if not initialize_database_system():
                sys.exit(1)
            click.echo()

        # Create the FastAPI app
        app = _edition().create_app(logger=logger)

        # Precedence: ENV (set by CLI args or environment) → Default
        server_host = temp_config.get_host()
        server_port = temp_config.get_port()

        # Start the server
        uvicorn_config = {
            "app": app,
            "host": server_host,
            "port": server_port,
            "log_level": effective_log_level.lower(),
            "access_log": effective_log_level.lower(),
            "log_config": None,  # Disable uvicorn's default logging — use our formatter
            "server_header": False,
            "date_header": False,
            # Forwarded headers are the application's to judge
            # (BRUTOR_TRUSTED_PROXIES, app/middleware/trusted_proxies.py).
            "proxy_headers": False,
            "forwarded_allow_ips": "",
        }

        # Add reload for development
        if reload:
            uvicorn_config["reload"] = True
            click.echo(click.style(
                "Auto-reload enabled (development mode)",
                fg='yellow'
            ))

        uvicorn.run(**uvicorn_config)

    except KeyboardInterrupt:
        click.echo("\nShutdown requested by user")
        if 'logger' in locals():
            logger.info("Server shutdown requested by user")
    except Exception as e:
        click.echo(click.style(f"\nFailed to start server: {e}", fg='red'))
        if 'logger' in locals():
            logger.error(
                "Server startup failed",
                context={"error": str(e)},
                exc_info=True
            )
        sys.exit(1)
    finally:
        click.echo("Server stopped")


def show_startup_banner(config_manager: ConfigManager):
    """Show a concise startup banner"""
    db_type = "PostgreSQL" if "postgresql" in config_manager.get_database_url() else "SQLite"

    click.echo(click.style("Starting Brutor Agent Control Plane", fg='cyan', bold=True))
    click.echo(f"  Version:   {config_manager.get_version()}")
    click.echo(f"  Server:    {config_manager.get_gateway_base_url()}")
    click.echo(f"  Database:  {db_type}")
    click.echo(f"  Log Level: {config_manager.get_log_level()}")


@click.command()
def init():
    """Initialize the database and create default tenant with admin users"""
    if not initialize_database_system():
        sys.exit(1)

    click.echo(click.style("\nGateway initialized successfully!", fg='green', bold=True))
    click.echo()
    click.echo(click.style("Created:", fg='cyan'))
    click.echo("  ✓ Database tables")
    click.echo("  ✓ Default tenant")
    click.echo("  ✓ Default tenant admin")
    for line in _edition().created:
        click.echo(f"  ✓ {line}")
    click.echo()
    click.echo(click.style("Accounts:", fg='yellow'))
    for block in _edition().credentials:
        for line in block:
            click.echo(line)
        click.echo()
    click.echo("  Tenant Admin:")
    click.echo(f"    Username: {env_vars.BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME.require()}")
    click.echo(f"    Password: the value of {env_vars.BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD.name} at first start")
    click.echo("    Tenant:   default")
