# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM Router Management Endpoints
Multi-Tenant with RBAC and Audit Logging
Includes Routing Groups CRUD for load balancing
"""

import uuid
from typing import Union, Optional

from fastapi import APIRouter, HTTPException, Depends, Query, Request
from sqlalchemy.orm import Session

from app.core.sync_bridge import block_on
from app.constants.tenant import TenantResourceTypes
from app.database import get_db
from app.services.tenant.tenant_service import TenantService
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.llm_routing_group import LLMRoutingGroup, LLMRoutingGroupTier
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    require_permission,
    get_audit_logger,
    require_tenant_resource_access,
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.api.v1.models.admin.llm_routing_group import (
    # Status Models
    RouterStatusResponse,
    RouterHealthCheckResponse,

    # Routing Group Models
    RoutingGroupTiersUpdate,
    RoutingGroupTiersResponse,
    RoutingGroupCreate,
    RoutingGroupUpdate,
    RoutingGroupResponse,
    RoutingGroupListResponse,
    RoutingGroupDeleteResponse,

    # Testing Models
    RouterTestRequest,
    RouterTestResponse,
    RouterTestHistoryResponse,

    # Deployment Models
    ModelDeployment,
    DeploymentListResponse,

    # Statistics Models
    RouterStatistics,

    # Enums
    HealthStatus,
)

from app.services.llm.router.llm_router_service import LLMRouterService
from app.services.cache_invalidation import invalidate_proxy_cache
from app.core.logging_system import BrutorLogger
from app.middleware.trusted_proxies import client_ip


def _require_forwardable_fallbacks(db: Session, tenant_id: str, group_data: dict) -> None:
    """Refuse (422) a routing group whose fallback lists name a row the
    gateway's chat forwarder would drop on every request
    (`app/services/llm/forward_candidates.py`)."""
    from app.services.llm import forward_candidates

    problem = forward_candidates.group_fallbacks_problem(db, tenant_id, group_data)
    if problem:
        raise HTTPException(status_code=422, detail=problem)


def create_llm_router_admin_router(
    router_service: LLMRouterService,
    logger: BrutorLogger
) -> APIRouter:
    """
    Factory function to create LLM router admin endpoints with full tenant isolation

    Args:
        router_service: LLM router service instance
        logger: Logger instance

    Returns:
        Configured APIRouter with all LLM router management endpoints
    """

    router = APIRouter(prefix="/admin/llm-router", tags=["LLM Router Admin"])

    # =========================================================================
    # Status Endpoints
    # =========================================================================

    @router.get(
        "/status",
        response_model=RouterStatusResponse,
        summary="Get router status",
        description="""
        Get comprehensive router status and metrics.
        
        Includes:
        - Deployment statistics
        - Routing group counts
        - Cache status
        - Performance metrics
        
        Tenant isolation: Returns status for user's tenant
        """
    )
    def get_router_status(
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.LLM_ROUTER_READ))
    ):
        """Get router status with tenant context"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            status_data = block_on(router_service.get_router_status(
                db=db,
                tenant_id=tenant_id
            ))

            logger.debug("Router status retrieved", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "total_deployments": status_data.get("total_deployments"),
                "total_routing_groups": status_data.get("total_routing_groups")
            })

            return status_data

        except Exception as e:
            logger.error("Failed to get router status", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to retrieve router status: {str(e)}"
            )

    @router.get(
        "/health",
        response_model=RouterHealthCheckResponse,
        summary="Quick health check",
        description="""
        Quick health check for router availability.
        
        Returns:
        - Overall router status
        - Available deployments count
        - Available routing groups count
        - Cache availability
        
        Tenant isolation: Returns health for user's tenant
        """
    )
    def router_health_check(
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.LLM_ROUTER_READ))
    ):
        """Quick health check for monitoring"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes
        admin_id = admin.id

        try:
            health_data = block_on(router_service.get_router_health(
                db=db,
                tenant_id=tenant_id
            ))

            logger.debug("Router health check completed", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "status": health_data.get("status")
            })

            return health_data

        except Exception as e:
            logger.warning("Router health check failed", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "error": str(e)
            })

            raise HTTPException(
                status_code=503,
                detail=f"Router health check failed: {str(e)}"
            )

    # =========================================================================
    # Routing Group CRUD Endpoints
    # =========================================================================

    @router.get(
        "/routing-groups",
        response_model=RoutingGroupListResponse,
        summary="List routing groups",
        description="""
        List all routing groups for the tenant.
        
        Routing groups enable load balancing across multiple models with:
        - Automatic failover and cooldown
        - Cost optimization and budget tracking
        - Performance-based routing strategies
        - Caching and alerting
        
        Query Parameters:
        - enabled_only: Filter to only enabled groups (default: false)
        
        Tenant isolation: Returns only groups for user's tenant
        """
    )
    def list_routing_groups(
        request: Request,
        enabled_only: bool = Query(False, description="Only return enabled routing groups"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.LLM_ROUTER_READ))
    ):
        """List all routing groups for tenant"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes
        admin_id = admin.id

        try:
            result = block_on(router_service.get_routing_groups(
                db=db,
                tenant_id=tenant_id,
                enabled_only=enabled_only
            ))

            logger.debug("Routing groups listed", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "total": result.get("total"),
                "enabled_only": enabled_only
            })

            return result

        except Exception as e:
            logger.error("Failed to list routing groups", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to list routing groups: {str(e)}"
            )

    @router.get(
        "/routing-groups/{group_id}",
        response_model=RoutingGroupResponse,
        summary="Get routing group",
        description="""
        Get detailed configuration for a specific routing group.
        
        Returns complete configuration including:
        - Routing strategy and load balancing settings
        - Failure handling and cooldown configuration
        - Fallback and caching settings
        - Alerting and cost tracking
        - Number of models in the group
        
        Tenant isolation: Only accessible if group belongs to user's tenant
        """
    )
    def get_routing_group(
        group_id: str,
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.LLM_ROUTER_READ))
    ):
        """Get routing group details"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes
        admin_id = admin.id

        try:
            routing_group = block_on(router_service.get_routing_group(
                db=db,
                tenant_id=tenant_id,
                group_id=group_id
            ))

            logger.debug("Routing group retrieved", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "group_id": group_id
            })

            return routing_group

        except ValueError as e:
            # Not found
            logger.warning("Routing group not found", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "group_id": group_id
            })

            raise HTTPException(
                status_code=404,
                detail=f"Routing group {group_id} not found"
            )

        except Exception as e:
            logger.error("Failed to get routing group", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to retrieve routing group: {str(e)}"
            )

    @router.post(
        "/routing-groups",
        response_model=RoutingGroupResponse,
        status_code=201,
        summary="Create routing group",
        description="""
        Create a new routing group for load balancing multiple models.
        
        Required fields:
        - id: Unique identifier (used in API calls)
        - name: Display name
        
        Optional configuration:
        - routing_strategy: Load balancing strategy (default: usage-based-routing-v2)
        - cooldown_time: Cooldown duration in seconds (default: 60)
        - alerting_enabled: Enable alerts (default: false)
        - rpm_limit / tpm_limit: Per-routing-group rate caps

        Spend caps and response caching live elsewhere — configure
        them via `usage_limits` on the relevant resource group and
        `semantic_cache_configs` respectively (see the Resource
        Groups + Semantic Cache admin tabs). Both apply across the
        whole tenant uniformly, including routing groups.

        Requires LLM_ROUTER_UPDATE permission
        Audit logged: Yes
        Tenant isolation: Group created for user's tenant
        """
    )
    def create_routing_group(
        routing_group: RoutingGroupCreate,
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.LLM_ROUTER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Create new routing group with audit logging"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Feature gate.
        TenantService.check_feature_access(db, tenant_id, 'model_routing')

        # Eager load admin attributes
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        _require_forwardable_fallbacks(db, tenant_id, routing_group.model_dump())

        try:
            # Convert to dict
            group_data = routing_group.model_dump()

            logger.debug("Creating routing group", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "group_id": group_data.get("id"),
                "routing_strategy": group_data.get("routing_strategy")
            })

            result = block_on(router_service.create_routing_group(
                db=db,
                tenant_id=tenant_id,
                group_data=group_data
            ))

            # Audit log
            audit.log(
                admin_id=admin_id,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                action="CREATE_ROUTING_GROUP",
                resource_type="llm_routing_group",
                resource_id=result["id"],
                details={
                    "name": result["name"],
                    "routing_strategy": result["routing_strategy"],
                    "enabled": result["enabled"]
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("Routing group created", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "group_id": result["id"]
            })

            block_on(invalidate_proxy_cache(request, "routing_group", tenant_id, result["id"], "create"))

            return result

        except ValueError as e:
            # Already exists
            logger.warning("Routing group creation failed - already exists", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "group_id": group_data.get("id")
            })

            raise HTTPException(
                status_code=409,
                detail=str(e)
            )

        except Exception as e:
            logger.error("Failed to create routing group", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to create routing group: {str(e)}"
            )

    @router.patch(
        "/routing-groups/{group_id}",
        response_model=RoutingGroupResponse,
        summary="Update routing group",
        description="""
        Update routing group configuration.
        
        All fields are optional - only provided fields will be updated.
        
        Common updates:
        - Change routing_strategy for different load balancing
        - Update cooldown_time and num_retries for failure handling
        - Tune rpm_limit / tpm_limit for rate caps
        - Enable/disable alerting

        Spend caps now live on the resource group's
        `usage_limits` rows; response caching is configured per-
        tenant via `semantic_cache_configs`. Both surfaces apply
        uniformly across all models, including routing groups.

        Requires LLM_ROUTER_UPDATE permission
        Audit logged: Yes
        Tenant isolation: Only accessible if group belongs to user's tenant
        """
    )
    def update_routing_group(
        group_id: str,
        updates: RoutingGroupUpdate,
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.LLM_ROUTER_UPDATE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update routing group with audit logging"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        _require_forwardable_fallbacks(db, tenant_id, updates.model_dump(exclude_unset=True))

        try:
            # Only what the client sent: an explicit null clears a clearable
            # field (ROUTING_GROUP_CLEARABLE_FIELDS), an omitted key is untouched.
            update_dict = updates.model_dump(exclude_unset=True)

            logger.debug("Updating routing group", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "group_id": group_id,
                "updates": list(update_dict.keys())
            })

            result = block_on(router_service.update_routing_group(
                db=db,
                tenant_id=tenant_id,
                group_id=group_id,
                update_data=update_dict
            ))

            # Audit log
            audit.log(
                admin_id=admin_id,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                action="UPDATE_ROUTING_GROUP",
                resource_type="llm_routing_group",
                resource_id=group_id,
                details={
                    "updates": list(update_dict.keys()),
                    "name": result["name"]
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            logger.info("Routing group updated", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "group_id": group_id
            })

            block_on(invalidate_proxy_cache(request, "routing_group", tenant_id, group_id, "update"))

            return result

        except ValueError as e:
            # Not found
            logger.warning("Routing group update failed - not found", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "group_id": group_id
            })

            raise HTTPException(
                status_code=404,
                detail=f"Routing group {group_id} not found"
            )

        except Exception as e:
            logger.error("Failed to update routing group", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to update routing group: {str(e)}"
            )

    @router.delete(
        "/routing-groups/{group_id}",
        response_model=RoutingGroupDeleteResponse,
        summary="Delete routing group",
        description="""
        Delete a routing group.
        
        The models assigned to the group are taken out of it (they stay,
        without a routing group) and the group's virtual model is deleted.
        
        Requires LLM_ROUTER_DELETE permission
        Audit logged: Yes
        Tenant isolation: Only accessible if group belongs to user's tenant
        """
    )
    def delete_routing_group(
        group_id: str,
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.LLM_ROUTER_DELETE)),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete routing group with validation and audit logging"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            result = block_on(router_service.delete_routing_group(
                db=db,
                tenant_id=tenant_id,
                group_id=group_id
            ))

            # Audit log
            audit.log(
                admin_id=admin_id,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                action="DELETE_ROUTING_GROUP",
                resource_type="llm_routing_group",
                resource_id=group_id,
                details={
                    "models_disconnected": result.get("models_disconnected", 0)
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            block_on(invalidate_proxy_cache(request, "routing_group", tenant_id, group_id, "delete"))

            return result

        except ValueError as e:
            # Not found
            logger.warning("Routing group delete failed - not found", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "group_id": group_id
            })

            raise HTTPException(
                status_code=404,
                detail=f"Routing group {group_id} not found"
            )

        except Exception as e:
            logger.error("Failed to delete routing group", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete routing group: {str(e)}"
            )

    # =========================================================================
    # Testing Endpoints
    # =========================================================================

    @router.post(
        "/test",
        response_model=RouterTestResponse,
        summary="Test routing",
        description=(
            "Sends one real chat completion through the gateway to the routing "
            "group and reports which member model answered, the status, the "
            "latency and the cost. The request reaches the provider and is "
            "billed like any other. Requires `llm:router:test`; reading the "
            "router's configuration (`llm:router:read`) does not open it. "
            "Only routing groups of the caller's tenant can be tested."
        ),
    )
    def test_routing(
        test_request: RouterTestRequest,
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.LLM_ROUTER_TEST))
    ):
        """Send a real request through the routing group."""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes
        admin_id = admin.id

        try:
            result = block_on(router_service.test_routing(
                db=db,
                tenant_id=tenant_id,
                message=test_request.message,
                routing_group_id=test_request.routing_group_id,
            ))

            logger.debug("Routing test completed", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "routing_group_id": test_request.routing_group_id,
                "selected_model": result.get("selected_model"),
                "status_code": result.get("status_code"),
            })

            return result

        except ValueError as e:
            # Routing group not found / not part of this tenant.
            logger.warning("Routing test failed - group not found", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "routing_group_id": test_request.routing_group_id,
                "error": str(e),
            })

            raise HTTPException(
                status_code=404,
                detail=str(e)
            )

        except Exception as e:
            logger.error("Failed to test routing", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to test routing: {str(e)}"
            )

    @router.get(
        "/test-history",
        response_model=RouterTestHistoryResponse,
        summary="Recent router test results for the tenant",
        description=(
            "Surfaces the in-memory ring buffer of recent test "
            "results recorded by `POST /v1/admin/llm-router/test`. "
            "Optional `routing_group_id` filter narrows the list. "
            "Used by the Status tab to show recent test activity "
            "per group. Bounded to 50 per tenant; not persisted "
            "across control-plane restarts."
        ),
    )
    async def get_router_test_history(
        routing_group_id: Optional[str] = None,
        limit: int = 20,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)
        ),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.LLM_ROUTER_READ)
        ),
    ):
        admin, tenant_id, admin_type = admin_and_tenant
        items = router_service.get_test_history(
            tenant_id=tenant_id,
            routing_group_id=routing_group_id,
            limit=limit,
        )
        return {"items": items, "total": len(items)}

    # =========================================================================
    # Deployment List Endpoints
    # =========================================================================

    @router.get(
        "/deployments",
        response_model=DeploymentListResponse,
        summary="List model deployments",
        description="""
        List all model deployments available for routing.
        
        Query Parameters:
        - filter_status: Filter by health status (healthy, unhealthy, unknown)
        - routing_group_id: Filter by routing group
        - enabled_only: Only return enabled models (default: false)
        - sort_by: Sort field (name, latency, requests, error_rate)
        
        Returns model details including:
        - Health status and performance metrics
        - Routing group assignment
        - Cost information
        
        Tenant isolation: Returns only models for user's tenant
        """
    )
    def list_deployments(
        request: Request,
        filter_status: Optional[HealthStatus] = Query(None, description="Filter by health status"),
        routing_group_id: Optional[str] = Query(None, description="Filter by routing group"),
        enabled_only: bool = Query(False, description="Only return enabled deployments"),
        sort_by: Optional[str] = Query(None, description="Sort field"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.LLM_ROUTER_READ))
    ):
        """List model deployments"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes
        admin_id = admin.id

        try:
            result = block_on(router_service.list_deployments(
                db=db,
                tenant_id=tenant_id,
                filter_status=filter_status.value if filter_status else None,
                routing_group_id=routing_group_id,
                enabled_only=enabled_only,
                sort_by=sort_by
            ))

            logger.debug("Deployments listed", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "total": result.get("total"),
                "filter_status": filter_status.value if filter_status else None,
                "routing_group_id": routing_group_id
            })

            return result

        except Exception as e:
            logger.error("Failed to list deployments", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to list deployments: {str(e)}"
            )

    @router.get(
        "/deployments/{model_id}",
        response_model=ModelDeployment,
        summary="Get deployment details",
        description="""
        Get detailed information for a specific model deployment.
        
        Returns complete model information including:
        - Health status and performance metrics
        - Routing group assignment (if any)
        - Cost per 1K tokens
        - Request statistics
        
        Tenant isolation: Only accessible if model belongs to user's tenant
        """
    )
    def get_deployment(
        model_id: str,
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.LLM_ROUTER_READ))
    ):
        """Get specific deployment details"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes
        admin_id = admin.id

        try:
            deployment = block_on(router_service.get_deployment(
                model_id=model_id,
                tenant_id=tenant_id,
                db=db
            ))

            logger.debug("Deployment details retrieved", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "model_id": model_id
            })

            return deployment

        except ValueError as e:
            # Not found
            logger.warning("Deployment not found", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "model_id": model_id
            })

            raise HTTPException(
                status_code=404,
                detail=f"Model {model_id} not found"
            )

        except Exception as e:
            logger.error("Failed to get deployment", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "model_id": model_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to retrieve deployment: {str(e)}"
            )

    # =========================================================================
    # Statistics Endpoints
    # =========================================================================

    @router.get(
        "/statistics",
        response_model=RouterStatistics,
        summary="Get router statistics",
        description="""
        Get aggregated router statistics and metrics.
        
        Query Parameters:
        - start_time: Start time (ISO 8601, default: last 24 hours)
        - end_time: End time (ISO 8601, default: now)
        - group_by_model: Include per-model breakdown (default: true)
        - group_by_routing_group: Include per-routing-group breakdown (default: true)
        
        Returns:
        - Request statistics (total, success rate, error rate)
        - Performance metrics (average latency, percentiles)
        - Per-model request distribution
        - Per-routing-group request distribution
        - Cache statistics
        
        Tenant isolation: Returns statistics for user's tenant only
        """
    )
    def get_statistics(
        request: Request,
        start_time: Optional[str] = Query(None, description="Start time (ISO 8601)"),
        end_time: Optional[str] = Query(None, description="End time (ISO 8601)"),
        group_by_model: bool = Query(True, description="Include per-model breakdown"),
        group_by_routing_group: bool = Query(True, description="Include per-routing-group breakdown"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(require_permission(Permission.LLM_ROUTER_READ))
    ):
        """Get router statistics"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes
        admin_id = admin.id

        try:
            stats = block_on(router_service.get_router_statistics(
                tenant_id=tenant_id,
                db=db,
                start_time=start_time,
                end_time=end_time,
                group_by_model=group_by_model,
                group_by_routing_group=group_by_routing_group
            ))

            logger.debug("Statistics retrieved", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "total_requests": stats.get("total_requests")
            })

            return stats

        except Exception as e:
            logger.error("Failed to get statistics", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail=f"Failed to retrieve statistics: {str(e)}"
            )

    # The routing-group exact-match cache is part of the Enterprise response
    # cache: its per-tenant configuration is served by a router of
    # its own on this prefix, `llm_router_cache_config.py`.

    # =====================================================================
    # Complexity-routing tier map
    # =====================================================================

    @router.get(
        "/routing-groups/{group_id}/tiers",
        response_model=RoutingGroupTiersResponse,
        summary="Get the complexity tier -> model map",
        description=(
            "Read the tier mapping used by the `complexity-based-routing` "
            "strategy. An empty list means the strategy is inert for this "
            "group: it scores nothing and the group falls back to ordinary "
            "usage-based selection."
        ),
    )
    def get_routing_group_tiers(
        group_id: str,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.LLM_ROUTER_READ)
        ),
    ):
        admin, tenant_id, admin_type = admin_and_tenant
        group = (
            db.query(LLMRoutingGroup)
            .filter(LLMRoutingGroup.id == group_id, LLMRoutingGroup.tenant_id == tenant_id)
            .first()
        )
        if not group:
            raise HTTPException(status_code=404, detail="Routing group not found")

        rows = (
            db.query(LLMRoutingGroupTier)
            .filter(
                LLMRoutingGroupTier.routing_group_id == group_id,
                LLMRoutingGroupTier.tenant_id == tenant_id,
            )
            .order_by(LLMRoutingGroupTier.tier, LLMRoutingGroupTier.position)
            .all()
        )
        return {
            "routing_group_id": group_id,
            "tiers": [r.to_dict() for r in rows],
            "total": len(rows),
        }

    @router.put(
        "/routing-groups/{group_id}/tiers",
        response_model=RoutingGroupTiersResponse,
        summary="Replace the complexity tier -> model map",
        description=(
            "PUT-replace semantics: the supplied list becomes the whole map. "
            "An empty list clears it, which makes complexity routing inert "
            "for this group.\n\n"
            "Every model named must already be a member of this routing "
            "group. That is the governance boundary — a tier map may only "
            "narrow what the group already grants, never reach outside it. "
            "The proxy enforces the same rule at selection time, so this "
            "check is a usability guard rather than the security control."
        ),
    )
    def replace_routing_group_tiers(
        group_id: str,
        body: RoutingGroupTiersUpdate,
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)
        ),
        db: Session = Depends(get_db),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.LLM_ROUTER_UPDATE)
        ),
        audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        admin, tenant_id, admin_type = admin_and_tenant
        group = (
            db.query(LLMRoutingGroup)
            .filter(LLMRoutingGroup.id == group_id, LLMRoutingGroup.tenant_id == tenant_id)
            .first()
        )
        if not group:
            raise HTTPException(status_code=404, detail="Routing group not found")

        try:
            member_ids = {
                m.id for m in (group.models or []) if not getattr(m, "is_routing", False)
            }
            for entry in body.tiers:
                if entry.llm_model_id not in member_ids:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Model {entry.llm_model_id} is not a member of this routing "
                            "group. Add it to the group before mapping it to a tier."
                        ),
                    )

            db.query(LLMRoutingGroupTier).filter(
                LLMRoutingGroupTier.routing_group_id == group_id,
                LLMRoutingGroupTier.tenant_id == tenant_id,
            ).delete(synchronize_session=False)

            for entry in body.tiers:
                db.add(
                    LLMRoutingGroupTier(
                        id=str(uuid.uuid4()),
                        tenant_id=tenant_id,
                        routing_group_id=group_id,
                        tier=entry.tier.value,
                        llm_model_id=entry.llm_model_id,
                        position=entry.position,
                    )
                )
            db.commit()

            rows = (
                db.query(LLMRoutingGroupTier)
                .filter(
                    LLMRoutingGroupTier.routing_group_id == group_id,
                    LLMRoutingGroupTier.tenant_id == tenant_id,
                )
                .order_by(LLMRoutingGroupTier.tier, LLMRoutingGroupTier.position)
                .all()
            )

            audit.log(
                admin_id=admin.id,
                admin_type=admin_type,
                action="update",
                resource_type="llm_routing_group_tiers",
                resource_id=group_id,
                tenant_id=tenant_id,
                details={"tier_count": len(rows)},
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db,
            )

            block_on(invalidate_proxy_cache(
                request, "routing_group", tenant_id, group_id, action="update"
            ))

            return {
                "routing_group_id": group_id,
                "tiers": [r.to_dict() for r in rows],
                "total": len(rows),
            }
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            logger.error(
                "Failed to replace routing group tiers",
                context={"tenant_id": tenant_id, "group_id": group_id, "error": str(e)},
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail=f"Failed to update tier map: {str(e)}"
            )

    return router