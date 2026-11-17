# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Admin endpoints for scheduled-export destination CRUD + manual trigger.

These endpoints are the operator's only path to wire up Splunk / S3 /
Datadog / S3-compatible-storage as a SIEM sink for proxy_logs. Every
endpoint is multi-tenant-scoped (per the `require_tenant_resource_access`
dep) and audited via `UnifiedAdminAuditLogger` — the same wrapper that
covers IdP / OAuth-app-client / model-CRUD endpoints.

# Endpoints

  GET    /v1/admin/audit-export-destinations               — list
  POST   /v1/admin/audit-export-destinations               — create
  GET    /v1/admin/audit-export-destinations/{id}          — read
  PATCH  /v1/admin/audit-export-destinations/{id}          — update
  DELETE /v1/admin/audit-export-destinations/{id}          — delete
  POST   /v1/admin/audit-export-destinations/{id}/run-now  — fire now

# Permissions

Listing and reading a destination ask for `proxy:logs:read`; creating,
updating, deleting and running one ask for `proxy:management`. The
`Tenant Auditor` role holds the first and not the second: auditors see
where the proxy log is delivered, they don't configure it.

# Secret handling

A secret (`secret_access_key`, `bearer_token`, `token`) arrives in a
request body and is encrypted before it is stored. No response carries it
or its ciphertext: `config.has_<name>` says whether one is stored. The
export service reads the ciphertext from `dest.config` and decrypts it
when it runs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.v1.models.admin.audit_export_destination import (
    AuditExportDestinationListResponse,
    AuditExportDestinationResponse,
    CreateAuditExportDestinationRequest,
    RunResultResponse,
    UpdateAuditExportDestinationRequest,
    validate_destination_config,
)
from app.constants.tenant import TenantResourceTypes
from app.core.admin_user.dependencies import (
    get_audit_logger,
    require_permission,
    require_tenant_resource_access,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.audit_export_destination import (
    AuditExportDestination,
    HEADER_NAMES_KEY,
    SECRET_KEYS,
)
from app.services.audit_export import AuditExportService
from app.services.oauth.encryption_service import EncryptionService
from app.middleware.trusted_proxies import client_ip


def _config_to_store(
    destination_type: str,
    config: dict,
    encryption_service: EncryptionService,
    stored: Optional[dict] = None,
) -> dict:
    """The `config` to store for a validated request: every secret the
    request carries encrypted under its stored key, and none in clear.

    `stored` is the destination's current stored configuration (an
    update). A secret the request does not name is kept from it; one the
    request sends empty is not stored.
    """
    secrets = SECRET_KEYS[destination_type]
    out = {key: value for key, value in config.items() if key not in secrets}
    for name, stored_key in secrets.items():
        if name not in config:
            if stored and stored.get(stored_key):
                out[stored_key] = stored[stored_key]
                if name == "headers" and stored.get(HEADER_NAMES_KEY):
                    out[HEADER_NAMES_KEY] = stored[HEADER_NAMES_KEY]
        elif config[name]:
            value = config[name]
            if name == "headers":
                # The object is one secret; its names are kept to be shown.
                out[HEADER_NAMES_KEY] = sorted(value)
                value = json.dumps(value, sort_keys=True)
            out[stored_key] = encryption_service.encrypt(value)
    return out


def _validated(destination_type: str, config: dict, stored: dict) -> dict:
    stored_secrets = frozenset(
        name for name, stored_key in SECRET_KEYS[destination_type].items() if stored.get(stored_key)
    )
    try:
        return validate_destination_config(destination_type, config, stored_secrets=stored_secrets)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def create_audit_export_router(
    encryption_service: EncryptionService,
    logger: Optional[BrutorLogger] = None,
) -> APIRouter:
    """Create the audit-export destinations router."""
    log = logger or BrutorLogger("audit_export_admin")
    router = APIRouter(
        prefix="/admin/audit-export-destinations",
        tags=["Admin - Audit Export"],
    )

    # ------------------------------------------------------------------
    # LIST
    # ------------------------------------------------------------------

    @router.get(
        "",
        response_model=AuditExportDestinationListResponse,
        summary="List audit-export destinations for the current tenant",
    )
    def list_destinations(
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.PROXY)
        ),
        admin=Depends(require_permission(Permission.PROXY_LOGS_READ)),
    ):
        _, tenant_id, _ = admin_and_tenant
        rows = (
            db.query(AuditExportDestination)
            .filter(AuditExportDestination.tenant_id == tenant_id)
            .order_by(AuditExportDestination.created_at.desc())
            .all()
        )
        return AuditExportDestinationListResponse(
            destinations=[
                AuditExportDestinationResponse(**r.to_dict()) for r in rows
            ],
            total=len(rows),
        )

    # ------------------------------------------------------------------
    # CREATE
    # ------------------------------------------------------------------

    @router.post(
        "",
        response_model=AuditExportDestinationResponse,
        status_code=201,
        summary="Create a new audit-export destination",
    )
    def create_destination(
        body: CreateAuditExportDestinationRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.PROXY)
        ),
        admin=Depends(require_permission(Permission.PROXY_MANAGEMENT)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, tenant_id, admin_type = admin_and_tenant

        # Unique name within tenant (matches the DB constraint; raising
        # 409 here gives a friendlier error than a Postgres unique-
        # violation traceback).
        existing = (
            db.query(AuditExportDestination)
            .filter(
                AuditExportDestination.tenant_id == tenant_id,
                AuditExportDestination.name == body.name,
            )
            .first()
        )
        if existing is not None:
            raise HTTPException(
                409,
                f"An audit-export destination named {body.name!r} already "
                "exists for this tenant",
            )

        encrypted_config = _config_to_store(body.destination_type, body.config, encryption_service)

        dest = AuditExportDestination(
            id=f"axd-{uuid4().hex[:16]}",
            tenant_id=tenant_id,
            name=body.name,
            destination_type=body.destination_type,
            enabled=body.enabled,
            config=encrypted_config,
            field_allowlist=body.field_allowlist,
            interval_seconds=body.interval_seconds,
            created_at=datetime.now(timezone.utc),
            created_by=str(admin_obj.id),
        )
        db.add(dest)
        db.flush()
        db.refresh(dest)

        audit.log(
            action="audit_export_destination_create",
            admin=admin_obj,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type=TenantResourceTypes.PROXY.value,
            resource_id=dest.id,
            details={
                "name": dest.name,
                "destination_type": dest.destination_type,
                "interval_seconds": dest.interval_seconds,
                "enabled": dest.enabled,
                # NB: deliberately not logging the config — would
                # write secrets to the admin audit log.
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )
        log.info(
            "Audit-export destination created",
            context={"destination_id": dest.id, "tenant_id": tenant_id, "type": dest.destination_type},
        )
        return AuditExportDestinationResponse(**dest.to_dict())

    # ------------------------------------------------------------------
    # READ (single)
    # ------------------------------------------------------------------

    @router.get(
        "/{destination_id}",
        response_model=AuditExportDestinationResponse,
        summary="Read one audit-export destination",
    )
    def get_destination(
        destination_id: str,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.PROXY)
        ),
        admin=Depends(require_permission(Permission.PROXY_LOGS_READ)),
    ):
        _, tenant_id, _ = admin_and_tenant
        dest = _get_for_tenant(db, tenant_id, destination_id)
        return AuditExportDestinationResponse(**dest.to_dict())

    # ------------------------------------------------------------------
    # UPDATE
    # ------------------------------------------------------------------

    @router.patch(
        "/{destination_id}",
        response_model=AuditExportDestinationResponse,
        summary="Update an audit-export destination",
    )
    def update_destination(
        destination_id: str,
        body: UpdateAuditExportDestinationRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.PROXY)
        ),
        admin=Depends(require_permission(Permission.PROXY_MANAGEMENT)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, tenant_id, admin_type = admin_and_tenant
        dest = _get_for_tenant(db, tenant_id, destination_id)

        changes: dict = {}
        # `model_dump(exclude_unset=True)` is critical — without it,
        # Pydantic fills missing fields with None and we'd null out
        # everything the operator didn't touch. The same pitfall bit
        # us on the `wants_listener` PATCH in v0.7.95.
        update_data = body.model_dump(exclude_unset=True)

        for field, value in update_data.items():
            if field == "config":
                # A secret the request does not name stays as stored.
                stored = dict(dest.config or {})
                value = _config_to_store(
                    dest.destination_type,
                    _validated(dest.destination_type, value or {}, stored),
                    encryption_service,
                    stored,
                )

            old = getattr(dest, field)
            if old != value:
                # For audit-log clarity, omit `config` from the diff —
                # logging it would leak ciphertext into the audit log
                # (not strictly a secret leak, but uninformative noise).
                if field != "config":
                    changes[field] = {"old": old, "new": value}
                setattr(dest, field, value)

        if changes or "config" in update_data:
            dest.updated_at = datetime.now(timezone.utc)
            db.flush()
            db.refresh(dest)

            audit.log(
                action="audit_export_destination_update",
                admin=admin_obj,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.PROXY.value,
                resource_id=dest.id,
                details={"changes": changes, "config_touched": "config" in update_data},
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db,
            )

        return AuditExportDestinationResponse(**dest.to_dict())

    # ------------------------------------------------------------------
    # DELETE
    # ------------------------------------------------------------------

    @router.delete(
        "/{destination_id}",
        status_code=204,
        summary="Delete an audit-export destination",
    )
    def delete_destination(
        destination_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.PROXY)
        ),
        admin=Depends(require_permission(Permission.PROXY_MANAGEMENT)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, tenant_id, admin_type = admin_and_tenant
        dest = _get_for_tenant(db, tenant_id, destination_id)
        name, dtype = dest.name, dest.destination_type
        db.delete(dest)
        db.flush()

        audit.log(
            action="audit_export_destination_delete",
            admin=admin_obj,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type=TenantResourceTypes.PROXY.value,
            resource_id=destination_id,
            details={"name": name, "destination_type": dtype},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

    # ------------------------------------------------------------------
    # RUN NOW
    # ------------------------------------------------------------------

    @router.post(
        "/{destination_id}/run-now",
        response_model=RunResultResponse,
        summary="Fire one export run immediately, bypassing the scheduler",
        description=(
            "Exports the proxy-log rows since the last successful run as one "
            "gzipped JSONL artifact. For S3 destinations a "
            "`<object>.manifest.json` sidecar follows it: `sha256` over the "
            "delivered bytes, `byte_size`, `row_count`, the window, "
            "`chain_columns_included` — whether the rows carry their `chain` "
            "object (chain columns, `chain_version`, and every field the "
            "row's canonical format covers, so `row_hash` can be recomputed "
            "offline) — and an Ed25519 signature when an audit signing key "
            "is configured."
        ),
    )
    def run_now(
        destination_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.PROXY)
        ),
        admin=Depends(require_permission(Permission.PROXY_MANAGEMENT)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin_obj, tenant_id, admin_type = admin_and_tenant
        dest = _get_for_tenant(db, tenant_id, destination_id)

        svc = AuditExportService(db=db, encryption_service=encryption_service)
        # Run synchronously so the operator gets feedback. For large
        # backlogs that take a while, the scheduler's normal cadence
        # is the right tool — `run-now` is for "did I configure this
        # right?" smoke testing.
        result = svc.run_destination(dest.id)
        db.flush()

        audit.log(
            action="audit_export_destination_run_now",
            admin=admin_obj,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=(result.error is None),
            resource_type=TenantResourceTypes.PROXY.value,
            resource_id=destination_id,
            details={
                "outcome": result.outcome.value,
                "rows_exported": result.rows_exported,
                "error": result.error,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

        return RunResultResponse(
            destination_id=result.destination_id,
            outcome=result.outcome.value,
            rows_exported=result.rows_exported,
            window_start=(
                result.window_start.isoformat() if result.window_start else None
            ),
            window_end=result.window_end.isoformat(),
            error=result.error,
        )

    return router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_for_tenant(
    db: Session, tenant_id: str, destination_id: str
) -> AuditExportDestination:
    """Fetch a destination, asserting it belongs to the current
    tenant. 404 if missing OR if it belongs to a different tenant —
    same response on both branches so an attacker can't enumerate
    other tenants' IDs by status-code differential."""
    dest = (
        db.query(AuditExportDestination)
        .filter(
            AuditExportDestination.id == destination_id,
            AuditExportDestination.tenant_id == tenant_id,
        )
        .one_or_none()
    )
    if dest is None:
        raise HTTPException(404, "Audit-export destination not found")
    return dest
