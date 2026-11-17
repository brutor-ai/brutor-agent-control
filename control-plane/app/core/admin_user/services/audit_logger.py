# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Unified Admin Audit Logging Service

Files an administrator's action in the audit trail of the principal that
acted. The principal builds its own row
(`PrincipalSpec.audit_entry`, `app/composition/principals.py`): this module
knows no administrator model and no audit table.

When a record is written. A record is written
through a session of its own and committed there. A refusal
(`success=False`) is written at once, so that it survives the rollback of
the refused request. A success logged on a session that holds uncommitted
writes is queued on that session and written when its root transaction
commits, in the order logged; it is dropped when that transaction rolls back
or the session is closed without a commit: there is no record of a success
for a write that was not kept. A success logged with nothing uncommitted (a
read, or after the handler's own commit) is written at once.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.composition.principals import PRINCIPALS
from app.core.admin_user import tenant_principal
from app.core.logging_system import BrutorLogger


# `Session.info` keys.
_QUEUED = "brutor_audit_queued"   # success records awaiting the root commit: (bind, entry)
_WROTE = "brutor_audit_wrote"     # the current root transaction flushed or executed DML

_log = BrutorLogger("admin-audit")


def _write(bind: Any, entries: List[Any]) -> None:
    """Write `entries` to the audit trail, committed on their own."""
    audit_db = Session(bind=bind)
    try:
        for entry in entries:
            audit_db.add(entry)
        audit_db.commit()
    finally:
        audit_db.close()


def _has_uncommitted_writes(session: Session) -> bool:
    if not session.in_transaction():
        return False
    if session.info.get(_WROTE):
        return True
    return bool(session.new or session.dirty or session.deleted)


def _queue_until_commit(session: Session, entry: Any) -> bool:
    """Queue a success record on the session whose writes it reports.

    False means "write it now": the session holds nothing uncommitted, so
    what the record reports is durable already (the handler committed) or
    was a read. When unsure, write now: that is what it always did.
    """
    try:
        if not _has_uncommitted_writes(session):
            return False
        bind = session.get_bind()
    except Exception:  # noqa: BLE001
        return False
    session.info.setdefault(_QUEUED, []).append((bind, entry))
    return True


def _on_after_flush(session: Session, _flush_context) -> None:
    session.info[_WROTE] = True


def _on_do_orm_execute(orm_execute_state) -> None:
    if not orm_execute_state.is_select:
        orm_execute_state.session.info[_WROTE] = True


def _on_after_commit(session: Session) -> None:
    # A savepoint release fires this too; only the root commit is durable.
    if session.in_nested_transaction():
        return
    queued = session.info.pop(_QUEUED, None)
    if not queued:
        return
    # In the order logged; one session per bind (there is one).
    by_bind: Dict[int, tuple] = {}
    for bind, entry in queued:
        by_bind.setdefault(id(bind), (bind, []))[1].append(entry)
    for bind, entries in by_bind.values():
        try:
            _write(bind, entries)
        except Exception as e:  # noqa: BLE001 - as `log`: the trail's failure is not the caller's
            _log.error("Failed to create audit log", context={
                "actions": [getattr(entry, "action", None) for entry in entries],
                "error": str(e),
            }, exc_info=True)


def _on_after_transaction_end(session: Session, transaction) -> None:
    if transaction.parent is not None:
        return
    session.info.pop(_WROTE, None)
    # A root commit has written its queue; what is still here belongs to a
    # transaction that rolled back or was closed: its writes are gone, and
    # so is the record that they succeeded.
    dropped = session.info.pop(_QUEUED, None)
    if dropped:
        _log.debug("Dropped success audit records of a transaction that did not commit", context={
            "actions": [getattr(entry, "action", None) for _bind, entry in dropped],
        })


_SESSION_HOOKS = (
    ("after_flush", _on_after_flush),
    ("do_orm_execute", _on_do_orm_execute),
    ("after_commit", _on_after_commit),
    ("after_transaction_end", _on_after_transaction_end),
)


def install_session_hooks() -> None:
    """Idempotent. A session that queues nothing pays one dictionary write
    per flush."""
    for name, fn in _SESSION_HOOKS:
        if not event.contains(Session, name, fn):
            event.listen(Session, name, fn)


install_session_hooks()


class UnifiedAdminAuditLogger:
    """
    Unified audit logging service: the row goes to the acting principal's trail

    - a tenant administrator's actions → its tenant's trail (with tenant_id)
    - any other registered principal's → that principal's own trail (the
      system administrator's has no tenant_id)

    This ensures proper data isolation and makes audit queries efficient.
    """

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("admin-audit")

    def _principal_for(self, admin: Any, admin_type: Any):
        """Whose trail the row is for.

        With an administrator object, the principal that owns it, whatever
        `admin_type` says; an object nobody owns is an error (the row is not
        written). Without one, the registered principal `admin_type` names;
        no type, or a type nobody registered, is filed as a tenant
        administrator's — the trail that needs an existing tenant — and never
        in a platform-wide one.
        """
        if admin:
            principal = PRINCIPALS.owner_of(admin)
            if principal is None or principal.audit_entry is None:
                raise ValueError(f"Unknown admin type: {type(admin)}")
            return principal
        principal = PRINCIPALS.get(admin_type)
        if principal is None or principal.audit_entry is None:
            principal = PRINCIPALS.get(tenant_principal.NAME)
        if principal is None or principal.audit_entry is None:
            raise ValueError(f"No audit trail for admin type: {admin_type!r}")
        return principal

    def log(
        self,
        action: str,
        db: Session,
        admin: Optional[Any] = None,
        admin_id: Optional[str] = None,
        admin_type: Optional[Any] = None,
        success: bool = True,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        affected_tenant_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_method: Optional[str] = None,
        request_path: Optional[str] = None
    ) -> str:
        """
        Log an admin action to the appropriate audit table

        The acting principal decides the table (see `_principal_for`).

        Args:
            action: Action being performed
            db: Database session
            admin: Admin object - preferred over admin_id
            admin_id: Admin ID (if admin object not provided)
            admin_type: 'system' or 'tenant' (if admin object not provided)
            success: Whether action succeeded
            resource_type: Optional resource type
            resource_id: Optional resource ID
            tenant_id: Tenant ID for tenant admin actions
            affected_tenant_id: Tenant affected by system admin action
            details: Optional additional details
            ip_address: Optional IP address
            user_agent: Optional user agent
            request_method: Optional HTTP method
            request_path: Optional request path

        Returns:
            Audit log entry ID
        """
        try:
            principal = self._principal_for(admin, admin_type)

            # Generate audit log ID
            audit_id = str(uuid.uuid4())

            entry = principal.audit_entry(
                db,
                self.logger,
                admin=admin or None,
                admin_id=admin_id,
                tenant_id=tenant_id,
                affected_tenant_id=affected_tenant_id,
                id=audit_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                success=success,
                details=details or {},
                ip_address=ip_address,
                user_agent=user_agent,
                request_method=request_method,
                request_path=request_path,
                timestamp=datetime.now(timezone.utc),
            )
            if entry is None:
                # Nowhere to file it (the principal logged why). Return a
                # placeholder to avoid breaking the caller.
                return audit_id

            # The record of a success is true only once the write it
            # reports is committed: on a session that holds
            # uncommitted writes it waits for that session's commit, and
            # goes with a rollback.
            if success is True and _queue_until_commit(db, entry):
                return audit_id

            # Everything else is written now, through a session of its
            # own: the record of a refusal must survive the rollback of
            # the request that was refused (a handler that raises 401/403
            # loses whatever its own session held).
            _write(db.get_bind(), [entry])

            # Audit records are already persisted to the audit table
            # (authoritative trail). Emitting a DEBUG log line per
            # write creates noise on normal traffic — especially during
            # bulk actions like KB document uploads where one API call
            # audits every document. Drop the per-row log; rely on the
            # DB table for audit visibility.

            return audit_id

        except Exception as e:
            self.logger.error("Failed to create audit log", context={
                "action": action,
                "admin_id": getattr(admin, "id", None) if admin else admin_id,
                "error": str(e)
            }, exc_info=True)
            # Return a placeholder ID to not break the caller
            return str(uuid.uuid4())
