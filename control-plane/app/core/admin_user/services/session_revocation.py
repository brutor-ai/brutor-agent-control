# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Ending an admin's sessions when their password changes.
"""

from typing import Optional

from sqlalchemy.orm import Session

from app.composition.principals import PRINCIPALS
from app.core.admin_user.admin_principal import AdminPrincipal


def deactivate_admin_sessions(
    db: Session,
    admin_row: AdminPrincipal,
    reason: str,
    keep_session_id: Optional[str] = None,
) -> int:
    """Deactivate the admin's active sessions, except `keep_session_id`.

    Where the sessions are is asked of the principal the administrator
    belongs to: this module names no session table. A row no
    registered principal owns has no sessions here, and none is ended.

    Flushes; the caller commits. Returns how many sessions ended.
    """
    principal = PRINCIPALS.owner_of(admin_row)
    if principal is None or principal.active_sessions is None:
        return 0
    ended = 0
    for row in principal.active_sessions(db, admin_row.id):
        if row.id != keep_session_id:
            row.deactivate(reason)
            ended += 1
    db.flush()
    return ended
