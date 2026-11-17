# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Gateway Configuration API Endpoints

Manages tenant-specific health check settings for MCP Servers and AI Models
"""

from typing import Optional, Union
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session

from app.api.v1.models.admin.gateway_config import AIHealthCheckSettings, MCPHealthCheckSettings, \
    HealthCheckConfigResponse, UpdateHealthCheckConfigRequest
from app.constants.tenant import TenantResourceTypes
from app.database.models import TenantAdmin, HealthCheckConfig
from app.core.logging_system import BrutorLogger
from app.database import get_db
from app.core.admin_user.dependencies import (
    require_permission,
    require_tenant_resource_access, get_audit_logger
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.admin_user.admin_principal import AdminPrincipal
from app.middleware.trusted_proxies import client_ip


def create_gateway_config_admin_router(logger: Optional[BrutorLogger] = None) -> APIRouter:
    """
    Create router for Gateway Configuration admin endpoints with audit logging

    Args:
        logger: Optional logger instance

    Returns:
        APIRouter configured with GatewayConfig endpoints
    """
    if logger is None:
        logger = BrutorLogger("gateway-config")

    router = APIRouter(prefix="/admin/gateway-config", tags=["Gateway Configuration"])

    # =============================================================================
    # API Endpoints
    # =============================================================================

    @router.get(
        "/health-config",
        response_model=HealthCheckConfigResponse)
    def get_health_check_config(
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GATEWAY_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.GATEWAY_CONFIG_READ)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Get health check configuration for current tenant

        Returns the health check settings for both MCP Servers and AI Models.
        If no configuration exists, creates and returns default configuration.

        **Permissions Required:**
        - gateway:config:read
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            # Check if config exists
            existing_config = db.query(HealthCheckConfig).filter(
                HealthCheckConfig.tenant_id == tenant_id
            ).first()

            config_created = False
            if not existing_config:
                config_created = True

            config = get_or_create_config(db, tenant_id, admin_id)
            db.flush()

            # Audit if new config was created
            if config_created:
                audit.log(
                    action="gateway_config_create",
                    admin=admin,
                    admin_type=admin_type,
                    tenant_id=tenant_id,
                    success=True,
                    resource_type=TenantResourceTypes.GATEWAY_CONFIGS.value,
                    resource_id=config.id,
                    details={
                        "config_id": config.id,
                        "action": "auto_created_defaults"
                    },
                    ip_address=client_ip(request),
                    user_agent=request.headers.get('user-agent'),
                    request_method=request.method,
                    request_path=str(request.url.path),
                    db=db
                )

            return HealthCheckConfigResponse(**config.to_dict())

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching health check config: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch health check configuration: {str(e)}"
            )

    @router.patch(
        "/health-config",
        response_model=HealthCheckConfigResponse)
    def update_health_check_config(
            request: Request,
            update_request: UpdateHealthCheckConfigRequest,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GATEWAY_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.GATEWAY_CONFIG_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Update health check configuration

        Updates the health check settings of the dashboard, MCP Servers and
        AI Models. Only provided settings are updated; omitted settings remain
        unchanged. The settings nothing reads (timeouts, retries, the recovery
        interval, the MCP failure threshold, the AI test prompt and token
        count, the dashboard's show flags) are not available in this release:
        a value is refused, and a response shows null.

        **Request Body:**
        ```json
        {
            "dashboard_settings": {
                "enabled": true,
                "interval_seconds": 60
            },
            "mcp_settings": {
                "enabled": true,
                "interval_seconds": 300,
                "pause_when_inactive": true
            },
            "ai_settings": {
                "enabled": true,
                "interval_seconds": 300,
                "failure_threshold": 3,
                "pause_when_inactive": true
            }
        }
        ```

        **Permissions Required:**
        - gateway:config:write
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            config = get_or_create_config(db, tenant_id, admin_id)

            # Track changes for audit
            changes = {}

            # Build update dict and track changes
            update_data = {}

            # The dashboard's settings are saved too.
            if update_request.dashboard_settings:
                dashboard_changes = {}
                dashboard_dict = update_request.dashboard_settings.dict(exclude_unset=True, exclude_none=True)
                for field, new_value in dashboard_dict.items():
                    old_value = getattr(config, f'dashboard_{field}')
                    if old_value != new_value:
                        dashboard_changes[field] = {"old": old_value, "new": new_value}

                if dashboard_changes:
                    changes['dashboard_settings'] = dashboard_changes
                    update_data['dashboard_settings'] = dashboard_dict

            if update_request.mcp_settings:
                mcp_changes = {}
                mcp_dict = update_request.mcp_settings.dict(exclude_unset=True, exclude_none=True)
                for field, new_value in mcp_dict.items():
                    old_value = getattr(config, f'mcp_{field}')
                    if old_value != new_value:
                        mcp_changes[field] = {"old": old_value, "new": new_value}

                if mcp_changes:
                    changes['mcp_settings'] = mcp_changes
                    update_data['mcp_settings'] = mcp_dict

            if update_request.ai_settings:
                ai_changes = {}
                ai_dict = update_request.ai_settings.dict(exclude_unset=True, exclude_none=True)
                for field, new_value in ai_dict.items():
                    old_value = getattr(config, f'ai_{field}')
                    if old_value != new_value:
                        ai_changes[field] = {"old": old_value, "new": new_value}

                if ai_changes:
                    changes['ai_settings'] = ai_changes
                    update_data['ai_settings'] = ai_dict

            # Update configuration
            config.update_from_dict(update_data, updated_by=admin_id)
            db.flush()

            logger.info(f"Updated health check config for tenant {tenant_id} by admin {admin_id}")

            # Audit success
            audit.log(
                action="gateway_config_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.GATEWAY_CONFIGS.value,
                resource_id=config.id,
                details={
                    "config_id": config.id,
                    "changes": changes
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            db.flush()

            return HealthCheckConfigResponse(**config.to_dict())

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating health check config: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update health check configuration: {str(e)}"
            )

    @router.post(
        "/health-config/reset",
        response_model=HealthCheckConfigResponse)
    def reset_health_check_config(
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GATEWAY_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.GATEWAY_CONFIG_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """
        Reset health check configuration to defaults

        Resets all health check settings to their default values.

        **Permissions Required:**
        - gateway:config:write
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            config = get_or_create_config(db, tenant_id, admin_id)

            # Reset to defaults
            defaults = HealthCheckConfig.get_default_config()
            config.update_from_dict({
                'mcp_settings': {
                    'enabled': defaults['mcp_enabled'],
                    'interval_seconds': defaults['mcp_interval_seconds'],
                    'timeout_seconds': defaults['mcp_timeout_seconds'],
                    'retry_attempts': defaults['mcp_retry_attempts'],
                    'failure_threshold': defaults['mcp_failure_threshold'],
                    'pause_when_inactive': defaults['mcp_pause_when_inactive'],
                    'recovery_check_interval': defaults['mcp_recovery_check_interval']
                },
                'ai_settings': {
                    'enabled': defaults['ai_enabled'],
                    'interval_seconds': defaults['ai_interval_seconds'],
                    'timeout_seconds': defaults['ai_timeout_seconds'],
                    'retry_attempts': defaults['ai_retry_attempts'],
                    'failure_threshold': defaults['ai_failure_threshold'],
                    'pause_when_inactive': defaults['ai_pause_when_inactive'],
                    'recovery_check_interval': defaults['ai_recovery_check_interval'],
                    'test_prompt': defaults['ai_test_prompt'],
                    'max_tokens': defaults['ai_max_tokens']
                }
            }, updated_by=admin_id)

            db.flush()

            logger.info(f"Reset health check config to defaults for tenant {tenant_id} by admin {admin_id}")

            # Audit success
            audit.log(
                action="gateway_config_reset",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.GATEWAY_CONFIGS.value,
                resource_id=config.id,
                details={
                    "config_id": config.id,
                    "action": "reset_to_defaults"
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            db.flush()

            return HealthCheckConfigResponse(**config.to_dict())

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error resetting health check config: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to reset health check configuration: {str(e)}"
            )

    @router.get(
        "/health-config/defaults",
        response_model=HealthCheckConfigResponse)
    async def get_default_health_check_config(
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.GATEWAY_CONFIGS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.GATEWAY_CONFIG_READ))
    ):
        """
        Get default health check configuration values

        Returns the default configuration values without creating/modifying any records.
        Useful for displaying default values in UI.

        **Permissions Required:**
        - gateway:config:read
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            defaults = HealthCheckConfig.get_default_config()

            return HealthCheckConfigResponse(
                id='default',
                tenant_id=tenant_id,
                mcp_settings=MCPHealthCheckSettings(
                    enabled=defaults['mcp_enabled'],
                    interval_seconds=defaults['mcp_interval_seconds'],
                    pause_when_inactive=defaults['mcp_pause_when_inactive'],
                ),
                ai_settings=AIHealthCheckSettings(
                    enabled=defaults['ai_enabled'],
                    interval_seconds=defaults['ai_interval_seconds'],
                    failure_threshold=defaults['ai_failure_threshold'],
                    pause_when_inactive=defaults['ai_pause_when_inactive'],
                ),
                created_at=None,
                updated_at=None,
                created_by=None,
                updated_by=None
            )

        except Exception as e:
            logger.error(f"Error fetching default health check config: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch default configuration: {str(e)}"
            )

    # =============================================================================
    # Helper Functions
    # =============================================================================

    def get_or_create_config(db: Session, tenant_id: str, admin_id: str = None) -> HealthCheckConfig:
        """Get existing config or create default config for tenant.

        Concurrency contract: `health_check_configs.tenant_id` carries
        a UNIQUE index (`ix_health_check_configs_tenant`). The naive
        SELECT-then-INSERT shape used to race when two admin requests
        for the same tenant arrived close together — the second
        INSERT blocked on the UNIQUE-index page lock waiting for the
        first to commit, and if the first sat in a longer transaction
        the second hit `statement_timeout` (60s default) and 5xx'd.

        We now use `INSERT … ON CONFLICT DO NOTHING` so concurrent
        inserters don't block each other on the index — at most one
        wins, the rest find the row on the follow-up SELECT. The
        re-SELECT after the upsert is necessary because
        `RETURNING *` returns no row on a CONFLICT skip.
        """
        config = db.query(HealthCheckConfig).filter(
            HealthCheckConfig.tenant_id == tenant_id
        ).first()
        if config:
            return config

        # Build the candidate row, then UPSERT. PostgreSQL-specific
        # `ON CONFLICT` — sqlalchemy.dialects.postgresql.insert.
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        defaults = HealthCheckConfig.get_default_config()
        candidate = {
            "tenant_id": tenant_id,
            "created_by": admin_id,
            **defaults,
        }
        # Stamp the SQLAlchemy default for `id` on our side (the
        # column has a Python-side `default=` lambda which sqlalchemy
        # would otherwise compute during ORM flush — we're going
        # through Core here so we apply the default explicitly).
        if "id" not in candidate or candidate["id"] is None:
            candidate["id"] = HealthCheckConfig.__table__.c.id.default.arg(None)

        stmt = (
            pg_insert(HealthCheckConfig.__table__)
            .values(**candidate)
            .on_conflict_do_nothing(index_elements=["tenant_id"])
        )
        db.execute(stmt)
        db.flush()

        # Re-SELECT — either we just inserted or a concurrent
        # inserter beat us. Either way the row exists now.
        config = db.query(HealthCheckConfig).filter(
            HealthCheckConfig.tenant_id == tenant_id
        ).first()
        if config is None:
            # Should be unreachable: ON CONFLICT DO NOTHING + the
            # follow-up SELECT means the row is present unless a
            # concurrent DELETE landed in the same window. Surface
            # this loudly rather than returning None to the caller.
            raise RuntimeError(
                f"health_check_configs row for tenant {tenant_id!r} "
                "vanished between upsert and re-select"
            )
        logger.debug(f"Created or fetched health check config for tenant {tenant_id}")
        return config

    return router