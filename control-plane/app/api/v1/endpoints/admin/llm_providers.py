# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Admin REST surface for `LlmProvider` rows.

Endpoints
---------
  * GET    /v1/admin/providers
        Tenant-scoped list. Supports `?category=`, `?enabled=`,
        `?include_model_count=`. Returns a stable
        `{providers: […], total: N}` envelope.
  * GET    /v1/admin/providers/{id}
        Single row + nested model_count.
  * POST   /v1/admin/providers
        Create a new provider. Body validates against
        `LLProviderCreate` (see Pydantic model). The slug `key` is
        unique per tenant; collisions return 409.
  * PATCH  /v1/admin/providers/{id}
        Partial update. `key` cannot be changed via this route —
        use the explicit `/rename` endpoint below if you really
        need to rotate the slug.
  * POST   /v1/admin/providers/{id}/rename
        Change the slug (rare). Refused if any `llm_model` row
        currently references this provider.
  * DELETE /v1/admin/providers/{id}
        Remove the row. Refused if `is_system=True` (catalogue
        provider) or if any `llm_model` references it.

The Rust proxy reads these rows via its own SQL (no JSON
roundtrip) and caches them for ten minutes
(`ProviderCache::DEFAULT_TTL_SECS`), so every write here has to
publish a `llm_provider` cache invalidation on the Redis pub/sub
channel (`brutor:cache:invalidate`) — the same bus `llm_model`
updates use. The proxy's arm for it drops the provider entry *and*
flushes the tenant's model entries, because a cached model carries
the provider slug stamped from this row.

The message `id` is the provider's primary key (`prov-…`), not its
slug: `ProviderCache` is keyed `tenant_id:provider_id`.

Why providers are first-class
-----------------------------
See `app/database/models/llm_provider.py`. In short: hardcoded provider enums don't scale
once operators want their own slugs (custom HuggingFace
endpoints, internal vLLM clusters, …) and forced "what providers
exist?" duplication across four files.
"""
from __future__ import annotations

from typing import Optional, Union

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session
from ulid import ULID

from app.core.sync_bridge import block_on
from app.api.v1.models.admin.llm_provider import (
    LLProviderCreate,
    LLProviderListResponse,
    LLProviderResponse,
    LLProviderUpdate,
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
from app.services.cache_invalidation import invalidate_proxy_cache
from app.services.tenant.tenant_service import TenantService
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.middleware.trusted_proxies import client_ip


def create_llm_providers_router(
    logger: BrutorLogger,
) -> APIRouter:
    """Factory function to mount the providers admin router under
    `/v1/admin/providers`."""

    router = APIRouter(
        prefix="/admin/providers",
        tags=["LLM Provider Admin"],
    )

    # =========================================================================
    # LIST
    # =========================================================================

    @router.get(
        "",
        response_model=LLProviderListResponse,
        summary="List LLM providers for the calling tenant",
    )
    def list_providers(
        request: Request,
        category: Optional[str] = Query(
            None,
            description=(
                "Optional category filter (chat | image | audio | "
                "video | embeddings | self_hosted | internal)."
            ),
        ),
        enabled: Optional[bool] = Query(
            None,
            description=(
                "Filter to only enabled (or only disabled) "
                "providers. Omit to return both."
            ),
        ),
        include_model_count: bool = Query(
            False,
            description=(
                "When true, populate `model_count` on each row. "
                "Off by default — adds an extra GROUP BY query."
            ),
        ),
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AI_MODEL_READ)
        ),
    ) -> LLProviderListResponse:
        _admin, tenant_id, _admin_type = admin_and_tenant

        q = db.query(LlmProvider).filter(LlmProvider.tenant_id == tenant_id)
        if category is not None:
            q = q.filter(LlmProvider.category == category)
        if enabled is not None:
            q = q.filter(LlmProvider.enabled == enabled)
        rows = q.order_by(LlmProvider.display_name.asc()).all()

        counts: dict[str, int] = {}
        if include_model_count and rows:
            count_rows = (
                db.query(LLModel.provider_id, func.count(LLModel.id))
                .filter(
                    LLModel.tenant_id == tenant_id,
                    LLModel.provider_id.in_([r.id for r in rows]),
                )
                .group_by(LLModel.provider_id)
                .all()
            )
            counts = {pid: n for pid, n in count_rows}

        providers = []
        for row in rows:
            data = row.to_dict()
            if include_model_count:
                data["model_count"] = counts.get(row.id, 0)
            providers.append(LLProviderResponse(**data))
        return LLProviderListResponse(
            providers=providers, total=len(providers)
        )

    # =========================================================================
    # GET ONE
    # =========================================================================

    @router.get(
        "/{provider_id}",
        response_model=LLProviderResponse,
        summary="Get a single provider by id",
    )
    def get_provider(
        provider_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AI_MODEL_READ)
        ),
    ) -> LLProviderResponse:
        _admin, tenant_id, _admin_type = admin_and_tenant
        row = (
            db.query(LlmProvider)
            .filter(
                LlmProvider.tenant_id == tenant_id,
                LlmProvider.id == provider_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Provider not found")
        data = row.to_dict()
        # Always-on model_count on the detail view — the caller
        # already paid for the row fetch, the COUNT is cheap.
        data["model_count"] = (
            db.query(func.count(LLModel.id))
            .filter(
                LLModel.tenant_id == tenant_id,
                LLModel.provider_id == provider_id,
            )
            .scalar()
            or 0
        )
        return LLProviderResponse(**data)

    # =========================================================================
    # CREATE
    # =========================================================================

    @router.post(
        "",
        response_model=LLProviderResponse,
        status_code=201,
        summary="Create a new provider",
    )
    def create_provider(
        body: LLProviderCreate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AI_MODEL_CREATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> LLProviderResponse:
        _admin, tenant_id, _admin_type = admin_and_tenant

        # Feature gate.
        TenantService.check_feature_access(db, tenant_id, 'models')

        # Slug uniqueness is also enforced by the DB unique
        # constraint, but a friendly 409 is cheaper than waiting
        # for sqlalchemy to surface IntegrityError.
        existing = (
            db.query(LlmProvider)
            .filter(
                LlmProvider.tenant_id == tenant_id,
                LlmProvider.key == body.key.lower(),
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A provider with key {body.key!r} already exists "
                    f"for this tenant (id={existing.id})."
                ),
            )

        row = LlmProvider(
            id=f"prov-{ULID()}",
            tenant_id=tenant_id,
            key=body.key.lower(),
            display_name=body.display_name,
            category=body.category,
            # Residency region. Omitted here previously, so a provider
            # created with a region silently came out unscoped — and an
            # unscoped provider is one the residency check cannot refuse.
            region=body.region,
            developer_url=body.developer_url,
            api_key_url=body.api_key_url,
            pricing_url=body.pricing_url,
            model_catalog_url=body.model_catalog_url,
            icon_url=body.icon_url,
            default_api_url=body.default_api_url,
            default_auth_type=body.default_auth_type,
            notes=body.notes,
            metadata_=body.metadata,
            enabled=body.enabled,
            # Operator-created providers are not is_system. The
            # only way to mint a system provider is through
            # `init_llm_providers.create_llm_providers`.
            is_system=False,
            # Passed only when supplied, so an omitted version falls
            # through to the column default ("1.0") rather than NULL.
            **({"version": body.version} if body.version else {}),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        # Drop the proxy's cached view. A create matters as much as an
        # edit: ProviderCache negative-caches nothing, but the model
        # cache stamps the provider slug at load time, so a model bound
        # to this brand-new provider must not be served from an entry
        # built before it existed.
        block_on(invalidate_proxy_cache(request, "llm_provider", tenant_id, row.id, "create"))
        logger.info(
            "Created LLM provider",
            context={
                "tenant_id": tenant_id,
                "provider_id": row.id,
                "key": row.key,
            },
        )
        audit.log(
            action="llm_provider_create",
            db=db,
            admin=_admin,
            admin_type=_admin_type,
            success=True,
            resource_type=TenantResourceTypes.LLM_MODELS.value,
            resource_id=str(row.id),
            tenant_id=tenant_id,
            details={
                "key": row.key,
                "display_name": row.display_name,
                "category": row.category,
                "enabled": row.enabled,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )
        return LLProviderResponse(**row.to_dict())

    # =========================================================================
    # UPDATE
    # =========================================================================

    @router.patch(
        "/{provider_id}",
        response_model=LLProviderResponse,
        summary="Update a provider (partial)",
    )
    def update_provider(
        provider_id: str,
        body: LLProviderUpdate,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AI_MODEL_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> LLProviderResponse:
        _admin, tenant_id, _admin_type = admin_and_tenant
        row = (
            db.query(LlmProvider)
            .filter(
                LlmProvider.tenant_id == tenant_id,
                LlmProvider.id == provider_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Provider not found")

        # Apply only the fields the caller actually sent. Pydantic's
        # `model_dump(exclude_unset=True)` distinguishes "field
        # absent" from "field explicitly set to null".
        patch = body.model_dump(exclude_unset=True)
        # Map Python's `metadata` keyword to the ORM's `metadata_`
        # attribute (SQLAlchemy reserves `metadata` on Base).
        if "metadata" in patch:
            row.metadata_ = patch.pop("metadata")
        for k, v in patch.items():
            setattr(row, k, v)
        db.commit()
        db.refresh(row)
        # Without this the proxy keeps dispatching against the old
        # default_api_url / auth type / region — and `enabled: false`
        # does not stop traffic — for the ProviderCache TTL (10 min).
        block_on(invalidate_proxy_cache(request, "llm_provider", tenant_id, row.id, "update"))
        logger.info(
            "Updated LLM provider",
            context={
                "tenant_id": tenant_id,
                "provider_id": row.id,
                "fields": sorted(patch.keys()),
            },
        )
        audit.log(
            action="llm_provider_update",
            db=db,
            admin=_admin,
            admin_type=_admin_type,
            success=True,
            resource_type=TenantResourceTypes.LLM_MODELS.value,
            resource_id=str(row.id),
            tenant_id=tenant_id,
            details={
                "key": row.key,
                "fields_changed": sorted(patch.keys()),
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )
        return LLProviderResponse(**row.to_dict())

    # =========================================================================
    # RENAME (slug rotation — rare, locked down)
    # =========================================================================

    @router.post(
        "/{provider_id}/rename",
        response_model=LLProviderResponse,
        summary="Change a provider's slug (key)",
        description=(
            "Slug changes are dangerous because they're the value "
            "the Rust proxy adapter dispatch matches on. We refuse "
            "the rename if any `llm_model` currently references this "
            "provider — operators must first reassign or delete "
            "those models."
        ),
    )
    def rename_provider(
        provider_id: str,
        request: Request,
        new_key: str = Query(
            ..., min_length=1, max_length=64,
            description="The new slug (lowercase alphanumeric / underscore / hyphen).",
        ),
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AI_MODEL_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> LLProviderResponse:
        _admin, tenant_id, _admin_type = admin_and_tenant

        # Validate the new slug shape via the same regex Pydantic uses.
        import re
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", new_key):
            raise HTTPException(
                status_code=400,
                detail=(
                    "new_key must be lowercase alphanumeric / "
                    "underscore / hyphen and start with a letter or digit."
                ),
            )

        row = (
            db.query(LlmProvider)
            .filter(
                LlmProvider.tenant_id == tenant_id,
                LlmProvider.id == provider_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Provider not found")

        in_use = (
            db.query(func.count(LLModel.id))
            .filter(
                LLModel.tenant_id == tenant_id,
                LLModel.provider_id == provider_id,
            )
            .scalar()
            or 0
        )
        if in_use:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot rename: {in_use} model(s) reference this "
                    "provider. Reassign or delete them first."
                ),
            )

        clash = (
            db.query(LlmProvider)
            .filter(
                LlmProvider.tenant_id == tenant_id,
                LlmProvider.key == new_key,
                LlmProvider.id != provider_id,
            )
            .first()
        )
        if clash:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"A provider with key {new_key!r} already exists "
                    f"(id={clash.id})."
                ),
            )

        old_key = row.key
        row.key = new_key
        db.commit()
        db.refresh(row)
        # A rename is the worst of the three to leave stale: cached
        # model entries still carry the OLD slug, so adapter dispatch
        # looks up a provider that no longer answers to that name.
        block_on(invalidate_proxy_cache(request, "llm_provider", tenant_id, row.id, "update"))
        logger.info(
            "Renamed LLM provider",
            context={
                "tenant_id": tenant_id,
                "provider_id": row.id,
                "old_key": old_key,
                "new_key": new_key,
            },
        )
        audit.log(
            action="llm_provider_rename",
            db=db,
            admin=_admin,
            admin_type=_admin_type,
            success=True,
            resource_type=TenantResourceTypes.LLM_MODELS.value,
            resource_id=str(row.id),
            tenant_id=tenant_id,
            details={
                "old_key": old_key,
                "new_key": new_key,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )
        return LLProviderResponse(**row.to_dict())

    # =========================================================================
    # DELETE
    # =========================================================================

    @router.delete(
        "/{provider_id}",
        status_code=204,
        summary="Delete a provider",
    )
    def delete_provider(
        provider_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AI_MODEL_DELETE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        _admin, tenant_id, _admin_type = admin_and_tenant
        row = (
            db.query(LlmProvider)
            .filter(
                LlmProvider.tenant_id == tenant_id,
                LlmProvider.id == provider_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Provider not found")
        if row.is_system:
            raise HTTPException(
                status_code=409,
                detail=(
                    "System providers cannot be deleted. "
                    "Soft-disable instead by setting `enabled=false`."
                ),
            )
        in_use = (
            db.query(func.count(LLModel.id))
            .filter(
                LLModel.tenant_id == tenant_id,
                LLModel.provider_id == provider_id,
            )
            .scalar()
            or 0
        )
        if in_use:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot delete: {in_use} model(s) still reference "
                    "this provider. Reassign or delete them first."
                ),
            )
        deleted_key = row.key
        db.delete(row)
        db.commit()
        block_on(invalidate_proxy_cache(request, "llm_provider", tenant_id, provider_id, "delete"))
        logger.info(
            "Deleted LLM provider",
            context={
                "tenant_id": tenant_id,
                "provider_id": provider_id,
                "key": deleted_key,
            },
        )
        audit.log(
            action="llm_provider_delete",
            db=db,
            admin=_admin,
            admin_type=_admin_type,
            success=True,
            resource_type=TenantResourceTypes.LLM_MODELS.value,
            resource_id=str(provider_id),
            tenant_id=tenant_id,
            details={
                "key": deleted_key,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

    # =========================================================================
    # ICON STORAGE
    # =========================================================================
    #
    # Mirrors `/v1/admin/mcp-servers/{id}/icon`. The GET endpoint is
    # deliberately permission-loose (`AI_MODEL_READ`) — anyone who
    # can see the model picker should be able to render the icons
    # in it. Upload + delete require AI_MODEL_UPDATE.

    from app.services.llm.provider.icon_manager import (
        LlmProviderIconManager,
    )
    icon_manager = LlmProviderIconManager(logger=logger)

    @router.get(
        "/{provider_id}/icon",
        summary="Fetch a provider's icon bytes (PUBLIC)",
        description=(
            "Returns the raw icon bytes (PNG / SVG / etc.) for the "
            "provider. **Public endpoint** — browsers can't send an "
            "Authorization header on `<img src=…>`, and provider "
            "icons aren't sensitive. Mirrors the MCP server icon "
            "pattern (`/v1/admin/mcp-servers/{id}/icon`). The "
            "provider_id is a ULID, so guessing one is infeasible. "
            "Caching: ETag uses the SHA-256 of the icon bytes; "
            "clients passing `If-None-Match` get a 304 when the "
            "icon hasn't changed."
        ),
    )
    def get_provider_icon(
        provider_id: str,
        request: Request,
        db: Session = Depends(get_db),
    ):
        # Resolve by id alone — globally unique. No tenant filter
        # because the `<img>` request can't carry tenant context
        # without auth (same call shape as MCP server icons).
        row = (
            db.query(LlmProvider)
            .filter(LlmProvider.id == provider_id)
            .first()
        )
        if row is None or row.icon_data is None:
            raise HTTPException(
                status_code=404,
                detail="Provider icon not found",
            )

        etag = f'"{row.icon_hash}"' if row.icon_hash else None
        if etag and request.headers.get("if-none-match") == etag:
            return Response(status_code=304)

        headers: dict[str, str] = {
            # Browsers cache aggressively; bust on the icon hash.
            "Cache-Control": "public, max-age=86400",
        }
        if etag:
            headers["ETag"] = etag
        if row.icon_uploaded_at:
            headers["Last-Modified"] = row.icon_uploaded_at.strftime(
                "%a, %d %b %Y %H:%M:%S GMT"
            )

        return Response(
            content=bytes(row.icon_data),
            media_type=row.icon_mime_type or "image/png",
            headers=headers,
        )

    @router.post(
        "/{provider_id}/icon",
        response_model=LLProviderResponse,
        summary="Upload or replace a provider's icon",
    )
    def upload_provider_icon(
        provider_id: str,
        request: Request,
        file: UploadFile = File(
            ...,
            description="Icon file (SVG/PNG/JPEG/GIF/WEBP/ICO, max 2 MiB).",
        ),
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AI_MODEL_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ) -> LLProviderResponse:
        _admin, tenant_id, _admin_type = admin_and_tenant
        row = (
            db.query(LlmProvider)
            .filter(
                LlmProvider.tenant_id == tenant_id,
                LlmProvider.id == provider_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Provider not found")

        try:
            block_on(icon_manager.save_icon_from_upload(
                provider_id, file, db
            ))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        db.commit()
        db.refresh(row)
        audit.log(
            action="llm_provider_icon_set",
            db=db,
            admin=_admin,
            admin_type=_admin_type,
            success=True,
            resource_type=TenantResourceTypes.LLM_MODELS.value,
            resource_id=str(row.id),
            tenant_id=tenant_id,
            details={
                "key": row.key,
                "icon_mime_type": row.icon_mime_type,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )
        return LLProviderResponse(**row.to_dict())

    @router.delete(
        "/{provider_id}/icon",
        status_code=204,
        summary="Clear a provider's icon",
    )
    def delete_provider_icon(
        provider_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.AI_MODEL_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        _admin, tenant_id, _admin_type = admin_and_tenant
        row = (
            db.query(LlmProvider)
            .filter(
                LlmProvider.tenant_id == tenant_id,
                LlmProvider.id == provider_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Provider not found")
        if row.icon_data is None:
            return  # already empty — idempotent
        provider_key = row.key
        icon_manager.delete_icon(provider_id, db)
        db.commit()
        audit.log(
            action="llm_provider_icon_clear",
            db=db,
            admin=_admin,
            admin_type=_admin_type,
            success=True,
            resource_type=TenantResourceTypes.LLM_MODELS.value,
            resource_id=str(provider_id),
            tenant_id=tenant_id,
            details={
                "key": provider_key,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
        )

    return router
