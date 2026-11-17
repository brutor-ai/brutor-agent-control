# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Virtual Server Management Endpoints 
Multi-Tenant with Unified Audit Logging
"""

from ulid import ULID
from typing import Union, Optional
from fastapi import (
    APIRouter,
    HTTPException,
    Depends,
    Query,
    Request
)
from sqlalchemy.orm import Session

from app.constants.tenant import TenantResourceTypes
from app.database import get_db
from app.database.models.virtual_server import VirtualServer
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.services.tenant.tenant_service import TenantService
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    require_permission,
    get_audit_logger,
    require_tenant_resource_access
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.api.v1.models.admin.virtual_server import (
    VirtualServerRequest,
    VirtualServerUpdateRequest
)
from app.services.mcp.server.virtual_server_manager import VirtualServerManager
from app.services.mcp.server.vmcp_normalizer import VMCPNormalizationError
from app.core.logging_system import BrutorLogger
from app.middleware.trusted_proxies import client_ip


def create_virtual_server_router(
    virtual_server_manager: VirtualServerManager,
    logger: BrutorLogger
) -> APIRouter:
    """Create router for multi-tenant virtual server management endpoints"""
    router = APIRouter(prefix="/admin/virtual-servers", tags=["Virtual Servers"])

    @router.get(
        "",
        summary="List virtual servers",
        description="List virtual servers for current tenant"
    )
    def list_virtual_servers(
        request: Request,
        owner: Optional[str] = Query(None, description="Filter by owner admin ID"),
        enabled_only: bool = Query(False, description="Only return enabled servers"),
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.VIRTUAL_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.VIRTUAL_SERVER_READ))
    ):
        """List virtual servers with tenant isolation"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            servers = virtual_server_manager.list_virtual_servers(
                owner=owner,
                enabled_only=enabled_only,
                tenant_id=tenant_id,
                db=db
            )

            logger.debug("Virtual servers listed", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "server_count": len(servers)
            })

            return {
                "servers": [
                    virtual_server_manager.to_response(server)
                    for server in servers
                ],
                "total": len(servers)
            }

        except Exception as e:
            logger.error("Failed to list virtual servers", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to list virtual servers: {str(e)}"
            )

    @router.get(
        "/{server_id}",
        summary="Get virtual server details",
        description="Get virtual server details (tenant isolation enforced automatically)"
    )
    def get_virtual_server(
        server_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.VIRTUAL_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.VIRTUAL_SERVER_READ))
    ):
        """Get virtual server details with tenant isolation"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query with tenant filter
            server = db.query(VirtualServer).filter(
                VirtualServer.id == server_id,
                VirtualServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Virtual server {server_id} not found"
                )

            logger.debug("Virtual server retrieved", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "server_id": server_id
            })

            return virtual_server_manager.to_response(server)

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get virtual server", context={
                "server_id": server_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to get virtual server: {str(e)}"
            )

    @router.post(
        "",
        status_code=201,
        summary="Create virtual server",
        description="Create a new virtual server for current tenant"
    )
    def create_virtual_server(
        server_request: VirtualServerRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.VIRTUAL_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.VIRTUAL_SERVER_CREATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Create a new virtual server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Feature gate.
        TenantService.check_feature_access(db, tenant_id, 'virtual_mcp_servers')

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Generate virtual server ID, Result: 'vs-01ARZ3NDEKTSV4RRFFQ69G5FAV'
            server_id = f"vs-{ULID()}"

            logger.debug("Creating virtual server", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "server_name": server_request.name
            })

            # Create server
            server = virtual_server_manager.create_virtual_server(
                server_id=server_id,
                name=server_request.name,
                owner=admin_id,
                tenant_id=tenant_id,
                description=server_request.description,
                capabilities=server_request.capabilities,
                enabled=True,
                db=db
            )

            # THREAT-01 — scan operator-supplied description_override
            # + exposed_name on each tool entry against the prompt-
            # injection detector. Defensive try/except so a scanner
            # failure doesn't roll back the VMCP create — same
            # convention as the upstream MCP-server path.
            vmcp_poison_findings: list = []
            try:
                from app.services.mcp.tool_description_scanner import (
                    scan_for_poisoned_vmcp_overrides,
                )
                vmcp_poison_findings = scan_for_poisoned_vmcp_overrides(
                    db=db,
                    vmcp_id=server.id,
                    vmcp_name=server.name,
                    tenant_id=tenant_id,
                    capabilities=server.capabilities or {},
                )
                if vmcp_poison_findings:
                    logger.warning(
                        "VMCP tool override poisoning detected",
                        context={
                            "vmcp_id": server.id,
                            "poisoned_field_count": len(vmcp_poison_findings),
                            "poisoned_tools": [
                                h.tool_name for h in vmcp_poison_findings
                            ],
                        },
                    )
            except Exception as scan_err:  # noqa: BLE001
                logger.error(
                    "VMCP tool override poison scan failed (continuing)",
                    context={
                        "vmcp_id": server.id,
                        "error": str(scan_err),
                    },
                    exc_info=True,
                )

            # Audit successful creation
            audit.log(
                action="virtual_server_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type="virtual_server",
                resource_id=server.id,
                details={
                    "server_name": server.name,
                    "owner": server.owner,
                    "capabilities_count": {
                        "tools": len(server.capabilities.get("tools", [])),
                        "resources": len(server.capabilities.get("resources", [])),
                        "prompts": len(server.capabilities.get("prompts", []))
                    }
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("Virtual server created successfully", context={
                "server_id": server.id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id
            })

            # Same payload shape as the upstream MCP create
            # response — admin UI's existing PoisonedToolFinding
            # modal renders both. `field_kind` distinguishes
            # overridden name vs overridden description.
            poison_payload = [
                {
                    "tool_name": h.tool_name,
                    "field_kind": h.field_kind,
                    "description_excerpt": h.description[:500],
                    "description_truncated": len(h.description) > 500,
                    "confidence": round(h.confidence, 3),
                    "patterns_matched": len(h.patterns_matched),
                    "suspicious_indicator_count": h.suspicious_indicator_count,
                }
                for h in vmcp_poison_findings
            ]
            msg = "Virtual server created successfully"
            if poison_payload:
                msg = (
                    f"Virtual server created — {len(poison_payload)} operator-supplied "
                    "override field(s) tripped the prompt-injection detector "
                    f"(across {len({p['tool_name'] for p in poison_payload})} tool(s)). "
                    "See `poisoned_tools` for details and review in Mission Control alerts."
                )

            return {
                "message": msg,
                "server": virtual_server_manager.to_response(server),
                "poisoned_tools": poison_payload,
            }

        except HTTPException:
            raise
        except VMCPNormalizationError as e:
            # Operator supplied bad capabilities JSON — surface as
            # 400 with the normalizer's user-friendly message
            # (which already names the offending tool / server).
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("Failed to create virtual server", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to create virtual server: {str(e)}"
            )

    @router.patch(
        "/{server_id}",
        summary="Update virtual server",
        description="Update a virtual server (tenant isolation enforced automatically)"
    )
    def update_virtual_server(
        server_id: str,
        update_request: VirtualServerUpdateRequest,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.VIRTUAL_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.VIRTUAL_SERVER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update virtual server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query with tenant filter
            server = db.query(VirtualServer).filter(
                VirtualServer.id == server_id,
                VirtualServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Virtual server {server_id} not found"
                )

            # Store original state for audit
            original_state = {
                "name": server.name,
                "description": server.description,
                "enabled": server.enabled,
                "owner": server.owner
            }

            logger.debug("Updating virtual server", context={
                "server_id": server_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id
            })

            # Build updates dict
            updates = {}
            if update_request.name is not None:
                updates["name"] = update_request.name
            if update_request.description is not None:
                updates["description"] = update_request.description
            if update_request.capabilities is not None:
                updates["capabilities"] = update_request.capabilities
            if update_request.enabled is not None:
                updates["enabled"] = update_request.enabled
            if update_request.owner is not None:
                updates["owner"] = update_request.owner

            # Update server
            updated_server = virtual_server_manager.update_virtual_server(
                server_id=server_id,
                updates=updates,
                db=db,
                tenant_id=tenant_id
            )

            if not updated_server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Virtual server {server_id} not found"
                )

            # THREAT-01 — re-scan the operator-supplied overrides
            # ONLY when capabilities changed in this PATCH. A
            # name-only or description-only update leaves the
            # catalog untouched, so the upstream scan results are
            # still authoritative. Same defensive try/except as
            # the create path.
            vmcp_poison_findings: list = []
            if "capabilities" in updates:
                try:
                    from app.services.mcp.tool_description_scanner import (
                        scan_for_poisoned_vmcp_overrides,
                    )
                    vmcp_poison_findings = scan_for_poisoned_vmcp_overrides(
                        db=db,
                        vmcp_id=updated_server.id,
                        vmcp_name=updated_server.name,
                        tenant_id=tenant_id,
                        capabilities=updated_server.capabilities or {},
                    )
                    if vmcp_poison_findings:
                        logger.warning(
                            "VMCP tool override poisoning detected on update",
                            context={
                                "vmcp_id": updated_server.id,
                                "poisoned_field_count": len(vmcp_poison_findings),
                                "poisoned_tools": [
                                    h.tool_name for h in vmcp_poison_findings
                                ],
                            },
                        )
                except Exception as scan_err:  # noqa: BLE001
                    logger.error(
                        "VMCP tool override poison scan failed on update (continuing)",
                        context={
                            "vmcp_id": updated_server.id,
                            "error": str(scan_err),
                        },
                        exc_info=True,
                    )

            # Audit successful update
            audit.log(
                action="virtual_server_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type="virtual_server",
                resource_id=server_id,
                details={
                    "server_name": updated_server.name,
                    "updated_fields": list(updates.keys()),
                    "changes": {
                        field: {
                            "from": original_state.get(field),
                            "to": updates.get(field)
                        }
                        for field in updates.keys()
                        if field in original_state
                    }
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("Virtual server updated successfully", context={
                "server_id": server_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "updated_fields": list(updates.keys())
            })

            poison_payload = [
                {
                    "tool_name": h.tool_name,
                    "field_kind": h.field_kind,
                    "description_excerpt": h.description[:500],
                    "description_truncated": len(h.description) > 500,
                    "confidence": round(h.confidence, 3),
                    "patterns_matched": len(h.patterns_matched),
                    "suspicious_indicator_count": h.suspicious_indicator_count,
                }
                for h in vmcp_poison_findings
            ]
            msg = "Virtual server updated successfully"
            if poison_payload:
                msg = (
                    f"Virtual server updated — {len(poison_payload)} operator-supplied "
                    "override field(s) tripped the prompt-injection detector "
                    f"(across {len({p['tool_name'] for p in poison_payload})} tool(s)). "
                    "See `poisoned_tools` for details and review in Mission Control alerts."
                )

            return {
                "message": msg,
                "server": virtual_server_manager.to_response(updated_server),
                "poisoned_tools": poison_payload,
            }

        except HTTPException:
            raise
        except VMCPNormalizationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("Failed to update virtual server", context={
                "server_id": server_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to update virtual server: {str(e)}"
            )

    @router.delete(
        "/{server_id}",
        status_code=200,
        summary="Delete virtual server",
        description="Delete a virtual server (tenant isolation enforced automatically)"
    )
    def delete_virtual_server(
        server_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.VIRTUAL_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.VIRTUAL_SERVER_DELETE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete virtual server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query with tenant filter
            server = db.query(VirtualServer).filter(
                VirtualServer.id == server_id,
                VirtualServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Virtual server {server_id} not found"
                )

            # Store info for audit before deletion
            server_name = server.name
            server_owner = server.owner
            capabilities_count = {
                "tools": len(server.capabilities.get("tools", [])),
                "resources": len(server.capabilities.get("resources", [])),
                "prompts": len(server.capabilities.get("prompts", []))
            }

            logger.debug("Deleting virtual server", context={
                "server_id": server_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id
            })

            # Delete server
            deleted = virtual_server_manager.delete_virtual_server(
                server_id=server_id,
                db=db,
                tenant_id=tenant_id
            )

            if not deleted:
                raise HTTPException(
                    status_code=404,
                    detail=f"Virtual server {server_id} not found"
                )

            # Audit successful deletion
            audit.log(
                action="virtual_server_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type="virtual_server",
                resource_id=server_id,
                details={
                    "server_name": server_name,
                    "owner": server_owner,
                    "capabilities_count": capabilities_count
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("Virtual server deleted successfully", context={
                "server_id": server_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id
            })

            return {
                "message": "Virtual server deleted successfully"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete virtual server", context={
                "server_id": server_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete virtual server: {str(e)}"
            )

    @router.post(
        "/{server_id}/capabilities/tool",
        status_code=201,
        summary="Add tool capability",
        description="Add a tool capability from an MCP server to a virtual server (tenant isolation enforced automatically)"
    )
    def add_tool_capability(
        server_id: str,
        source_server_id: str = Query(..., description="Source MCP server ID"),
        tool_name: str = Query(..., description="Tool name"),
        request: Request = None,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.VIRTUAL_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.VIRTUAL_SERVER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Add tool capability to virtual server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query with tenant filter
            server = db.query(VirtualServer).filter(
                VirtualServer.id == server_id,
                VirtualServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Virtual server {server_id} not found"
                )

            logger.debug("Adding tool capability", context={
                "server_id": server_id,
                "source_server_id": source_server_id,
                "tool_name": tool_name,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id
            })

            # Add capability
            success = virtual_server_manager.add_tool_capability(
                virtual_server_id=server_id,
                source_server_id=source_server_id,
                tool_name=tool_name,
                db=db,
                tenant_id=tenant_id
            )

            if not success:
                raise HTTPException(
                    status_code=400,
                    detail="Failed to add tool capability"
                )

            # Audit successful addition
            audit.log(
                action="virtual_server_capability_add",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type="virtual_server_capability",
                resource_id=server_id,
                details={
                    "server_name": server.name,
                    "capability_type": "tool",
                    "source_server_id": source_server_id,
                    "tool_name": tool_name
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {
                "message": "Tool capability added successfully",
                "server_id": server_id,
                "source_server_id": source_server_id,
                "tool_name": tool_name
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to add tool capability", context={
                "server_id": server_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to add tool capability: {str(e)}"
            )

    @router.post(
        "/{server_id}/capabilities/resource",
        status_code=201,
        summary="Add resource capability",
        description="Add a resource capability from an MCP server to a virtual server (tenant isolation enforced automatically)"
    )
    def add_resource_capability(
        server_id: str,
        source_server_id: str = Query(..., description="Source MCP server ID"),
        resource_uri: str = Query(..., description="Resource URI"),
        request: Request = None,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.VIRTUAL_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.VIRTUAL_SERVER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Add resource capability to virtual server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query with tenant filter
            server = db.query(VirtualServer).filter(
                VirtualServer.id == server_id,
                VirtualServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Virtual server {server_id} not found"
                )

            logger.debug("Adding resource capability", context={
                "server_id": server_id,
                "source_server_id": source_server_id,
                "resource_uri": resource_uri,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id
            })

            # Add capability
            success = virtual_server_manager.add_resource_capability(
                virtual_server_id=server_id,
                source_server_id=source_server_id,
                resource_name=resource_uri,
                db=db,
                tenant_id=tenant_id
            )

            if not success:
                raise HTTPException(
                    status_code=400,
                    detail="Failed to add resource capability"
                )

            # Audit successful addition
            audit.log(
                action="virtual_server_capability_add",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type="virtual_server_capability",
                resource_id=server_id,
                details={
                    "server_name": server.name,
                    "capability_type": "resource",
                    "source_server_id": source_server_id,
                    "resource_uri": resource_uri
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {
                "message": "Resource capability added successfully",
                "server_id": server_id,
                "source_server_id": source_server_id,
                "resource_uri": resource_uri
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to add resource capability", context={
                "server_id": server_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to add resource capability: {str(e)}"
            )

    @router.post(
        "/{server_id}/capabilities/prompt",
        status_code=201,
        summary="Add prompt capability",
        description="Add a prompt capability from an MCP server to a virtual server (tenant isolation enforced automatically)"
    )
    def add_prompt_capability(
        server_id: str,
        source_server_id: str = Query(..., description="Source MCP server ID"),
        prompt_name: str = Query(..., description="Prompt name"),
        request: Request = None,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.VIRTUAL_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.VIRTUAL_SERVER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Add prompt capability to virtual server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query with tenant filter
            server = db.query(VirtualServer).filter(
                VirtualServer.id == server_id,
                VirtualServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Virtual server {server_id} not found"
                )

            logger.debug("Adding prompt capability", context={
                "server_id": server_id,
                "source_server_id": source_server_id,
                "prompt_name": prompt_name,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id
            })

            # Add capability
            success = virtual_server_manager.add_prompt_capability(
                virtual_server_id=server_id,
                source_server_id=source_server_id,
                prompt_name=prompt_name,
                db=db,
                tenant_id=tenant_id
            )

            if not success:
                raise HTTPException(
                    status_code=400,
                    detail="Failed to add prompt capability"
                )

            # Audit successful addition
            audit.log(
                action="virtual_server_capability_add",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type="virtual_server_capability",
                resource_id=server_id,
                details={
                    "server_name": server.name,
                    "capability_type": "prompt",
                    "source_server_id": source_server_id,
                    "prompt_name": prompt_name
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {
                "message": "Prompt capability added successfully",
                "server_id": server_id,
                "source_server_id": source_server_id,
                "prompt_name": prompt_name
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to add prompt capability", context={
                "server_id": server_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to add prompt capability: {str(e)}"
            )

    @router.delete(
        "/{server_id}/capabilities/source/{source_server_id}",
        status_code=200,
        summary="Remove all capabilities from source server",
        description="Remove all capabilities from a specific source MCP server (tenant isolation enforced automatically)"
    )
    def remove_source_capabilities(
        server_id: str,
        source_server_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.VIRTUAL_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.VIRTUAL_SERVER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Remove all capabilities from a source server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query with tenant filter
            server = db.query(VirtualServer).filter(
                VirtualServer.id == server_id,
                VirtualServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Virtual server {server_id} not found"
                )

            logger.debug("Removing capabilities from source", context={
                "server_id": server_id,
                "source_server_id": source_server_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id
            })

            # Remove capabilities
            removed_count = virtual_server_manager.remove_server_capabilities(
                virtual_server_id=server_id,
                source_server_id=source_server_id,
                db=db,
                tenant_id=tenant_id
            )

            # Audit successful removal
            audit.log(
                action="virtual_server_capability_remove",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type="virtual_server_capability",
                resource_id=server_id,
                details={
                    "server_name": server.name,
                    "source_server_id": source_server_id,
                    "removed_count": removed_count
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return {
                "message": f"Removed {removed_count} capabilities from source server",
                "removed_count": removed_count
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to remove capabilities", context={
                "server_id": server_id,
                "source_server_id": source_server_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to remove capabilities: {str(e)}"
            )

    @router.get(
        "/{server_id}/statistics",
        summary="Get virtual server statistics",
        description="Get statistics for a virtual server (tenant isolation enforced automatically)"
    )
    def get_server_statistics(
        server_id: str,
        request: Request,
        db: Session = Depends(get_db),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.VIRTUAL_SERVERS)),
        admin: AdminPrincipal = Depends(require_permission(Permission.VIRTUAL_SERVER_READ))
    ):
        """Get virtual server statistics"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Query with tenant filter
            server = db.query(VirtualServer).filter(
                VirtualServer.id == server_id,
                VirtualServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=404,
                    detail=f"Virtual server {server_id} not found"
                )

            # Get referenced servers
            referenced_servers = virtual_server_manager.get_referenced_servers(
                virtual_server_id=server_id,
                db=db,
                tenant_id=tenant_id
            )

            # Get capability counts
            capability_counts = virtual_server_manager.get_capability_counts(
                virtual_server_id=server_id,
                db=db,
                tenant_id=tenant_id
            )

            return {
                "server_id": server_id,
                "server_name": server.name,
                "enabled": server.enabled,
                "owner": server.owner,
                "referenced_servers": referenced_servers,
                "referenced_server_count": len(referenced_servers),
                "capability_counts": capability_counts,
                "total_capabilities": sum(capability_counts.values())
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get server statistics", context={
                "server_id": server_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to get statistics: {str(e)}"
            )

    return router