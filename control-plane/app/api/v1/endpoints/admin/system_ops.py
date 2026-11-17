# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""System Operations Console — REST endpoints.

Every sub-section of the console (Overview, Counters, Caches, Audit,
Connections, Tasks, Sessions, Logs, Maintenance) is delivered by the
single router built here. One router, because every endpoint reads the
same `tenant_id` from the auth
chain, every action writes a row through the shared
`write_system_operation()` helper, and per-section NOTIFY plumbing
flows through `publish_system_notify()`. Splitting these into
separate files would just duplicate the dependency chain.

The overview (`GET /v1/admin/system/overview`, an at-a-glance health
grid) comes first; every other section's endpoints are on the same router.
"""

from datetime import datetime, timezone
from typing import Optional, Union

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.sync_bridge import block_on
from app.constants.tenant import TenantResourceTypes
from app.core.admin_user.dependencies import (
    require_installation_operator,
    require_permission,
    require_tenant_resource_access,
)
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.api.v1.models.admin.system_ops import (
    CounterEntry,
    CounterKind,
    CounterListResponse,
    CounterReleaseCooldownRequest,
    CounterResetRequest,
    CounterScope,
    OverviewResponse,
)
from app.database.models.system_operation_log import (
    ACTION_COOLDOWN_RELEASE,
    ACTION_COUNTER_RESET,
)
from app.services.system_ops.audit_row import write_system_operation
from app.services.system_ops.counters import list_counters
from app.services.system_ops.notify import (
    CHANNEL_COUNTERS_RESET,
    publish_system_notify,
)
from app.services.system_ops.overview import build_overview

# Route imports of the console's sections.
from app.api.v1.models.admin.system_ops import CacheFlushRequest
from app.api.v1.models.admin.system_ops_caches import ( CacheLayerStats, CacheStatsResponse, )
from app.database.models.system_operation_log import ( ACTION_CACHE_FLUSH, SCOPE_CACHE_LAYER, )
from app.services.system_ops.caches import list_cache_stats
from app.services.system_ops.notify import CHANNEL_CACHE_INVALIDATE
from datetime import datetime
from typing import List
from fastapi.responses import StreamingResponse
from app.api.v1.models.admin.system_ops import ( SystemOperationLogListResponse, )
from app.api.v1.models.admin.system_ops_audit import ( AuditFacetValue, AuditFacetsResponse, )
from app.services.admin_export import ( build_meta, export_filename, media_type_for, stream_csv, )
from app.services.system_ops.audit_list import ( AUDIT_CSV_HEADERS, facetable_fields, facets as audit_facets, list_audit, stream_csv_rows, )
from app.api.v1.models.admin.system_ops_connections import ( ConnectionListResponse, ConnectionPool, PoolRecycleRequest, )
from app.database.models.system_operation_log import ACTION_POOL_RECONNECT
from app.services.system_ops.connections import list_pools, recycle_pool
from app.api.v1.models.admin.system_ops_logs import ( LogLevelEntry, LogLevelListResponse, SetLogLevelRequest, SetLogLevelResponse, )
from app.database.models.system_operation_log import ACTION_LOG_LEVEL_CHANGE
from app.services.system_ops.logs import ( get_log_levels, set_log_level, KNOWN_LOG_MODULES, )
from app.services.system_ops.notify import CHANNEL_LOG_LEVEL
from app.api.v1.models.admin.system_ops import (
    MaintenancePatchRequest,
    MaintenanceStateResponse,
)
from app.api.v1.models.admin.system_ops_maintenance import (
    DbVacuumRequest,
    DbVacuumResponse,
    RestartRollingRequest,
    RestartRollingResponse,
)
from app.services.system_ops.maintenance import (
    InvalidVacuumTableError,
    UnknownTenantError,
    get_maintenance,
    run_db_vacuum,
    set_maintenance,
    trigger_rolling_restart,
)

# Tasks section — schedulers.
# `SchedulerActionRequest` lives in the parent `system_ops` model module;
# only the response/wire types live in `system_ops_tasks`.
from app.api.v1.models.admin.system_ops import SchedulerActionRequest
from app.api.v1.models.admin.system_ops_tasks import (
    SchedulerListResponse,
    SchedulerStatus,
)
from app.services.system_ops.schedulers import (
    UnknownSchedulerError,
    list_schedulers,
    pause_scheduler,
    resume_scheduler,
    trigger_scheduler,
)
from app.services.system_ops.notify import (
    CHANNEL_SCHEDULER_PAUSE,
    CHANNEL_SCHEDULER_RESUME,
    CHANNEL_SCHEDULER_TRIGGER,
)

# Sessions section — admin / portal / api-key session listing + revoke.
from app.api.v1.models.admin.system_ops_sessions import (
    SessionEntry,
    SessionListResponse,
    SessionRevokeRequest,
    SessionRevokeResponse,
)
from app.services.system_ops.sessions import (
    KIND_ADMIN,
    KIND_API_KEY,
    KIND_PORTAL,
    SessionNotFoundError,
    UnknownSessionKindError,
    list_admin_sessions,
    list_api_keys,
    list_portal_sessions,
    revoke_session,
)
from app.services.system_ops.notify import CHANNEL_SESSION_REVOKE
from app.database.models.system_operation_log import (
    ACTION_FORCE_LOGOUT,
    SCOPE_SESSION,
)


SCHEDULER_ACTION_WHO = (
    "A scheduler belongs to the installation, not to a tenant: the action is the "
    "system administrator's where the installation has one, and in a single-tenant "
    "installation the tenant administrator's. Requires `system:operate`."
)


def create_system_ops_router(logger: BrutorLogger) -> APIRouter:
    """Factory for the System Operations Console router.

    Mounted under `/admin/system`. Every endpoint requires the
    authenticated admin to hold `system:read` (or a stronger permission
    where appropriate — checked per-endpoint). System admins are
    allowed via `require_tenant_resource_access` by passing
    `?tenant_id=` like every other admin endpoint.
    """
    router = APIRouter(prefix="/admin/system", tags=["System Operations"])

    # =========================================================================
    # GET /admin/system/overview
    # =========================================================================

    @router.get(
        "/overview",
        response_model=OverviewResponse,
        summary="Tenant runtime health grid",
        description=(
            "Returns the System Operations Overview payload — one card per "
            "dependency (database / Redis / Qdrant / configured LLM "
            "upstreams / configured MCP servers), plus aggregate counters "
            "and the current maintenance mode. Infrastructure probes run "
            "live with a 1.5s cap; LLM and MCP card status reads from the "
            "system_alerts table to stay consistent with Mission Control. "
            "Requires the `system:read` permission."
        ),
    )
    def get_overview(
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> OverviewResponse:
        admin, tenant_id, _admin_type = admin_and_tenant

        try:
            payload = block_on(build_overview(
                db=db, tenant_id=tenant_id, logger=logger
            ))
            # Validate via the response model — catches any drift
            # between probe-builder dict shape and the wire schema
            # without paying the typing overhead in the probe path.
            return OverviewResponse(**payload)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "System overview build failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to build system overview",
            )

    # =========================================================================
    # Counters
    # =========================================================================

    @router.get(
        "/counters",
        response_model=CounterListResponse,
        summary="List rate-limit / quota counters",
        description=(
            "Returns one entry per (group, subject, kind) bucket with "
            "non-zero recent activity. `used` and `limit` are best-effort: "
            "used is aggregated from `usage_logs` over the last 60s, "
            "limit is read from the resource group's rate-limit config. "
            "`cooldown_active` is False until the gateway-core round-trip "
            "is wired; reset still works."
        ),
    )
    def list_counters_endpoint(
        request: Request,
        group_id: Optional[str] = Query(default=None),
        kind: Optional[CounterKind] = Query(default=None),
        subject_id: Optional[str] = Query(default=None),
        scope: Optional[CounterScope] = Query(
            default=None,
            description=(
                "Optional subject-type filter. Pairs with `subject_id` "
                "so a deep link from a Proxy Logs row or PagerDuty alert "
                "can land on the matching counter row even when the "
                "subject hasn't had traffic in the 60s window (operator "
                "sees an explicit 'no rows for scope=tenant id=foo' "
                "rather than the page silently rendering every tenant)."
            ),
        ),
        only_cooling_down: bool = Query(default=False),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> CounterListResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        try:
            rows = list_counters(
                db=db,
                tenant_id=tenant_id,
                group_id=group_id,
                kind=kind,
                subject_id=subject_id,
                scope=scope,
                only_cooling_down=only_cooling_down,
            )
            return CounterListResponse(
                tenant_id=tenant_id,
                generated_at=datetime.now(timezone.utc).isoformat(),
                counters=[CounterEntry(**r) for r in rows],
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Counter list failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to list counters",
            )

    @router.post(
        "/counters/reset",
        summary="Reset a counter bucket",
        description=(
            "Publishes a NOTIFY on `brutor:counters:reset` that every "
            "gateway-core replica consumes. Writes an audit row before "
            "publishing, so the action is durable even if NOTIFY delivery "
            "fails. Requires `system:operate`."
        ),
        status_code=status.HTTP_202_ACCEPTED,
    )
    def reset_counter(
        request: Request,
        body: CounterResetRequest = Body(...),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_OPERATE)
        ),
    ):
        admin, tenant_id, admin_type = admin_and_tenant
        payload = body.model_dump(mode="json")
        try:
            write_system_operation(
                db=db,
                action=ACTION_COUNTER_RESET,
                actor=admin,
                tenant_id=tenant_id,
                scope_kind=body.scope,
                scope_id=body.id,
                payload=payload,
                request=request,
            )
            wire = publish_system_notify(
                db=db,
                channel=CHANNEL_COUNTERS_RESET,
                payload={
                    "tenant_id": tenant_id,
                    "scope": body.scope,
                    "id": body.id,
                    "limit_types": body.limit_types,
                },
            )
            return {"status": "queued", "channel": CHANNEL_COUNTERS_RESET, "payload": wire}
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Counter reset failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to reset counter",
            )

    @router.post(
        "/counters/release-cooldown",
        summary="Release a counter cooldown",
        description=(
            "Tells the proxy to clear a cooldown for a specific scope. "
            "Same audit + NOTIFY shape as reset; cooldown release uses "
            "the same channel — the proxy distinguishes by payload "
            "structure (`scope` + `id` with no `limit_types` means "
            "release cooldown). Requires `system:operate`."
        ),
        status_code=status.HTTP_202_ACCEPTED,
    )
    def release_cooldown(
        request: Request,
        body: CounterReleaseCooldownRequest = Body(...),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_OPERATE)
        ),
    ):
        admin, tenant_id, admin_type = admin_and_tenant
        payload = body.model_dump(mode="json")
        try:
            write_system_operation(
                db=db,
                action=ACTION_COOLDOWN_RELEASE,
                actor=admin,
                tenant_id=tenant_id,
                scope_kind=body.scope,
                scope_id=body.id,
                payload=payload,
                request=request,
            )
            wire = publish_system_notify(
                db=db,
                channel=CHANNEL_COUNTERS_RESET,
                payload={
                    "tenant_id": tenant_id,
                    "scope": body.scope,
                    "id": body.id,
                    "release_cooldown": True,
                },
            )
            return {"status": "queued", "channel": CHANNEL_COUNTERS_RESET, "payload": wire}
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Counter cooldown release failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to release cooldown",
            )


    # ─── caches ───

    @router.get(
        "/caches",
        response_model=CacheStatsResponse,
        summary="Per-layer cache stats",
        description=(
            "Returns one entry per cache layer. Semantic-layer hit/miss "
            "counts come from `proxy_logs.llm_cache_hit` over the last 60 "
            "minutes; the other 4 layers (policy / group / tool / response) "
            "live in the gateway-core in-process moka stores and surface "
            "zero-valued counters with a truthful `detail` string until the "
            "fan-out reply round-trip ships. `last_flushed_at` is read from "
            "the system_operation_log so the Caches and Audit tabs stay "
            "consistent. Requires the `system:read` permission."
        ),
    )
    def list_caches_endpoint(
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> CacheStatsResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        try:
            rows = block_on(list_cache_stats(db=db, tenant_id=tenant_id))
            return CacheStatsResponse(
                tenant_id=tenant_id,
                generated_at=datetime.now(timezone.utc).isoformat(),
                layers=[CacheLayerStats(**r) for r in rows],
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Cache stats list failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to list cache stats",
            )

    @router.post(
        "/caches/flush",
        summary="Flush a cache layer",
        description=(
            "Publishes a NOTIFY on `brutor:cache:invalidate` that every "
            "gateway-core replica's SystemNotifyListener dispatches to the "
            "matching in-memory cache. Writes an audit row before publishing "
            "so the action is durable even if NOTIFY delivery fails. "
            "Requires `system:operate`."
        ),
        status_code=status.HTTP_202_ACCEPTED,
    )
    def flush_cache(
        request: Request,
        body: CacheFlushRequest = Body(...),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_OPERATE)
        ),
    ):
        admin, tenant_id, admin_type = admin_and_tenant
        payload = body.model_dump(mode="json")
        try:
            # The audit row records the layer as the `scope_id` (and
            # SCOPE_CACHE_LAYER as the kind) so the Caches page can
            # query "last flush per layer" without re-parsing payload
            # JSON — see services/system_ops/caches.py::_last_flush_per_layer.
            write_system_operation(
                db=db,
                action=ACTION_CACHE_FLUSH,
                actor=admin,
                tenant_id=tenant_id,
                scope_kind=SCOPE_CACHE_LAYER,
                scope_id=body.layer,
                payload=payload,
                request=request,
            )
            wire = publish_system_notify(
                db=db,
                channel=CHANNEL_CACHE_INVALIDATE,
                payload={
                    "tenant_id": tenant_id,
                    "layer": body.layer,
                    "scope": body.scope,
                    "id": body.id,
                },
            )
            return {
                "status": "queued",
                "channel": CHANNEL_CACHE_INVALIDATE,
                "payload": wire,
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Cache flush failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "layer": body.layer,
                    "scope": body.scope,
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to flush cache",
            )


    # ─── audit ───

    def _parse_iso(value: Optional[str]) -> Optional[datetime]:
        """Coerce an ISO-8601 query string to an aware datetime.

        Accepts the Z suffix shorthand that JS Date.toISOString()
        emits (the audit UI sends those directly). Returns None on
        empty / unparsable input — the caller treats absence as
        "no constraint" rather than 400-ing on a clipboard accident.
        """
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None

    @router.get(
        "/audit",
        response_model=SystemOperationLogListResponse,
        summary="List audit rows from the System Console",
        description=(
            "Faceted search across `system_operation_log`. Every filter is "
            "AND-across, multi-value OR-within. `q` searches the JSONB "
            "payload + before/after state + scope_id via ILIKE. Requires "
            "`system:read`."
        ),
    )
    def list_audit_endpoint(
        request: Request,
        actions: List[str] = Query(default_factory=list),
        scope_kinds: List[str] = Query(default_factory=list),
        actor_admin_ids: List[str] = Query(default_factory=list),
        actor_types: List[str] = Query(default_factory=list),
        start: Optional[str] = Query(default=None),
        end: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> SystemOperationLogListResponse:
        admin, tenant_id, _ = admin_and_tenant
        try:
            rows, total = list_audit(
                db=db,
                tenant_id=tenant_id,
                actions=actions or None,
                scope_kinds=scope_kinds or None,
                actor_admin_ids=actor_admin_ids or None,
                actor_types=actor_types or None,
                start=_parse_iso(start),
                end=_parse_iso(end),
                q=q,
                limit=limit,
                offset=offset,
                sort_order=sort_order,
            )
            return SystemOperationLogListResponse(items=rows, total=total)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Audit list failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to list audit rows",
            )

    @router.get(
        "/audit/facets",
        response_model=AuditFacetsResponse,
        summary="Distinct values per facetable column",
        description=(
            "Returns top-N (value, count) for every facetable audit column "
            "(action / scope_kind / actor_admin_id / actor_type), scoped to "
            "the same filter envelope the list endpoint sees. Shared scope "
            "is the contract that keeps the FacetSidebar's counts in lock-"
            "step with the table. Requires `system:read`."
        ),
    )
    def list_audit_facets_endpoint(
        request: Request,
        actions: List[str] = Query(default_factory=list),
        scope_kinds: List[str] = Query(default_factory=list),
        actor_admin_ids: List[str] = Query(default_factory=list),
        actor_types: List[str] = Query(default_factory=list),
        start: Optional[str] = Query(default=None),
        end: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        per_field_limit: int = Query(default=50, ge=1, le=500),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> AuditFacetsResponse:
        admin, tenant_id, _ = admin_and_tenant
        try:
            raw = audit_facets(
                db=db,
                tenant_id=tenant_id,
                actions=actions or None,
                scope_kinds=scope_kinds or None,
                actor_admin_ids=actor_admin_ids or None,
                actor_types=actor_types or None,
                start=_parse_iso(start),
                end=_parse_iso(end),
                q=q,
                per_field_limit=per_field_limit,
            )
            return AuditFacetsResponse(
                facets={
                    field: [AuditFacetValue(**v) for v in values]
                    for field, values in raw.items()
                },
                labels=facetable_fields(),
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Audit facets failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to compute audit facets",
            )

    @router.get(
        "/audit/export",
        summary="Stream filtered audit rows as CSV",
        description=(
            "Streams audit rows matching the same envelope as `/audit`. "
            "Auditors with wide windows can hit hundreds of thousands of "
            "rows; the response is generator-streamed so worker memory "
            "stays flat. Schema-versioned meta-comment header on top of "
            "the CSV for downstream tooling. Requires `system:read`."
        ),
    )
    def export_audit_csv_endpoint(
        request: Request,
        actions: List[str] = Query(default_factory=list),
        scope_kinds: List[str] = Query(default_factory=list),
        actor_admin_ids: List[str] = Query(default_factory=list),
        actor_types: List[str] = Query(default_factory=list),
        start: Optional[str] = Query(default=None),
        end: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ):
        admin, tenant_id, _ = admin_and_tenant
        try:
            start_dt = _parse_iso(start)
            end_dt = _parse_iso(end)

            row_iter = stream_csv_rows(
                db=db,
                tenant_id=tenant_id,
                actions=actions or None,
                scope_kinds=scope_kinds or None,
                actor_admin_ids=actor_admin_ids or None,
                actor_types=actor_types or None,
                start=start_dt,
                end=end_dt,
                q=q,
            )

            # The audit-row counter for the export self-describing
            # meta comment is filled by the streamer as rows fly past;
            # surface 'unknown' here because we don't pre-count to
            # keep the response start-time low.
            filters_for_meta = {
                "tenant_id": tenant_id,
                "actions": actions,
                "scope_kinds": scope_kinds,
                "actor_admin_ids": actor_admin_ids,
                "actor_types": actor_types,
                "start": start,
                "end": end,
                "q": q,
            }
            meta = build_meta(
                resource="system_audit",
                format_type="csv",
                exported_by=str(getattr(admin, "id", "unknown")),
                filters=filters_for_meta,
                row_count=-1,
            )

            filename = export_filename(
                "system-audit", "csv",
                start_time=start_dt, end_time=end_dt,
            )
            headers = {"Content-Disposition": f"attachment; filename={filename}"}

            generator = stream_csv(
                records=row_iter,
                headers=AUDIT_CSV_HEADERS,
                meta=meta,
            )
            return StreamingResponse(
                generator,
                media_type=media_type_for("csv"),
                headers=headers,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Audit CSV export failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to export audit CSV",
            )


    # ─── connections ───

    @router.get(
        "/connections",
        response_model=ConnectionListResponse,
        summary="List outbound connection pools",
        description=(
            "Returns one entry per outbound dependency the control plane "
            "knows about: Postgres (real SQLAlchemy pool stats), Redis "
            "(honest 'unknown' until a persistent client lands), Qdrant "
            "(single client per request — most-recent latency comes from "
            "`system_alerts`), and one entry per configured LLM provider "
            "with a pointer to the Rust proxy's /metrics for the actual "
            "pool numbers. Requires `system:read`."
        ),
    )
    def list_connections_endpoint(
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> ConnectionListResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        try:
            pools = list_pools(db=db, tenant_id=tenant_id, logger=logger)
            return ConnectionListResponse(
                tenant_id=tenant_id,
                generated_at=datetime.now(timezone.utc).isoformat(),
                pools=[ConnectionPool(**p) for p in pools],
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Connection list failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to list connection pools",
            )

    @router.post(
        "/connections/recycle",
        summary="Recycle an outbound connection pool",
        description=(
            "Disposes the named pool so the next checkout opens a fresh "
            "connection. Only `postgres` and `redis` are accepted — the "
            "Rust proxy's pools are out of scope for this endpoint. "
            "Audits with ACTION_POOL_RECONNECT BEFORE attempting the "
            "dispose so an operator click is durable even if dispose "
            "itself raises. Requires `system:operate`."
        ),
        status_code=status.HTTP_200_OK,
    )
    def recycle_pool_endpoint(
        request: Request,
        body: PoolRecycleRequest = Body(...),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_OPERATE)
        ),
    ):
        admin, tenant_id, _admin_type = admin_and_tenant
        payload = body.model_dump(mode="json")
        try:
            # Audit first — the operator's intent is durable even if
            # the dispose itself errors. The audit row's `payload`
            # captures kind + reason; result lands on the response.
            write_system_operation(
                db=db,
                action=ACTION_POOL_RECONNECT,
                actor=admin,
                tenant_id=tenant_id,
                scope_kind="pool",
                scope_id=body.kind,
                payload=payload,
                request=request,
            )
            result = recycle_pool(db=db, kind=body.kind, logger=logger)
            return {
                "kind": body.kind,
                "status": result.get("status"),
                "detail": result.get("detail"),
            }
        except HTTPException:
            raise
        except ValueError as exc:
            # Unsupported kind — Pydantic should have caught it, but
            # belt-and-braces in case the literal type drifts.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            )
        except Exception as exc:
            logger.error(
                "Pool recycle failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "kind": body.kind,
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to recycle pool",
            )


    # ─── tasks ───

    @router.get(
        "/tasks",
        response_model=SchedulerListResponse,
        summary="List background scheduler status",
        description=(
            "Returns one entry per KNOWN_SCHEDULERS constant. Rows that "
            "have never been paused appear with default state (paused=False, "
            "state='unknown' / 'running' based on recent audit activity). "
            "Requires `system:read`."
        ),
    )
    def list_tasks_endpoint(
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> SchedulerListResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        try:
            rows = list_schedulers(db=db, tenant_id=tenant_id)
            return SchedulerListResponse(
                items=[SchedulerStatus(**r) for r in rows]
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Scheduler list failed",
                context={
                    "tenant_id": tenant_id,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to list schedulers",
            )


    def _handle_scheduler_action(action_fn, *, db, request, body, admin, tenant_id, action_label):
        """Shared error-handling wrapper for trigger/pause/resume. Keeps
        the three endpoints identical in shape so the audit / NOTIFY
        contract drift is impossible."""
        try:
            return action_fn(
                db,
                scheduler_name=body.scheduler_name,
                actor=admin,
                tenant_id=tenant_id,
                reason=body.reason,
                request=request,
            )
        except UnknownSchedulerError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            )


    @router.post(
        "/tasks/trigger",
        summary="Fire one out-of-band tick",
        description=(
            "Publishes NOTIFY brutor:scheduler:trigger. The receiving "
            "control-plane replica skips its next sleep and runs the "
            "scheduler's tick handler immediately. No DB mutation on "
            "scheduler_runtime_state. " + SCHEDULER_ACTION_WHO
        ),
        status_code=status.HTTP_202_ACCEPTED,
    )
    def trigger_task(
        request: Request,
        body: SchedulerActionRequest = Body(...),
        admin: AdminPrincipal = Depends(require_installation_operator),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_OPERATE)
        ),
    ):
        tenant_id = getattr(admin, "tenant_id", None)
        result = _handle_scheduler_action(
            trigger_scheduler,
            db=db,
            request=request,
            body=body,
            admin=admin,
            tenant_id=tenant_id,
            action_label="trigger",
        )
        return {"status": "queued", **result}


    @router.post(
        "/tasks/pause",
        summary="Pause a scheduler",
        description=(
            "Upserts scheduler_runtime_state.paused = TRUE and publishes "
            "NOTIFY brutor:scheduler:pause. Idempotent — pausing an "
            "already-paused scheduler refreshes the paused_at + paused_by "
            "fields and re-fires the notify. " + SCHEDULER_ACTION_WHO
        ),
        status_code=status.HTTP_202_ACCEPTED,
    )
    def pause_task(
        request: Request,
        body: SchedulerActionRequest = Body(...),
        admin: AdminPrincipal = Depends(require_installation_operator),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_OPERATE)
        ),
    ):
        tenant_id = getattr(admin, "tenant_id", None)
        result = _handle_scheduler_action(
            pause_scheduler,
            db=db,
            request=request,
            body=body,
            admin=admin,
            tenant_id=tenant_id,
            action_label="pause",
        )
        return {"status": "queued", **result}


    @router.post(
        "/tasks/resume",
        summary="Resume a scheduler",
        description=(
            "Upserts scheduler_runtime_state.paused = FALSE and publishes "
            "NOTIFY brutor:scheduler:resume. Symmetric to pause. " + SCHEDULER_ACTION_WHO
        ),
        status_code=status.HTTP_202_ACCEPTED,
    )
    def resume_task(
        request: Request,
        body: SchedulerActionRequest = Body(...),
        admin: AdminPrincipal = Depends(require_installation_operator),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_OPERATE)
        ),
    ):
        tenant_id = getattr(admin, "tenant_id", None)
        result = _handle_scheduler_action(
            resume_scheduler,
            db=db,
            request=request,
            body=body,
            admin=admin,
            tenant_id=tenant_id,
            action_label="resume",
        )
        return {"status": "queued", **result}


    # ─── sessions ───



    # `create_system_ops_router` factory in this file — append the bodies
    # below before the trailing `return router` line so they share the
    # router instance + logger closure with the existing overview /
    # counters endpoints. The decorators reference `router` which is the
    # local variable in that factory function.

    @router.get(
        "/sessions",
        response_model=SessionListResponse,
        summary="List live admin / portal / API key sessions",
        description=(
            "Returns the rendered Sessions table for the chosen kind. "
            "`admin` reads tenant_admin_sessions; `portal` aggregates the "
            "last 24h of proxy_log activity (there is no portal session "
            "table); `api_key` lists keys that pass auth right now. "
            "Requires `system:read`."
        ),
    )
    def list_sessions(
        request: Request,
        kind: str = Query(..., pattern="^(admin|portal|api_key)$"),
        limit: int = Query(default=50, ge=1, le=500),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> SessionListResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        try:
            if kind == KIND_ADMIN:
                rows = list_admin_sessions(db, tenant_id, limit=limit)
            elif kind == KIND_PORTAL:
                rows = list_portal_sessions(db, tenant_id, limit=limit)
            else:
                rows = list_api_keys(db, tenant_id, limit=limit)
            return SessionListResponse(
                tenant_id=tenant_id,
                kind=kind,
                generated_at=datetime.now(timezone.utc).isoformat(),
                items=[SessionEntry(**r) for r in rows],
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Session list failed",
                context={
                    "tenant_id": tenant_id,
                    "kind": kind,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to list sessions",
            )


    @router.post(
        "/sessions/revoke",
        response_model=SessionRevokeResponse,
        summary="Revoke a live session / API key",
        description=(
            "Tombstones the session (admin → is_active=False; api_key → "
            "revoked=True; portal has no DB row) and publishes "
            "`brutor:session:revoke` NOTIFY so every replica drops the "
            "credential from its in-memory auth cache. Writes an audit "
            "row with action=force_logout. Requires `system:operate`."
        ),
        status_code=status.HTTP_202_ACCEPTED,
    )
    def revoke_session_endpoint(
        request: Request,
        body: SessionRevokeRequest = Body(...),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_OPERATE)
        ),
    ) -> SessionRevokeResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        try:
            result = revoke_session(
                db=db,
                tenant_id=tenant_id,
                kind=body.kind,
                id=body.id,
                revoked_by=getattr(admin, "id", None),
                reason=body.reason,
            )
            # Audit AFTER the service call so the row reflects what
            # actually happened (force_logout never fires for a not-found id).
            write_system_operation(
                db=db,
                action=ACTION_FORCE_LOGOUT,
                actor=admin,
                tenant_id=tenant_id,
                scope_kind=SCOPE_SESSION,
                scope_id=body.id,
                payload={
                    "kind": body.kind,
                    "reason": body.reason,
                },
                request=request,
            )
            return SessionRevokeResponse(
                status="queued",
                kind=body.kind,
                id=body.id,
                tenant_id=tenant_id,
                channel=result["channel"],
                payload=result["payload"],
            )
        except SessionNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Session not found",
            )
        except UnknownSessionKindError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Session revoke failed",
                context={
                    "tenant_id": tenant_id,
                    "kind": body.kind,
                    "id": body.id,
                    "admin_id": getattr(admin, "id", None),
                    "error": str(exc),
                },
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to revoke session",
            )


    # ─── logs ───

    @router.get(
        "/logs/levels",
        response_model=LogLevelListResponse,
        summary="List runtime log-level overrides + curated modules",
        description=(
            "Returns one entry per curated logger (see KNOWN_LOG_MODULES) plus "
            "any ad-hoc overrides not in the curated list. `level` is the LIVE "
            "effective level; `override_level` is the persisted row value. They "
            "should agree; the UI surfaces drift so an operator can spot a "
            "manual DB edit or boot-replay glitch immediately. Requires "
            "`system:read`."
        ),
    )
    def list_log_levels(
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> LogLevelListResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        try:
            items = get_log_levels(db=db)
            return LogLevelListResponse(items=[LogLevelEntry(**r) for r in items])
        except Exception as exc:
            logger.error(
                "Log level list failed",
                context={"tenant_id": tenant_id, "admin_id": getattr(admin, "id", None), "error": str(exc)},
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to list log levels",
            )

    @router.post(
        "/logs/levels",
        response_model=SetLogLevelResponse,
        summary="Set a module's runtime log level",
        description=(
            "Mutates `logging.getLogger(module).level` on the receiving "
            "replica and persists the override. When `scope='all'`, also "
            "publishes a NOTIFY on `brutor:log_level` so peer replicas pick "
            "up the change without a restart. Writes an audit row before "
            "publishing. Requires `system:operate`."
        ),
        status_code=status.HTTP_200_OK,
    )
    def set_log_level_endpoint(
        request: Request,
        body: SetLogLevelRequest = Body(...),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_OPERATE)
        ),
    ) -> SetLogLevelResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        payload = body.model_dump(mode="json")
        try:
            write_system_operation(
                db=db,
                action=ACTION_LOG_LEVEL_CHANGE,
                actor=admin,
                tenant_id=tenant_id,
                scope_kind="module",
                scope_id=body.module,
                payload=payload,
                request=request,
            )
            result = set_log_level(
                db=db,
                module=body.module,
                level=body.level,
                actor_id=getattr(admin, "id", None),
            )
            if body.scope == "all":
                publish_system_notify(
                    db=db,
                    channel=CHANNEL_LOG_LEVEL,
                    payload={
                        "service": "gateway-control",
                        "module": body.module,
                        "level": result["level"],
                        "scope": "all",
                    },
                )
            return SetLogLevelResponse(
                module=result["module"],
                level=result["level"],
                effective_level_int=result["effective_level_int"],
                applied_at=result["applied_at"],
                fanout_channel=CHANNEL_LOG_LEVEL,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Log level mutation failed",
                context={"tenant_id": tenant_id, "admin_id": getattr(admin, "id", None), "module": body.module, "error": str(exc)},
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to set log level",
            )


    # ─── maintenance ───


    # =========================================================================
    # Maintenance
    # =========================================================================

    @router.get(
        "/maintenance",
        response_model=MaintenanceStateResponse,
        summary="Current tenant maintenance state",
        description=(
            "Returns the per-tenant maintenance mode and audit metadata. "
            "Requires `system:read`. Example via Brutor gateway:\n\n"
            "    curl https://gateway.example.com/v1/admin/system/maintenance "
            "-H 'Authorization: Bearer <admin-jwt>' -H 'X-Tenant-ID: tenant-acme'"
        ),
    )
    def get_maintenance_endpoint(
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_READ)
        ),
    ) -> MaintenanceStateResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        try:
            payload = get_maintenance(db=db, tenant_id=tenant_id)
            return MaintenanceStateResponse(**payload)
        except UnknownTenantError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Maintenance read failed",
                context={"tenant_id": tenant_id, "error": str(exc)},
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to read maintenance state",
            )


    @router.patch(
        "/maintenance",
        response_model=MaintenanceStateResponse,
        summary="Set tenant maintenance mode",
        description=(
            "Switches mode between off / read_only / drain / paused. Writes "
            "an audit row and publishes `brutor:maintenance` NOTIFY so every "
            "replica picks up the new mode within milliseconds. Requires "
            "`system:maintenance:toggle`."
        ),
    )
    def patch_maintenance_endpoint(
        request: Request,
        body: MaintenancePatchRequest = Body(...),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_MAINTENANCE_TOGGLE)
        ),
    ) -> MaintenanceStateResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        # Caller may override tenant_id (sysadmin acting on a specific
        # tenant); when null, use the auth-context tenant. Same pattern
        # other admin endpoints follow.
        target_tenant = body.tenant_id or tenant_id
        try:
            payload = set_maintenance(
                db=db,
                tenant_id=target_tenant,
                mode=body.mode,
                actor=admin,
                drain_grace_secs=body.drain_grace_secs,
                reason=body.reason,
                request=request,
            )
            return MaintenanceStateResponse(**payload)
        except UnknownTenantError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Maintenance toggle failed",
                context={"tenant_id": target_tenant, "error": str(exc)},
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to toggle maintenance mode",
            )


    @router.post(
        "/maintenance/restart-rolling",
        response_model=RestartRollingResponse,
        summary="Record a rolling restart",
        description=(
            "Writes an audit row recording the operator's intent. The actual "
            "rolling restart is operator-driven (kubectl rollout / ECS new "
            "deployment). Requires `system:maintenance:toggle`."
        ),
        status_code=status.HTTP_202_ACCEPTED,
    )
    def restart_rolling_endpoint(
        request: Request,
        body: RestartRollingRequest = Body(default_factory=RestartRollingRequest),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_MAINTENANCE_TOGGLE)
        ),
    ) -> RestartRollingResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        target_tenant = body.tenant_id or tenant_id
        try:
            result = trigger_rolling_restart(
                db=db,
                actor=admin,
                tenant_id=target_tenant,
                reason=body.reason,
                request=request,
            )
            return RestartRollingResponse(**result)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Rolling restart record failed",
                context={"tenant_id": target_tenant, "error": str(exc)},
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to record rolling restart",
            )


    @router.post(
        "/maintenance/db-vacuum",
        response_model=DbVacuumResponse,
        summary="VACUUM ANALYZE a whitelisted table",
        description=(
            "Executes VACUUM ANALYZE on the supplied table. Only whitelisted "
            "tables are accepted — see ALLOWED_VACUUM_TABLES. Requires "
            "`system:maintenance:toggle`."
        ),
    )
    def db_vacuum_endpoint(
        request: Request,
        body: DbVacuumRequest = Body(...),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.SYSTEM_OPS)
        ),
        db: Session = Depends(get_db),
        _perm: AdminPrincipal = Depends(
            require_permission(Permission.SYSTEM_MAINTENANCE_TOGGLE)
        ),
    ) -> DbVacuumResponse:
        admin, tenant_id, _admin_type = admin_and_tenant
        target_tenant = body.tenant_id or tenant_id
        try:
            result = run_db_vacuum(
                db=db,
                table=body.table,
                actor=admin,
                tenant_id=target_tenant,
                reason=body.reason,
                request=request,
            )
            return DbVacuumResponse(**result)
        except InvalidVacuumTableError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "DB vacuum failed",
                context={"tenant_id": target_tenant, "table": body.table, "error": str(exc)},
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to run VACUUM ANALYZE",
            )


    return router
