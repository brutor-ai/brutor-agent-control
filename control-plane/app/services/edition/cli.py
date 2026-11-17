# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
`python main.py edition-reset`: the operator command that lets the Community
Edition start on a database the Enterprise Edition has used.

    python main.py edition-reset                      what would be deleted (dry run)
    python main.py edition-reset --keep-tenant default --confirm-delete-enterprise-data

A dry run unless it is given `--confirm-delete-enterprise-data` on the command
line. The flag has no other spelling: it is not read from the process
environment, and the command never asks. The database is the one the control
plane uses (DATABASE_URL). What the reset does is in
`app/services/edition/reset.py`.

Exit status: 0 for a dry run, a finished reset, or nothing to reset; 2 when
the reset is refused (the operator has to choose something); 1 when it failed
and was rolled back.
"""

from __future__ import annotations

import getpass
import socket
import sys
from typing import Optional

import click

from app.services.edition import reset


def _engine():
    from app.database import engine

    return engine


def _operator() -> str:
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:  # noqa: BLE001 - no user database in some containers
        return "unknown"


@click.command(name="edition-reset")
@click.option(
    "--confirm-delete-enterprise-data",
    is_flag=True,
    default=False,
    help="Delete what the dry run lists. Without it nothing is changed.",
)
@click.option(
    "--keep-tenant",
    default=None,
    metavar="ID",
    help="The tenant that is kept when there are several. Every other tenant is deleted.",
)
def edition_reset(confirm_delete_enterprise_data: bool, keep_tenant: Optional[str]) -> None:
    """Delete the Enterprise Edition's data, so that the Community Edition starts.

    Stop the control plane and the core gateway first, and take a pg_dump.
    """
    engine = _engine()
    click.echo("Edition reset: deletes the Enterprise Edition's data from the database,")
    click.echo("so that the Community Edition starts on it.")
    click.echo(f"Database: {engine.url.render_as_string(hide_password=True)}")
    click.echo("")

    try:
        outcome = reset.reset_enterprise_data(
            engine,
            confirm=confirm_delete_enterprise_data is True,
            keep_tenant=keep_tenant,
            operator=_operator(),
        )
    except Exception as error:  # noqa: BLE001 - report it and leave with an error
        click.echo(f"FAILED: {type(error).__name__}. The reset was rolled back. Nothing was changed.")
        sys.exit(1)

    plan = outcome.plan
    if plan.nothing_to_do:
        click.echo("Nothing to reset: no Enterprise Edition data and no edition marker. Nothing was changed.")
        return

    click.echo(reset.format_plan(plan, done=outcome.executed))
    click.echo("")
    if plan.blockers:
        click.echo("REFUSED. Nothing was changed.")
        for blocker in plan.blockers:
            click.echo(f"  - {blocker}")
        sys.exit(2)
    if outcome.executed:
        click.echo(f"RESET DONE (admin audit log record {outcome.audit_id}).")
        click.echo("The Community Edition now starts on this database.")
        click.echo("If you did not take a pg_dump before this, the deleted data cannot be brought back.")
        return
    click.echo("DRY RUN. Nothing was changed.")
    click.echo("Before the reset: stop the control plane and the core gateway, and take a backup, for example")
    click.echo("    pg_dump --format=custom --file=before-edition-reset.dump \"$DATABASE_URL\"")
    click.echo(f"Then run the command again with {reset.CONFIRM_FLAG} to delete what is listed above.")
