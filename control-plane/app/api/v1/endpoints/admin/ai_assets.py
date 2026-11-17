# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI Asset Registry admin API

Endpoints under `/v1/admin/assets/*`:

  GET    ""           — list assets (filters + pagination)
  GET    "/{id}"      — detail
  POST   ""           — create a manual asset (201)
  PATCH  "/{id}"      — partial governance update
  DELETE "/{id}"      — hard delete (204)
  POST   "/sync"      — auto-discover assets from config tables

Read routes require `AI_ASSET_READ`; write routes require `AI_ASSET_WRITE`.
Tenant scope follows the shared `_resolve_tenant_id` convention: tenant
admins are pinned to their own tenant; system admins must pass
`?tenant_id=`.
"""

from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.v1.models.admin.ai_asset import (
    AiAssetCreateRequest,
    AiAssetListResponse,
    AiAssetResponse,
    AiAssetUpdateRequest,
    AssetSyncResponse,
)
from app.api.v1.models.admin.factsheet import (
    FactsheetResponse,
    FactsheetSnapshotListResponse,
    FactsheetSnapshotResponse,
)
from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    require_permission,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.services.admin_export import (
    build_meta,
    export_filename,
    stream_csv,
)
from app.services.asset_registry_service import (
    AssetRegistryError,
    AssetRegistryService,
)
from app.services.factsheet_service import (
    DEFAULT_PERIOD_HOURS,
    FactsheetError,
    FactsheetService,
)


# Map the service error codes to HTTP statuses.
_ERROR_STATUS = {
    "not_found": 404,
    "duplicate": 409,
}


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id


def _http_from_service_error(e: AssetRegistryError) -> HTTPException:
    """Translate a domain error into the right HTTP status (default 400)."""
    return HTTPException(
        status_code=_ERROR_STATUS.get(e.code, 400),
        detail=e.message,
    )


def _http_from_factsheet_error(e: FactsheetError) -> HTTPException:
    """Translate a factsheet domain error into the right HTTP status."""
    return HTTPException(
        status_code=_ERROR_STATUS.get(e.code, 400),
        detail=e.message,
    )


def create_ai_assets_router(logger: BrutorLogger) -> APIRouter:
    """Build the `/admin/assets/*` router."""

    router = APIRouter(prefix="/admin/assets", tags=["AI Assets"])
    service = AssetRegistryService(logger)
    factsheet_service = FactsheetService(logger)

    # ────────────────────────────────────────────────────────────────────
    # List
    # ────────────────────────────────────────────────────────────────────

    def _with_edges(db: Session, tenant_id: str, rows):
        """Responses with the derived edge view attached: which AI
        Systems bind each component asset, and the worst tier among them.
        Derived at read time, never stored."""
        owning = service.owning_systems_map(db, tenant_id, list(rows))
        out = []
        for r in rows:
            own = owning.get(r.id, [])
            out.append(AiAssetResponse(
                **r.to_dict(),
                owning_systems=own,
                derived_risk_tier=AssetRegistryService.derived_risk_tier(own),
            ))
        return out

    @router.get(
        "",
        response_model=AiAssetListResponse,
        summary="List AI assets",
    )
    def list_assets(
        tenant_id: Optional[str] = Query(None),
        asset_kind: Optional[str] = Query(None),
        lifecycle_stage: Optional[str] = Query(None),
        risk_tier: Optional[str] = Query(None),
        status: Optional[str] = Query(None),
        q: Optional[str] = Query(None, description="Substring on display_name / ref_id"),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AI_ASSET_READ)),
    ) -> AiAssetListResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        try:
            rows, total = service.list_assets(
                db, resolved_tenant,
                asset_kind=asset_kind,
                lifecycle_stage=lifecycle_stage,
                risk_tier=risk_tier,
                status=status,
                q=q,
                limit=limit,
                offset=offset,
            )
        except AssetRegistryError as e:
            raise _http_from_service_error(e)
        return AiAssetListResponse(
            assets=_with_edges(db, resolved_tenant, rows),
            total=total,
        )

    # ────────────────────────────────────────────────────────────────────
    # Sync — declared BEFORE /{asset_id} so first-match routing doesn't
    # resolve 'sync' as an asset id.
    # ────────────────────────────────────────────────────────────────────
    @router.post(
        "/sync",
        response_model=AssetSyncResponse,
        summary="Auto-discover AI assets from configuration tables",
    )
    def sync_assets(
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.AI_ASSET_WRITE)),
    ) -> AssetSyncResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        result = service.sync_tenant(db, resolved_tenant)
        audit.log(
            action="ai_asset.sync",
            db=db,
            admin=admin,
            resource_type="ai_asset",
            resource_id=None,
            tenant_id=resolved_tenant,
            details=result,
        )
        return AssetSyncResponse(**result)

    # ────────────────────────────────────────────────────────────────────
    # Detail
    # ────────────────────────────────────────────────────────────────────
    @router.get(
        "/{asset_id}",
        response_model=AiAssetResponse,
        summary="Get an AI asset by id",
    )
    def get_asset(
        asset_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AI_ASSET_READ)),
    ) -> AiAssetResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        try:
            asset = service.get_asset(db, resolved_tenant, asset_id)
        except AssetRegistryError as e:
            raise _http_from_service_error(e)
        return _with_edges(db, resolved_tenant, [asset])[0]

    # ────────────────────────────────────────────────────────────────────
    # Create
    # ────────────────────────────────────────────────────────────────────
    @router.post(
        "",
        response_model=AiAssetResponse,
        status_code=201,
        summary="Create a manual AI asset",
    )
    def create_asset(
        body: AiAssetCreateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.AI_ASSET_WRITE)),
    ) -> AiAssetResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        try:
            asset = service.create_asset(
                db, resolved_tenant,
                asset_kind=body.asset_kind,
                ref_id=body.ref_id,
                display_name=body.display_name,
                owner=body.owner,
                lifecycle_stage=body.lifecycle_stage,
                risk_tier=body.risk_tier,
                intended_use=body.intended_use,
                gpai_classification=body.gpai_classification,
                operated_by=body.operated_by,
                enforcement_mode=body.enforcement_mode,
                tags=body.tags,
                source="manual",
            )
        except AssetRegistryError as e:
            raise _http_from_service_error(e)
        audit.log(
            action="ai_asset.create",
            db=db,
            admin=admin,
            resource_type="ai_asset",
            resource_id=asset.id,
            tenant_id=resolved_tenant,
            details={"asset_kind": asset.asset_kind, "ref_id": asset.ref_id},
        )
        return AiAssetResponse(**asset.to_dict())

    # ────────────────────────────────────────────────────────────────────
    # Update
    # ────────────────────────────────────────────────────────────────────
    @router.patch(
        "/{asset_id}",
        response_model=AiAssetResponse,
        summary="Update an AI asset",
    )
    def update_asset(
        asset_id: str,
        body: AiAssetUpdateRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.AI_ASSET_WRITE)),
    ) -> AiAssetResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        try:
            asset = service.update_asset(
                db, resolved_tenant, asset_id,
                display_name=body.display_name,
                owner=body.owner,
                lifecycle_stage=body.lifecycle_stage,
                risk_tier=body.risk_tier,
                intended_use=body.intended_use,
                gpai_classification=body.gpai_classification,
                operated_by=body.operated_by,
                enforcement_mode=body.enforcement_mode,
                tags=body.tags,
                status=body.status,
            )
        except AssetRegistryError as e:
            raise _http_from_service_error(e)
        audit.log(
            action="ai_asset.update",
            db=db,
            admin=admin,
            resource_type="ai_asset",
            resource_id=asset.id,
            tenant_id=resolved_tenant,
            details=body.model_dump(exclude_none=True),
        )
        return AiAssetResponse(**asset.to_dict())

    # ────────────────────────────────────────────────────────────────────
    # Delete
    # ────────────────────────────────────────────────────────────────────
    @router.delete(
        "/{asset_id}",
        status_code=204,
        summary="Delete an AI asset",
    )
    def delete_asset(
        asset_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.AI_ASSET_WRITE)),
    ):
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        try:
            service.delete_asset(db, resolved_tenant, asset_id)
        except AssetRegistryError as e:
            raise _http_from_service_error(e)
        audit.log(
            action="ai_asset.delete",
            db=db,
            admin=admin,
            resource_type="ai_asset",
            resource_id=asset_id,
            tenant_id=resolved_tenant,
            details={},
        )

    # ════════════════════════════════════════════════════════════════════
    # Factsheets
    #
    # The snapshot-list / single-snapshot routes carry deeper literal
    # path segments than `/{asset_id}/factsheet/snapshot`, so first-match
    # routing resolves them correctly regardless of declaration order.
    # ════════════════════════════════════════════════════════════════════

    @router.get(
        "/{asset_id}/factsheet",
        response_model=None,
        summary="Live factsheet for an AI asset (JSON or CSV)",
    )
    def get_factsheet(
        asset_id: str,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        period_hours: int = Query(
            DEFAULT_PERIOD_HOURS,
            ge=0,
            le=8760,
            description="Evidence window in hours; 0 = all-time",
        ),
        format: str = Query("json", pattern="^(json|csv)$"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.AI_ASSET_READ)),
    ):
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        try:
            dossier = factsheet_service.build_factsheet(
                db, resolved_tenant, asset_id, period_hours=period_hours
            )
        except FactsheetError as e:
            raise _http_from_factsheet_error(e)

        if format == "csv":
            headers, rows = FactsheetService.factsheet_csv_rows(dossier)
            meta = build_meta(
                resource="ai_asset_factsheet",
                format_type="csv",
                exported_by=getattr(admin, "username", str(getattr(admin, "id", ""))),
                filters={
                    "asset_id": asset_id,
                    "period_hours": period_hours,
                },
                row_count=len(rows),
                extra={"asset_id": asset_id, "tenant_id": resolved_tenant},
            )
            audit.log(
                action="ai_asset.factsheet.export",
                db=db,
                admin=admin,
                resource_type="ai_asset",
                resource_id=asset_id,
                tenant_id=resolved_tenant,
                details={"format": "csv", "period_hours": period_hours},
            )
            filename = export_filename("ai-asset-factsheet", "csv")
            return StreamingResponse(
                stream_csv(records=rows, headers=headers, meta=meta),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f"attachment; filename={filename}"
                },
            )

        return FactsheetResponse(**dossier)

    @router.post(
        "/{asset_id}/factsheet/snapshot",
        response_model=FactsheetSnapshotResponse,
        status_code=201,
        summary="Capture a point-in-time factsheet snapshot",
    )
    def capture_factsheet_snapshot(
        asset_id: str,
        tenant_id: Optional[str] = Query(None),
        period_hours: int = Query(
            DEFAULT_PERIOD_HOURS,
            ge=0,
            le=8760,
            description="Evidence window in hours; 0 = all-time",
        ),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.AI_ASSET_WRITE)),
    ) -> FactsheetSnapshotResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        try:
            snapshot = factsheet_service.capture_snapshot(
                db, resolved_tenant, asset_id, period_hours=period_hours
            )
        except FactsheetError as e:
            raise _http_from_factsheet_error(e)
        audit.log(
            action="ai_asset.factsheet.snapshot",
            db=db,
            admin=admin,
            resource_type="ai_asset",
            resource_id=asset_id,
            tenant_id=resolved_tenant,
            details={"snapshot_id": snapshot.id, "period_hours": period_hours},
        )
        return FactsheetSnapshotResponse(**snapshot.to_dict())

    @router.get(
        "/{asset_id}/factsheet/snapshots",
        response_model=FactsheetSnapshotListResponse,
        summary="List factsheet snapshots for an AI asset",
    )
    def list_factsheet_snapshots(
        asset_id: str,
        tenant_id: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AI_ASSET_READ)),
    ) -> FactsheetSnapshotListResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        try:
            rows, total = factsheet_service.list_snapshots(
                db, resolved_tenant, asset_id, limit=limit, offset=offset
            )
        except FactsheetError as e:
            raise _http_from_factsheet_error(e)
        return FactsheetSnapshotListResponse(
            snapshots=[FactsheetSnapshotResponse(**r.to_dict()) for r in rows],
            total=total,
        )

    @router.get(
        "/{asset_id}/factsheet/snapshots/{snapshot_id}",
        response_model=FactsheetSnapshotResponse,
        summary="Get a single factsheet snapshot",
    )
    def get_factsheet_snapshot(
        asset_id: str,
        snapshot_id: str,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AI_ASSET_READ)),
    ) -> FactsheetSnapshotResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        try:
            snap = factsheet_service.get_snapshot(
                db, resolved_tenant, snapshot_id
            )
        except FactsheetError as e:
            raise _http_from_factsheet_error(e)
        # Defend against a snapshot id that belongs to a different asset.
        if snap.asset_id != asset_id:
            raise HTTPException(status_code=404, detail="Snapshot not found for asset")
        return FactsheetSnapshotResponse(**snap.to_dict())

    return router
