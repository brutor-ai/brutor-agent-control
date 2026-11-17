# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Identity Provider (IdP) Admin Endpoints with multi-tenant support and audit logging
"""

from ulid import ULID
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.sync_bridge import block_on
from app.constants.tenant import TenantResourceTypes
from app.services.tenant.tenant_service import TenantService
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.services.outbound_http import OutboundRefused, outbound_async_client
from app.services.outbound_policy import assert_allowed_url, refusal_message
from app.core.admin_user.dependencies import (
    get_audit_logger,
    require_permission,
    require_tenant_resource_access
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.admin_user.utils import extract_user_context
from app.composition.identity import IDENTITY_PROTOCOLS
from app.database.models.idp_server import IdPServer
from app.api.v1.models.admin.idp_server import (
    IdPConfigRequest,
    IdPConfigUpdateRequest,
    IdPDiscoveryResponse,
    IdPTestResponse,
    IdPListResponse,
    IdPDetailResponse,
    IdPInUseRefusal,
)
from app.middleware.trusted_proxies import client_ip


def _assert_idp_urls_allowed(config: Any) -> None:
    """The write-time SSRF guard (see outbound_policy), for a request model or a
    stored IdP. Test and discover fetch these URLs from the control plane, so a
    metadata-service address must not get as far as being saved or fetched.

    Every IdP field the control plane or the core fetches: the registered
    protocols say which of theirs hold one (`composition/identity.py`)."""
    for field in IDENTITY_PROTOCOLS.url_fields():
        url = getattr(config, field, None)
        if url:
            assert_allowed_url(url, field)


def _refuse_unavailable_protocol(idp: IdPServer, action: str) -> None:
    """Refuse (409) to act on a stored IdP whose protocol is not registered.

    Such a row belongs to a protocol this composition does not have. It is
    shown, so that the operator can see it is there, and that is all: it is
    not changed, deleted, tested or discovered, and above all it is not
    handled as if it spoke OIDC. A row with no protocol at all is refused the
    same way."""
    if IDENTITY_PROTOCOLS.get(idp.protocol) is not None:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "identity_protocol_unavailable",
            "protocol": idp.protocol,
            "message": (
                f"Identity Provider '{idp.name}' uses the identity protocol "
                f"{idp.protocol!r}, which is not available in this edition. "
                f"It cannot be {action} here."
            ),
        },
    )


# What can use an identity provider, as the refusal names it.
_USES = {
    "oauth_app_clients": "OAuth app clients (and the access configurations built on them)",
    "mcp_servers": "MCP servers",
    "agent_identities": "agent identities",
}


def _what_uses(db: Session, tenant_id: str, idp_id: str) -> Dict[str, list]:
    """The tenant's rows that depend on the identity provider, by kind:
    `{kind: [{"id", "name"}]}`, with the kinds nothing uses left out.

    Deleting the provider would delete its OAuth app clients (and with
    them the access configurations and their group bindings), and leave
    the MCP servers and agent identities that name it without one.
    """
    from app.database.models.agent_identity import AgentIdentity
    from app.database.models.mcp_server import MCPServer
    from app.database.models.oauth_app_client import OAuthAppClient

    kinds = {
        "oauth_app_clients": (OAuthAppClient, OAuthAppClient.idp_id),
        "mcp_servers": (MCPServer, MCPServer.idp_id),
        "agent_identities": (AgentIdentity, AgentIdentity.idp_server_id),
    }
    used_by: Dict[str, list] = {}
    for kind, (model, column) in kinds.items():
        rows = (
            db.query(model.id, model.name)
            .filter(model.tenant_id == tenant_id, column == idp_id)
            .order_by(model.name)
            .all()
        )
        if rows:
            used_by[kind] = [{"id": row.id, "name": row.name} for row in rows]
    return used_by


def create_idp_admin_router(logger: Optional[BrutorLogger] = None) -> APIRouter:
    """
    Create router for IdP admin endpoints with comprehensive audit logging

    Args:
        logger: Optional logger instance

    Returns:
        APIRouter configured with IdP endpoints
    """
    if logger is None:
        logger = BrutorLogger("idp-admin")

    router = APIRouter(prefix="/admin/idps", tags=["Identity Providers"])

    @router.get(
        "",
        response_model=IdPListResponse,
        summary="List all Identity Providers",
        description="Get a list of all configured Identity Providers for the tenant"
    )
    def list_idps(
        request: Request,
        enabled_only: bool = False,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.IDENTITY_PROVIDERS)),
        _: Any = Depends(require_permission(Permission.IDP_SERVER_READ)),
        db: Session = Depends(get_db)
    ) -> IdPListResponse:
        """
        List all Identity Providers

        Query Parameters:
        - enabled_only: If true, only return enabled IdPs

        Returns:
        - Dictionary of IdP configurations with metadata
        """
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            # Build query with tenant filter
            query = db.query(IdPServer).filter(
                IdPServer.tenant_id == tenant_id
            )

            if enabled_only:
                query = query.filter(IdPServer.enabled == True)

            idps = query.all()

            # Convert to response format
            idps_dict = {}
            enabled_count = 0

            for idp in idps:
                idp_data = idp.to_dict()
                idps_dict[idp.id] = idp_data

                if idp.enabled:
                    enabled_count += 1

            logger.debug(f"Listed {len(idps_dict)} IdPs for tenant", context={
                "tenant_id": tenant_id,
                "admin_type": admin_type.value,
                "enabled_only": enabled_only,
                "total": len(idps_dict),
                "enabled": enabled_count
            })

            return IdPListResponse(
                idps=idps_dict,
                total=len(idps_dict),
                enabled_count=enabled_count,
                accessed_by=extract_user_context(admin)
            )

        except Exception as e:
            logger.error("Failed to list IdPs", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list Identity Providers: {str(e)}"
            )


    @router.get(
        "/{idp_id}",
        response_model=IdPDetailResponse,
        summary="Get Identity Provider details",
        description="Get detailed information about a specific Identity Provider"
    )
    def get_idp(
        request: Request,
        idp_id: str,
        check_reachability: bool = False,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.IDENTITY_PROVIDERS)),
        _: Any = Depends(require_permission(Permission.IDP_SERVER_READ)),
        db: Session = Depends(get_db)
    ) -> IdPDetailResponse:
        """
        Get Identity Provider details

        Path Parameters:
        - idp_id: Identity Provider ID

        Query Parameters:
        - check_reachability: If true, test IdP connectivity

        Returns:
        - Detailed IdP configuration and status
        """
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            # Get IdP with tenant filter
            idp = db.query(IdPServer).filter(
                IdPServer.id == idp_id,
                IdPServer.tenant_id == tenant_id
            ).first()

            if not idp:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Identity Provider '{idp_id}' not found"
                )

            idp_data = idp.to_dict()

            # Optional reachability check
            is_reachable = None
            last_test_at = None

            # A row of a protocol that is not registered is shown, never probed.
            if check_reachability and idp.enabled and IDENTITY_PROTOCOLS.get(idp.protocol) is not None:
                try:
                    _assert_idp_urls_allowed(idp)
                    test_result = block_on(_test_idp_connectivity(idp, timeout=5))
                    is_reachable = test_result['connected']
                    last_test_at = datetime.now(timezone.utc).isoformat()
                except Exception as e:
                    logger.warning(f"IdP reachability check failed for {idp_id}", context={
                        "error": str(e)
                    })
                    is_reachable = False

            logger.debug(f"Retrieved IdP details: {idp_id}", context={
                "tenant_id": tenant_id,
                "admin_type": admin_type.value,
                "reachability_checked": check_reachability
            })

            return IdPDetailResponse(
                idp=idp_data,
                is_reachable=is_reachable,
                last_test_at=last_test_at,
                accessed_by=extract_user_context(admin)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to get IdP: {idp_id}", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to retrieve Identity Provider: {str(e)}"
            )


    @router.post(
        "",
        response_model=IdPDetailResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Create Identity Provider",
        description="Create a new Identity Provider configuration"
    )
    def create_idp(
        request: Request,
        idp_config: IdPConfigRequest,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.IDENTITY_PROVIDERS)),
        _: Any = Depends(require_permission(Permission.IDP_SERVER_CREATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        db: Session = Depends(get_db)
    ) -> IdPDetailResponse:
        """
        Create a new Identity Provider

        Request Body:
        - IdP configuration including issuer, endpoints, and OAuth settings

        Returns:
        - Created IdP configuration with metadata
        """
        admin, tenant_id, admin_type = admin_and_tenant
        _assert_idp_urls_allowed(idp_config)

        try:
            # Check if IdP with same issuer already exists for this tenant
            existing = db.query(IdPServer).filter(
                IdPServer.tenant_id == tenant_id,
                IdPServer.issuer == idp_config.issuer
            ).first()

            if existing:
                # Audit duplicate creation attempt
                audit.log(
                    action="idp_create",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=tenant_id,
                    success=False,
                    resource_type="idp_server",
                    details={
                        "error": "IdP with this issuer already exists",
                        "issuer": idp_config.issuer,
                        "existing_id": existing.id,
                        "existing_name": existing.name
                    },
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )

                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Identity Provider with issuer '{idp_config.issuer}' already exists"
                )

            # Generate IdP ID, Result: 'idp-01ARZ3NDEKTSV4RRFFQ69G5FAV'
            idp_id = f"idp-{ULID()}"

            # Create new Identity Provider. The request model has enforced
            # protocol/field consistency, so the validated payload is
            # trusted here. The columns of the common part and of OIDC are
            # written below; what another registered protocol adds to a new
            # row comes from the registry.
            idp = IdPServer(
                id=idp_id,
                tenant_id=tenant_id,
                name=idp_config.name,
                issuer=idp_config.issuer,
                type=idp_config.type,
                protocol=idp_config.protocol,
                well_known_url=idp_config.well_known_url,
                authorization_endpoint=idp_config.authorization_endpoint,
                token_endpoint=idp_config.token_endpoint,
                revocation_endpoint=idp_config.revocation_endpoint,
                introspection_endpoint=idp_config.introspection_endpoint,
                jwks_uri=idp_config.jwks_uri,
                userinfo_endpoint=idp_config.userinfo_endpoint,
                supports_pkce=idp_config.supports_pkce,
                supports_refresh_tokens=idp_config.supports_refresh_tokens,
                supports_client_credentials=idp_config.supports_client_credentials,
                token_endpoint_auth_methods=idp_config.token_endpoint_auth_methods,
                scopes_supported=idp_config.scopes_supported,
                response_types_supported=idp_config.response_types_supported,
                grant_types_supported=idp_config.grant_types_supported,
                discovery_cache_ttl=idp_config.discovery_cache_ttl,
                enabled=idp_config.enabled,
                verified=idp_config.verified,
                description=idp_config.description,
                logo_url=idp_config.logo_url,
                # None/[] means this IdP authenticates no workloads.
                workload_audiences=idp_config.workload_audiences or None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                **IDENTITY_PROTOCOLS.create_columns(idp_config),
            )

            db.add(idp)
            db.flush()
            db.refresh(idp)

            logger.info(f"Created IdP: {idp.name}", context={
                "idp_id": idp.id,
                "tenant_id": tenant_id,
                "admin_type": admin_type.value,
                "issuer": idp.issuer
            })

            # Audit successful creation
            audit.log(
                action="idp_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type="idp_server",
                resource_id=idp.id,
                details={
                    "idp_name": idp.name,
                    "issuer": idp.issuer,
                    "enabled": idp.enabled,
                    "supports_pkce": idp.supports_pkce
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return IdPDetailResponse(
                idp=idp.to_dict(),
                is_reachable=None,
                last_test_at=None,
                accessed_by=extract_user_context(admin)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to create IdP", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            # Audit failed creation
            audit.log(
                action="idp_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=False,
                resource_type="idp_server",
                details={
                    "error": str(e),
                    "idp_name": idp_config.name
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create Identity Provider: {str(e)}"
            )


    @router.patch(
        "/{idp_id}",
        response_model=IdPDetailResponse,
        summary="Update Identity Provider",
        description="Update an existing Identity Provider configuration"
    )
    def update_idp(
        request: Request,
        idp_id: str,
        idp_update: IdPConfigUpdateRequest,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.IDENTITY_PROVIDERS)),
        _: Any = Depends(require_permission(Permission.IDP_SERVER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        db: Session = Depends(get_db)
    ) -> IdPDetailResponse:
        """
        Update Identity Provider configuration

        Path Parameters:
        - idp_id: Identity Provider ID

        Request Body:
        - Partial IdP configuration to update

        Returns:
        - Updated IdP configuration
        """
        admin, tenant_id, admin_type = admin_and_tenant
        _assert_idp_urls_allowed(idp_update)

        try:
            # Get IdP with tenant filter
            idp = db.query(IdPServer).filter(
                IdPServer.id == idp_id,
                IdPServer.tenant_id == tenant_id
            ).first()

            if not idp:
                # Audit failed access attempt
                audit.log(
                    action="idp_update",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=tenant_id,
                    success=False,
                    resource_type="idp_server",
                    resource_id=idp_id,
                    details={"error": "IdP not found"},
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )

                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Identity Provider '{idp_id}' not found"
                )

            _refuse_unavailable_protocol(idp, "changed")

            # Track changes for audit
            changes = {}

            # Update fields if provided. Only the common fields and those of
            # a registered protocol are written: the request model has
            # refused any other that carried a value.
            owned = IDENTITY_PROTOCOLS.owned_fields()
            update_data = idp_update.dict(exclude_unset=True)
            # `"protocol": null` names no protocol: the row keeps its own.
            if update_data.get("protocol") is None:
                update_data.pop("protocol", None)

            # What was sent must belong to
            # the protocol the row has, or is given by this request.
            try:
                IDENTITY_PROTOCOLS.validate_update(
                    {column.name: getattr(idp, column.name) for column in IdPServer.__table__.columns},
                    update_data,
                )
            except ValueError as refusal:
                audit.log(
                    action="idp_update",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=tenant_id,
                    success=False,
                    resource_type="idp_server",
                    resource_id=idp_id,
                    details={"error": str(refusal)},
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )
                raise HTTPException(status_code=422, detail=str(refusal))

            for field, value in update_data.items():
                if field in owned and hasattr(idp, field):
                    old_value = getattr(idp, field)
                    if old_value != value:
                        changes[field] = {"old": old_value, "new": value}
                        setattr(idp, field, value)

            if changes:
                idp.updated_at = datetime.now(timezone.utc)
                db.flush()
                db.refresh(idp)

                logger.info(f"Updated IdP: {idp.name}", context={
                    "idp_id": idp_id,
                    "tenant_id": tenant_id,
                    "admin_type": admin_type.value,
                    "changes": list(changes.keys())
                })

                # Audit successful update
                audit.log(
                    action="idp_update",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=tenant_id,
                    success=True,
                    resource_type="idp_server",
                    resource_id=idp_id,
                    details={
                        "idp_name": idp.name,
                        "changes": changes
                    },
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )
            else:
                logger.debug(f"No changes for IdP: {idp_id}")

            return IdPDetailResponse(
                idp=idp.to_dict(),
                is_reachable=None,
                last_test_at=None,
                accessed_by=extract_user_context(admin)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update IdP: {idp_id}", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            # Audit failed update
            audit.log(
                action="idp_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=False,
                resource_type="idp_server",
                resource_id=idp_id,
                details={"error": str(e)},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update Identity Provider: {str(e)}"
            )


    @router.delete(
        "/{idp_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Delete Identity Provider",
        description=(
            "Delete an Identity Provider configuration. Refused with 409 while "
            "it is in use: by an OAuth app client (and the access configurations "
            "built on it), an MCP server or an agent identity of the tenant. The "
            "response lists them under `detail.used_by`."
        ),
        responses={
            409: {
                "model": IdPInUseRefusal,
                "description": "The Identity Provider is in use; `detail.used_by` lists what uses it.",
            }
        },
    )
    def delete_idp(
        request: Request,
        idp_id: str,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.IDENTITY_PROVIDERS)),
        _: Any = Depends(require_permission(Permission.IDP_SERVER_DELETE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        db: Session = Depends(get_db)
    ):
        """
        Delete Identity Provider

        Path Parameters:
        - idp_id: Identity Provider ID

        Returns:
        - 204 No Content on success
        """
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            # Get IdP with tenant filter
            idp = db.query(IdPServer).filter(
                IdPServer.id == idp_id,
                IdPServer.tenant_id == tenant_id
            ).first()

            if not idp:
                # Audit failed deletion attempt
                audit.log(
                    action="idp_delete",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=tenant_id,
                    success=False,
                    resource_type="idp_server",
                    resource_id=idp_id,
                    details={"error": "IdP not found"},
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )

                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Identity Provider '{idp_id}' not found"
                )

            _refuse_unavailable_protocol(idp, "deleted")

            used_by = _what_uses(db, tenant_id, idp_id)
            if used_by:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": (
                            f"Identity Provider '{idp.name}' is in use and is not deleted: "
                            + "; ".join(
                                f"{_USES[kind]}: {', '.join(row['name'] for row in rows)}"
                                for kind, rows in used_by.items()
                            )
                            + ". Remove or re-point these first."
                        ),
                        "used_by": used_by,
                    },
                )

            # Store info for audit before deletion
            idp_name = idp.name
            idp_issuer = idp.issuer

            # Delete IdP
            db.delete(idp)
            db.flush()

            logger.info(f"Deleted IdP: {idp_name}", context={
                "idp_id": idp_id,
                "tenant_id": tenant_id,
                "admin_type": admin_type
            })

            # Audit successful deletion
            audit.log(
                action="idp_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type="idp_server",
                resource_id=idp_id,
                details={
                    "idp_name": idp_name,
                    "issuer": idp_issuer
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return None

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to delete IdP: {idp_id}", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            # Audit failed deletion
            audit.log(
                action="idp_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=False,
                resource_type="idp_server",
                resource_id=idp_id,
                details={"error": str(e)},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete Identity Provider: {str(e)}"
            )


    @router.post(
        "/{idp_id}/test",
        response_model=IdPTestResponse,
        summary="Test Identity Provider",
        description="Test connectivity and configuration of an Identity Provider"
    )
    def test_idp(
        request: Request,
        idp_id: str,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.IDENTITY_PROVIDERS)),
        _: Any = Depends(require_permission(Permission.IDP_SERVER_TEST)),
        db: Session = Depends(get_db)
    ) -> IdPTestResponse:
        """
        Test Identity Provider connectivity and configuration

        Path Parameters:
        - idp_id: Identity Provider ID

        Returns:
        - Test results including connectivity and endpoint availability
        """
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            # Get IdP with tenant filter
            idp = db.query(IdPServer).filter(
                IdPServer.id == idp_id,
                IdPServer.tenant_id == tenant_id
            ).first()

            if not idp:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Identity Provider '{idp_id}' not found"
                )

            _refuse_unavailable_protocol(idp, "tested")
            _assert_idp_urls_allowed(idp)

            # Perform connectivity test
            test_result = block_on(_test_idp_connectivity(idp, timeout=10))

            # Perform OIDC discovery if available
            discovery_result = None
            if idp.well_known_url:
                discovery_result = block_on(_perform_oidc_discovery(idp, timeout=10))

            logger.debug(f"Tested IdP: {idp.name}", context={
                "idp_id": idp_id,
                "tenant_id": tenant_id,
                "admin_type": admin_type.value,
                "connected": test_result.get('connected', False)
            })

            return IdPTestResponse(
                idp_id=idp_id,
                idp_name=idp.name,
                connected=test_result.get('connected', False),
                response_time_ms=test_result.get('response_time_ms'),
                endpoints_available=test_result.get('endpoints_available', {}),
                discovery_works=test_result.get('discovery_works', False),
                error=test_result.get('error'),
                tested_at=datetime.now(timezone.utc).isoformat(),
                tested_by=extract_user_context(admin)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Failed to test IdP: {idp_id}", context={
                "tenant_id": tenant_id,
                "error": str(e)
            })

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to test Identity Provider: {str(e)}"
            )


    @router.post(
        "/{idp_id}/discover",
        response_model=IdPDiscoveryResponse,
        summary="Perform OIDC Discovery",
        description="Perform OpenID Connect discovery for an Identity Provider"
    )
    def discover_idp(
        request: Request,
        idp_id: str,
        update_config: bool = False,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.IDENTITY_PROVIDERS)),
        _: Any = Depends(require_permission(Permission.IDP_SERVER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
        db: Session = Depends(get_db)
    ) -> IdPDiscoveryResponse:
        """
        Perform OIDC discovery and optionally update IdP configuration

        Path Parameters:
        - idp_id: Identity Provider ID

        Query Parameters:
        - update_config: If true, update IdP config with discovered values

        Returns:
        - Discovery results and updated configuration
        """
        admin, tenant_id, admin_type = admin_and_tenant

        try:
            # Get IdP with tenant filter
            idp = db.query(IdPServer).filter(
                IdPServer.id == idp_id,
                IdPServer.tenant_id == tenant_id
            ).first()

            if not idp:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Identity Provider '{idp_id}' not found"
                )

            _refuse_unavailable_protocol(idp, "discovered")

            if not idp.well_known_url:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="IdP does not have a well-known URL configured"
                )

            _assert_idp_urls_allowed(idp)

            # Perform discovery
            discovery_result = block_on(_perform_oidc_discovery(idp, timeout=10))

            if discovery_result.get('refused'):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=discovery_result['error'],
                )
            if not discovery_result['success']:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Discovery failed: {discovery_result.get('error')}"
                )

            metadata = discovery_result['metadata']
            updates_applied = {}

            # Update IdP config if requested
            if update_config:
                if metadata.get('authorization_endpoint'):
                    idp.authorization_endpoint = metadata['authorization_endpoint']
                    updates_applied['authorization_endpoint'] = metadata['authorization_endpoint']

                if metadata.get('token_endpoint'):
                    idp.token_endpoint = metadata['token_endpoint']
                    updates_applied['token_endpoint'] = metadata['token_endpoint']

                if metadata.get('userinfo_endpoint'):
                    idp.userinfo_endpoint = metadata['userinfo_endpoint']
                    updates_applied['userinfo_endpoint'] = metadata['userinfo_endpoint']

                if metadata.get('jwks_uri'):
                    idp.jwks_uri = metadata['jwks_uri']
                    updates_applied['jwks_uri'] = metadata['jwks_uri']

                if metadata.get('scopes_supported'):
                    idp.scopes_supported = metadata['scopes_supported']
                    updates_applied['scopes_supported'] = metadata['scopes_supported']

                # What an IdP advertises gets fetched later, so it answers to
                # the same policy as a URL an admin typed.
                _assert_idp_urls_allowed(idp)

                idp.updated_at = datetime.now(timezone.utc)
                db.flush()
                db.refresh(idp)

                # Audit successful discovery-driven config update
                audit.log(
                    action="idp_discover_update",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=tenant_id,
                    success=True,
                    resource_type="idp_server",
                    resource_id=idp_id,
                    details={
                        "idp_name": idp.name,
                        "updated_fields": list(updates_applied.keys())
                    },
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )

            logger.debug(f"Performed discovery for IdP: {idp.name}", context={
                "idp_id": idp_id,
                "tenant_id": tenant_id,
                "admin_type": admin_type.value,
                "update_config": update_config,
                "updates_count": len(updates_applied)
            })

            return IdPDiscoveryResponse(
                idp_id=idp_id,
                idp_name=idp.name,
                discovery_successful=True,
                authorization_endpoint=idp.authorization_endpoint,
                token_endpoint=idp.token_endpoint,
                jwks_uri=idp.jwks_uri,
                userinfo_endpoint=idp.userinfo_endpoint,
                scopes_supported=idp.scopes_supported,
                supports_pkce=idp.supports_pkce,
                supports_client_credentials=idp.supports_client_credentials,
                supports_refresh_tokens=idp.supports_refresh_tokens,
                cache_ttl_seconds=idp.discovery_cache_ttl,
                discovered_at=datetime.now(timezone.utc).isoformat()
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Failed to discover IdP: {idp_id}", context={
                "tenant_id": tenant_id,
                "error": str(e)
            })

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to perform OIDC discovery: {str(e)}"
            )


    # Helper functions remain the same
    async def _test_idp_connectivity(idp: IdPServer, timeout: int = 10) -> Dict[str, Any]:
        """
        Test IdP connectivity

        Args:
            idp: Identity Provider instance
            timeout: Request timeout

        Returns:
            Connectivity test results
        """
        start_time = time.time()

        result = {
            'connected': False,
            'endpoints_available': {},
            'discovery_works': False,
            'response_time_ms': 0,
            'error': None
        }

        def refused(url: str, e: Exception) -> None:
            # The outbound policy's refusal is said, not just counted as down.
            if isinstance(e, OutboundRefused) and not result['error']:
                result['error'] = refusal_message(url, "Identity provider URL", e.reason)

        try:
            async with outbound_async_client(timeout=timeout) as client:
                # Test well-known endpoint
                if idp.well_known_url:
                    try:
                        response = await client.get(idp.well_known_url)
                        if response.status_code == 200:
                            result['discovery_works'] = True
                            result['endpoints_available']['well_known'] = True
                        else:
                            result['endpoints_available']['well_known'] = False
                    except Exception as e:
                        result['endpoints_available']['well_known'] = False
                        refused(idp.well_known_url, e)
                        logger.debug(f"Well-known endpoint test failed: {str(e)}")

                # Test authorization endpoint
                if idp.authorization_endpoint:
                    try:
                        response = await client.get(idp.authorization_endpoint)
                        result['endpoints_available']['authorization'] = response.status_code < 500
                    except Exception as e:
                        result['endpoints_available']['authorization'] = False
                        refused(idp.authorization_endpoint, e)

                # Test token endpoint
                if idp.token_endpoint:
                    try:
                        response = await client.post(idp.token_endpoint, json={})
                        # Expect 4xx (bad request) not 5xx (server error)
                        result['endpoints_available']['token'] = response.status_code < 500
                    except Exception as e:
                        result['endpoints_available']['token'] = False
                        refused(idp.token_endpoint, e)

                # Overall connectivity
                result['connected'] = any(result['endpoints_available'].values())

        except Exception as e:
            result['error'] = str(e)
            logger.debug(f"IdP connectivity test failed: {str(e)}")

        result['response_time_ms'] = int((time.time() - start_time) * 1000)

        return result

    async def _perform_oidc_discovery(idp: IdPServer, timeout: int = 10) -> Dict[str, Any]:
        """
        Perform OIDC discovery for IdP

        Args:
            idp: Identity Provider instance
            timeout: Request timeout

        Returns:
            Discovery results
        """
        result = {
            'success': False,
            'metadata': {},
            'error': None
        }

        if not idp.well_known_url:
            result['error'] = "No well-known URL configured"
            return result

        try:
            async with outbound_async_client(timeout=timeout) as client:
                response = await client.get(idp.well_known_url)

                if response.status_code == 200:
                    metadata = response.json()
                    result['success'] = True
                    result['metadata'] = metadata
                else:
                    result['error'] = f"Discovery failed with status {response.status_code}"

        except OutboundRefused as e:
            result['error'] = refusal_message(idp.well_known_url, "Identity provider URL", e.reason)
            result['refused'] = True
        except Exception as e:
            result['error'] = str(e)
            logger.debug(f"OIDC discovery failed: {str(e)}")

        return result

    return router