# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

from app.core.sync_bridge import block_on
from app.database.models import IdPServer

"""
MCP Server Management Endpoints 
Multi-Tenant with Audit Logging
"""

from ulid import ULID
from datetime import datetime, timezone
from typing import Any, Union, Optional, List

from fastapi import (APIRouter, HTTPException, Depends, Query, Request, UploadFile, File, Form)
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.constants.tenant import TenantResourceTypes
from app.constants.admin import AdminType
from app.database import get_db
from app.services.outbound_http import error_text
from app.services.outbound_policy import assert_allowed_url
from app.database.models.mcp_server import MCPServer, BEARER_AUTH_TYPE

TOKEN_IS_A_BEARER_SERVERS = (
    "auth_token is the static token of a server with auth_type 'bearer': it is not stored for "
    "another auth type. Send auth_type 'bearer' with it, or leave it out. (Saving a server with "
    "another auth type removes a token stored for it.)"
)
from app.database.models.virtual_server import VirtualServer
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.composition.hooks import HOOKS, MCP_SERVER_DEPLOYMENT_TEARDOWN
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    acting_tenant_id,
    require_permission,
    get_audit_logger,
    require_tenant_resource_access,
    get_current_admin,
    tenant_scope,
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.api.v1.models.admin.mcp_server import (ServerConnectionTestRequest, ServerCreateRequest, ServerUpdateRequest,
                                                ServerListResponse, ServerDetailResponse, ServerCreateResponse,
                                                ServerUpdateResponse, ConnectionTestResponse,
                                                MCPServerHealthStatusResponse, MCPServerBulkHealthCheckResponse,
                                                RefreshCapabilitiesResponse, PoisonedToolFinding)
from app.services.mcp.server import server_token
from app.services.mcp.server.mcp_server_manager import MCPServerManager
from app.services.mcp.server.icon_manager import IconManager
from app.services.tenant.tenant_service import TenantService
from app.services.cache_invalidation import invalidate_proxy_cache
from app.api.v1.models.admin.in_use import IN_USE_RESPONSE, in_use
from app.core.logging_system import BrutorLogger
from app.middleware.trusted_proxies import client_ip


def _require_tenant_idp(db: Session, tenant_id: str, idp_id: Optional[str]) -> None:
    """An `idp_id` names an identity provider of the tenant, or the save
    is refused: the gateway pairs a server with its tenant's providers
    only, so any other value can never be used."""
    from app.services.mcp.server.oauth_pairing import tenant_has_idp

    if idp_id and not tenant_has_idp(db, tenant_id, idp_id):
        raise HTTPException(status_code=400, detail=f"Identity provider '{idp_id}' not found")


def create_mcp_server_router(
    server_manager: MCPServerManager,
    logger: BrutorLogger,
    encryption_service: Any = None,
) -> APIRouter:
    """Create router for multi-tenant MCP server management endpoints.

    `encryption_service` encrypts a server's static token before it is
    stored; without one, a request that carries a token is refused (503).
    """
    router = APIRouter(prefix="/admin/mcp-servers", tags=["MCP Servers"])
    icon_manager = IconManager(logger)

    @router.post(
        "/test-connection",
        response_model=ConnectionTestResponse,
        summary="Test connection to MCP server",
        description="""
        Test connection to an MCP server with OAuth/OIDC discovery.
        
        Tenant isolation:
        - Tenant admins: Test connections for their tenant
        - System admins: Must specify tenant_id
        """
    )
    def test_server_connection(
        test_request: ServerConnectionTestRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.MCP_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.MCP_SERVER_TEST))
    ):
        """Test connection to an MCP server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        if not test_request.url:
            raise HTTPException(status_code=400, detail="URL is required")

        logger.debug("Testing server connection", context={
            "admin_id": admin_id,
            "admin_type": admin_type_value,
            "tenant_id": tenant_id,
            "server_url": test_request.url
        })

        try:
            assert_allowed_url(test_request.url, "MCP server URL")
            # Create temporary server object for testing
            temp_server = MCPServer(
                id="test-connection",
                tenant_id=tenant_id,
                name="Test Connection",
                base_url=test_request.url,
                mcp_endpoint_path="",
                auth_type=test_request.auth_type,
                timeout=test_request.timeout,
                enabled=True
            )

            # Test connection to MCP Server without access token, see how much info we can get via WWW-Authenicate and
            # discovery endpoints
            result = block_on(server_manager.test_connection(
                server=temp_server,
                timeout=test_request.timeout,
                db=db
            ))

            logger.debug("Server connection test completed", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "status": result["status"]
            })

            return result

        except Exception as e:
            logger.warning("Server connection test failed", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "error": str(e)
            })

            raise HTTPException(
                status_code=500,
                detail=f"Connection test failed: {str(e)}"
            )

    @router.get(
        "",
        response_model=ServerListResponse,
        summary="List MCP servers",
        description="""
        List MCP servers.
        
        Tenant isolation:
        - Tenant admins: Only see their tenant's servers
        - System admins: See all servers or filter by tenant_id
        """
    )
    def list_servers(
        request: Request,
        tenant_id: Optional[str] = Query(None, description="Filter by tenant (system admins only)"),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.MCP_SERVER_READ))
    ):
        """List MCP servers with tenant isolation"""
        admin, admin_type = admin_and_type

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Build query with LEFT JOIN to include IdP name
            query = db.query(
                MCPServer,
                IdPServer.name.label('idp_name')
            ).outerjoin(
                IdPServer,
                MCPServer.idp_id == IdPServer.id
            )

            # Tenant admins only see their tenant's servers; system admins
            # can filter by tenant or see all; a type nobody registered
            # sees none (403).
            effective_tenant_id = tenant_scope(admin, admin_type, tenant_id)
            if effective_tenant_id:
                query = query.filter(MCPServer.tenant_id == effective_tenant_id)

            # Execute query and get results with IdP names
            results = query.all()

            logger.debug("MCP servers listed", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": effective_tenant_id,
                "server_count": len(results)
            })

            # Build response dictionary
            return ServerListResponse(
                servers={
                    server.id: server_manager.to_response(server, idp_name=idp_name)
                    for server, idp_name in results
                },
                total=len(results)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to list servers", context={
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to list servers: {str(e)}"
            )

    @router.get(
        "/{server_id}",
        response_model=ServerDetailResponse,
        summary="Get server details",
        description="""
        Get MCP server details.

        Tenant isolation enforced automatically.
        """
    )
    def get_server(
            server_id: str,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.MCP_SERVERS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.MCP_SERVER_READ))
    ):
        """Get MCP server details with tenant verification"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query with LEFT JOIN to include IdP name
            result = db.query(
                MCPServer,
                IdPServer.name.label('idp_name')
            ).outerjoin(
                IdPServer,
                MCPServer.idp_id == IdPServer.id
            ).filter(
                MCPServer.id == server_id,
                MCPServer.tenant_id == tenant_id
            ).first()

            if not result:
                raise HTTPException(
                    status_code=404,
                    detail=f"Server {server_id} not found"
                )

            # Unpack the result
            server, idp_name = result

            logger.debug("MCP server details accessed", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "server_id": server_id
            })

            return ServerDetailResponse(
                server=server_manager.to_response(server, idp_name=idp_name)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get server", context={
                "server_id": server_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to get server: {str(e)}"
            )

    @router.post(
        "",
        response_model=ServerCreateResponse,
        status_code=201,
        summary="Create new MCP server",
        description="""
            Create new MCP server.

            Tenant isolation:
            - Tenant admins: Server created in their tenant
            - System admins: Must specify tenant_id
            """
    )
    def create_server(
            server_request: ServerCreateRequest,
            request: Request,
            discover_capabilities: bool = Query(
                True, description="Automatically discover server capabilities"
            ),
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.MCP_SERVERS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.MCP_SERVER_CREATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Create new MCP server with tenant context"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Feature + quota gates
            TenantService.check_feature_access(db, tenant_id, 'mcp_servers')
            TenantService.check_quota(db, tenant_id, TenantResourceTypes.MCP_SERVERS)
            assert_allowed_url(server_request.base_url, "MCP server base URL")
            _require_tenant_idp(db, tenant_id, server_request.idp_id)

            # Check if MCP server already exists in this tenant
            # It is considered that the server already exists if it has a matching base URL + MCP protocol endpoint path
            # and is in the same tenant.
            # If it does, return 409 (Conflict) with a message indicating the server already exists
            existing = db.query(MCPServer).filter_by(
                base_url=server_request.base_url,
                mcp_endpoint_path=server_request.mcp_endpoint_path,
                tenant_id=tenant_id
            ).first()

            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"Server '{server_request.base_url}/{server_request.mcp_endpoint_path}' already exists in this tenant"
                )

            logger.debug("Server creation requested", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "base_url": server_request.base_url,
                "mcp_endpoint_path": server_request.mcp_endpoint_path,
                "ip_address": client_ip(request)
            })

            # Encrypted before anything is written; refused (503) where
            # this control plane cannot encrypt.
            # A token is a bearer server's: one sent for another auth type
            # is refused, not dropped.
            if server_request.auth_token and server_request.auth_type != BEARER_AUTH_TYPE:
                raise HTTPException(status_code=422, detail=TOKEN_IS_A_BEARER_SERVERS)
            stored_token = server_token.seal(encryption_service, server_request.auth_token)

            # Generate MCP server ID, Result: 'mcp-01ARZ3NDEKTSV4RRFFQ69G5FAV'
            server_id = f"mcp-{ULID()}"

            # Create server with tenant_id
            server = MCPServer(
                id=server_id,
                tenant_id=tenant_id,
                name=server_request.name,
                description=server_request.description,
                base_url=server_request.base_url,
                mcp_endpoint_path=server_request.mcp_endpoint_path,
                auth_type=server_request.auth_type,
                auth_token=stored_token,
                idp_id=server_request.idp_id,
                timeout=server_request.timeout,
                max_concurrent_requests=server_request.max_concurrent_requests,
                enabled=server_request.enabled,
                mcp_apps_enabled=server_request.mcp_apps_enabled,
                # Seed wants_listener from the operator's
                # choice; the first capability discovery
                # overwrites it with the gateway-derived value.
                # Safe to take the create-form default here
                # because the column is NOT NULL.
                wants_listener=server_request.wants_listener,
                tenant_limits=server_request.tenant_limits,
                tenant_governance=server_request.tenant_governance,
                # Data residency. The Rust residency check reads the
                # column: a server without it is unscoped and a
                # block_global tenant cannot use it.
                region=server_request.region,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
                # Passed only when supplied, so an omitted version falls
                # through to the column default ("1.0") rather than NULL.
                **({"version": server_request.version} if server_request.version else {}),
            )

            db.add(server)
            db.flush()
            db.refresh(server)

            # Optionally discover capabilities, only useful for non-oauth servers
            capabilities_discovered = False
            poison_findings: list = []
            if discover_capabilities and server.enabled:
                try:
                    capabilities_discovered = block_on(server_manager.discover_capabilities(
                        server=server,
                        db=db,
                        out_poison_findings=poison_findings,
                    ))
                except Exception as e:
                    logger.warning("Capability discovery failed during creation", context={
                        "server_id": server.id,
                        "error": error_text(e, server.mcp_url, "MCP server URL")
                    })

            logger.info("Server created successfully", context={
                "server_id": server.id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "capabilities_discovered": capabilities_discovered
            })

            # Audit successful creation
            audit.log(
                action="mcp_server_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.MCP_SERVERS.value,
                resource_id=server.id,
                details={
                    "server_name": server.name,
                    "server_url": server.mcp_url,
                    "auth_type": server.auth_type,
                    "enabled": server.enabled,
                    "capabilities_discovered": capabilities_discovered
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "mcp_server", tenant_id, server.id, "create"))

            # Surface THREAT-01 findings inline so the admin UI
            # can show a warning modal on click. The findings are
            # also written as system_alert rows asynchronously
            # by the scanner; this payload field is the synchronous
            # signal.
            from app.api.v1.models.admin.mcp_server import PoisonedToolFinding
            poison_payload = [
                PoisonedToolFinding(
                    tool_name=h.tool_name,
                    field_kind=h.field_kind,
                    description_excerpt=h.description[:500],
                    description_truncated=len(h.description) > 500,
                    confidence=round(h.confidence, 3),
                    patterns_matched=len(h.patterns_matched),
                    suspicious_indicator_count=h.suspicious_indicator_count,
                )
                for h in poison_findings
            ]
            msg = "Server created successfully"
            if poison_payload:
                msg = (
                    f"Server created — {len(poison_payload)} tool metadata field(s) "
                    "tripped the prompt-injection detector "
                    f"(name / title / description across {len({p.tool_name for p in poison_payload})} tool(s)). "
                    "See `poisoned_tools` for details and review in Mission Control alerts."
                )

            return ServerCreateResponse(
                server=server_manager.to_response(server),
                message=msg,
                poisoned_tools=poison_payload,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Server creation failed", context={
                "server_id": server_request.id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to create server: {str(e)}"
            )

    @router.patch(
        "/{server_id}",
        response_model=ServerUpdateResponse,
        summary="Update MCP server",
        description="""
            Update MCP server configuration.

            Tenant isolation enforced automatically.
            """
    )
    def update_server(
            server_id: str,
            server_update: ServerUpdateRequest,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.MCP_SERVERS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.MCP_SERVER_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update MCP server configuration"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_tenant_id = acting_tenant_id(request, admin)
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Filter by tenant_id in query
            server = db.query(MCPServer).filter(
                MCPServer.id == server_id,
                MCPServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Server {server_id} not found"
                )

            # Track changes for audit
            changes = {}
            update_data = server_update.dict(exclude_unset=True)

            _require_tenant_idp(db, tenant_id, update_data.get("idp_id"))

            # The MCP server quota counts enabled rows: switching a row on
            # is one more of them, and is asked as a creation is.
            if update_data.get("enabled") is True and not server.enabled:
                TenantService.check_quota(db, tenant_id, TenantResourceTypes.MCP_SERVERS)

            # The static token: not named leaves it, empty removes it, a
            # value replaces it (encrypted). The audit row says that it
            # changed and never what it was or is.
            # A token is a bearer server's: a server saved with another
            # auth type keeps none, whatever the request carries.
            final_auth_type = update_data.get("auth_type", server.auth_type)
            if final_auth_type != BEARER_AUTH_TYPE:
                if update_data.pop("auth_token", None):
                    raise HTTPException(status_code=422, detail=TOKEN_IS_A_BEARER_SERVERS)
                if server.auth_token:
                    changes["auth_token"] = {"old": "***", "new": None}
                    server.auth_token = None
            if "auth_token" in update_data:
                new_token = server_token.seal(encryption_service, update_data.pop("auth_token"))
                if new_token is not None or server.auth_token:
                    changes["auth_token"] = {"old": "***", "new": "***"}
                    server.auth_token = new_token

            for field, value in update_data.items():
                if hasattr(server, field):
                    old_value = getattr(server, field)
                    if old_value != value:
                        changes[field] = {"old": old_value, "new": value}
                        setattr(server, field, value)

            if changes:
                server.updated_at = datetime.now(timezone.utc)
                db.flush()
                db.refresh(server)

                logger.info("Server updated successfully", context={
                    "server_id": server_id,
                    "tenant_id": server.tenant_id,
                    "admin_id": admin_id,
                    "admin_type": admin_type_value,
                    "changes": list(changes.keys())
                })

                # Audit successful update
                audit.log(
                    action="mcp_server_update",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=server.tenant_id,
                    success=True,
                    resource_type=TenantResourceTypes.MCP_SERVERS.value,
                    resource_id=server_id,
                    details={
                        "server_name": server.name,
                        "changes": changes
                    },
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )
            else:
                logger.debug("No changes for server update", context={
                    "server_id": server_id
                })

            block_on(invalidate_proxy_cache(request, "mcp_server", tenant_id, server_id, "update"))

            return ServerUpdateResponse(
                server=server_manager.to_response(server),
                message="Server updated successfully" if changes else "No changes made"
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Server update failed", context={
                "server_id": server_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to update server: {str(e)}"
            )

    @router.post(
        "/{server_id}/discover-capabilities",
        response_model=RefreshCapabilitiesResponse,
        summary="Refresh MCP server capabilities (one-click discover)",
        description="""
            Re-fetch the upstream server's tools/resources/prompts and
            persist them, without requiring the operator to open the
            edit form. Same code path the create / update endpoints
            use — runs the THREAT-01 prompt-injection scan and the
            capability-diff drift detection inline, so any new
            findings still raise system_alerts.

            OAuth-authenticated servers are skipped here (no stored
            token at refresh time); use the OAuth login modal instead.
            Disabled servers are skipped too — enable first.

            Tenant isolation enforced automatically. Requires
            MCP_SERVER_UPDATE.
        """
    )
    def refresh_server_capabilities(
            server_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.MCP_SERVERS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.MCP_SERVER_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Re-discover capabilities for a persisted MCP server."""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            server = db.query(MCPServer).filter(
                MCPServer.id == server_id,
                MCPServer.tenant_id == tenant_id,
            ).first()

            if not server:
                raise HTTPException(status_code=404, detail=f"Server {server_id} not found")

            # OAuth servers need a live access token from the operator's
            # session — that path is the OAuth login modal, not this
            # one-click action. Return a clear skip rather than a
            # confusing 500 from inside the MCP session setup.
            if server.auth_type == "oauth":
                return RefreshCapabilitiesResponse(
                    success=True,
                    server_id=server.id,
                    discovered=False,
                    message="OAuth server — refresh capabilities via the OAuth login flow.",
                )

            if not server.enabled:
                return RefreshCapabilitiesResponse(
                    success=True,
                    server_id=server.id,
                    discovered=False,
                    message="Server is disabled. Enable it before refreshing capabilities.",
                )

            # A stored token that cannot be read: said, and the server is
            # not called.
            try:
                server_token.open_token(server_manager.encryption_service, server.auth_token)
            except server_token.TokenUnreadable as unreadable:
                raise HTTPException(status_code=503, detail=str(unreadable)) from None

            poison_findings: list = []
            diff_summary: dict = {}

            try:
                discovered = block_on(server_manager.discover_capabilities(
                    server=server,
                    db=db,
                    out_poison_findings=poison_findings,
                    out_diff_summary=diff_summary,
                ))
            except Exception as e:
                # The SDK's transport runs in a task group: the policy's
                # refusal (host and setting) is said, not the group's own
                # "unhandled errors in a TaskGroup".
                said = error_text(e, server.mcp_url, "MCP server URL")
                logger.warning("Capability refresh failed", context={
                    "server_id": server.id,
                    "tenant_id": tenant_id,
                    "error": said,
                })
                raise HTTPException(
                    status_code=502,
                    detail=f"Upstream discovery failed: {said}",
                )

            # Count what's now in the column. discover_capabilities
            # persists `{tools, resources, resource_templates, prompts}`
            # under `server.capabilities` via update_capabilities().
            caps = (server.capabilities or {}) if discovered else {}
            tools_count = len(caps.get("tools") or [])
            resources_count = len(caps.get("resources") or [])
            resource_templates_count = len(caps.get("resource_templates") or [])
            prompts_count = len(caps.get("prompts") or [])

            drift_detected = bool(diff_summary.get("change_count", 0))

            # Translate PoisonHit → wire model (same shape used by the
            # Create-MCP-Server endpoint).
            poison_payload = [
                PoisonedToolFinding(
                    tool_name=h.tool_name,
                    field_kind=h.field_kind,
                    description_excerpt=h.description[:500],
                    description_truncated=len(h.description) > 500,
                    confidence=round(h.confidence, 3),
                    patterns_matched=len(h.patterns_matched),
                    suspicious_indicator_count=h.suspicious_indicator_count,
                )
                for h in poison_findings
            ]

            # Build a toast-friendly summary. Keep it short and
            # parseable — the UI also has the raw counts.
            if discovered:
                parts = [f"{tools_count} tool{'s' if tools_count != 1 else ''}"]
                if resources_count:
                    parts.append(f"{resources_count} resource{'s' if resources_count != 1 else ''}")
                if prompts_count:
                    parts.append(f"{prompts_count} prompt{'s' if prompts_count != 1 else ''}")
                summary = f"Refreshed: {', '.join(parts)}"
                if drift_detected:
                    summary += f" ({diff_summary.get('summary') or 'drift detected'})"
            else:
                summary = "Discovery skipped."

            audit.log(
                action="mcp_server_refresh_capabilities",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.MCP_SERVERS.value,
                resource_id=server.id,
                details={
                    "server_name": server.name,
                    "discovered": discovered,
                    "tools_count": tools_count,
                    "resources_count": resources_count,
                    "prompts_count": prompts_count,
                    "drift_detected": drift_detected,
                    "poison_finding_count": len(poison_payload),
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db,
            )

            block_on(invalidate_proxy_cache(request, "mcp_server", tenant_id, server.id, "update"))

            logger.info("MCP server capabilities refreshed", context={
                "server_id": server.id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "discovered": discovered,
                "tools_count": tools_count,
                "drift_detected": drift_detected,
            })

            return RefreshCapabilitiesResponse(
                success=True,
                server_id=server.id,
                discovered=discovered,
                tools_count=tools_count,
                resources_count=resources_count,
                resource_templates_count=resource_templates_count,
                prompts_count=prompts_count,
                drift_detected=drift_detected,
                poisoned_tools=poison_payload,
                message=summary,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to refresh capabilities", context={
                "server_id": server_id,
                "tenant_id": tenant_id,
                "error": str(e),
            }, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to refresh capabilities: {str(e)}",
            )

    @router.delete(
        "/{server_id}",
        status_code=204,
        summary="Delete MCP server",
        description="""
                Delete MCP server.

                Refused with 409 while a virtual server references it: take
                it out of those first.

                Tenant isolation enforced automatically.
                """,
        responses={
            409: {
                **IN_USE_RESPONSE,
                "description": "A virtual server references the server; `detail.used_by.virtual_servers` lists them.",
            }
        },
    )
    def delete_server(
            server_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.MCP_SERVERS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.MCP_SERVER_DELETE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete MCP server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Filter by tenant_id in query
            server = db.query(MCPServer).filter(
                MCPServer.id == server_id,
                MCPServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Server {server_id} not found"
                )

            # Store info for audit before deletion
            server_name = server.name
            server_url = server.mcp_url
            tenant_id = server.tenant_id
            had_icon = server.has_icon()

            # Check for linked virtual servers
            # Get all virtual servers in this tenant
            virtual_servers = db.query(VirtualServer).filter(
                VirtualServer.tenant_id == tenant_id,
                VirtualServer.enabled == True
            ).all()

            # Filter to find which ones reference this server
            affected_virtual_servers = []
            for vs in virtual_servers:
                if server_id in vs.get_referenced_servers():
                    affected_virtual_servers.append(vs)

            # Referenced by virtual servers - cannot delete
            if affected_virtual_servers:
                virtual_server_names = [vs.name for vs in affected_virtual_servers]

                # Prevent deletion
                raise in_use(
                    f"Cannot delete server. It is referenced by {len(affected_virtual_servers)} virtual server(s): "
                    f"{', '.join(virtual_server_names)}",
                    virtual_servers=affected_virtual_servers,
                )

            # Tear down the live deployment BEFORE deleting the DB row: the
            # cascade removes the deployment rows, not the container / pod /
            # task they describe. The teardown belongs to the Enterprise
            # Edition's deployment feature and is a behaviour hook
            # (`composition/hooks.py`); a composition without it has no
            # deployments, and nothing is torn down.
            #
            # Best-effort: a teardown failure is logged but does not
            # block the DB delete — better an orphan container the
            # operator can `docker rm -f` than a phantom DB row the
            # admin UI can't act on.
            for teardown in HOOKS.all(MCP_SERVER_DEPLOYMENT_TEARDOWN):
                try:
                    teardown(server=server, logger=logger)
                except Exception as exc:
                    logger.warning("Container teardown failed during MCP server delete (non-fatal)", context={
                        "server_id": server_id,
                        "error": str(exc),
                    })

            # Delete server (cascades to icon data + deployment rows)
            db.delete(server)
            db.flush()

            logger.info("Server deleted successfully", context={
                "server_id": server_id,
                "server_name": server_name,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value
            })

            # Successful deletion
            audit.log(
                action="mcp_server_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.MCP_SERVERS.value,
                resource_id=server_id,
                details={
                    "server_name": server_name,
                    "server_url": server_url,
                    "had_icon": had_icon,
                    "auth_type": server.auth_type if hasattr(server, 'auth_type') else None,
                    "checked_virtual_servers": len(virtual_servers),
                    "affected_virtual_servers": 0  # Successfully deleted, so none affected
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "mcp_server", tenant_id, server_id, "delete"))

            return None

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Server deletion failed", context={
                "server_id": server_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete server: {str(e)}"
            )

    @router.get(
        "/{server_id}/health",
        response_model=MCPServerHealthStatusResponse,
        summary="Check server health",
        description="""
        Perform an immediate health check on a specific MCP server.

        This endpoint:
        - Tests connectivity to the MCP server
        - Verifies authentication if required
        - Updates the server's health status in the database
        - Returns detailed health information

        Tenant isolation enforced automatically.
        """
    )
    def check_server_health(
            server_id: str,
            request: Request,
            timeout: float = Query(10.0, ge=1.0, le=60.0, description="Health check timeout in seconds"),
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.MCP_SERVERS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.MCP_SERVER_HEALTH)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """
        Check health of a specific MCP server

        Performs immediate health check and updates database status.
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_username = admin.username
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query server with tenant isolation
            server = db.query(MCPServer).filter(
                MCPServer.id == server_id,
                MCPServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Server {server_id} not found"
                )

            if not server.enabled:
                # Server disabled - skip health check
                return {
                    "server_id": server_id,
                    "server_name": server.name,
                    "status": "server is disabled",
                    "admin_id": admin_id
                }

            logger.debug("Health check requested", context={
                "server_id": server_id,
                "server_name": server.name,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "timeout": timeout
            })

            # Perform health check
            result = block_on(server_manager.check_health(
                server=server,
                timeout=timeout,
                db=db
            ))

            # Add checked_by information
            result["checked_by"] = {
                "user_id": admin_id,
                "username": admin_username,
                "admin_type": admin_type_value
            }

            # Determine success based on health status
            is_healthy = result.get("status") == "healthy"

            logger.debug("Health check completed", context={
                "server_id": server_id,
                "server_name": server.name,
                "status": result.get("status"),
                "duration_ms": result.get("check_duration_ms"),
                "admin_id": admin_id
            })

            return result

        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Health check failed", context={
                "server_id": server_id,
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "error": str(e)
            })

            raise HTTPException(
                status_code=500,
                detail=f"Failed to check server health: {str(e)}"
            )

    @router.post(
        "/health/bulk",
        response_model=MCPServerBulkHealthCheckResponse,
        summary="Bulk health check",
        description="""
        Perform health checks on multiple servers at once.

        This endpoint:
        - Checks health of all specified servers (or all enabled servers if none specified)
        - Runs checks in parallel for efficiency
        - Returns aggregated results
        - Updates database for all checked servers

        Tenant isolation enforced automatically.
        """
    )
    def bulk_health_check(
            request: Request,
            server_ids: Optional[List[str]] = None,
            timeout: float = Query(10.0, ge=1.0, le=60.0, description="Health check timeout per server"),
            enabled_only: bool = Query(True, description="Only check enabled servers"),
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.MCP_SERVERS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.MCP_SERVER_HEALTH)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """
        Perform health checks on multiple servers

        If server_ids is provided, checks only those servers.
        Otherwise, checks all servers for the current tenant.
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            # Build query with tenant filter
            query = db.query(MCPServer).filter(MCPServer.tenant_id == tenant_id)

            if server_ids:
                query = query.filter(MCPServer.id.in_(server_ids))

            if enabled_only:
                query = query.filter(MCPServer.enabled == True)

            servers = query.all()

            if not servers:
                return {
                    "total": 0,
                    "checked": 0,
                    "healthy": 0,
                    "unhealthy": 0,
                    "failed": 0,
                    "results": []
                }

            logger.debug("Bulk health check started", context={
                "server_count": len(servers),
                "admin_id": admin_id,
                "tenant_id": tenant_id
            })

            # Shared ``BulkRunner``. Cap is derived from
            # ``BRUTOR_GATEWAY_DB_POOL_SIZE`` exactly like the LLM bulk
            # health check, so both endpoints scale uniformly with
            # the proxy's pool ceiling and we don't accumulate
            # divergent per-endpoint hardcodes.
            from app.services.concurrency import BulkRunner

            async def check_one_server(server):
                try:
                    return await server_manager.check_health(
                        server=server,
                        timeout=timeout,
                        db=db,
                    )
                except Exception as e:
                    return {
                        "server_id": server.id,
                        "server_name": server.name,
                        "status": "error",
                        "error": str(e),
                    }

            run = block_on(BulkRunner(
                coroutine_factory=check_one_server,
                inputs=servers,
                op_kind="mcp_health_check",
                logger=logger,
            ).run())
            # As with LLM bulk health, per-item exceptions are
            # absorbed inside ``check_one_server`` so ``errors[i]``
            # should be None throughout. Coerce any leftover
            # BulkRunner-level exception into the same error-shape
            # downstream readers expect.
            results = []
            for server, r, err in zip(run.inputs, run.results, run.errors):
                if r is not None:
                    results.append(r)
                else:
                    logger.error(
                        f"MCP health check raised past per-item handler for "
                        f"{getattr(server, 'id', '?')}: {err}"
                    )
                    results.append(
                        {
                            "server_id": getattr(server, "id", "?"),
                            "server_name": getattr(server, "name", "?"),
                            "status": "error",
                            "error": f"Bulk runner error: {err}",
                        }
                    )

            # Count results
            healthy_count = sum(1 for r in results if r.get("status") == "healthy")
            unhealthy_count = sum(1 for r in results if r.get("status") == "unhealthy")
            failed_count = sum(1 for r in results if r.get("status") == "error")

            logger.debug("Bulk health check completed", context={
                "total": len(servers),
                "healthy": healthy_count,
                "unhealthy": unhealthy_count,
                "failed": failed_count
            })

            return {
                "total": len(servers),
                "checked": len(results),
                "healthy": healthy_count,
                "unhealthy": unhealthy_count,
                "failed": failed_count,
                "results": results
            }

        except Exception as e:
            logger.warning("Bulk health check failed", context={
                "admin_id": admin.id,
                "tenant_id": tenant_id,
                "error": str(e)
            })

            raise HTTPException(
                status_code=500,
                detail=f"Bulk health check failed: {str(e)}"
            )

    @router.get("/{server_id}/icon")
    def get_server_icon(
            server_id: str,
            db: Session = Depends(get_db)
    ):
        """
        Get server icon - PUBLIC endpoint
        Icons are non-sensitive and server_id is globally unique
        """
        # Query server directly by ID (no tenant filter needed)
        server = db.query(MCPServer).filter(MCPServer.id == server_id).first()

        if not server or not server.icon_data:
            raise HTTPException(status_code=404, detail="Icon not found")

        return Response(
            content=server.icon_data,
            media_type=server.icon_mime_type or "image/png",
            headers={
                "Cache-Control": "no-cache, must-revalidate",
                "ETag": f'"{server.icon_hash}"' if server.icon_hash else None,
                "Last-Modified": server.icon_uploaded_at.strftime('%a, %d %b %Y %H:%M:%S GMT') if server.icon_uploaded_at else None
            }
        )

    @router.post(
        "/{server_id}/icon",
        summary="Upload server icon",
        description="""
        Upload an icon for an MCP server from multiple sources.
        
        Supports three upload methods (provide exactly one):
        - file: Upload file directly (multipart/form-data)
        - icon_base64: Base64 encoded image data
        - icon_url: URL to download icon from
        
        Tenant isolation enforced automatically.
        """
    )
    def upload_server_icon(
        server_id: str,
        request: Request,
        db: Session = Depends(get_db),
        file: Optional[UploadFile] = File(None),
        icon_base64: Optional[str] = Form(None),
        icon_url: Optional[str] = Form(None),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.MCP_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.MCP_SERVER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Upload icon for MCP server from file, base64, or URL

        Form Parameters:
            - file: File upload (multipart/form-data)
            - icon_base64: Base64 encoded image string
            - icon_url: URL to download icon from
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Filter by tenant_id in query
            server = db.query(MCPServer).filter(
                MCPServer.id == server_id,
                MCPServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Server {server_id} not found"
                )

            # Validate that exactly one upload method is provided
            upload_methods = [file, icon_base64, icon_url]
            provided_methods = [m for m in upload_methods if m is not None]

            if len(provided_methods) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="Must provide one of: file, icon_base64, or icon_url"
                )

            if len(provided_methods) > 1:
                raise HTTPException(
                    status_code=400,
                    detail="Provide exactly one upload method: file, icon_base64, or icon_url"
                )

            # Determine upload method and source info for logging
            upload_method = None
            source_info = {}

            if file:
                upload_method = "file"
                source_info = {
                    "filename": file.filename,
                    "content_type": file.content_type
                }
            elif icon_base64:
                upload_method = "base64"
                source_info = {
                    "data_length": len(icon_base64)
                }
            elif icon_url:
                upload_method = "url"
                source_info = {
                    "source_url": icon_url
                }

            logger.debug("Icon upload requested", context={
                "server_id": server_id,
                "tenant_id": server.tenant_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "upload_method": upload_method,
                **source_info
            })

            # Use IconManager with appropriate method
            if file:
                icon_info = block_on(icon_manager.save_icon_from_upload(
                    server_id=server_id,
                    file=file,
                    db=db,
                    optimize=True
                ))
            elif icon_base64:
                icon_info = block_on(icon_manager.save_icon_from_base64(
                    server_id=server_id,
                    base64_data=icon_base64,
                    db=db,
                    optimize=True
                ))
            elif icon_url:
                icon_info = block_on(icon_manager.save_icon_from_url(
                    server_id=server_id,
                    url=icon_url,
                    db=db,
                    optimize=True
                ))

            logger.info("Icon uploaded successfully", context={
                "server_id": server_id,
                "tenant_id": server.tenant_id,
                "admin_id": admin_id,
                "upload_method": upload_method,
                **source_info
            })

            # Audit successful upload
            audit.log(
                action="mcp_server_icon_upload",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type="mcp_server_icon",
                resource_id=server_id,
                details={
                    "server_name": server.name,
                    "upload_method": upload_method,
                    "icon_size": icon_info["size"],
                    "mime_type": icon_info["mime_type"],
                    "icon_hash": icon_info["hash"][:16],
                    **source_info
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {
                "message": "Icon uploaded successfully",
                "icon": icon_info,
                "upload_method": upload_method
            }

        except HTTPException:
            raise
        except ValueError as e:
            # Validation errors from IconManager
            logger.warning("Icon upload validation failed", context={
                "server_id": server_id,
                "error": str(e)
            })

            raise HTTPException(
                status_code=400,
                detail=str(e)
            )
        except Exception as e:
            logger.error("Icon upload failed", context={
                "server_id": server_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to upload icon: {str(e)}"
            )

    @router.delete(
        "/{server_id}/icon",
        status_code=200,
        summary="Delete server icon",
        description="""
        Delete the icon for an MCP server.
        
        Tenant isolation enforced automatically.
        """
    )
    def delete_server_icon(
        server_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.MCP_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.MCP_SERVER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete icon for MCP server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Filter by tenant_id in query
            server = db.query(MCPServer).filter(
                MCPServer.id == server_id,
                MCPServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Server {server_id} not found"
                )

            # Store info for audit before deletion
            had_icon = server.has_icon()
            icon_hash = server.icon_hash if had_icon else None
            icon_size = server.icon_size if had_icon else None

            if not had_icon:
                return {"message": "Server has no icon to delete"}

            logger.debug("Icon deletion requested", context={
                "server_id": server_id,
                "tenant_id": server.tenant_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value
            })

            # Use IconManager directly to delete icon with tenant_id
            deleted = icon_manager.delete_icon(server_id, tenant_id, db)

            if deleted:
                logger.info("Icon deleted successfully", context={
                    "server_id": server_id,
                    "tenant_id": server.tenant_id,
                    "admin_id": admin.id
                })

                # Audit successful deletion
                audit.log(
                    action="mcp_server_icon_delete",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=tenant_id,
                    success=True,
                    resource_type="mcp_server_icon",
                    resource_id=server_id,
                    details={
                        "server_name": server.name,
                        "icon_hash": icon_hash[:16] if icon_hash else None,
                        "icon_size": icon_size
                    },
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )

                return {"message": "Icon deleted successfully"}
            else:
                return {"message": "No icon to delete"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Icon deletion failed", context={
                "server_id": server_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete icon: {str(e)}"
            )

    return router