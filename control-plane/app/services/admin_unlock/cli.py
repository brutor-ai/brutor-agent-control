# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
`python main.py admin-unlock USERNAME [--tenant ID]`: unlock an administrator
from the command line.

For the installation whose only administrator is locked: nobody is left who
could call `POST /v1/admin/users/tenant/{id}/unlock`. A lock after too many
wrong passwords ends by itself after `LOCKOUT_MINUTES`; this ends it now,
and it is the only way to lift a lock that has no end.

The database is the one the control plane uses (DATABASE_URL). The command
clears the lock, its end and the count of failed attempts, records that in
the tenant's admin audit log, and says what it did. It changes nothing else:
not the password, not the account's active flag. It prints no secret.

Exit status: 0 when the account is unlocked now (also when it was not
locked); 2 when there is no such administrator; 1 when it failed (nothing was
changed).
"""

from __future__ import annotations

import getpass
import socket
import sys
import uuid
from datetime import datetime, timezone

import click

from app.core.admin_user import lockout

COMMAND = "admin-unlock"
AUDIT_ACTION = "tenant_admin_unlocked"


def _engine():
    from app.database import engine

    return engine


def _operator() -> str:
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:  # noqa: BLE001 - no user database in some containers
        return "unknown"


@click.command(name=COMMAND)
@click.argument("username")
@click.option(
    "--tenant",
    default="default",
    show_default=True,
    metavar="ID",
    help="The tenant the administrator belongs to.",
)
def admin_unlock(username: str, tenant: str) -> None:
    """Unlock the administrator USERNAME: clear the lock and the count of failed sign-ins."""
    from sqlalchemy.orm import Session

    from app.database.models.admin import TenantAdmin, TenantAdminAuditLog

    engine = _engine()
    click.echo(f"Database: {engine.url.render_as_string(hide_password=True)}")
    try:
        with Session(bind=engine, autoflush=False, expire_on_commit=False) as db:
            row = (
                db.query(TenantAdmin)
                .filter(TenantAdmin.username == username, TenantAdmin.tenant_id == tenant)
                .first()
            )
            if row is None:
                click.echo(f"No administrator '{username}' in tenant '{tenant}'. Nothing was changed.")
                sys.exit(2)
            if not row.is_locked and not (row.failed_login_attempts or 0):
                click.echo(f"Administrator '{username}' of tenant '{tenant}' was not locked. Nothing was changed.")
                _say_if_deactivated(row)
                return

            was_locked = lockout.currently_locked(row)
            until = row.locked_until
            attempts = row.failed_login_attempts or 0
            lockout.clear(row)
            row.updated_at = datetime.now(timezone.utc)
            audit_id = str(uuid.uuid4())
            db.add(
                TenantAdminAuditLog(
                    id=audit_id,
                    tenant_id=tenant,
                    admin_id=None,
                    action=AUDIT_ACTION,
                    resource_type="tenant_admin",
                    resource_id=row.id,
                    request_method="CLI",
                    request_path=COMMAND,
                    success=True,
                    details={
                        "operator": _operator(),
                        "unlocked_admin_id": row.id,
                        "unlocked_username": row.username,
                        "was_locked": was_locked,
                        "failed_login_attempts": attempts,
                    },
                    timestamp=datetime.now(timezone.utc),
                )
            )
            db.commit()
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - report it and leave with an error
        click.echo(f"FAILED: {type(error).__name__}. Nothing was changed.")
        sys.exit(1)

    if was_locked:
        end = f"until {until.isoformat()}" if until is not None else "with no end"
        click.echo(f"UNLOCKED administrator '{username}' of tenant '{tenant}' (it was locked {end}).")
    else:
        click.echo(f"UNLOCKED administrator '{username}' of tenant '{tenant}' (its lock had already expired).")
    click.echo(f"Count of failed sign-ins cleared (it was {attempts}). The password is unchanged.")
    click.echo(f"Recorded in the admin audit log of tenant '{tenant}' (record {audit_id}).")
    _say_if_deactivated(row)


def _say_if_deactivated(row) -> None:
    if not row.is_active:
        click.echo("Note: this account is deactivated, which this command does not change; it cannot sign in.")
