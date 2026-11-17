# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Server Configs Endpoints (Admin)
Admin endpoints for managing server configurations with multi-group support
Multi-Tenant with Unified Audit Logging
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from sqlalchemy.orm import Session
from typing import Optional, Union

from app.core.sync_bridge import block_on
from app.services.tenant.tenant_service import TenantService
from app.constants.tenant import TenantResourceTypes
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.core.admin_user.dependencies import (
    require_permission,
    require_tenant_resource_access,
    get_audit_logger
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.services.portal.server_config_manager import ServerConfigManager
from app.api.v1.models.admin.server_config import (
    ServerConfigCreate,
    ServerConfigUpdate,
    ServerConfigResponse,
    ServerConfigListResponse,
    ServerConfigStatsResponse
)
from app.services.cache_invalidation import invalidate_proxy_cache
from app.core.logging_system import BrutorLogger
from app.middleware.trusted_proxies import client_ip


def create_admin_server_configs_router(
        server_config_manager: ServerConfigManager,
        logger: BrutorLogger
) -> APIRouter:
    """Create admin server configs management router"""

    router = APIRouter(
        prefix="/admin/server-configs",
        tags=["Server Configs (Admin)"]
    )

    @router.get("", response_model=ServerConfigListResponse)
    def list_server_configs(
            request: Request,
            mcp_server_id: Optional[str] = Query(None, description="Filter by MCP server"),
            oauth_client_id: Optional[str] = Query(None, description="Filter by OAuth client"),
            app_type: Optional[str] = Query(None, description="Filter by app type"),
            enabled_only: bool = Query(False, description="Only return enabled configs"),
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.SERVER_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.SERVER_CONFIG_READ))
    ):
        """
        List all server configurations for current tenant.

        Server configs define how MCP servers can be accessed through
        different OAuth clients (webapp, mobile, desktop, etc.)
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            logger.debug("Server configs list requested", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "mcp_server_id": mcp_server_id,
                "oauth_client_id": oauth_client_id,
                "app_type": app_type
            })

            configs = block_on(server_config_manager.list_configs(
                db=db,
                tenant_id=tenant_id,
                mcp_server_id=mcp_server_id,
                oauth_client_id=oauth_client_id,
                app_type=app_type,
                enabled_only=enabled_only,
                include_relationships=True
            ))

            return ServerConfigListResponse(
                configs=configs,
                total=len(configs)
            )

        except Exception as e:
            logger.error("Failed to list server configs", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list server configs: {str(e)}"
            )

    # Before "/{config_id}", or that route would take "stats" for an id.
    @router.get("/stats", response_model=ServerConfigStatsResponse)
    def get_server_config_statistics(
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.SERVER_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.SERVER_CONFIG_READ))
    ):
        """
        Get server configuration statistics for current tenant
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            stats = block_on(server_config_manager.get_statistics(
                db=db,
                tenant_id=tenant_id
            ))

            return ServerConfigStatsResponse(**stats)

        except Exception as e:
            logger.error("Failed to get server config statistics", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get statistics: {str(e)}"
            )

    @router.get("/{config_id}", response_model=ServerConfigResponse)
    def get_server_config(
            config_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.SERVER_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.SERVER_CONFIG_READ))
    ):
        """
        Get details of a specific server configuration

        Tenant isolation enforced automatically.
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            config = block_on(server_config_manager.get_config(
                config_id=config_id,
                db=db,
                tenant_id=tenant_id,
                include_relationships=True
            ))

            if not config:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Server config '{config_id}' not found"
                )

            logger.debug("Server config accessed", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "config_id": config_id
            })

            return ServerConfigResponse(**config) if isinstance(config, dict) else config

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to retrieve server config", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "config_id": config_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to retrieve server config: {str(e)}"
            )

    @router.post("", response_model=ServerConfigResponse, status_code=status.HTTP_201_CREATED)
    def create_server_config(
            config: ServerConfigCreate,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.SERVER_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.SERVER_CONFIG_CREATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Create a new server configuration for current tenant
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            logger.debug("Creating server config", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "mcp_server_id": config.mcp_server_id,
                "oauth_client_id": config.oauth_client_id
            })

            created_config = block_on(server_config_manager.create_config(
                config=config,
                db=db,
                tenant_id=tenant_id
            ))

            # Audit successful creation
            audit.log(
                action="server_config_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.SERVER_CONFIGS,
                resource_id=created_config.get('id') if isinstance(created_config, dict) else created_config.id,
                details={
                    "mcp_server_id": config.mcp_server_id,
                    "oauth_client_id": config.oauth_client_id,
                    "app_type": config.app_type if hasattr(config, 'app_type') else None,
                    "enabled": config.enabled if hasattr(config, 'enabled') else True,
                    "groups_count": len(config.group_ids) if hasattr(config, 'group_ids') and config.group_ids else 0
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("Server config created successfully", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "config_id": created_config.get('id') if isinstance(created_config, dict) else created_config.id
            })

            config_id_value = created_config.get('id') if isinstance(created_config, dict) else created_config.id
            block_on(invalidate_proxy_cache(request, "server_config", tenant_id, config_id_value, "create"))

            return ServerConfigResponse(**created_config) if isinstance(created_config, dict) else created_config

        except HTTPException:
            raise
        except ValueError as e:
            # The manager's own refusals: a server, group or client outside this
            # tenant, or not exactly one backend. The caller's error, not ours.
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except Exception as e:
            logger.error("Failed to create server config", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            # Audit failed creation
            audit.log(
                action="server_config_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=False,
                resource_type=TenantResourceTypes.SERVER_CONFIGS,
                resource_id=None,
                details={
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "mcp_server_id": config.mcp_server_id,
                    "oauth_client_id": config.oauth_client_id
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create server config: {str(e)}"
            )

    @router.patch("/{config_id}", response_model=ServerConfigResponse)
    def update_server_config(
            config_id: str,
            updates: ServerConfigUpdate,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.SERVER_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.SERVER_CONFIG_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Update a server configuration

        Tenant isolation enforced automatically.
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Get existing config with tenant filter
            existing_config = block_on(server_config_manager.get_config(
                config_id=config_id,
                db=db,
                tenant_id=tenant_id
            ))

            if not existing_config:
                # Audit not found
                audit.log(
                    action="server_config_update",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=tenant_id,
                    success=False,
                    resource_type=TenantResourceTypes.SERVER_CONFIGS,
                    resource_id=config_id,
                    details={"error": "Config not found"},
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )

                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Server config '{config_id}' not found"
                )

            # Store original state for audit
            original_state = {
                "enabled": existing_config.get('enabled') if isinstance(existing_config, dict) else getattr(existing_config, 'enabled', None),
                "mcp_server_id": existing_config.get('mcp_server_id') if isinstance(existing_config, dict) else getattr(existing_config, 'mcp_server_id', None),
                "oauth_client_id": existing_config.get('oauth_client_id') if isinstance(existing_config, dict) else getattr(existing_config, 'oauth_client_id', None)
            }

            logger.debug("Updating server config", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "config_id": config_id
            })

            updated_config = block_on(server_config_manager.update_config(
                config_id=config_id,
                updates=updates,
                db=db,
                tenant_id=tenant_id
            ))

            # Build changes dict
            changes = {}
            update_dict = updates.dict(exclude_unset=True) if hasattr(updates, 'dict') else updates
            for field, new_value in update_dict.items():
                if field in original_state and original_state[field] != new_value:
                    changes[field] = {
                        "from": original_state[field],
                        "to": new_value
                    }

            # Audit successful update
            audit.log(
                action="server_config_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.SERVER_CONFIGS,
                resource_id=config_id,
                details={
                    "updated_fields": list(update_dict.keys()),
                    "changes": changes
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("Server config updated successfully", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "config_id": config_id,
                "updated_fields": list(update_dict.keys())
            })

            block_on(invalidate_proxy_cache(request, "server_config", tenant_id, config_id, "update"))

            return ServerConfigResponse(**updated_config) if isinstance(updated_config, dict) else updated_config

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to update server config", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "config_id": config_id,
                "error": str(e)
            }, exc_info=True)

            # Audit failed update
            audit.log(
                action="server_config_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=False,
                resource_type=TenantResourceTypes.SERVER_CONFIGS,
                resource_id=config_id,
                details={
                    "error": str(e),
                    "error_type": type(e).__name__
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update server config: {str(e)}"
            )

    @router.delete("/{config_id}", status_code=status.HTTP_200_OK)
    def delete_server_config(
            config_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.SERVER_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.SERVER_CONFIG_DELETE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Delete a server configuration

        Tenant isolation enforced automatically.
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Get existing config with tenant filter
            existing_config = block_on(server_config_manager.get_config(
                config_id=config_id,
                db=db,
                tenant_id=tenant_id
            ))

            if not existing_config:
                # Audit not found
                audit.log(
                    action="server_config_delete",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=tenant_id,
                    success=False,
                    resource_type=TenantResourceTypes.SERVER_CONFIGS,
                    resource_id=config_id,
                    details={"error": "Config not found"},
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )

                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Server config '{config_id}' not found"
                )

            # Store info for audit before deletion
            mcp_server_id = existing_config.get('mcp_server_id') if isinstance(existing_config, dict) else getattr(existing_config, 'mcp_server_id', None)
            oauth_client_id = existing_config.get('oauth_client_id') if isinstance(existing_config, dict) else getattr(existing_config, 'oauth_client_id', None)

            logger.debug("Deleting server config", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "config_id": config_id
            })

            success = block_on(server_config_manager.delete_config(
                config_id=config_id,
                db=db,
                tenant_id=tenant_id
            ))

            if not success:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Server config '{config_id}' not found"
                )

            # Audit successful deletion
            audit.log(
                action="server_config_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.SERVER_CONFIGS,
                resource_id=config_id,
                details={
                    "mcp_server_id": mcp_server_id,
                    "oauth_client_id": oauth_client_id
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("Server config deleted", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "config_id": config_id
            })

            block_on(invalidate_proxy_cache(request, "server_config", tenant_id, config_id, "delete"))

            return {
                "message": "Server config deleted successfully",
                "config_id": config_id
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to delete server config", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "config_id": config_id,
                "error": str(e)
            }, exc_info=True)

            # Audit failed deletion
            audit.log(
                action="server_config_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=False,
                resource_type=TenantResourceTypes.SERVER_CONFIGS,
                resource_id=config_id,
                details={
                    "error": str(e),
                    "error_type": type(e).__name__
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete server config: {str(e)}"
            )

    @router.get("/by-server/{mcp_server_id}")
    def get_configs_for_server(
            mcp_server_id: str,
            request: Request,
            enabled_only: bool = Query(False),
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.SERVER_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.SERVER_CONFIG_READ))
    ):
        """
        Get all configurations for a specific MCP server in current tenant
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            configs = block_on(server_config_manager.get_configs_by_server(
                mcp_server_id=mcp_server_id,
                db=db,
                tenant_id=tenant_id,
                enabled_only=enabled_only
            ))

            return {
                "mcp_server_id": mcp_server_id,
                "configs": configs,
                "total": len(configs)
            }

        except Exception as e:
            logger.error("Failed to get configs for server", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "mcp_server_id": mcp_server_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get configs for server: {str(e)}"
            )

    @router.get("/by-oauth-client/{oauth_client_id}")
    def get_configs_for_oauth_client(
            oauth_client_id: str,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.SERVER_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.SERVER_CONFIG_READ))
    ):
        """
        Get all configurations using a specific OAuth client in current tenant
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            configs = block_on(server_config_manager.get_configs_by_oauth_client(
                oauth_client_id=oauth_client_id,
                db=db,
                tenant_id=tenant_id
            ))

            return {
                "oauth_client_id": oauth_client_id,
                "configs": configs,
                "total": len(configs)
            }

        except Exception as e:
            logger.error("Failed to get configs for OAuth client", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "oauth_client_id": oauth_client_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get configs for OAuth client: {str(e)}"
            )

    @router.get("/by-group/{group_id}")
    def get_configs_for_group(
            group_id: str,
            request: Request,
            enabled_only: bool = Query(False),
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.SERVER_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.SERVER_CONFIG_READ)),
    ):
        """
        Get all configurations assigned to a specific user group in current tenant
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            configs = block_on(server_config_manager.get_configs_by_group(
                group_id=group_id,
                db=db,
                tenant_id=tenant_id,
                enabled_only=enabled_only
            ))

            return {
                "group_id": group_id,
                "configs": configs,
                "total": len(configs)
            }

        except Exception as e:
            logger.error("Failed to get configs for group", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get configs for group: {str(e)}"
            )

    return router