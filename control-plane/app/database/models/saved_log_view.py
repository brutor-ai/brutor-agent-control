# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
SavedLogView — a per-tenant named filter preset for the proxy log
and admin audit log search UIs. See migration `a078_saved_log_views` for the design
rationale; in short:

  * scope == 'proxy_logs' or 'admin_audit_logs' — each viewer sees
    only its own scope's views in the dropdown.
  * owner_admin_id is the creator. The UI groups views into
    "Mine" (owner == current admin) and "Shared" (is_shared).
  * filters is the request envelope verbatim — JSONB so the
    schema doesn't need a migration every time the search
    envelope grows a new field.
"""

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func


# JSONB on Postgres (matches the migration); JSON on SQLite so the
# in-memory test harness can run create_all() without a CompileError.
# The endpoint only reads/writes the column as a Python dict, so the
# narrower SQLite type is functionally equivalent.
_FILTERS_TYPE = JSONB()

from app.database import Base


# Scope constants — these strings are the wire format (request bodies,
# DB rows, admin UI dropdown labels). Don't rename without a migration.
SCOPE_PROXY_LOGS = "proxy_logs"
SCOPE_ADMIN_AUDIT_LOGS = "admin_audit_logs"
VALID_SCOPES = (SCOPE_PROXY_LOGS, SCOPE_ADMIN_AUDIT_LOGS)


class SavedLogView(Base):
    """One named filter preset saved by a tenant admin."""

    __tablename__ = "saved_log_views"

    id = Column(
        String(64),
        primary_key=True,
        comment="ULID-shaped identifier, prefix slv-",
    )

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this view belongs to (hard isolation)",
    )

    scope = Column(
        String(32),
        nullable=False,
        comment=(
            "Which viewer the view applies to: 'proxy_logs' or "
            "'admin_audit_logs'. See module-level SCOPE_* constants."
        ),
    )

    owner_admin_id = Column(
        String(255),
        nullable=True,
        comment=(
            "Admin who created the view. NULL is permitted for "
            "system-seeded shared defaults — those show up under "
            "'Shared' for everyone on the tenant."
        ),
    )

    name = Column(
        String(255),
        nullable=False,
        comment="Operator-facing label",
    )

    description = Column(
        Text,
        nullable=True,
        comment="Optional longer description, surfaced in a tooltip",
    )

    filters = Column(
        _FILTERS_TYPE,
        nullable=False,
        comment=(
            "Request envelope: multi-value filter arrays, time range, "
            "free-text query. Re-validated against the live Pydantic "
            "search model on apply, so an old stored view can never "
            "produce a malformed request even after the model grows."
        ),
    )

    is_shared = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "When TRUE, all admins on the tenant see this view under "
            "'Shared'. Default is private."
        ),
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            f"scope IN ('{SCOPE_PROXY_LOGS}', '{SCOPE_ADMIN_AUDIT_LOGS}')",
            name="ck_saved_log_views_scope",
        ),
        UniqueConstraint(
            "tenant_id", "scope", "owner_admin_id", "name",
            name="uix_saved_log_views_tenant_scope_owner_name",
        ),
        Index(
            "ix_saved_log_views_tenant_scope",
            "tenant_id", "scope",
        ),
        Index(
            "ix_saved_log_views_tenant_scope_owner",
            "tenant_id", "scope", "owner_admin_id",
        ),
        {
            "comment": (
                "Per-tenant named filter presets for the proxy log + "
                "admin audit log viewers."
            )
        },
    )

    def __repr__(self) -> str:
        return (
            f"<SavedLogView(id={self.id!r}, tenant_id={self.tenant_id!r}, "
            f"scope={self.scope!r}, name={self.name!r}, "
            f"owner={self.owner_admin_id!r}, shared={self.is_shared})>"
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "scope": self.scope,
            "owner_admin_id": self.owner_admin_id,
            "name": self.name,
            "description": self.description,
            "filters": dict(self.filters or {}),
            "is_shared": bool(self.is_shared),
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "updated_at": (
                self.updated_at.isoformat() if self.updated_at else None
            ),
        }
