# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""SystemOperationLog — audit row for operator actions taken from the
System Operations Console.

Each row records one click of a destructive action — counter reset,
cache flush, force logout, scheduler trigger, maintenance toggle,
log-level change, db vacuum, db backend terminate, …

This table is intentionally separate from `admin_audit_log` (which
tracks config writes — create tenant, edit guardrail, etc.). System
actions need `before_state` and `after_state` JSONB so the Audit tab
can render a runtime-state diff; admin audit rows don't. The retention
policies also differ — system rows could reasonably retention out at
365 days per tenant; admin audit rows survive indefinitely.

Insert-only. The endpoints never UPDATE or DELETE a row — the audit
trail is immutable. Operators with `SYSTEM_READ` can list and
filter; nobody has DELETE permissions (cascade from tenant delete is
the only legitimate removal path, and that's the trade-off for the
tenant FK).

See `alembic/versions/a081_system_ops_console.py` for column-level
design notes.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.sql import func

from app.database import Base


# Wire-format constants. Don't rename these strings without a migration:
# they live in row data, audit-tab filter URLs, and operator runbooks.

ACTOR_TYPE_SYSTEM_ADMIN = "system_admin"
ACTOR_TYPE_TENANT_ADMIN = "tenant_admin"
# The platform acting on its own, e.g. a drift response
# tightening a system's autonomy. Distinct from an admin on purpose: an
# operator scanning the audit trail must be able to tell at a glance which
# changes a human made and which the closed loop made, and attributing an
# automatic action to whoever happened to write the policy would be a lie.
ACTOR_TYPE_SYSTEM = "system"
VALID_ACTOR_TYPES = (
    ACTOR_TYPE_SYSTEM_ADMIN,
    ACTOR_TYPE_TENANT_ADMIN,
    ACTOR_TYPE_SYSTEM,
)

# The `actor_admin_id` written for automatic actions.
SYSTEM_ACTOR_DRIFT_RESPONSE = "system:drift-response"

# The action enum is OPEN (not a CHECK constraint) so future System
# sub-sections can ship new actions without a migration. The Audit
# tab's action facet reads `DISTINCT action` from the table; the
# list below is the canonical set known at write time
# but is not enforced.
ACTION_COUNTER_RESET = "counter_reset"
ACTION_COOLDOWN_RELEASE = "cooldown_release"
ACTION_CACHE_FLUSH = "cache_flush"
ACTION_FORCE_LOGOUT = "force_logout"
ACTION_SCHEDULER_TRIGGER = "scheduler_trigger"

# The closed loop.
ACTION_DRIFT_RESPONSE = "drift_response"
ACTION_DRIFT_RESPONSE_RESTORE = "drift_response_restore"
ACTION_SCHEDULER_PAUSE = "scheduler_pause"
ACTION_SCHEDULER_RESUME = "scheduler_resume"
ACTION_MAINTENANCE_TOGGLE = "maintenance_toggle"
ACTION_LOG_LEVEL_CHANGE = "log_level_change"
ACTION_DB_VACUUM = "db_vacuum"
ACTION_DB_TERMINATE = "db_terminate"
ACTION_POOL_DISPOSE = "pool_dispose"
ACTION_POOL_RECONNECT = "pool_reconnect"
ACTION_QDRANT_JWT_ROTATE = "qdrant_jwt_rotate"
ACTION_UPSTREAM_TEST = "upstream_test"
ACTION_RESTART_ROLLING = "restart_rolling"

# Scope kinds — operator-meaningful narrowings of what an action
# targeted. Same open-enum pattern as actions.
SCOPE_TENANT = "tenant"
SCOPE_GROUP = "group"
SCOPE_USER = "user"
SCOPE_API_KEY = "api_key"
SCOPE_MODEL = "model"
SCOPE_MCP_SERVER = "mcp_server"
SCOPE_CACHE_LAYER = "cache_layer"
SCOPE_SCHEDULER = "scheduler"
SCOPE_SESSION = "session"
SCOPE_DB_TABLE = "db_table"
SCOPE_LOG_MODULE = "log_module"


# JSONB on Postgres, JSON on SQLite for the in-memory test harness.
# The endpoint reads / writes these columns as Python dicts; the
# narrower SQLite shape is functionally equivalent for tests.
_JSONB_TYPE = JSONB()
_INET_TYPE = INET().with_variant(String(45), "sqlite")  # IPv6 max len = 45


class SystemOperationLog(Base):
    """One row per operator action from the System Operations Console."""

    __tablename__ = "system_operation_log"

    id = Column(
        String(64),
        primary_key=True,
        comment="ULID-shaped identifier, prefix sol-",
    )

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        comment=(
            "Tenant the action targeted. NULL = global / cross-tenant "
            "action (only system admins can emit these)."
        ),
    )

    actor_admin_id = Column(
        String(255),
        nullable=False,
        comment="Admin who triggered the action (system_admin or tenant_admin id).",
    )

    actor_type = Column(
        String(32),
        nullable=False,
        comment="'system_admin', 'tenant_admin' or 'system' — see VALID_ACTOR_TYPES.",
    )

    action = Column(
        String(64),
        nullable=False,
        comment=(
            "Action verb. See module-level ACTION_* constants for the "
            "canonical set. Open enum — new actions are allowed without "
            "a migration."
        ),
    )

    scope_kind = Column(
        String(32),
        nullable=True,
        comment=(
            "What the action scoped to. See module-level SCOPE_* "
            "constants. NULL = action has no narrower scope than "
            "tenant_id."
        ),
    )

    scope_id = Column(
        String(255),
        nullable=True,
        comment=(
            "Identifier of the scoped subject. Shape depends on "
            "scope_kind (group_id, user_id, etc.)."
        ),
    )

    payload = Column(
        _JSONB_TYPE,
        nullable=True,
        comment=(
            "Sanitised request body. Endpoint helpers strip secrets / "
            "API keys / tokens before persisting. GIN-indexed for "
            "free-text search from the Audit tab."
        ),
    )

    before_state = Column(
        _JSONB_TYPE,
        nullable=True,
        comment=(
            "Snapshot of the affected runtime state BEFORE the action. "
            "NULL when the action has no meaningful before-state."
        ),
    )

    after_state = Column(
        _JSONB_TYPE,
        nullable=True,
        comment="Snapshot AFTER the action — mirror of before_state.",
    )

    ip = Column(
        _INET_TYPE,
        nullable=True,
        comment="Source IP of the admin's request.",
    )

    user_agent = Column(
        Text,
        nullable=True,
        comment="User-Agent header for forensics.",
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        # Derived from VALID_ACTOR_TYPES rather than restated. A hard-coded
        # list here would drift from the migration's CHECK, and `create_all`
        # builds fresh installs from the model.
        CheckConstraint(
            "actor_type IN ({})".format(
                ", ".join(f"'{t}'" for t in VALID_ACTOR_TYPES)
            ),
            name="ck_system_operation_log_actor_type",
        ),
        Index(
            "ix_system_operation_log_tenant_created",
            "tenant_id", "created_at",
        ),
        Index(
            "ix_system_operation_log_actor_created",
            "actor_admin_id", "created_at",
        ),
        Index(
            "ix_system_operation_log_action_created",
            "action", "created_at",
        ),
        # GIN index on `payload` for free-text audit search.
        # `postgresql_using` is a dialect option SQLAlchemy simply ignores on
        # SQLite (the test harness), where it degrades to a plain index — and
        # `payload` is `_JSONB_TYPE`, a JSONB-with-sqlite-JSON-variant, so
        # create_all works on both. Declaring it here keeps fresh Postgres
        # installs from missing the index (they build from create_all, not the
        # migration chain).
        Index(
            "ix_system_operation_log_payload_gin",
            "payload",
            postgresql_using="gin",
        ),
        {
            "comment": (
                "Audit log for operator actions taken from the System "
                "Operations Console."
            ),
        },
    )

    def __repr__(self) -> str:
        return (
            f"<SystemOperationLog(id={self.id!r}, "
            f"tenant_id={self.tenant_id!r}, action={self.action!r}, "
            f"actor={self.actor_admin_id!r}, "
            f"scope={self.scope_kind!r}:{self.scope_id!r})>"
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "actor_admin_id": self.actor_admin_id,
            "actor_type": self.actor_type,
            "action": self.action,
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "payload": dict(self.payload) if self.payload else None,
            "before_state": dict(self.before_state) if self.before_state else None,
            "after_state": dict(self.after_state) if self.after_state else None,
            "ip": str(self.ip) if self.ip else None,
            "user_agent": self.user_agent,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
        }
