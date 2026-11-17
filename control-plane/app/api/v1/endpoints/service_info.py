# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Control Plane service information
What the control plane says about itself: `/`, `/auth/info`, `/version`, `/health`.
Health, stats, and runtime metrics live on the proxy (control) server.
"""

import time
from datetime import datetime, timezone
from typing import Union

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.sync_bridge import block_on
from app.constants.admin import AdminType
from app.database import get_db
from app.composition.principals import PRINCIPALS
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.mcp_server import MCPServer
from app.database.models.virtual_server import VirtualServer
from app.core.admin_user.dependencies import (
    require_permission,
    get_current_admin,
    tenant_scope,
)
from app.core.admin_user.permissions import Permission
from app.core.logging_system import BrutorLogger
from app.services.config_manager import ConfigManager


# What `/`, `/version` and `/health` answer as `service` (the product is
# Brutor Agent Control; this is its Control Plane).
SERVICE_NAME = "Brutor Agent Control Plane"


def create_service_info_router(
    config_manager: ConfigManager,
    logger: BrutorLogger
) -> APIRouter:
    """
    Create control plane router for gateway configuration and management.

    Multi-Tenancy Support:
    - System admins: See platform-wide configuration
    - Tenant admins: See tenant-scoped configuration
    """

    start_time = time.time()

    router = APIRouter(tags=["Control Plane"])

    @router.get("/")
    def root(
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(require_permission(Permission.GATEWAY_STATUS_READ))
    ):
        """
        Control plane root endpoint with configuration overview.

        Multi-Tenancy:
        - System admins see platform-wide configuration counts
        - Tenant admins see tenant-scoped configuration counts
        """
        admin, admin_type = admin_and_type
        admin_id = admin.id
        admin_tenant_id = admin.tenant_id if hasattr(admin, 'tenant_id') else None

        # Build query based on admin type: a tenant administrator's own
        # tenant, the platform for the system administrator, nothing (403)
        # for a type nobody registered.
        if tenant_scope(admin, admin_type):
            total_servers = db.query(MCPServer).filter(
                MCPServer.tenant_id == admin_tenant_id
            ).count()
            enabled_servers = db.query(MCPServer).filter(
                MCPServer.tenant_id == admin_tenant_id,
                MCPServer.enabled == True
            ).count()
            virtual_servers_count = db.query(VirtualServer).filter(
                VirtualServer.tenant_id == admin_tenant_id
            ).count()
            scope = f"tenant: {admin_tenant_id}"
        else:
            total_servers = db.query(MCPServer).count()
            enabled_servers = db.query(MCPServer).filter_by(enabled=True).count()
            virtual_servers_count = db.query(VirtualServer).count()
            scope = "platform-wide"

        logger.debug("Control plane root endpoint accessed", context={
            "admin_id": admin_id,
            "admin_type": admin_type,
            "admin_username": admin.username,
            "tenant_id": admin_tenant_id if admin_type == AdminType.TENANT_ADMIN_TYPE.value else None
        })

        return {
            "service": SERVICE_NAME,
            "version": config_manager.get_version(),
            "status": "running",
            "rbac_enabled": True,
            "authenticated_user": {
                "id": str(admin_id),
                "username": admin.username,
                "email": admin.email,
                "full_name": admin.full_name,
                "is_active": admin.is_active,
                "admin_type": admin_type,
                "tenant_id": admin_tenant_id if admin_type == AdminType.TENANT_ADMIN_TYPE.value else None,
                "last_login": admin.last_login.isoformat() + "Z" if admin.last_login else None
            },
            "data_scope": scope,
            "gateway_base_url": config_manager.get_gateway_base_url(),
            "configuration": {
                "registered_servers": total_servers,
                "enabled_servers": enabled_servers,
                "virtual_servers": virtual_servers_count
            }
        }

    @router.get("/auth/info")
    async def get_auth_info():
        """Get authentication information - public endpoint for client discovery"""

        return {
            "rbac_enabled": True,
            "authentication_required": True,
            "version": config_manager.get_version(),
            "authentication_methods": ["jwt", "session"],
            "admin_types": ["system", "tenant"],
            "features": {
                "jwt_auth": True,
                "admin_authentication": True,
                "role_based_access": True,
                "audit_logging": True,
                "multi_role_support": True,
                "permission_granularity": True,
                "device_fingerprinting": True,
                "risk_scoring": True,
                "session_security": True,
                "multi_tenancy": True,
                "tenant_isolation": True
            },
            "endpoints": {
                "login": "/v1/admin/users/tenant/login",
                "logout": "/v1/admin/users/logout",
                "profile": "/auth/profile",
                "refresh": "/auth/refresh"
            },
            "rbac": {
                "enabled": True,
                "admin_types": {
                    "system": {
                        "description": "Platform-wide access",
                        "scope": "all tenants"
                    },
                    "tenant": {
                        "description": "Tenant-scoped access",
                        "scope": "single tenant"
                    }
                },
                "roles_available": True,
                "permissions_granular": True,
                "device_fingerprinting": True,
                "risk_scoring": True
            }
        }

    @router.get("/version")
    async def get_version():
        """Get control plane version - public endpoint"""

        return {
            "service": SERVICE_NAME,
            "version": config_manager.get_version(),
            "features": {
                "multi_tenancy": True,
                "rbac": True,
                "audit_logging": True,
                "mcp_server_management": True,
                "virtual_server_management": True,
                "llm_model_management": True,
                "api_key_management": True,
                "user_management": True,
                "guardrail_management": True
            }
        }

    @router.get("/health")
    def control_plane_health_check(
            request: Request,
            db: Session = Depends(get_db),
    ):
        """
        Control plane health check — public for Docker/load balancer probes.
        Returns basic status without auth. With a valid admin token, returns
        detailed component info.
        """
        uptime_seconds = time.time() - start_time

        # Basic health response (always returned, no auth required)
        basic = {
            "status": "healthy",
            "service": SERVICE_NAME,
            "version": config_manager.get_version(),
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "uptime_seconds": uptime_seconds,
        }

        # Test database connectivity
        try:
            db.execute(text("SELECT 1"))
        except Exception as e:
            basic["status"] = "degraded"
            basic["database"] = "unhealthy"
            return basic

        # If an admin token is present, add detailed info
        admin = None
        try:
            from app.core.admin_user.dependencies import get_current_admin
            admin = block_on(get_current_admin(request, db))
        except Exception:
            pass

        if admin:
            tenant_id = admin.tenant_id if hasattr(admin, 'tenant_id') else None
            is_tenant_admin = tenant_id is not None

            components = {
                "config_manager": "healthy",
                "rbac_system": "healthy",
                "database": "healthy",
            }

            try:
                if is_tenant_admin:
                    db.query(TenantAdmin).filter(
                        TenantAdmin.tenant_id == tenant_id
                    ).count()
                else:
                    # A platform-wide administrator: the administrator
                    # table of every registered principal answers.
                    for principal in PRINCIPALS.specs():
                        if principal.find_admin is not None:
                            principal.find_admin(db, getattr(admin, "id", None))
                components["rbac_database"] = "healthy"
            except Exception as e:
                components["database"] = "unhealthy"
                components["rbac_database"] = "unhealthy"
                components["database_error"] = str(e)

            all_healthy = all(
                status == "healthy"
                for status in components.values()
                if isinstance(status, str)
            )

            basic["status"] = "healthy" if all_healthy else "degraded"
            basic["components"] = components
            basic["tenant_context"] = {
                "tenant_id": tenant_id,
                "scope": "tenant" if is_tenant_admin else "platform"
            }

        return basic

    return router
