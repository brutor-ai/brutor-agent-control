# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MCP Server Catalog Admin Endpoints

CRUD for catalog entries + import action that creates linked
MCPServer / IdPServer / OAuthAppClient / ServerConfig records.
"""

from typing import Any, Optional, Union
from datetime import datetime, timezone

from ulid import ULID
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session

from app.core.sync_bridge import block_on
from app.database import get_db
from app.database.models.mcp_catalog_entry import MCPCatalogEntry
from app.database.models.mcp_server import MCPServer
from app.database.models.idp_server import IdPServer
from app.database.models.oauth_app_client import OAuthAppClient
from app.database.models.server_config import ServerConfig
from app.database.models.resource_group import ResourceGroup
from app.database.models.admin import TenantAdmin

from app.api.v1.models.admin.mcp_catalog import (
    CatalogEntryCreateRequest,
    CatalogEntryUpdateRequest,
    CatalogImportRequest,
    CatalogEntryBrief,
    CatalogEntryDetailResponse,
    CatalogEntryListResponse,
    CatalogCategoriesResponse,
    CatalogImportResponse,
    CategoryCount,
)

from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.composition.features import DEPLOYMENT
from app.composition.hooks import (
    HOOKS,
    MCP_CATALOG_CONTAINER_GATE,
    MCP_CATALOG_CONTAINER_INSTALL,
    portal_oauth_callback_url,
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    acting_tenant_id,
    require_permission,
    get_audit_logger,
    get_current_admin,
    is_platform_admin,
    is_platform_wide,
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.services.mcp.server import server_token
from app.services.mcp.server.capability_probe import probe_capabilities
from sqlalchemy import or_


def _require_platform_admin(admin, admin_type) -> None:
    """The global entries are the installation's: only its administrator
    curates them (`require_platform_admin`) — the system
    administrator where one exists, else the tenant administrator holding
    `platform:catalog:manage`."""
    if not is_platform_admin(admin, admin_type):
        raise HTTPException(
            status_code=403,
            detail="Platform administrator access required to curate global catalog entries",
        )


def _own_tenant_id(admin, admin_type) -> Optional[str]:
    """The caller's tenant scope, or None for system admins (who have no
    home tenant — they curate the global catalog)."""
    return None if is_platform_wide(admin_type) else admin.tenant_id


def _visibility_filter(admin, admin_type):
    """SQLAlchemy predicate for the entries a caller may browse:
    GLOBAL (tenant_id IS NULL) ∪ own-tenant custom entries.

    System admins have no home tenant, so they see only the global rows
    (plus, harmlessly, any rows whose tenant_id is NULL — same thing)."""
    own = _own_tenant_id(admin, admin_type)
    if own is None:
        return MCPCatalogEntry.tenant_id.is_(None)
    return or_(
        MCPCatalogEntry.tenant_id.is_(None),
        MCPCatalogEntry.tenant_id == own,
    )


def create_mcp_catalog_router(logger: BrutorLogger, encryption_service: Any = None) -> APIRouter:
    """Create router for MCP server catalog management endpoints.

    `encryption_service` encrypts the static token of a server an import
    creates; without one, an import that carries a token is refused (503).
    """
    router = APIRouter(prefix="/admin/mcp-catalog", tags=["MCP Catalog"])

    # ========================================================================
    # LIST — GET /admin/mcp-catalog
    # ========================================================================
    @router.get(
        "",
        response_model=CatalogEntryListResponse,
        summary="List catalog entries",
    )
    def list_catalog_entries(
        request: Request,
        db: Session = Depends(get_db),

        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.MCP_CATALOG_READ)),
        search: Optional[str] = Query(None, description="Search name/description"),
        runtime: Optional[str] = Query(None, description="Filter by runtime: remote, containerized"),
        auth_pattern: Optional[str] = Query(None, description="Filter by auth: oauth, api_key, header, none"),
        category: Optional[str] = Query(None, description="Filter by category name"),
        source: Optional[str] = Query(None, description="Filter by source: system, custom"),
        enabled_only: bool = Query(False, description="Only return enabled entries"),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ):
        admin, admin_type = admin_and_type

        # Browse = GLOBAL system entries ∪ own-tenant custom entries.
        query = db.query(MCPCatalogEntry).filter(
            _visibility_filter(admin, admin_type)
        )

        if enabled_only:
            query = query.filter(MCPCatalogEntry.enabled == True)
        if runtime:
            query = query.filter(MCPCatalogEntry.runtime == runtime)
        if auth_pattern:
            query = query.filter(MCPCatalogEntry.auth_pattern == auth_pattern)
        if source:
            query = query.filter(MCPCatalogEntry.source == source)
        if category:
            # Cast JSON column to JSONB for @> containment operator
            from sqlalchemy import cast, text
            from sqlalchemy.dialects.postgresql import JSONB
            query = query.filter(
                cast(MCPCatalogEntry.categories, JSONB).contains(cast([category], JSONB))
            )
        if search:
            search_term = f"%{search}%"
            query = query.filter(
                (MCPCatalogEntry.name.ilike(search_term)) |
                (MCPCatalogEntry.short_description.ilike(search_term))
            )

        total = query.count()
        entries = query.order_by(MCPCatalogEntry.name).offset(offset).limit(limit).all()

        return CatalogEntryListResponse(
            entries=[CatalogEntryBrief(**e.to_dict(brief=True)) for e in entries],
            total=total,
        )

    # ========================================================================
    # CATEGORIES — GET /admin/mcp-catalog/categories
    # ========================================================================
    @router.get(
        "/categories",
        response_model=CatalogCategoriesResponse,
        summary="List unique categories with counts",
    )
    def list_categories(
        request: Request,
        db: Session = Depends(get_db),

        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.MCP_CATALOG_READ)),
    ):
        admin, admin_type = admin_and_type

        entries = db.query(MCPCatalogEntry).filter(
            _visibility_filter(admin, admin_type),
            MCPCatalogEntry.enabled == True,
        ).all()

        # Aggregate categories across all entries
        cat_counts: dict[str, int] = {}
        for entry in entries:
            for cat in (entry.categories or []):
                cat_counts[cat] = cat_counts.get(cat, 0) + 1

        categories = sorted(
            [CategoryCount(name=k, count=v) for k, v in cat_counts.items()],
            key=lambda c: (-c.count, c.name),
        )

        return CatalogCategoriesResponse(categories=categories)

    # ========================================================================
    # GET — GET /admin/mcp-catalog/{entry_id}
    # ========================================================================
    @router.get(
        "/{entry_id}",
        response_model=CatalogEntryDetailResponse,
        summary="Get catalog entry detail",
    )
    def get_catalog_entry(
        entry_id: str,
        request: Request,
        db: Session = Depends(get_db),

        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.MCP_CATALOG_READ)),
    ):
        admin, admin_type = admin_and_type

        entry = db.query(MCPCatalogEntry).filter(
            MCPCatalogEntry.id == entry_id,
            _visibility_filter(admin, admin_type),
        ).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Catalog entry not found")

        return CatalogEntryDetailResponse(**entry.to_dict(brief=False))

    # ========================================================================
    # CREATE — POST /admin/mcp-catalog
    # ========================================================================
    @router.post(
        "",
        response_model=CatalogEntryDetailResponse,
        status_code=201,
        summary="Create custom catalog entry",
    )
    def create_catalog_entry(
        body: CatalogEntryCreateRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.MCP_CATALOG_CREATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type

        if body.is_global:
            # ── GLOBAL system entry (system admins only) ────────────────
            _require_platform_admin(admin, admin_type)

            existing = db.query(MCPCatalogEntry).filter(
                MCPCatalogEntry.tenant_id.is_(None),
                MCPCatalogEntry.slug == body.slug,
            ).first()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"Global catalog entry with slug '{body.slug}' already exists",
                )

            entry_id = f"mcp-cat-system-{body.slug}"
            new_tenant_id = None
            new_source = "system"
            audit_admin_type = "system"
            audit_tenant_id = None
        else:
            # ── Per-tenant custom entry (existing behaviour) ────────────
            new_tenant_id = acting_tenant_id(request, admin)
            new_source = "custom"
            audit_admin_type = "tenant"
            audit_tenant_id = new_tenant_id

            existing = db.query(MCPCatalogEntry).filter(
                MCPCatalogEntry.tenant_id == new_tenant_id,
                MCPCatalogEntry.slug == body.slug,
            ).first()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"Catalog entry with slug '{body.slug}' already exists",
                )

            entry_id = f"mcp-cat-{ULID()}"

        entry = MCPCatalogEntry(
            id=entry_id,
            tenant_id=new_tenant_id,
            name=body.name,
            slug=body.slug,
            short_description=body.short_description,
            description=body.description,
            icon_url=body.icon_url,
            repo_url=body.repo_url,
            runtime=body.runtime,
            remote_config=body.remote_config,
            containerized_config=body.containerized_config,
            env_vars=body.env_vars or [],
            tool_preview=body.tool_preview or [],
            categories=body.categories or [],
            metadata_extra=body.metadata_extra,
            auth_pattern=body.auth_pattern,
            source=new_source,
            enabled=body.enabled,
        )

        db.add(entry)
        db.commit()
        db.refresh(entry)

        logger.info("Created catalog entry", context={
            "tenant_id": new_tenant_id, "entry_id": entry_id,
            "name": body.name, "source": new_source,
        })

        audit.log(
            db=db,
            action="mcp_catalog_create",
            admin=admin,
            admin_type=audit_admin_type,
            tenant_id=audit_tenant_id,
            success=True,
            resource_type=TenantResourceTypes.MCP_CATALOG.value,
            resource_id=entry_id,
            details={
                "name": body.name, "slug": body.slug,
                "runtime": body.runtime, "source": new_source,
            },
        )

        return CatalogEntryDetailResponse(**entry.to_dict(brief=False))

    # ========================================================================
    # UPDATE — PUT /admin/mcp-catalog/{entry_id}
    # ========================================================================
    @router.put(
        "/{entry_id}",
        response_model=CatalogEntryDetailResponse,
        summary="Update catalog entry",
    )
    def update_catalog_entry(
        entry_id: str,
        body: CatalogEntryUpdateRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.MCP_CATALOG_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type

        # Load from GLOBAL ∪ own, then layer the sysadmin check for globals.
        entry = db.query(MCPCatalogEntry).filter(
            MCPCatalogEntry.id == entry_id,
            _visibility_filter(admin, admin_type),
        ).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Catalog entry not found")

        is_global = entry.tenant_id is None
        if is_global:
            _require_platform_admin(admin, admin_type)

        # Apply updates for non-None fields
        update_data = body.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(entry, field, value)

        entry.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(entry)

        logger.info("Updated catalog entry", context={
            "tenant_id": entry.tenant_id, "entry_id": entry_id,
            "is_global": is_global,
        })

        audit.log(
            db=db,
            action="mcp_catalog_update",
            admin=admin,
            admin_type="system" if is_global else "tenant",
            tenant_id=None if is_global else entry.tenant_id,
            success=True,
            resource_type=TenantResourceTypes.MCP_CATALOG.value,
            resource_id=entry_id,
            details={"updated_fields": list(update_data.keys()), "is_global": is_global},
        )

        return CatalogEntryDetailResponse(**entry.to_dict(brief=False))

    # ========================================================================
    # DELETE — DELETE /admin/mcp-catalog/{entry_id}
    # ========================================================================
    @router.delete(
        "/{entry_id}",
        status_code=204,
        summary="Delete catalog entry",
    )
    def delete_catalog_entry(
        entry_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.MCP_CATALOG_DELETE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type

        entry = db.query(MCPCatalogEntry).filter(
            MCPCatalogEntry.id == entry_id,
            _visibility_filter(admin, admin_type),
        ).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Catalog entry not found")

        is_global = entry.tenant_id is None
        if is_global:
            _require_platform_admin(admin, admin_type)

        entry_name = entry.name
        entry_tenant_id = entry.tenant_id
        db.delete(entry)
        db.commit()

        logger.info("Deleted catalog entry", context={
            "tenant_id": entry_tenant_id, "entry_id": entry_id,
            "name": entry_name, "is_global": is_global,
        })

        audit.log(
            db=db,
            action="mcp_catalog_delete",
            admin=admin,
            admin_type="system" if is_global else "tenant",
            tenant_id=None if is_global else entry_tenant_id,
            success=True,
            resource_type=TenantResourceTypes.MCP_CATALOG.value,
            resource_id=entry_id,
            details={"name": entry_name, "is_global": is_global},
        )

    # ========================================================================
    # SIGN — POST /admin/mcp-catalog/{entry_id}/sign
    # ========================================================================
    @router.post(
        "/{entry_id}/sign",
        summary="Sign catalog entry with tenant Ed25519 key",
        description=(
            "Apply an Ed25519 signature over the entry's canonical-"
            "JSON body. Reuses the tenant's A2A signing key. Mutates "
            "the entry to trust_tier='tenant_signed'. Revoked entries "
            "cannot be re-signed."
        ),
    )
    def sign_catalog_entry_endpoint(
        entry_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin=Depends(require_permission(Permission.MCP_CATALOG_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        from app.services.mcp.catalog_signing import (
            CatalogSigningError,
            sign_catalog_entry as _sign,
        )
        tenant_id = acting_tenant_id(request, admin)

        entry = (
            db.query(MCPCatalogEntry)
            .filter(MCPCatalogEntry.id == entry_id)
            .filter(MCPCatalogEntry.tenant_id == tenant_id)
            .first()
        )
        if not entry:
            raise HTTPException(status_code=404, detail="Catalog entry not found")

        # The tenant's signing key is stored encrypted: signing reads it
        # with the router's encryption service.
        if encryption_service is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "The entry cannot be signed: this control plane has no encryption service "
                    "(ENCRYPTION_KEY) to read the tenant's signing key with."
                ),
            )
        try:
            _sign(
                db=db,
                entry=entry,
                signed_by=str(admin.id),
                encryption_service=encryption_service,
            )
        except CatalogSigningError as e:
            raise HTTPException(status_code=400, detail=str(e))

        db.commit()

        audit.log(
            db=db,
            action="mcp_catalog_sign",
            admin=admin,
            admin_type="tenant",
            tenant_id=tenant_id,
            success=True,
            resource_type=TenantResourceTypes.MCP_CATALOG.value,
            resource_id=entry_id,
            details={
                "signature_algorithm": entry.signature_algorithm,
                "trust_tier": entry.trust_tier,
            },
        )

        return CatalogEntryDetailResponse(**entry.to_dict(brief=False))

    # ========================================================================
    # VERIFY — GET /admin/mcp-catalog/{entry_id}/verify
    # ========================================================================
    @router.get(
        "/{entry_id}/verify",
        summary="Verify catalog entry signature",
        description=(
            "Read-only signature check. Returns "
            "{verified: bool, trust_tier: str}. Operators run this "
            "before trusting / importing a signed entry."
        ),
    )
    def verify_catalog_entry_endpoint(
        entry_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin=Depends(require_permission(Permission.MCP_CATALOG_READ)),
    ):
        from app.services.mcp.catalog_signing import (
            verify_catalog_entry_signature as _verify,
        )

        tenant_id = acting_tenant_id(request, admin)

        entry = (
            db.query(MCPCatalogEntry)
            .filter(MCPCatalogEntry.id == entry_id)
            .filter(MCPCatalogEntry.tenant_id == tenant_id)
            .first()
        )
        if not entry:
            raise HTTPException(status_code=404, detail="Catalog entry not found")

        return {
            "entry_id": entry.id,
            "verified": _verify(db=db, entry=entry),
            "trust_tier": entry.trust_tier or "unsigned",
            "signed_by": entry.signed_by,
            "signed_at": entry.signed_at.isoformat() if entry.signed_at else None,
            "signature_algorithm": entry.signature_algorithm,
        }

    # ========================================================================
    # REVOKE — POST /admin/mcp-catalog/{entry_id}/revoke
    # ========================================================================
    @router.post(
        "/{entry_id}/revoke",
        summary="Revoke catalog entry trust",
        description=(
            "Terminal state. Sets trust_tier='revoked'. The signature "
            "bytes stay on the row for forensic purposes but the "
            "entry can never be re-signed (operators clone + sign the "
            "clone instead)."
        ),
    )
    def revoke_catalog_entry_endpoint(
        entry_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin=Depends(require_permission(Permission.MCP_CATALOG_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        from app.services.mcp.catalog_signing import (
            revoke_catalog_entry as _revoke,
        )

        tenant_id = acting_tenant_id(request, admin)

        entry = (
            db.query(MCPCatalogEntry)
            .filter(MCPCatalogEntry.id == entry_id)
            .filter(MCPCatalogEntry.tenant_id == tenant_id)
            .first()
        )
        if not entry:
            raise HTTPException(status_code=404, detail="Catalog entry not found")

        _revoke(db=db, entry=entry, revoked_by=str(admin.id))
        db.commit()

        audit.log(
            db=db,
            action="mcp_catalog_revoke",
            admin=admin,
            admin_type="tenant",
            tenant_id=tenant_id,
            success=True,
            resource_type=TenantResourceTypes.MCP_CATALOG.value,
            resource_id=entry_id,
            details={},
        )

        return CatalogEntryDetailResponse(**entry.to_dict(brief=False))

    # ========================================================================
    # IMPORT — POST /admin/mcp-catalog/{entry_id}/import
    # ========================================================================
    @router.post(
        "/{entry_id}/import",
        response_model=CatalogImportResponse,
        summary="Import catalog entry as MCP server",
        description="Creates MCPServer + IdPServer + OAuthAppClient + ServerConfig from a catalog entry.",
    )
    def import_catalog_entry(
        entry_id: str,
        body: CatalogImportRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _perm=Depends(require_permission(Permission.MCP_CATALOG_IMPORT)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, admin_type = admin_and_type

        # Import materialises MCPServer/ServerConfig rows in the caller's
        # own tenant. The catalog entry itself may be GLOBAL or own-tenant
        # custom — both are resolvable, but the imported rows always land in
        # the caller's tenant. System admins have no home tenant to import
        # into, so the import path is tenant-admin only.
        tenant_id = _own_tenant_id(admin, admin_type)
        if tenant_id is None:
            raise HTTPException(
                status_code=400,
                detail="System admins have no tenant to import into; import as a tenant admin",
            )

        # ------------------------------------------------------------------
        # The containerised step (`deploy_container=true` on a containerized
        # entry) belongs to the Enterprise Edition's deployment feature and
        # is a behaviour hook (`composition/hooks.py`): a gate, asked here
        # before anything is written, and the install itself, further down.
        #
        # A composition without the hook cannot deploy. The caller asked for
        # a deployment by name, so the import is refused with the reason and
        # nothing is written — it is not imported "without the container"
        # behind the caller's back.
        #
        # Only the *deploy* leg is concerned: config-only imports
        # (deploy_container=false, and every remote entry, for which the
        # field has always been a no-op) are unaffected.
        # ------------------------------------------------------------------
        # The entry is looked up as the import itself resolves it (GLOBAL ∪
        # own-tenant): for an entry the caller cannot see, the gate has
        # nothing to say and the import answers 404 below.
        container_install = None
        if body.deploy_container:
            _entry_for_gate = db.query(MCPCatalogEntry).filter(
                MCPCatalogEntry.id == entry_id,
                _visibility_filter(admin, admin_type),
            ).first()
            if _entry_for_gate and _entry_for_gate.runtime == "containerized":
                container_gate = HOOKS.one(MCP_CATALOG_CONTAINER_GATE)
                container_install = HOOKS.one(MCP_CATALOG_CONTAINER_INSTALL)
                if container_gate is None or container_install is None:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": "enterprise_edition_feature",
                            "feature": DEPLOYMENT,
                            "message": (
                                "Containerised install is part of the Brutor "
                                "Enterprise Edition. This installation runs "
                                "the Community Edition, which does not deploy "
                                "MCP servers: import the entry without "
                                "deploying it, run the server yourself and "
                                "set its URL on the imported MCP server."
                            ),
                        },
                    )
                # Refuses (400) on the tenant's policy or when the
                # installation has no container backend.
                container_gate(db=db, tenant_id=tenant_id, logger=logger)

        # Resolve the entry from GLOBAL ∪ own-tenant.
        entry = db.query(MCPCatalogEntry).filter(
            MCPCatalogEntry.id == entry_id,
            _visibility_filter(admin, admin_type),
        ).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Catalog entry not found")

        # The MCP server quota counts enabled rows: an import that is
        # switched on is one more of them, asked as a creation is and
        # before anything is written. Imported switched off (the
        # default) it is not counted; switching it on asks.
        if body.enabled:
            TenantService.check_quota(db, tenant_id, TenantResourceTypes.MCP_SERVERS)

        server_name = body.name_override or entry.name
        creds = body.credentials

        # Generate IDs
        mcp_server_id = f"mcp-{ULID()}"
        server_config_id = f"sc-{ULID()}"
        idp_server_id = None
        oauth_client_id = None
        docker_deployment_id = None

        # ----------------------------------------------------------------
        # Determine base_url
        # ----------------------------------------------------------------
        base_url = ""
        mcp_endpoint_path = ""
        auth_type = "none"
        auth_token = None

        if entry.runtime == "remote":
            rc = entry.remote_config or {}
            if rc.get("fixedURL"):
                base_url = rc["fixedURL"]
            elif rc.get("hostname"):
                base_url = f"https://{rc['hostname']}"

            # Substitute URL template variables like {user}, {repo}
            # e.g. https://gitmcp.io/{user}/{repo} → https://gitmcp.io/facebook/react
            if "{" in base_url and creds and creds.url_vars:
                import re
                missing = []
                def _replace(match):
                    var_name = match.group(1)
                    value = creds.url_vars.get(var_name)
                    if not value:
                        missing.append(var_name)
                        return match.group(0)
                    return value
                base_url = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, base_url)
                if missing:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Missing URL template variables: {', '.join(missing)}",
                    )
            elif "{" in base_url:
                # URL has template vars but no substitutions provided
                import re
                required = re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", base_url)
                raise HTTPException(
                    status_code=400,
                    detail=f"URL has template variables that require values: {', '.join(required)}",
                )

            if entry.auth_pattern == "oauth":
                auth_type = "oauth"
            elif entry.auth_pattern == "header":
                auth_type = "bearer"
                # Extract token from credentials
                if creds and creds.api_key:
                    auth_token = creds.api_key
                elif creds and creds.header_values:
                    # Use first header value as bearer token
                    auth_token = next(iter(creds.header_values.values()), None)
            elif entry.auth_pattern == "api_key":
                auth_type = "bearer"
                if creds and creds.api_key:
                    auth_token = creds.api_key

        elif entry.runtime == "containerized":
            cc = entry.containerized_config or {}
            internal_port = cc.get("port", 8099)
            # Placeholder URL — updated after container deployment
            base_url = f"http://localhost:{internal_port}"
            mcp_endpoint_path = cc.get("path", "/")

        # A token that could not be stored encrypted: refused before
        # anything is written or deployed.
        server_token.require_storable(encryption_service, auth_token)

        # ----------------------------------------------------------------
        # Create IdPServer (OAuth entries only)
        # ----------------------------------------------------------------
        if entry.auth_pattern == "oauth" and entry.runtime == "remote":
            # One OIDC IdP per issuer and tenant: a second import of the
            # same server shares the IdP the first one created.
            idp = db.query(IdPServer).filter(
                IdPServer.tenant_id == tenant_id,
                IdPServer.protocol == "oidc",
                IdPServer.issuer == base_url,
            ).first()
            if idp is None:
                idp = IdPServer(
                    id=f"idp-{ULID()}",
                    tenant_id=tenant_id,
                    name=f"{server_name} IdP",
                    type="oidc_discoverable",
                    issuer=base_url,
                    enabled=True,
                )
                db.add(idp)
                db.flush()
            idp_server_id = idp.id

            # Create OAuthAppClient
            oauth_client_id = f"oauth-{ULID()}"
            client_id_value = "CONFIGURE_ME"
            client_secret_value = None

            if creds:
                if creds.client_id:
                    client_id_value = creds.client_id
                if creds.client_secret:
                    client_secret_value = creds.client_secret

            oauth_client = db.query(OAuthAppClient).filter(
                OAuthAppClient.tenant_id == tenant_id,
                OAuthAppClient.idp_id == idp_server_id,
                OAuthAppClient.client_id == client_id_value,
            ).first()
            if oauth_client is None:
                oauth_client = OAuthAppClient(
                    id=oauth_client_id,
                    tenant_id=tenant_id,
                    name=f"{server_name} OAuth Client",
                    idp_id=idp_server_id,
                    client_id=client_id_value,
                    client_type="public",
                    supports_pkce=True,
                    app_type="webapp",
                    redirect_uri=_get_portal_callback_url(),
                    is_active=True,
                )
                db.add(oauth_client)
                db.flush()
            oauth_client_id = oauth_client.id

        # ----------------------------------------------------------------
        # Create MCPServer
        # ----------------------------------------------------------------
        mcp_server = MCPServer(
            id=mcp_server_id,
            tenant_id=tenant_id,
            name=server_name,
            description=entry.short_description,
            base_url=base_url,
            mcp_endpoint_path=mcp_endpoint_path,
            auth_type=auth_type,
            auth_token=server_token.seal(encryption_service, auth_token),
            idp_id=idp_server_id,
            timeout=30.0,
            enabled=body.enabled,
        )
        db.add(mcp_server)
        db.flush()

        # ----------------------------------------------------------------
        # Create ServerConfig
        # ----------------------------------------------------------------
        # Find default resource group
        default_group = db.query(ResourceGroup).filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.is_system == True,
        ).first()

        server_config = ServerConfig(
            id=server_config_id,
            tenant_id=tenant_id,
            mcp_server_id=mcp_server_id,
            oauth_client_id=oauth_client_id,
            name=f"{server_name} Config",
            description=f"Imported from catalog: {entry.name}",
            display_name=server_name,
            display_description=entry.short_description,
            icon_url=entry.icon_url,
            is_default=True,
            enabled=body.enabled,
        )
        db.add(server_config)
        db.flush()

        # Bind to default resource group
        if default_group:
            from app.database.models.resource_group import resource_group_server_configs
            db.execute(
                resource_group_server_configs.insert().values(
                    resource_group_id=default_group.id,
                    server_config_id=server_config_id,
                )
            )

        # ----------------------------------------------------------------
        # Copy icon from catalog entry URL to MCP server binary icon
        # ----------------------------------------------------------------
        if entry.icon_url:
            try:
                from app.services.mcp.server.icon_manager import IconManager
                icon_mgr = IconManager()
                block_on(icon_mgr.save_icon_from_url(
                    server_id=mcp_server_id,
                    url=entry.icon_url,
                    db=db,
                ))
            except Exception as e:
                logger.warning("Failed to copy catalog icon to server (non-fatal)", context={
                    "icon_url": entry.icon_url, "error": str(e),
                })

        db.commit()

        # ----------------------------------------------------------------
        # Deploy the container (the Enterprise hook resolved above; None
        # unless a containerised install was asked for and let through).
        # A failure here is a *runtime* error (image pull failed, port
        # conflict, etc.) and is non-fatal: the hook does not raise, and the
        # MCP server row stays so the operator can retry.
        # ----------------------------------------------------------------
        installed = None
        if container_install is not None and entry.runtime == "containerized" and body.deploy_container:
            installed = container_install(
                db=db,
                entry=entry,
                mcp_server=mcp_server,
                tenant_id=tenant_id,
                env_values=(creds.env_values if creds and creds.env_values else {}),
                logger=logger,
            )
        container_deployed = bool(installed is not None and installed.deployed)
        if container_deployed:
            docker_deployment_id = installed.deployment_id

        # ----------------------------------------------------------------
        # Probe: discover capabilities. For a container just deployed, the
        # install's `diagnose` reads the cause of a failed probe from the
        # container's log.
        # ----------------------------------------------------------------
        probe = block_on(probe_capabilities(
            mcp_server=mcp_server,
            db=db,
            auth_type=entry.auth_pattern,
            enabled=mcp_server.enabled,
            skip_probe=not (entry.runtime == "remote" or container_deployed),
            diagnose=installed.diagnose if container_deployed else None,
            logger=logger,
            encryption_service=encryption_service,
        ))
        capabilities_discovered = probe.capabilities_discovered
        deployment_warnings = probe.deployment_warnings
        container_log_excerpt = probe.container_log_excerpt

        logger.info("Imported catalog entry as MCP server", context={
            "tenant_id": tenant_id,
            "catalog_entry_id": entry_id,
            "catalog_name": entry.name,
            "mcp_server_id": mcp_server_id,
            "server_config_id": server_config_id,
            "idp_server_id": idp_server_id,
            "oauth_client_id": oauth_client_id,
            "docker_deployment_id": docker_deployment_id,
            "runtime": entry.runtime,
            "auth_pattern": entry.auth_pattern,
            "container_deployed": container_deployed,
            "capabilities_discovered": capabilities_discovered,
        })

        audit.log(
            db=db,
            action="mcp_catalog_import",
            admin=admin,
            admin_type="tenant",
            tenant_id=tenant_id,
            success=True,
            resource_type=TenantResourceTypes.MCP_CATALOG.value,
            resource_id=entry_id,
            details={
                "catalog_name": entry.name,
                "mcp_server_id": mcp_server_id,
                "server_config_id": server_config_id,
                "idp_server_id": idp_server_id,
                "oauth_client_id": oauth_client_id,
                "runtime": entry.runtime,
                "auth_pattern": entry.auth_pattern,
            },
        )

        message = f"Successfully imported '{server_name}'"
        if container_deployed:
            message += " — container deployed"
        if capabilities_discovered:
            message += " with capabilities discovered"
        elif deployment_warnings:
            # Warnings present → soften the headline. The full reason
            # goes in `deployment_warnings` for the admin UI to render.
            message += " but the server is not responding correctly"
        if entry.auth_pattern == "oauth" and client_id_value == "CONFIGURE_ME":
            message += ". Configure OAuth client credentials before enabling."
        elif entry.runtime == "containerized" and not container_deployed:
            if HOOKS.one(MCP_CATALOG_CONTAINER_INSTALL) is not None:
                message += ". Container deployment failed — deploy manually from MCP Server Deployment."
            else:
                message += (
                    ". The server is not deployed: this edition does not run "
                    "containerised servers. Run it yourself and set its URL."
                )

        return CatalogImportResponse(
            mcp_server_id=mcp_server_id,
            idp_server_id=idp_server_id,
            oauth_client_id=oauth_client_id,
            server_config_id=server_config_id,
            docker_deployment_id=docker_deployment_id,
            message=message,
            deployment_warnings=deployment_warnings,
            container_log_excerpt=container_log_excerpt,
        )

    return router


def _get_portal_callback_url() -> str:
    """Get the OAuth callback URL for the portal."""
    return portal_oauth_callback_url()
