# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
System Alerts API endpoints.

Reads the `system_alerts` table (populated by the Rust core proxy via the
`services::system_alerts::raise` helper) and exposes
list / acknowledge / resolve operations to the admin console.

Distinct from `usage_alerts` (quota-scoped): this table captures
downstream service failures (OpenAI 5xx, OAuth IdP unreachable, Redis
outage, Qdrant down), resource-health transitions (MCP server / LLM
model flipped to unhealthy), infrastructure-threshold crossings, and
internal code errors.

Dedup is enforced at the DB level via a partial unique index on
`dedup_key WHERE status IN ('active','acknowledged')`, so the re-raise
storm during an outage produces at most one row per failure signature.
Mission Control exposes the `occurrence_count` to show how intense the
storm is without flooding the UI.

Auto-resolver (Rust `services::system_alerts_resolver`) closes
`resource_health` alerts 5 min after the underlying resource has been
healthy and `downstream_*` alerts after 15 min of silence. Acknowledged
alerts are never auto-resolved — operator ownership is respected.
"""

from datetime import datetime, timezone
from typing import Optional, Union, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status, Request, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.database.models.system_alert import SystemAlert, SEVERITIES, CATEGORIES
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import require_permission, get_current_admin
from app.composition.principals import PRINCIPALS
from app.core.logging_system import BrutorLogger


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SystemAlertResponse(BaseModel):
    """Single alert row as returned by the API."""
    id: str
    tenant_id: Optional[str] = None
    severity: str
    category: str
    source: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    resource_name: Optional[str] = None
    error_code: Optional[str] = None
    title: str
    message: str
    context: Optional[Dict[str, Any]] = None
    status: str
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    occurrence_count: int
    acknowledged_at: Optional[str] = None
    acknowledged_by: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution_note: Optional[str] = None
    dedup_key: str


class SystemAlertsSummary(BaseModel):
    """Counts used to render the dashboard badges without rescanning rows."""
    total: int = 0
    active: int = 0
    acknowledged: int = 0
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    by_category: Dict[str, int] = Field(default_factory=dict)


class SystemAlertsListResponse(BaseModel):
    alerts: List[SystemAlertResponse]
    summary: SystemAlertsSummary
    timestamp: str
    tenant_scope: str  # "self" or "all"


class AcknowledgeResponse(BaseModel):
    success: bool
    alert_id: str
    status: str
    acknowledged_at: str
    acknowledged_by: str


class SystemAlertResolveRequest(BaseModel):
    resolution_note: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Optional note explaining how the alert was resolved. Shown in history view.",
    )


class ResolveResponse(BaseModel):
    success: bool
    alert_id: str
    status: str
    resolved_at: str
    resolved_by: str
    resolution_note: Optional[str] = None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_system_alerts_router(logger: BrutorLogger) -> APIRouter:
    """Create the /admin/system-alerts router.

    Read endpoints require `PROXY_STATS_READ` (same permission model as
    `usage_alerts` and metrics). Mutating endpoints (acknowledge, resolve)
    require `RESOURCE_GROUP_UPDATE` — the closest existing "operator
    acts on alerts" permission; system admins inherit it.
    """
    router = APIRouter(prefix="/admin/system-alerts", tags=["System Alerts"])

    # =====================================================================
    # List
    # =====================================================================

    @router.get(
        "",
        response_model=SystemAlertsListResponse,
        summary="List system alerts for the current tenant",
        description=(
            "Returns active/acknowledged/resolved alerts for the caller's tenant "
            "(or all tenants when `tenant_scope=all` and the caller is a system admin). "
            "Filters stack: a request with no filters returns the same ordering that "
            "Mission Control displays (severity desc, then last_seen_at desc)."
        ),
    )
    def list_system_alerts(
        request: Request,
        alert_status: Optional[str] = Query(
            None,
            alias="status",
            pattern=r"^(active|acknowledged|resolved)$",
            description="Filter by lifecycle state",
        ),
        severity: Optional[str] = Query(
            None,
            pattern=r"^(critical|high|medium|low)$",
        ),
        category: Optional[str] = Query(
            None,
            description="Filter by category. Valid values: "
            + ", ".join(CATEGORIES),
        ),
        tenant_scope: str = Query(
            "self",
            pattern=r"^(self|all)$",
            description=(
                "'self' returns alerts for the caller's tenant (or platform-wide "
                "rows where tenant_id IS NULL). 'all' returns every row across "
                "tenants — system admins only."
            ),
        ),
        limit: int = Query(100, ge=1, le=500),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.PROXY_STATS_READ)
        ),
    ) -> SystemAlertsListResponse:
        admin_user, admin_type_val = admin_and_type

        # Validate category at runtime (Query regex would bloat the OpenAPI
        # spec and we want a clean error message instead of 422).
        if category is not None and category not in CATEGORIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid category '{category}'. Must be one of: "
                    f"{', '.join(CATEGORIES)}"
                ),
            )

        # Scope enforcement: only system admins may query all tenants.
        effective_scope = tenant_scope
        if tenant_scope == "all" and admin_type_val != "system":
            # Silently narrow rather than 403; tenant admins don't even
            # have the list of other tenants, so returning their own
            # data is the most useful thing.
            effective_scope = "self"

        query = db.query(SystemAlert)
        if effective_scope == "self":
            # Tenant admin (or system admin opting into their own scope):
            # own tenant's alerts AND platform-wide rows (tenant_id IS NULL).
            query = query.filter(
                # A system administrator has no tenant of its own:
                # its own scope is the platform-wide rows.
                (SystemAlert.tenant_id == getattr(admin_user, "tenant_id", None))
                | (SystemAlert.tenant_id.is_(None))
            )

        if alert_status:
            query = query.filter(SystemAlert.status == alert_status)
        if severity:
            query = query.filter(SystemAlert.severity == severity)
        if category:
            query = query.filter(SystemAlert.category == category)

        # Mission Control ordering: newest last_seen first so the UI
        # surfaces the active storm at the top. Severity tie-breaker
        # keeps a single critical row visible even if it's older.
        severity_order = {
            "critical": 4,
            "high": 3,
            "medium": 2,
            "low": 1,
        }
        rows = query.order_by(
            SystemAlert.last_seen_at.desc(),
        ).limit(limit).all()
        # Stable secondary sort in Python (preserve DB-level ordering for
        # equal severity, bump criticals to top within a same-minute burst).
        rows.sort(
            key=lambda r: (
                0 if r.status == "active" else (1 if r.status == "acknowledged" else 2),
                -severity_order.get(r.severity, 0),
            )
        )

        summary = SystemAlertsSummary()
        summary.total = len(rows)
        for r in rows:
            if r.status == "active":
                summary.active += 1
            elif r.status == "acknowledged":
                summary.acknowledged += 1
            if r.severity in SEVERITIES:
                setattr(summary, r.severity, getattr(summary, r.severity) + 1)
            summary.by_category[r.category] = summary.by_category.get(r.category, 0) + 1

        return SystemAlertsListResponse(
            alerts=[SystemAlertResponse(**r.to_dict()) for r in rows],
            summary=summary,
            timestamp=datetime.now(timezone.utc).isoformat(),
            tenant_scope=effective_scope,
        )

    # =====================================================================
    # Acknowledge
    # =====================================================================

    @router.post(
        "/{alert_id}/acknowledge",
        response_model=AcknowledgeResponse,
        summary="Acknowledge a system alert",
        description=(
            "Marks the alert as acknowledged. Acknowledged alerts are never "
            "auto-resolved — the operator owns them until explicit resolution."
        ),
    )
    def acknowledge_alert(
        alert_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.RESOURCE_GROUP_UPDATE)
        ),
    ) -> AcknowledgeResponse:
        admin_user, _admin_type_val = admin_and_type

        alert = _find_alert_with_scope(db, alert_id, admin_user)
        if alert is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"System alert '{alert_id}' not found",
            )

        if alert.status == "resolved":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Alert is already resolved and cannot be acknowledged",
            )
        # If already acknowledged, the op is idempotent — just return the
        # existing state. Avoids a spurious 409 when two operators click
        # the button near-simultaneously.
        if alert.status == "acknowledged":
            return AcknowledgeResponse(
                success=True,
                alert_id=alert.id,
                status=alert.status,
                acknowledged_at=alert.acknowledged_at.isoformat()
                if alert.acknowledged_at
                else datetime.now(timezone.utc).isoformat(),
                acknowledged_by=alert.acknowledged_by or admin_user.username,
            )

        alert.status = "acknowledged"
        alert.acknowledged_at = datetime.now(timezone.utc)
        alert.acknowledged_by = admin_user.username
        db.flush()

        logger.info(
            "System alert acknowledged",
            context={
                "alert_id": alert_id,
                "category": alert.category,
                "source": alert.source,
                "severity": alert.severity,
                "acknowledged_by": admin_user.username,
            },
        )

        return AcknowledgeResponse(
            success=True,
            alert_id=alert.id,
            status=alert.status,
            acknowledged_at=alert.acknowledged_at.isoformat(),
            acknowledged_by=alert.acknowledged_by,
        )

    # =====================================================================
    # Resolve
    # =====================================================================

    @router.post(
        "/{alert_id}/resolve",
        response_model=ResolveResponse,
        summary="Resolve a system alert",
        description=(
            "Marks the alert as resolved with an optional note. After this, "
            "the dedup index no longer matches the row, so a new occurrence "
            "of the same failure creates a fresh active alert."
        ),
    )
    def resolve_alert(
        alert_id: str,
        body: SystemAlertResolveRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.RESOURCE_GROUP_UPDATE)
        ),
    ) -> ResolveResponse:
        admin_user, _admin_type_val = admin_and_type

        alert = _find_alert_with_scope(db, alert_id, admin_user)
        if alert is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"System alert '{alert_id}' not found",
            )

        if alert.status == "resolved":
            # Idempotent — return existing state.
            return ResolveResponse(
                success=True,
                alert_id=alert.id,
                status=alert.status,
                resolved_at=alert.resolved_at.isoformat()
                if alert.resolved_at
                else datetime.now(timezone.utc).isoformat(),
                resolved_by=alert.resolved_by or admin_user.username,
                resolution_note=alert.resolution_note,
            )

        alert.status = "resolved"
        alert.resolved_at = datetime.now(timezone.utc)
        alert.resolved_by = admin_user.username
        alert.resolution_note = body.resolution_note
        db.flush()

        logger.info(
            "System alert resolved",
            context={
                "alert_id": alert_id,
                "category": alert.category,
                "source": alert.source,
                "severity": alert.severity,
                "resolved_by": admin_user.username,
                "had_note": body.resolution_note is not None,
            },
        )

        return ResolveResponse(
            success=True,
            alert_id=alert.id,
            status=alert.status,
            resolved_at=alert.resolved_at.isoformat(),
            resolved_by=alert.resolved_by,
            resolution_note=alert.resolution_note,
        )

    return router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_alert_with_scope(
    db: Session,
    alert_id: str,
    admin_user: AdminPrincipal,
) -> Optional[SystemAlert]:
    """Fetch an alert, enforcing tenant isolation.

    Tenant admins can touch only alerts in their own tenant plus platform-
    wide rows (tenant_id IS NULL). System admins see everything.
    """
    query = db.query(SystemAlert).filter(SystemAlert.id == alert_id)
    # Everything is the platform-wide principal's only; anyone else — and an
    # administrator object no registered principal owns — gets its own
    # tenant's rows and the platform-wide ones.
    owner = PRINCIPALS.owner_of(admin_user)
    if owner is None or not owner.platform_wide:
        query = query.filter(
            (SystemAlert.tenant_id == getattr(admin_user, "tenant_id", None))
            | (SystemAlert.tenant_id.is_(None))
        )
    return query.first()
