# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Global LLM Catalog admin API

Endpoints under `/v1/admin/llm-catalog/*`:

  GET    ""             — browse / search the global catalog (filters +
                          pagination)
  GET    "/{id}"        — a single catalog entry
  POST   "/{id}/import" — import the entry into the caller's tenant as a
                          configured `llm_model` (returns the standard
                          LLModelDetailResponse shape)

The catalog itself is GLOBAL (no tenant scope) — browse / get don't take a
tenant. Import is tenant-scoped: tenant admins are pinned to their own
tenant; system admins must pass `?tenant_id=`.

Read routes require `AI_MODEL_READ`; import requires `AI_MODEL_CREATE`
(the same permissions the existing `/v1/admin/llms` router uses) + audit.
"""

from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.v1.models.admin.llm_catalog import (
    LlmCatalogCreateRequest,
    LlmCatalogEntryResponse,
    LlmCatalogImportRequest,
    LlmCatalogListResponse,
    LlmCatalogUpdateRequest,
)
from app.api.v1.models.admin.llm_model import LLModelDetailResponse
from app.constants.tenant import TenantResourceTypes
from app.core.admin_user.dependencies import (
    get_audit_logger,
    get_current_admin,
    require_permission,
    require_platform_admin,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.services.llm.model.llm_access_service import DEFAULT_GROUP_SENTINEL, LLMAccessService
from app.services.llm.model.llm_models_manager import LLModelsManager
from app.services.llm_catalog_service import LlmCatalogError, LlmCatalogService
from app.services.oauth.encryption_service import EncryptionService


# Map service error codes to HTTP statuses.
_ERROR_STATUS = {
    "not_found": 404,
    "duplicate": 409,
    "invalid_value": 400,
    "api_url_required": 422,
}


# One tenant-scoping rule for every admin route — see
# app/core/admin_user/dependencies.py::resolve_tenant_id. This was a
# five-line copy in twenty-five modules and had already drifted into two
# behaviours; the shared one refuses a mismatched ?tenant_id= rather than
# silently answering about a different tenant.
from app.core.admin_user.dependencies import resolve_tenant_id as _resolve_tenant_id
from app.middleware.trusted_proxies import client_ip


def _http_from_service_error(e: LlmCatalogError) -> HTTPException:
    """Translate a domain error into the right HTTP status (default 400)."""
    return HTTPException(
        status_code=_ERROR_STATUS.get(e.code, 400),
        detail=e.message,
    )


def create_llm_catalog_router(
    model_manager: LLModelsManager,
    logger: BrutorLogger,
    encryption_service: Optional[EncryptionService] = None,
) -> APIRouter:
    """Build the `/admin/llm-catalog/*` router."""

    router = APIRouter(prefix="/admin/llm-catalog", tags=["LLM Catalog"])
    service = LlmCatalogService(logger)

    # ────────────────────────────────────────────────────────────────────
    # Browse / search
    # ────────────────────────────────────────────────────────────────────
    @router.get(
        "",
        response_model=LlmCatalogListResponse,
        summary="Browse the global LLM catalog",
    )
    def list_catalog(
        provider_key: Optional[str] = Query(None),
        mode: Optional[str] = Query(
            None, description="Filter by mode (chat, embedding, ...)"
        ),
        recommended: Optional[bool] = Query(None),
        q: Optional[str] = Query(
            None, description="Substring on display_name / model_name"
        ),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AI_MODEL_READ)),
    ) -> LlmCatalogListResponse:
        try:
            rows, total = service.list_catalog(
                db,
                provider_key=provider_key,
                mode=mode,
                recommended=recommended,
                q=q,
                limit=limit,
                offset=offset,
            )
        except LlmCatalogError as e:
            raise _http_from_service_error(e)
        return LlmCatalogListResponse(
            catalog=[LlmCatalogEntryResponse(**r.to_dict()) for r in rows],
            total=total,
        )

    # ────────────────────────────────────────────────────────────────────
    # Detail
    # ────────────────────────────────────────────────────────────────────
    @router.get(
        "/{catalog_id}",
        response_model=LlmCatalogEntryResponse,
        summary="Get a single LLM catalog entry",
    )
    def get_catalog_entry(
        catalog_id: str,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.AI_MODEL_READ)),
    ) -> LlmCatalogEntryResponse:
        try:
            entry = service.get_entry(db, catalog_id)
        except LlmCatalogError as e:
            raise _http_from_service_error(e)
        return LlmCatalogEntryResponse(**entry.to_dict())

    # ────────────────────────────────────────────────────────────────────
    # Import → configured llm_model instance
    # ────────────────────────────────────────────────────────────────────
    @router.post(
        "/{catalog_id}/import",
        response_model=LLModelDetailResponse,
        status_code=201,
        summary="Import a catalog entry into the tenant as a configured model",
    )
    def import_catalog_entry(
        catalog_id: str,
        request: Request,
        body: Optional[LlmCatalogImportRequest] = None,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _perm=Depends(require_permission(Permission.AI_MODEL_CREATE)),
    ) -> LLModelDetailResponse:
        admin, admin_type = admin_and_type
        resolved_tenant = _resolve_tenant_id(admin, admin_type, tenant_id)
        body = body or LlmCatalogImportRequest()

        # Build the LLModel column overrides from the request body. Encrypt
        # api_key at rest the same way the create endpoint does.
        overrides: dict = {}
        if body.name is not None:
            overrides["name"] = body.name
        if body.enabled is not None:
            overrides["enabled"] = body.enabled
        if body.api_url and body.api_url.strip():
            overrides["api_url"] = body.api_url.strip()
        if body.api_key:
            # A cloud entry carries no api_key at all (422), whatever there
            # is to encrypt it with.
            from app.services.llm import provider_config as provider_config_rules

            try:
                entry = service.get_entry(db, catalog_id)
            except LlmCatalogError as e:
                raise _http_from_service_error(e)
            problem = provider_config_rules.api_key_problem(entry.provider_key.lower(), body.api_key)
            if problem:
                raise HTTPException(status_code=422, detail=problem)
            # Encrypted, or refused where there is nothing to encrypt it
            # with.
            if encryption_service is None:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "api_key cannot be stored: this control plane has no encryption service "
                        "(ENCRYPTION_KEY), and a credential is never stored in clear. Nothing was saved."
                    ),
                )
            overrides["api_key"] = encryption_service.encrypt(body.api_key)

        try:
            model = service.import_to_model(
                db, resolved_tenant, catalog_id, overrides=overrides
            )
        except LlmCatalogError as e:
            raise _http_from_service_error(e)

        # Bind the model to its groups. No group named (no body, no field,
        # null or []) is the tenant's Default group, as the request model
        # documents; explicit ids are taken as given ('default' is the
        # sentinel for the Default group).
        bound_group_ids = LLMAccessService.resolve_group_ids(
            db, body.group_ids or [DEFAULT_GROUP_SENTINEL], resolved_tenant
        )
        LLMAccessService.assign_model_to_groups(
            db=db,
            model_id=model.id,
            group_ids=bound_group_ids,
            tenant_id=resolved_tenant,
        )

        audit.log(
            action="llm_catalog.import",
            db=db,
            admin=admin,
            admin_type=admin_type,
            tenant_id=resolved_tenant,
            success=True,
            resource_type=TenantResourceTypes.LLM_MODELS.value,
            resource_id=model.id,
            details={
                "catalog_id": catalog_id,
                "model_name": model.model_name,
                "provider": model.provider.key if model.provider else None,
                "group_ids": bound_group_ids,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

        return LLModelDetailResponse(
            model=model_manager.to_response(model, include_groups=True)
        )

    # ────────────────────────────────────────────────────────────────────
    # Curation (system-admin only) — create / update / delete
    #
    # NOTE: `POST /admin/llm-catalog` (create) and
    # `POST /admin/llm-catalog/{id}/import` are distinct paths — no conflict.
    # ────────────────────────────────────────────────────────────────────
    @router.post(
        "",
        response_model=LlmCatalogEntryResponse,
        status_code=201,
        summary="Create a custom global catalog entry (platform administrator only)",
    )
    def create_catalog_entry(
        body: LlmCatalogCreateRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _platform_admin=Depends(require_platform_admin),
    ) -> LlmCatalogEntryResponse:
        admin, admin_type = admin_and_type
        # Per-model import-default fields are forwarded as **extra so the
        # service can persist them without expanding the signature for every
        # column. Only fields the client actually sent are forwarded.
        _explicit = {
            "provider_key", "model_name", "display_name", "description",
            "mode", "capabilities", "context_window", "max_output_tokens",
            "default_cost_per_input_token", "default_cost_per_output_token",
            "icon_url", "recommended", "deprecated",
        }
        extra = {
            k: v
            for k, v in body.model_dump(exclude_unset=True).items()
            if k not in _explicit
        }
        try:
            entry = service.create_entry(
                db,
                provider_key=body.provider_key,
                model_name=body.model_name,
                display_name=body.display_name,
                description=body.description,
                mode=body.mode,
                capabilities=body.capabilities,
                context_window=body.context_window,
                max_output_tokens=body.max_output_tokens,
                default_cost_per_input_token=body.default_cost_per_input_token,
                default_cost_per_output_token=body.default_cost_per_output_token,
                icon_url=body.icon_url,
                recommended=body.recommended,
                deprecated=body.deprecated,
                **extra,
            )
        except LlmCatalogError as e:
            raise _http_from_service_error(e)

        audit.log(
            action="llm_catalog.create",
            db=db,
            admin=admin,
            admin_type=admin_type,
            tenant_id=None,
            success=True,
            resource_type=TenantResourceTypes.LLM_MODELS.value,
            resource_id=entry.id,
            details={
                "provider_key": entry.provider_key,
                "model_name": entry.model_name,
                "mode": entry.mode,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )
        return LlmCatalogEntryResponse(**entry.to_dict())

    @router.patch(
        "/{catalog_id}",
        response_model=LlmCatalogEntryResponse,
        summary="Update a global catalog entry (platform administrator only)",
    )
    def update_catalog_entry(
        catalog_id: str,
        body: LlmCatalogUpdateRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _platform_admin=Depends(require_platform_admin),
    ) -> LlmCatalogEntryResponse:
        admin, admin_type = admin_and_type
        # Only the fields the caller actually supplied (partial update).
        partial = body.model_dump(exclude_unset=True)
        try:
            entry = service.update_entry(db, catalog_id, **partial)
        except LlmCatalogError as e:
            raise _http_from_service_error(e)

        audit.log(
            action="llm_catalog.update",
            db=db,
            admin=admin,
            admin_type=admin_type,
            tenant_id=None,
            success=True,
            resource_type=TenantResourceTypes.LLM_MODELS.value,
            resource_id=entry.id,
            details={"fields": sorted(partial.keys())},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )
        return LlmCatalogEntryResponse(**entry.to_dict())

    @router.delete(
        "/{catalog_id}",
        status_code=204,
        summary="Delete a global catalog entry (platform administrator only)",
    )
    def delete_catalog_entry(
        catalog_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        _platform_admin=Depends(require_platform_admin),
    ) -> Response:
        admin, admin_type = admin_and_type
        try:
            service.delete_entry(db, catalog_id)
        except LlmCatalogError as e:
            raise _http_from_service_error(e)

        audit.log(
            action="llm_catalog.delete",
            db=db,
            admin=admin,
            admin_type=admin_type,
            tenant_id=None,
            success=True,
            resource_type=TenantResourceTypes.LLM_MODELS.value,
            resource_id=catalog_id,
            details={},
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )
        return Response(status_code=204)

    return router
