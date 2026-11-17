# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Large Language Models (LLM) Admin Endpoints 
Multi-Tenant with RBAC and Audit Logging
"""

from ulid import ULID
from datetime import datetime, timezone
from typing import Any, Union, Optional

from fastapi import APIRouter, HTTPException, Depends, Query, Request
from sqlalchemy.orm import Session, selectinload

from app.core.sync_bridge import block_on
from app.constants.proxy import proxy_internal_url, gateway_service_api_key
from app.constants.server import ServerStatusType
from app.constants.tenant import TenantResourceTypes
from app.constants.llm import default_health_check_timeout_secs
from app.services.tenant.tenant_service import TenantService
from app.database import get_db
from app.database.models.llm_model import LLModel
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.database.models.resource_group import resource_group_llm_models
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    acting_tenant_id,
    require_permission,
    get_audit_logger,
    require_tenant_resource_access,
    get_current_admin
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.api.v1.models.admin.llm_model import (
    LLModelCreate,
    LLModelUpdate,
    HealthCheckRequest,
    HealthCheckResponse,
    LLModelListResponse,
    LLModelDetailResponse,
    ModelGroupAssignment,
    ModelGroupUnassignment,
    UserAccessibleModelsRequest,
    UserAccessibleModelsResponse,
    LLMModelBulkHealthCheckResponse, BulkHealthCheckRequest, LLModelSearchRequest,
    LLModelSearchWithSummaryResponse, LLModelSearchResponse
)
from app.services.llm.model.llm_models_manager import (
    HEALTH_CHECK_BACKOFF_MULTIPLIER,
    PROBE_CATALOG,
    LLModelsManager,
    should_back_off_background_check,
)
from app.database.models.gateway_config import HealthCheckConfig
from app.services.llm.model.llm_access_service import DEFAULT_GROUP_SENTINEL, LLMAccessService
from app.services.llm.model.llm_search_service import LLModelSearchService
from app.services.notifications import notify_group_members_about_resource_change
from app.core.logging_system import BrutorLogger
from app.services.oauth.encryption_service import EncryptionService
from app.services.cache_invalidation import invalidate_proxy_cache

import httpx
from app.middleware.trusted_proxies import client_ip


def _resolve_provider_id(
    db: Session,
    tenant_id: str,
    *,
    provider_id: Optional[str],
    provider: Optional[str],
) -> str:
    """Resolve a model row's `provider_id`.

    Providers are first-class entities. The admin
    endpoint accepts EITHER `provider_id` (preferred — admin UI
    dropdown emits this) OR the legacy `provider` slug. This
    helper normalises both into a `provider_id`, looking up the
    slug in `llm_providers` if needed and lazy-creating the row
    via `upsert_provider_for_key` for slugs we haven't seen.

    Raises HTTPException(400) if neither field is set; raises
    HTTPException(404) if `provider_id` is supplied but doesn't
    map to a row in this tenant.
    """
    from app.database.init_llm_providers import upsert_provider_for_key
    from app.database.models.llm_provider import LlmProvider

    if provider_id:
        row = (
            db.query(LlmProvider)
            .filter(
                LlmProvider.tenant_id == tenant_id,
                LlmProvider.id == provider_id,
            )
            .first()
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Provider {provider_id!r} not found",
            )
        return row.id

    if not provider:
        raise HTTPException(
            status_code=400,
            detail=(
                "Either `provider_id` or `provider` (legacy slug) "
                "must be supplied."
            ),
        )

    row = upsert_provider_for_key(
        db, tenant_id=tenant_id, key=provider.lower(),
        is_system=False,
    )
    return row.id


# Model fields that hold a credential: `provider_config` carries a
# provider's own (AWS keys, a service-account document).
_SECRET_FIELDS = frozenset({"api_key", "provider_config"})


def _require_destination(
    db: Session,
    *,
    provider_id: str,
    mode: Optional[str],
    model_name: Optional[str],
    api_url: Optional[str],
) -> None:
    """Refuse (422) a row the gateway would refuse every request for: no
    `api_url` and a provider it knows no base URL of for the row's mode
    or an Amazon Bedrock model it does not
    implement in the row's mode. `app/services/llm/destination.py`."""
    from app.database.models.llm_provider import LlmProvider
    from app.services.llm.destination import destination_problem

    provider = db.get(LlmProvider, provider_id)
    problem = destination_problem(
        provider.key if provider is not None else None, mode, model_name, api_url
    )
    if problem:
        raise HTTPException(status_code=422, detail=problem)


def _provider_key(db: Session, provider_id: Optional[str]) -> Optional[str]:
    from app.database.models.llm_provider import LlmProvider

    provider = db.get(LlmProvider, provider_id) if provider_id else None
    return provider.key if provider is not None else None


def _stored_provider_config(
    db: Session, encryption_service: Any, *, provider_id: Optional[str], config: Optional[dict]
) -> Optional[str]:
    """`config` as it is stored: encrypted JSON, or None for no config.

    Refuses a config the gateway could not use for the row's provider (422;
    `app/services/llm/provider_config.py`), and any config where there is no
    encryption service to store it with (503)."""
    import json

    from app.services.llm import provider_config as provider_config_rules

    if not config:
        return None
    problem = provider_config_rules.problem(_provider_key(db, provider_id), config)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if encryption_service is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "provider_config cannot be stored: this control plane has no encryption service "
                "(ENCRYPTION_KEY), and a cloud credential is never stored in clear. Nothing was saved."
            ),
        )
    return encryption_service.encrypt(json.dumps(config))


def _stored_api_key(encryption_service: Any, api_key: Any) -> Any:
    """`api_key` as it is stored: encrypted. Empty stays as it is (no key).
    Refused where there is nothing to encrypt it with (503)."""
    if not api_key:
        return api_key
    if encryption_service is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "api_key cannot be stored: this control plane has no encryption service "
                "(ENCRYPTION_KEY), and a credential is never stored in clear. Nothing was saved."
            ),
        )
    return encryption_service.encrypt(api_key)


def _run_save_checks(provider: Optional[str], row: dict) -> None:
    """What an extension asks of a row that is about to be saved
    (`LLM_MODEL_SAVE_CHECKS`): a hook refuses by raising `HTTPException`."""
    from app.composition.hooks import HOOKS, LLM_MODEL_SAVE_CHECKS

    for check in HOOKS.all(LLM_MODEL_SAVE_CHECKS):
        check(provider=provider, row=row)


def _require_forwardable(
    db: Session,
    tenant_id: str,
    *,
    provider: Optional[str],
    model_name: Optional[str],
    mode: Optional[str],
    routing_group_id: Optional[str],
    fallback_models: Any,
    check_member: bool,
    check_chain: bool,
    provider_id: Optional[str] = None,
    retire_date: Any = None,
) -> None:
    """Refuse (422) a row the gateway would drop where this save puts it:
    a member of a routing group, or an entry of the row's own fallback
    chain, that the forwarder has no backend for
    (`app/services/llm/forward_candidates.py`) or that the gateway does
    not serve at all: a retired model, or one of a disabled provider
    (`app/services/llm/availability.py`)."""
    from app.services.llm import availability, forward_candidates

    problem = None
    if check_member and routing_group_id:
        not_served = availability.not_served_reason(
            retire_date=retire_date,
            provider_enabled=availability.provider_enabled(db, provider_id),
            mode=mode,
        )
        problem = forward_candidates.member_problem(provider, model_name, mode, not_served)
    if not problem and check_chain and fallback_models:
        problem = forward_candidates.fallback_problem(db, tenant_id, fallback_models, mode, "fallback_models")
    if problem:
        raise HTTPException(status_code=422, detail=problem)


def _require_cloud_credential(
    provider: Optional[str],
    *,
    model_name: Optional[str],
    api_url: Optional[str],
    api_key: Any,
    config: Optional[dict],
    model_metadata: Any,
    enabled: bool,
) -> None:
    """Refuse (422) a cloud row the gateway would refuse at dispatch for
    its credential: one carrying an `api_key`, which the gateway reads
    nowhere on such a row, and an enabled one without its own
    credential, or on Vertex AI without a project.
    `app/services/llm/provider_config.py`."""
    from app.services.llm import provider_config as provider_config_rules

    problem = provider_config_rules.api_key_problem(provider, api_key)
    if not problem and enabled:
        problem = provider_config_rules.enabled_row_problem(
            provider, model_name, api_url, config, model_metadata
        )
    if problem:
        raise HTTPException(status_code=422, detail=problem)


async def _sync_invalidate_proxy_model_cache(model_id: str, tenant_id: str) -> None:
    """
    Synchronously instruct the Rust proxy to drop its in-memory
    ModelCache entry for the given model and clear the tenant's
    group cache.

    Called from the admin endpoints that mutate an llm_model row
    (update, copy) so that immediately-following admin actions
    (notably the "Test Connection" button firing right after a save)
    always see the fresh row with the new api_key instead of a
    stale cache hit. Redis pub/sub still fans out to any other pods
    asynchronously.

    Best-effort: timeouts and 4xx/5xx propagate to the caller as
    exceptions so the caller can log them but NOT fail the admin
    request — the DB write has already succeeded and the Redis
    pub/sub will eventually converge.
    """
    proxy_url = proxy_internal_url()
    headers = {
        "Content-Type": "application/json",
        "X-Tenant-ID": tenant_id,
    }
    key = gateway_service_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-API-Key"] = key

    url = f"{proxy_url}/v1/proxy/llm/admin/{model_id}/invalidate-cache"
    async with httpx.AsyncClient(timeout=3.0) as client:
        resp = await client.post(url, headers=headers)
        resp.raise_for_status()


def create_llm_models_router(
        model_manager: LLModelsManager,
        logger: BrutorLogger,
        encryption_service: EncryptionService = None,
) -> APIRouter:
    """
    Factory function to create LLMs admin router with full tenant isolation

    Args:
        model_manager: LLM manager service
        logger: Logger instance
        encryption_service: Encryption service for api_key encryption at rest

    Returns:
        Configured APIRouter with all LLM server admin endpoints
    """

    router = APIRouter(prefix="/admin/llms", tags=["LLM Admin"])

    # =========================================================================
    # Test Connection Endpoint
    # =========================================================================

    @router.post(
        "/test-connection",
        response_model=HealthCheckResponse,
        summary="Test connection to LLM",
        description="""
        Test connection to an LLM.
        """
    )
    def test_llm_connection(
            test_request: HealthCheckRequest,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_TEST))
    ):
        """Test connection to an LLM server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        # Check for api_url
        if not test_request.api_url:
            raise HTTPException(status_code=400, detail="API URL is required")

        logger.debug("Testing LLM connection", context={
            "admin_id": admin_id,
            "admin_type": admin_type_value,
            "tenant_id": tenant_id,
            "api_url": test_request.api_url,
            "provider": test_request.provider
        })

        try:
            # Create the temporary model object. The provider FK is
            # resolved against `llm_providers` for the tenant — for
            # ad-hoc test connections where the provider hasn't been
            # provisioned yet, we lazy-create a non-system provider
            # row via `upsert_provider_for_key` so the test still
            # runs end-to-end. The temp model is never flushed to
            # the DB — `db.add` is not called below.
            provider_id = _resolve_provider_id(
                db,
                tenant_id,
                provider_id=getattr(test_request, "provider_id", None),
                provider=getattr(test_request, "provider", None),
            )
            temp_model = LLModel(
                id="test-connection",
                tenant_id=tenant_id,
                name="Test Connection",
                provider_id=provider_id,
                model_name=test_request.model_name or "test",
                api_url=test_request.api_url,
                auth_type=test_request.auth_type,
                api_key=test_request.api_key,
                timeout=test_request.timeout,
                enabled=True
            )

            # Test connection
            result = block_on(model_manager.test_connection(
                model=temp_model,
                timeout=test_request.timeout,
                db=db
            ))

            logger.debug("LLM connection test completed", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "status": result["status"]
            })

            return result

        except Exception as e:
            logger.warning("LLM connection test failed", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "error": str(e)
            })

            raise HTTPException(
                status_code=500,
                detail=f"Connection test failed: {str(e)}"
            )

    # =========================================================================
    # List Models Endpoint
    # =========================================================================

    @router.get(
        "",
        response_model=LLModelListResponse,
        summary="List LLMs",
        description="""
        List LLMs with tenant isolation.
        
        Tenant isolation:
        - Tenant admins: Only see their tenant's models
        - System admins: See all models or filter by tenant_id
        
        Group filtering:
        - user_id: Filter models accessible to specific user
        - include_groups: Include group membership information
        """
    )
    def list_llms(
            request: Request,
            tenant_id: Optional[str] = Query(None, description="Filter by tenant (system admins only)"),
            user_id: Optional[str] = Query(None, description="Filter by user access"),
            include_groups: bool = Query(True, description="Include group information"),
            provider: Optional[str] = Query(None, description="Filter by provider"),
            mode: Optional[str] = Query(None, description="Filter by mode: chat, embedding, image, audio, etc."),
            enabled: Optional[bool] = Query(None, description="Filter by enabled status"),
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_READ))
    ):
        """List LLMs with tenant isolation"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # User-based filtering
            if user_id and tenant_id:
                models = LLMAccessService.get_accessible_models(
                    db=db,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    enabled_only=enabled if enabled is not None else False
                )

                # Apply provider filter if specified.
                # `m.provider` is an `LlmProvider`
                # relationship, not the legacy slug string. Filter
                # against `m.provider.key` instead.
                if provider:
                    target = provider.lower()
                    models = [
                        m for m in models
                        if m.provider and m.provider.key == target
                    ]
                if mode:
                    models = [m for m in models if m.mode == mode.lower()]

            else:
                # Original query logic.
                #
                # Eager-load resource_groups in a single follow-up query
                # via selectinload — without this, to_response() triggers
                # an N+1 storm (3 lazy-load fires per model: is_global
                # check, group_ids, optional groups detail). With ~300
                # seeded models the list call ballooned to ~3s on dev
                # machines; selectinload pulls every group in one
                # additional SELECT keyed by llm_model_id IN (…).
                query = (
                    db.query(LLModel)
                    .options(selectinload(LLModel.resource_groups))
                )
                query = query.filter(LLModel.tenant_id == tenant_id)

                # Apply additional filters.
                # The provider lives on `llm_providers.key`
                # via the FK; join in to filter by slug. Falls back
                # to a `provider_id ==` lookup if the caller passes
                # an id directly (some admin scripts do).
                if provider:
                    from app.database.models.llm_provider import LlmProvider
                    query = (
                        query.join(
                            LlmProvider,
                            LLModel.provider_id == LlmProvider.id,
                        )
                        .filter(LlmProvider.key == provider.lower())
                    )

                if mode:
                    query = query.filter(LLModel.mode == mode.lower())

                if enabled is not None:
                    query = query.filter(LLModel.enabled == enabled)

                models = query.order_by(LLModel.created_at.desc()).all()

            logger.debug("LLMs listed", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": tenant_id,
                "model_count": len(models),
                "provider_filter": provider,
                "enabled_filter": enabled,
                "user_filter": user_id,
                "include_groups": include_groups
            })

            # Build response using manager (pass include_groups)
            return LLModelListResponse(
                models={
                    model.id: model_manager.to_response(model, include_groups=include_groups)
                    for model in models
                },
                total=len(models)
            )

        except Exception as e:
            logger.error("Failed to list LLMs", context={
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail="Failed to list LLMs"
            )

    # =========================================================================
    # Get Model Endpoint
    # =========================================================================

    @router.get(
        "/{model_id}",
        response_model=LLModelDetailResponse,
        summary="Get LLM details",
        description="""
        Get detailed information about an LLM.
        
        Tenant isolation: Model must belong to user's tenant
        """
    )
    def get_llm(
            model_id: str,
            request: Request,
            include_groups: bool = Query(True, description="Include group information"),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_READ))
    ):
        """Get LLM server details"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            model = db.query(LLModel).filter(
                LLModel.id == model_id,
                LLModel.tenant_id == tenant_id
            ).first()

            if not model:
                raise HTTPException(
                    status_code=404,
                    detail=f"LLM {model_id} not found"
                )

            logger.debug("LLM retrieved", context={
                "model_id": model_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id
            })

            # Pass include_groups parameter
            return LLModelDetailResponse(
                model=model_manager.to_response(model, include_groups=include_groups)
            )

        except HTTPException:
            raise

        except Exception as e:
            logger.error("Failed to get LLM", context={
                "model_id": model_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail="Failed to get LLM"
            )

    # =========================================================================
    # Create Model Endpoint
    # =========================================================================

    @router.post(
        "",
        response_model=LLModelDetailResponse,
        status_code=201,
        summary="Create LLM",
        description="""
        Create a new LLM configuration.
        
        Tenant isolation:
        - Tenant admins: Create for their tenant
        - System admins: Must specify tenant_id in request body
        
        Group access:
        - group_ids: List of group IDs that can access this model
        - Empty/null: Model is automatically assigned to the Default resource group (accessible to all users)
        """
    )
    def create_llm(
            model_data: LLModelCreate,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_CREATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Create new LLM with tenant isolation"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        # Feature + quota gates
        TenantService.check_feature_access(db, tenant_id, 'models')
        TenantService.check_quota(db, tenant_id, TenantResourceTypes.LLM_MODELS)

        try:
            # Check for duplicate names within tenant
            existing = db.query(LLModel).filter(
                LLModel.tenant_id == tenant_id,
                LLModel.name == model_data.name
            ).first()

            if existing:
                raise HTTPException(
                    status_code=400,
                    detail=f"LLM with name '{model_data.name}' already exists"
                )

            # Create model with all fields
            model_fields = model_data.model_dump(exclude={'group_ids'})

            # Resolve `provider_id` from either the FK the admin UI
            # sends or the legacy slug. Drop the legacy slug from
            # the splat — the LLModel constructor doesn't accept it.
            resolved_provider_id = _resolve_provider_id(
                db,
                tenant_id,
                provider_id=model_fields.pop("provider_id", None),
                provider=model_fields.pop("provider", None),
            )
            model_fields["provider_id"] = resolved_provider_id

            _require_destination(
                db,
                provider_id=resolved_provider_id,
                mode=model_fields.get("mode"),
                model_name=model_fields.get("model_name"),
                api_url=model_fields.get("api_url"),
            )

            # Mode-consistency gate before building the row.
            # Same invariant as `update_llm`: a model going into a
            # routing group must share the group's declared mode.
            # Validating pre-construction means we don't run the
            # encryption / DB insert work for a request that's
            # going to 400 anyway.
            from app.services.llm.router.llm_router_service import LLMRouterService
            try:
                LLMRouterService.assert_model_mode_matches_group(
                    db=db,
                    tenant_id=tenant_id,
                    routing_group_id=model_fields.get("routing_group_id"),
                    model_mode=model_fields.get("mode"),
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

            # A cloud row: no `api_key`, and its own credential if it is
            # enabled. Before anything is encrypted or stored.
            provider_key = _provider_key(db, resolved_provider_id)
            from app.services.llm import provider_config as provider_config_rules

            config_problem = provider_config_rules.problem(provider_key, model_fields.get('provider_config'))
            if config_problem:
                raise HTTPException(status_code=422, detail=config_problem)
            _require_cloud_credential(
                provider_key,
                model_name=model_fields.get("model_name"),
                api_url=model_fields.get("api_url"),
                api_key=model_fields.get("api_key"),
                config=model_fields.get("provider_config"),
                model_metadata=model_fields.get("model_metadata"),
                enabled=bool(model_fields.get("enabled", True)),
            )

            _run_save_checks(provider_key, {"supports_batch": model_fields.get("supports_batch")})
            _require_forwardable(
                db,
                tenant_id,
                provider=provider_key,
                model_name=model_fields.get("model_name"),
                mode=model_fields.get("mode"),
                routing_group_id=model_fields.get("routing_group_id"),
                fallback_models=model_fields.get("fallback_models"),
                check_member=True,
                check_chain=True,
                provider_id=resolved_provider_id,
                retire_date=model_fields.get("retire_date"),
            )

            # Encrypt api_key at rest
            model_fields['api_key'] = _stored_api_key(encryption_service, model_fields.get('api_key'))

            # `provider_config` at rest: validated for the row's provider,
            # then JSON, encrypted. Refused where it cannot be encrypted.
            model_fields['provider_config'] = _stored_provider_config(
                db,
                encryption_service,
                provider_id=resolved_provider_id,
                config=model_fields.get('provider_config'),
            )

            model = LLModel(
                # `llm-` prefix matches the seed + kserve-poller + catalog
                # formats so operators and tooling can scan IDs without
                # having to juggle multiple shapes.
                id=f"llm-{ULID()}",
                tenant_id=tenant_id,
                **model_fields,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc)
            )

            db.add(model)
            db.flush()

            # Bind the model to its groups. No group named (absent, null or
            # []) is the tenant's Default group, as the request model
            # documents; explicit ids are taken as given ('default' is the
            # sentinel for the Default group).
            effective_group_ids = LLMAccessService.resolve_group_ids(
                db, model_data.group_ids or [DEFAULT_GROUP_SENTINEL], tenant_id
            )

            LLMAccessService.assign_model_to_groups(
                db=db,
                model_id=model.id,
                group_ids=effective_group_ids,
                tenant_id=tenant_id
            )

            logger.info("LLM created", context={
                "model_id": model.id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "model_name": model.name,
                "provider_id": model.provider_id,
                "provider": model.provider.key if model.provider else None,
                "group_ids": effective_group_ids,
                "auto_default": not model_data.group_ids
            })

            # Audit success
            audit.log(
                action="llm_create",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.LLM_MODELS.value,
                resource_id=model.id,
                details={
                    "model_name": model.name,
                    # Slug from the provider relationship.
                    "provider": model.provider.key if model.provider else None,
                    "model_name_value": model.model_name,
                    "group_ids": effective_group_ids
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            # Invalidate proxy cache — pub/sub + synchronous local drop.
            # Synchronous step matters when the UI immediately launches
            # a Test Connection against a newly-created model.
            block_on(invalidate_proxy_cache(request, "model", tenant_id, model.id, "create"))
            try:
                block_on(_sync_invalidate_proxy_model_cache(model.id, tenant_id))
            except Exception as inv_err:  # noqa: BLE001 — best-effort
                logger.warning(
                    "Synchronous proxy cache invalidation failed (pub/sub still issued)",
                    context={
                        "model_id": model.id,
                        "tenant_id": tenant_id,
                        "error": str(inv_err),
                    },
                )

            # Return with groups
            return LLModelDetailResponse(
                model=model_manager.to_response(model, include_groups=True)
            )

        except HTTPException:
            raise

        except Exception as e:
            logger.error("Failed to create LLM", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail="Failed to create LLM"
            )

    # =========================================================================
    # Update Model Endpoint
    # =========================================================================

    @router.patch(
        "/{model_id}",
        response_model=LLModelDetailResponse,
        summary="Update LLM",
        description="""
        Update an LLM configuration.
        
        Tenant isolation: Model must belong to user's tenant
        
        Group access:
        - group_ids: Update group assignments (replaces existing)
        - Include 'default' to make accessible to all users
        - Empty list: Remove from all groups (model becomes inaccessible)
        - Null/omitted: No change to groups
        """
    )
    def update_llm(
            model_id: str,
            model_data: LLModelUpdate,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Update LLM server with tenant verification"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            model = db.query(LLModel).filter(
                LLModel.id == model_id,
                LLModel.tenant_id == tenant_id
            ).first()

            if not model:
                raise HTTPException(
                    status_code=404,
                    detail=f"LLM {model_id} not found"
                )

            # The model quota counts enabled rows: switching a row on is
            # one more of them, and is asked as a creation is.
            if model_data.enabled is True and not model.enabled:
                TenantService.check_quota(db, tenant_id, TenantResourceTypes.LLM_MODELS)

            # Check for duplicate names if name is being changed
            if model_data.name and model_data.name != model.name:
                existing = db.query(LLModel).filter(
                    LLModel.tenant_id == tenant_id,
                    LLModel.name == model_data.name,
                    LLModel.id != model_id
                ).first()

                if existing:
                    raise HTTPException(
                        status_code=400,
                        detail=f"LLM with name '{model_data.name}' already exists"
                    )

            # Track changes for audit
            changes = {}

            # Update all fields
            # Use exclude_unset to allow empty strings and None values
            # Exclude group_ids as it's handled separately
            update_data = model_data.model_dump(exclude_unset=True, exclude={'group_ids'})

            # `provider` is a relationship to LlmProvider,
            # not a string column. The admin UI still serialises the
            # slug (and may eventually emit `provider_id` from the new
            # dropdown). Translate either into a `provider_id` and drop
            # the legacy keys before the generic setattr loop — passing
            # a string into `setattr(model, "provider", "openai")` would
            # try to assign it as the relationship object and crash.
            incoming_provider_id = update_data.pop("provider_id", None)
            incoming_provider_slug = update_data.pop("provider", None)
            if incoming_provider_id is not None or incoming_provider_slug is not None:
                resolved_provider_id = _resolve_provider_id(
                    db,
                    tenant_id=tenant_id,
                    provider_id=incoming_provider_id,
                    provider=incoming_provider_slug,
                )
            else:
                resolved_provider_id = model.provider_id

            # The row as it will be: it has a destination, or it is not
            # saved. Checked before anything is set on the row.
            _require_destination(
                db,
                provider_id=resolved_provider_id,
                mode=update_data["mode"] if "mode" in update_data else model.mode,
                model_name=(
                    update_data["model_name"] if "model_name" in update_data else model.model_name
                ),
                api_url=update_data["api_url"] if "api_url" in update_data else model.api_url,
            )

            # `provider_config` is a patch of what is stored: a key present replaces, a key sent as null
            # removes, a key absent is kept; null itself clears the value.
            # What is validated is the result, for the provider the row
            # will have. Decided before anything is set on the row.
            from app.services.llm import provider_config as provider_config_rules

            # The config the row will have, in clear, for the cloud rule
            # below: what is stored unless this update changes it.
            final_config = None
            config_known = True
            if "provider_config" not in update_data or update_data["provider_config"] is not None:
                final_config = provider_config_rules.read(model.provider_config, encryption_service)
                config_known = final_config is not None or not model.provider_config

            if update_data.get("provider_config") is not None:
                if model.provider_config and encryption_service is None:
                    # Refused below: nothing can be stored without it.
                    stored_config = None
                else:
                    stored_config = provider_config_rules.read(model.provider_config, encryption_service)
                    if model.provider_config and stored_config is None:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "provider_config: the stored value cannot be read with this control plane's "
                                "encryption key, so it cannot be changed key by key. Send "
                                "provider_config: null to clear it, then the whole value."
                            ),
                        )
                merged_config = provider_config_rules.merge(stored_config, update_data["provider_config"])
                final_config, config_known = merged_config, True
                if merged_config == (stored_config or {}) and model.provider_id == resolved_provider_id:
                    # Nothing changes: the stored bytes stay as they are.
                    del update_data["provider_config"]
                else:
                    update_data["provider_config"] = _stored_provider_config(
                        db, encryption_service, provider_id=resolved_provider_id, config=merged_config
                    )
            elif resolved_provider_id != model.provider_id and model.provider_config:
                # The row moves to another provider with the config it has.
                problem = provider_config_rules.problem(
                    _provider_key(db, resolved_provider_id),
                    provider_config_rules.read(model.provider_config, encryption_service),
                )
                if problem and "provider_config" not in update_data:
                    raise HTTPException(status_code=422, detail=problem)

            # A cloud row as it will be: no `api_key`, and its own
            # credential if it is enabled. Asked when the update touches
            # what the answer depends on, so that a row from before the
            # rule can still be described, grouped or switched off; and
            # not asked of a stored config this process cannot read.
            final = lambda field: update_data[field] if field in update_data else getattr(model, field)  # noqa: E731
            if "allowed_fails" in update_data or "cooldown_time" in update_data:
                from app.api.v1.models.admin.llm_model import circuit_breaker_pair_problem

                pair_problem = circuit_breaker_pair_problem(final("allowed_fails"), final("cooldown_time"))
                if pair_problem:
                    raise HTTPException(status_code=422, detail=pair_problem)
            final_provider = _provider_key(db, resolved_provider_id)
            moves = resolved_provider_id != model.provider_id
            touched = moves or any(
                field in update_data
                for field in ("provider_config", "enabled", "model_name", "api_url", "model_metadata")
            )
            _require_cloud_credential(
                final_provider,
                model_name=final("model_name"),
                api_url=final("api_url"),
                api_key=final("api_key") if (moves or "api_key" in update_data) else None,
                config=final_config,
                model_metadata=final("model_metadata"),
                enabled=bool(final("enabled")) and touched and config_known,
            )

            if moves or "supports_batch" in update_data:
                _run_save_checks(final_provider, {"supports_batch": final("supports_batch")})

            stored_api_key = _stored_api_key(encryption_service, update_data.get("api_key"))

            # Where the row will be forwarded to: asked when the update
            # changes what the row is or where it is put.
            what_changes = moves or "model_name" in update_data or "mode" in update_data
            _require_forwardable(
                db,
                tenant_id,
                provider=final_provider,
                model_name=final("model_name"),
                mode=final("mode"),
                routing_group_id=final("routing_group_id"),
                fallback_models=final("fallback_models"),
                check_member=what_changes or "routing_group_id" in update_data or "retire_date" in update_data,
                check_chain="mode" in update_data or "fallback_models" in update_data,
                provider_id=resolved_provider_id,
                retire_date=final("retire_date"),
            )

            if resolved_provider_id != model.provider_id:
                changes["provider_id"] = {
                    "old": model.provider_id,
                    "new": resolved_provider_id,
                }
                model.provider_id = resolved_provider_id

            # Mode-consistency gate for routing-group membership.
            # Only fires when the patch actually touches `mode` or
            # `routing_group_id` — otherwise an unrelated PATCH on
            # a legacy row (whose persisted pair pre-dates this
            # column and may already mismatch) would be blocked
            # forever. The validator's job is to prevent NEW
            # mismatches, not retro-validate pre-existing rows.
            #
            # When the patch DOES touch one of these fields, we
            # compute the post-update pair from incoming ∪
            # existing values — both fields can change in the
            # same PATCH and we need the final combination.
            #
            # Detach (routing_group_id None / empty) is always
            # accepted: the helper short-circuits on falsy
            # routing_group_id, which is also the operator's
            # escape hatch out of a legacy mismatch.
            touches_mode_or_group = (
                "mode" in update_data or "routing_group_id" in update_data
            )
            if touches_mode_or_group:
                from app.services.llm.router.llm_router_service import LLMRouterService
                post_update_routing_group_id = (
                    update_data["routing_group_id"]
                    if "routing_group_id" in update_data
                    else model.routing_group_id
                )
                # Treat "" as None so the frontend can submit an
                # empty-string detach without hitting the
                # validator's "not found" branch.
                if post_update_routing_group_id == "":
                    post_update_routing_group_id = None
                post_update_mode = (
                    update_data["mode"] if "mode" in update_data else model.mode
                )
                try:
                    LLMRouterService.assert_model_mode_matches_group(
                        db=db,
                        tenant_id=tenant_id,
                        routing_group_id=post_update_routing_group_id,
                        model_mode=post_update_mode,
                    )
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))

            for field, value in update_data.items():
                if hasattr(model, field):
                    # Encrypt api_key at rest (encrypted, or refused, before
                    # anything was set on the row: `stored_api_key`).
                    if field == 'api_key':
                        value = stored_api_key

                    # `provider_config` was merged, validated and
                    # encrypted above.

                    old_value = getattr(model, field)
                    # Allow empty strings - they're valid values
                    if old_value != value:
                        # A credential is never in the audit record, in
                        # clear or as stored: that it changed is.
                        secret = field in _SECRET_FIELDS
                        changes[field] = {"old": "[REDACTED]" if secret else old_value,
                                          "new": "[REDACTED]" if secret else value}
                        setattr(model, field, value)

            model.updated_at = datetime.now(timezone.utc)

            # Handle group assignments only if explicitly included in request
            # Using model_fields_set ensures we don't accidentally clear groups
            # when the frontend sends an update without group_ids
            if 'group_ids' in model_data.model_fields_set:
                LLMAccessService.assign_model_to_groups(
                    db=db,
                    model_id=model.id,
                    group_ids=model_data.group_ids if model_data.group_ids is not None else [],
                    tenant_id=tenant_id
                )
                changes['group_ids'] = {"new": model_data.group_ids}

            db.flush()

            logger.info("LLM updated", context={
                "model_id": model_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "changes": list(changes.keys())
            })

            # Audit success
            audit.log(
                action="llm_update",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.LLM_MODELS.value,
                resource_id=model_id,
                details={
                    "model_name": model.name,
                    "changes": changes
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            # COMMIT before invalidating the proxy cache.
            #
            # `db.flush()` above only stages the row inside this
            # transaction — the new api_key isn't visible to a
            # different DB session yet. If we invalidate the proxy
            # cache here, then *between* the invalidation and our
            # commit, a parallel request hitting the proxy could
            # re-cache the model from a still-stale view of the row.
            # The follow-up "Test Connection" then reads that stale
            # cache and fails with "missing api_key".
            #
            # The fix is to commit FIRST so any subsequent DB read —
            # by the proxy after the cache flush — sees the new row.
            # This is a known race that hit the multimodal probe
            # path (TTS / image / STT) where the cache hadn't yet
            # converged with the just-saved api_key.
            db.commit()
            db.refresh(model)

            # Invalidate proxy cache.
            #
            # Two-step strategy:
            #   1. Redis pub/sub — fans out to every proxy pod in
            #      multi-pod deployments. Fire-and-forget.
            #   2. Synchronous HTTP invalidate on the canonical proxy —
            #      guarantees THIS pod has dropped the stale model
            #      entry before we return 200 to the UI. Without
            #      this, the UI's immediate follow-up call (e.g.
            #      "Test Connection") races past pub/sub propagation
            #      and the proxy serves the pre-update cache entry —
            #      a missing api_key on a just-saved model is the
            #      most common symptom.
            block_on(invalidate_proxy_cache(request, "model", tenant_id, model_id, "update"))
            try:
                block_on(_sync_invalidate_proxy_model_cache(model_id, tenant_id))
            except Exception as inv_err:  # noqa: BLE001 — best-effort
                logger.warning(
                    "Synchronous proxy cache invalidation failed (pub/sub still issued)",
                    context={
                        "model_id": model_id,
                        "tenant_id": tenant_id,
                        "error": str(inv_err),
                    },
                )

            # Return with groups
            return LLModelDetailResponse(
                model=model_manager.to_response(model, include_groups=True)
            )

        except HTTPException:
            raise

        except Exception as e:
            logger.error("Failed to update LLM", context={
                "model_id": model_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail="Failed to update LLM"
            )

    # =========================================================================
    # Delete Model Endpoint
    # =========================================================================

    @router.delete(
        "/{model_id}",
        status_code=204,
        summary="Delete LLM",
        description="""
        Delete an LLM configuration. This will also delete all associated data, including model weights.
        
        Tenant isolation: Server must belong to user's tenant
        """
    )
    def delete_llm(
            model_id: str,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_DELETE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete LLM server with tenant verification"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            model = db.query(LLModel).filter(
                LLModel.id == model_id,
                LLModel.tenant_id == tenant_id
            ).first()

            if not model:
                raise HTTPException(
                    status_code=404,
                    detail=f"LLM server {model_id} not found"
                )

            server_name = model.name
            # Slug from the provider relationship.
            server_provider = model.provider.key if model.provider else None

            # ── Guard: virtual routing models are managed by the router ──
            if model.is_routing:
                msg = (
                    f"Model \"{server_name}\" is a virtual routing-group model. "
                    f"Delete the routing group instead."
                )
                logger.warning("Cannot delete virtual routing-group model", context={
                    "model_id": model_id, "model_name": server_name,
                    "tenant_id": tenant_id, "reason": msg
                })
                raise HTTPException(status_code=409, detail=msg)

            # ── Guard: model still assigned to resource groups ──
            # Get the groups this model is assigned to
            assigned_groups = db.query(
                resource_group_llm_models.c.resource_group_id
            ).filter(
                resource_group_llm_models.c.llm_model_id == model_id
            ).all()

            assigned_group_ids = [row[0] for row in assigned_groups]

            if assigned_group_ids:
                # Resolve the Default group to distinguish it from real groups
                try:
                    real_default_id = LLMAccessService.resolve_default_group_id(db, tenant_id)
                except ValueError:
                    real_default_id = None

                # Non-Default groups assigned to this model
                non_default_groups = [
                    gid for gid in assigned_group_ids
                    if gid != real_default_id
                ]

                if non_default_groups:
                    # Model is in non-Default groups → block deletion
                    msg = (
                        f"Cannot delete model \"{server_name}\": still assigned to "
                        f"{len(non_default_groups)} resource group(s) besides Default. "
                        f"Remove the model from all non-Default resource groups first."
                    )
                    logger.warning("Cannot delete model - non-default resource groups assigned", context={
                        "model_id": model_id, "model_name": server_name,
                        "tenant_id": tenant_id,
                        "non_default_group_count": len(non_default_groups),
                        "reason": msg
                    })
                    raise HTTPException(status_code=409, detail=msg)

                # Only Default group → auto-disassociate before deletion
                db.query(resource_group_llm_models).filter(
                    resource_group_llm_models.c.llm_model_id == model_id
                ).delete(synchronize_session=False)

            # ── Guard: model still belongs to a routing group ──
            if model.routing_group_id:
                msg = (
                    f"Cannot delete model \"{server_name}\": still assigned to "
                    f"routing group \"{model.routing_group_id}\". Remove the model "
                    f"from the routing group first."
                )
                logger.warning("Cannot delete model - routing group assigned", context={
                    "model_id": model_id, "model_name": server_name,
                    "tenant_id": tenant_id,
                    "routing_group_id": model.routing_group_id,
                    "reason": msg
                })
                raise HTTPException(status_code=409, detail=msg)

            db.delete(model)
            db.flush()

            logger.info("LLM deleted", context={
                "model_id": model_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "model_name": server_name
            })

            # Audit deletion
            audit.log(
                action="llm_delete",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.LLM_MODELS.value,
                resource_id=model_id,
                details={
                    "model_name": server_name,
                    "provider": server_provider
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            # Invalidate proxy cache
            block_on(invalidate_proxy_cache(request, "model", tenant_id, model_id, "delete"))

        except HTTPException:
            raise

        except Exception as e:
            logger.error("Failed to delete LLM", context={
                "model_id": model_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=500,
                detail="Failed to delete LLM"
            )

    # =========================================================================
    # Health Check Endpoints
    # =========================================================================

    @router.post(
        "/{model_id}/copy",
        response_model=LLModelDetailResponse,
        status_code=201,
        summary="Copy an existing LLM model",
        description="""
        Duplicate an existing LLM model configuration. Every column on
        the source row is carried over — including `api_key` (still
        encrypted at rest), `parameters`, `supports_batch`,
        `max_concurrent_requests`, `additional_drop_params`, and the
        full set of adapter / routing / cost / resilience knobs.

        The copy is given:
          - a fresh `llm-<ULID>` id (matches the seeded / live format)
          - a unique name, prefixed with "[COPY] " so it sorts next to
            the original in the alphabetical list
          - `health_status`, performance counters, and runtime stats
            reset so the new row starts from a clean slate
          - the same resource-group bindings as the source

        Admin's choice of copy over client-side duplication lives here
        because the API never returns the raw `api_key` — doing the
        copy server-side lets us forward the encrypted secret without
        ever exposing it to the browser.
        """,
    )
    def copy_llm(
            model_id: str,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_CREATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger),
    ):
        """Server-side model duplication that preserves secrets."""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        # Tenant quota applies to the new row, same as create_llm.
        TenantService.check_quota(db, tenant_id, TenantResourceTypes.LLM_MODELS)

        source = db.query(LLModel).filter(
            LLModel.id == model_id,
            LLModel.tenant_id == tenant_id,
        ).first()
        if not source:
            raise HTTPException(status_code=404, detail=f"LLM {model_id} not found")

        # Virtual routing-group models are synthetic — duplicating them
        # would create an orphaned row that can't be wired to any
        # routing group. Match the delete guard.
        if source.is_routing:
            raise HTTPException(
                status_code=409,
                detail="Cannot copy a virtual routing-group model.",
            )

        # Find a unique name. The admin's preferred shape is
        # "[COPY] <name>"; if that already exists (e.g. user hit
        # Copy twice) we append a counter so we never collide with
        # the tenant-scoped uniqueness constraint on `name`.
        base_name = f"[COPY] {source.name}"
        new_name = base_name
        counter = 2
        while db.query(LLModel).filter(
            LLModel.tenant_id == tenant_id,
            LLModel.name == new_name,
        ).first():
            new_name = f"{base_name} ({counter})"
            counter += 1

        # Reflect every column on the source into the copy — except
        # the fields that must be generated fresh (id, timestamps) or
        # must reset to zero (health + perf counters). Columns added
        # in the future are picked up automatically because we
        # enumerate via the SQLAlchemy mapper instead of hardcoding
        # the list.
        reset_fields = {
            "id", "name", "created_at", "updated_at",
            # Health/perf counters should start clean — a copy is
            # effectively a brand-new deployment target from the
            # runtime's perspective.
            "health_status", "last_health_check", "last_health_success",
            "consecutive_health_failures", "health_check_error",
            "total_requests", "failed_requests", "total_tokens_consumed",
            "avg_latency_ms",
        }
        copied_fields = {}
        for col in source.__table__.columns:
            if col.name in reset_fields:
                continue
            copied_fields[col.name] = getattr(source, col.name)

        new_model = LLModel(
            id=f"llm-{ULID()}",
            name=new_name,
            **copied_fields,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(new_model)
        db.flush()

        # Mirror the source's group bindings so the copy shows up in
        # the same audience without the admin having to rewire access.
        assigned_group_ids = [
            row[0]
            for row in db.query(resource_group_llm_models.c.resource_group_id)
            .filter(resource_group_llm_models.c.llm_model_id == source.id)
            .all()
        ]
        if assigned_group_ids:
            LLMAccessService.assign_model_to_groups(
                db=db,
                model_id=new_model.id,
                group_ids=assigned_group_ids,
                tenant_id=tenant_id,
            )

        db.flush()
        db.refresh(new_model)

        audit.log(
            action="llm_model_copied",
            admin=admin,
            admin_type=admin_type,
            tenant_id=tenant_id,
            success=True,
            resource_type="llm_model",
            resource_id=new_model.id,
            details={
                "source_model_id": source.id,
                "source_name": source.name,
                "new_name": new_name,
                "group_ids": assigned_group_ids,
            },
            ip_address=client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_method=request.method,
            request_path=str(request.url.path),
            db=db,
        )

        # Invalidate the proxy's in-memory model cache — pub/sub for
        # fan-out, synchronous HTTP for this pod so the UI's
        # immediate follow-up (test connection, health check) sees
        # the new row instead of a cache miss or stale entry.
        block_on(invalidate_proxy_cache(request, "model", tenant_id, new_model.id, "create"))
        try:
            block_on(_sync_invalidate_proxy_model_cache(new_model.id, tenant_id))
        except Exception as inv_err:  # noqa: BLE001 — best-effort
            logger.warning(
                "Synchronous proxy cache invalidation failed (pub/sub still issued)",
                context={
                    "model_id": new_model.id,
                    "tenant_id": tenant_id,
                    "error": str(inv_err),
                },
            )

        logger.info("LLM model copied", context={
            "source_model_id": source.id,
            "new_model_id": new_model.id,
            "new_name": new_name,
            "tenant_id": tenant_id,
            "admin_id": admin_id,
        })

        return LLModelDetailResponse(
            model=model_manager.to_response(new_model, include_groups=True)
        )

    @router.post(
        "/{model_id}/health-check",
        response_model=HealthCheckResponse,
        summary="Perform health check on LLM",
        description="""
        Perform a health check on an existing LLM.
        
        Tenant isolation: Model must belong to user's tenant
        """
    )
    def health_check_llm_server(
            model_id: str,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_HEALTH))
    ):
        """Perform health check on LLM server"""
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            model = db.query(LLModel).filter(
                LLModel.id == model_id,
                LLModel.tenant_id == tenant_id
            ).first()

            if not model:
                raise HTTPException(
                    status_code=404,
                    detail=f"LLM {model_id} not found"
                )

            logger.debug("Performing health check on LLM", context={
                "model_id": model_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id
            })

            # Perform health check. When the model has an explicit
            # ``timeout`` field use it verbatim; otherwise fall back
            # to a mode-aware default (image=300s, video=900s,
            # audio=120s, chat/embedding=60s). The previous flat 30s
            # fallback aborted image and video probes mid-flight.
            result = block_on(model_manager.test_connection(
                model=model,
                timeout=(
                    int(model.timeout)
                    if model.timeout
                    else default_health_check_timeout_secs(model.mode)
                ),
                db=db
            ))

            # ``test_connection`` already classifies the result and
            # writes the canonical health status to the DB (including
            # the ``requires_tier`` case for restricted-tier models).
            # Nothing extra to do here.
            db.flush()

            logger.debug("Health check completed", context={
                "model_id": model_id,
                "status": result["status"],
                "required_tier": result.get("required_tier"),
            })

            return result

        except HTTPException:
            raise

        except Exception as e:
            logger.warning("Health check failed", context={
                "model_id": model_id,
                "tenant_id": tenant_id,
                "error": str(e)
            })

            raise HTTPException(
                status_code=500,
                detail="Health check failed"
            )

    @router.post(
        "/health-check/bulk",
        response_model=LLMModelBulkHealthCheckResponse,
        summary="Bulk health check for LLM models",
        description="""
        Perform concurrent health checks on multiple LLM models.

        **Uses the same test_connection logic as the single model endpoint.**

        Request Options:
        - model_ids: Specific models to check (null = check all enabled models)
        - enabled_only: Only check enabled models (default: true)
        - timeout_seconds: Override default timeout per model

        Tenant isolation:
        - Tenant admins: Check models in their tenant
        - System admins: Must specify tenant_id in request

        Permissions Required:
        - ai:model:test
        """
    )
    def bulk_health_check_models(
            check_request: BulkHealthCheckRequest,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_TEST))
    ):
        """
        Perform concurrent health checks on multiple LLM models.

        This endpoint uses model_manager.test_connection() - the SAME method
        used by the single model test-connection endpoint.
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id

        try:
            # Build query for models to check
            query = db.query(LLModel).filter(LLModel.tenant_id == tenant_id)

            # Filter by enabled status
            if check_request.enabled_only:
                query = query.filter(LLModel.enabled == True)

            # Filter by specific model IDs if provided
            if check_request.model_ids:
                query = query.filter(LLModel.id.in_(check_request.model_ids))

            models = query.all()

            if not models:
                raise HTTPException(
                    status_code=404,
                    detail="No models found matching the criteria"
                )

            logger.debug(f"Starting bulk health check for {len(models)} models in tenant {tenant_id}")

            # Background sweep back-off (probe="catalog" only — an
            # operator's check always probes). A model whose last
            # ``ai_failure_threshold`` checks failed with a non-transient
            # (config / model) error is re-checked every
            # ``ai_interval_seconds * HEALTH_CHECK_BACKOFF_MULTIPLIER``
            # instead of every interval. Read-only lookup: a tenant with
            # no saved config uses the defaults (never creates a row).
            backed_off_models: list[LLModel] = []
            if check_request.probe == PROBE_CATALOG:
                cfg = db.query(HealthCheckConfig).filter(
                    HealthCheckConfig.tenant_id == tenant_id
                ).first()
                defaults = HealthCheckConfig.get_default_config()
                failure_threshold = (
                    cfg.ai_failure_threshold if cfg else defaults['ai_failure_threshold']
                )
                interval_seconds = (
                    cfg.ai_interval_seconds if cfg else defaults['ai_interval_seconds']
                )
                backoff_seconds = interval_seconds * HEALTH_CHECK_BACKOFF_MULTIPLIER
                to_probe = []
                for m in models:
                    if should_back_off_background_check(
                        m,
                        failure_threshold=failure_threshold,
                        backoff_interval_seconds=backoff_seconds,
                    ):
                        backed_off_models.append(m)
                    else:
                        to_probe.append(m)
                models = to_probe
                if backed_off_models:
                    logger.info(
                        "Background LLM health sweep backed off persistently "
                        "failing models",
                        context={
                            "tenant_id": tenant_id,
                            "backed_off": len(backed_off_models),
                            "backoff_seconds": backoff_seconds,
                            "failure_threshold": failure_threshold,
                        },
                    )

            # Bounded concurrency via the shared
            # ``BulkRunner``. The cap is derived from
            # ``BRUTOR_GATEWAY_DB_POOL_SIZE`` so an operator who scales the
            # proxy's pool sees the cap rise automatically — no
            # per-endpoint hardcoded magic number to keep in sync.
            from app.services.concurrency import BulkRunner

            async def check_single_model(model: LLModel) -> HealthCheckResponse:
                """Check health of a single model using
                ``model_manager.test_connection()`` — same code path as
                the single-model endpoint. Per-item exceptions are
                captured by ``BulkRunner`` rather than handled here,
                but we still convert structured failures into a
                ``status='unhealthy'`` response so the bulk caller
                gets a uniform shape across all 70+ probes.
                """
                try:
                    # Precedence: explicit request override → model's
                    # own timeout field → mode-aware default. Without
                    # the per-mode fallback a null model.timeout would
                    # forward as None and trip httpx's flat default,
                    # which is far too short for image / video probes.
                    effective_timeout = (
                        check_request.timeout_seconds
                        or (int(model.timeout) if model.timeout else None)
                        or default_health_check_timeout_secs(model.mode)
                    )
                    result = await model_manager.test_connection(
                        model=model,
                        db=db,
                        timeout=effective_timeout,
                        probe=check_request.probe,
                    )
                    return HealthCheckResponse(**result)
                except Exception as e:
                    logger.warning(f"Health check failed for model {model.id}: {e}")
                    return HealthCheckResponse(
                        model_id=model.id,
                        model_name=model.name,
                        status="unhealthy",
                        response_time_ms=None,
                        error_message=f"Health check error: {str(e)}",
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )

            run = block_on(BulkRunner(
                coroutine_factory=check_single_model,
                inputs=models,
                op_kind="llm_health_check",
                logger=logger,
            ).run())
            # ``BulkRunner.results`` is aligned to ``inputs``. Convert
            # to the legacy ``results`` list expected downstream;
            # because ``check_single_model`` already converts
            # exceptions into ``unhealthy`` responses, ``errors[i]``
            # should always be None — but we defensively coerce any
            # leftover error into a synthetic unhealthy row so the
            # caller never sees ``None`` in the response list.
            results: list[HealthCheckResponse] = []
            for model, r, err in zip(run.inputs, run.results, run.errors):
                if r is not None:
                    results.append(r)
                else:
                    logger.error(
                        f"Health check raised past the per-item handler for "
                        f"{model.id}: {err}"
                    )
                    results.append(
                        HealthCheckResponse(
                            model_id=model.id,
                            model_name=model.name,
                            status="unhealthy",
                            response_time_ms=None,
                            error_message=f"Bulk runner error: {err}",
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )
                    )

            # Backed-off models report their last recorded verdict —
            # never a fresh "healthy" — so the counts stay honest.
            for m in backed_off_models:
                results.append(
                    HealthCheckResponse(
                        model_id=m.id,
                        model_name=m.model_name,
                        status=m.health_status or ServerStatusType.UNHEALTHY.value,
                        response_time_ms=None,
                        error_message=m.health_check_error,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        probe_type=None,
                        backed_off=True,
                    )
                )

            # Commit all database updates (test_connection updates model health status)
            db.flush()

            # Calculate statistics
            healthy_count = sum(1 for r in results if r.status == 'healthy')
            unhealthy_count = sum(1 for r in results if r.status == 'unhealthy')
            timeout_count = sum(1 for r in results if r.status == 'timeout')

            timestamp = datetime.now(timezone.utc).isoformat()

            logger.info(
                f"Bulk health check completed for tenant {tenant_id}: "
                f"{healthy_count} healthy, {unhealthy_count} unhealthy, {timeout_count} timeout "
                f"out of {len(results)} total"
            )

            return LLMModelBulkHealthCheckResponse(
                results=list(results),
                total_checked=len(results),
                healthy_count=healthy_count,
                unhealthy_count=unhealthy_count,
                timeout_count=timeout_count,
                timestamp=timestamp
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Bulk health check failed: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Bulk health check failed: {str(e)}"
            )

    # =========================================================================
    # Group Management Endpoints
    # =========================================================================

    @router.post(
        "/{model_id}/groups/assign",
        response_model=LLModelDetailResponse,
        summary="Assign model to groups",
        description="""
        Assign model to specific groups (replaces existing assignments).
        
        Include 'default' in group_ids to make accessible to all users.
        Tenant isolation: Model must belong to user's tenant
        """
    )
    def assign_model_to_groups(
            model_id: str,
            assignment: ModelGroupAssignment,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Assign model to groups"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            # Verify model exists
            model = db.query(LLModel).filter(
                LLModel.id == model_id,
                LLModel.tenant_id == tenant_id
            ).first()

            if not model:
                raise HTTPException(status_code=404, detail=f"LLM {model_id} not found")

            # Capture current bindings BEFORE the replace-style assign
            # so we can compute added/removed sets for the per-member
            # inbox notification. The service's `assign_model_to_groups`
            # is idempotent — it deletes everything and re-adds — so a
            # before/after diff is the only way to know what actually
            # changed for the user.
            prev_group_ids = {
                row.resource_group_id
                for row in db.query(resource_group_llm_models)
                .filter(resource_group_llm_models.c.llm_model_id == model_id)
                .all()
            }

            # Assign groups
            LLMAccessService.assign_model_to_groups(
                db=db,
                model_id=model_id,
                group_ids=assignment.group_ids,
                tenant_id=tenant_id
            )

            new_group_ids = {
                row.resource_group_id
                for row in db.query(resource_group_llm_models)
                .filter(resource_group_llm_models.c.llm_model_id == model_id)
                .all()
            }
            added_group_ids = new_group_ids - prev_group_ids
            removed_group_ids = prev_group_ids - new_group_ids
            if added_group_ids:
                notify_group_members_about_resource_change(
                    db=db,
                    tenant_id=tenant_id,
                    group_ids=added_group_ids,
                    resource_kind="model",
                    resource_id=model.id,
                    resource_name=model.name,
                    action="added",
                    actor_username=admin.username,
                    logger=logger,
                )
            if removed_group_ids:
                notify_group_members_about_resource_change(
                    db=db,
                    tenant_id=tenant_id,
                    group_ids=removed_group_ids,
                    resource_kind="model",
                    resource_id=model.id,
                    resource_name=model.name,
                    action="removed",
                    actor_username=admin.username,
                    logger=logger,
                )

            logger.info("Model assigned to groups", context={
                "model_id": model_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "group_ids": assignment.group_ids
            })

            # Audit
            audit.log(
                action="llm_group_assign",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.LLM_MODELS.value,
                resource_id=model_id,
                details={"group_ids": assignment.group_ids},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            # Invalidate proxy cache
            block_on(invalidate_proxy_cache(request, "assign", tenant_id, model_id, "assign"))

            db.refresh(model)
            return LLModelDetailResponse(
                model=model_manager.to_response(model, include_groups=True)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to assign groups", context={
                "model_id": model_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to assign groups")

    @router.post(
        "/{model_id}/groups/remove",
        response_model=LLModelDetailResponse,
        summary="Remove model from groups",
        description="""
        Remove model from specific groups (keeps other assignments).
        
        Tenant isolation: Model must belong to user's tenant
        """
    )
    def remove_model_from_groups(
            model_id: str,
            unassignment: ModelGroupUnassignment,
            request: Request,
            admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.LLM_MODELS)),
            db: Session = Depends(get_db),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_UPDATE)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Remove model from groups"""
        admin, tenant_id, admin_type = admin_and_tenant
        admin_id = admin.id

        try:
            # Verify model exists
            model = db.query(LLModel).filter(
                LLModel.id == model_id,
                LLModel.tenant_id == tenant_id
            ).first()

            if not model:
                raise HTTPException(status_code=404, detail=f"LLM {model_id} not found")

            # Capture which of the requested groups currently hold
            # the model — only those constitute a real "removal" from
            # a member's perspective. Asking to remove a binding that
            # didn't exist shouldn't notify anyone.
            existing_group_ids = {
                row.resource_group_id
                for row in db.query(resource_group_llm_models)
                .filter(
                    resource_group_llm_models.c.llm_model_id == model_id,
                    resource_group_llm_models.c.resource_group_id.in_(
                        unassignment.group_ids
                    ),
                )
                .all()
            }

            # Remove from groups
            LLMAccessService.remove_model_from_groups(
                db=db,
                model_id=model_id,
                group_ids=unassignment.group_ids,
                tenant_id=tenant_id
            )

            if existing_group_ids:
                notify_group_members_about_resource_change(
                    db=db,
                    tenant_id=tenant_id,
                    group_ids=existing_group_ids,
                    resource_kind="model",
                    resource_id=model.id,
                    resource_name=model.name,
                    action="removed",
                    actor_username=admin.username,
                    logger=logger,
                )

            logger.info("Model removed from groups", context={
                "model_id": model_id,
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "group_ids": unassignment.group_ids
            })

            # Audit
            audit.log(
                action="llm_group_remove",
                admin=admin,
                admin_type=admin_type,
                tenant_id=tenant_id,
                success=True,
                resource_type=TenantResourceTypes.LLM_MODELS.value,
                resource_id=model_id,
                details={"group_ids": unassignment.group_ids},
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            # Invalidate proxy cache
            block_on(invalidate_proxy_cache(request, "assign", tenant_id, model_id, "assign"))

            db.refresh(model)
            return LLModelDetailResponse(
                model=model_manager.to_response(model, include_groups=True)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to remove from groups", context={
                "model_id": model_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to remove from groups")

    @router.post(
        "/user-access",
        response_model=UserAccessibleModelsResponse,
        summary="Get user accessible models",
        description="""
        Get models accessible to a specific user based on group membership.
        
        Useful for:
        - Determining what models a user can use
        - Building user-specific model selection UIs
        - Audit logs
        """
    )
    def get_user_accessible_models(
            access_request: UserAccessibleModelsRequest,
            request: Request,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_READ))
    ):
        """Get models accessible to user"""
        admin, admin_type = admin_and_type
        admin_id = admin.id
        tenant_id = acting_tenant_id(request, admin)

        try:
            # The request model documents "if not provided, uses authenticated
            # user"; the response model's user_id is a required str.
            user_id = access_request.user_id or admin_id

            # Get user's groups
            user_group_ids = LLMAccessService.get_user_group_ids(
                db=db,
                user_id=user_id,
                tenant_id=tenant_id
            )

            # Get accessible models
            models = LLMAccessService.get_accessible_models(
                db=db,
                tenant_id=tenant_id,
                user_group_ids=user_group_ids,
                enabled_only=access_request.enabled_only
            )

            logger.debug("Retrieved user accessible models", context={
                "admin_id": admin_id,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "model_count": len(models)
            })

            # Convert to response
            model_responses = [
                model_manager.to_response(model, include_groups=False)
                for model in models
            ]

            return UserAccessibleModelsResponse(
                user_id=user_id,
                user_groups=user_group_ids,
                models=model_responses,
                total=len(model_responses)
            )

        except Exception as e:
            logger.error("Failed to get user accessible models", context={
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to get accessible models")

    @router.get(
        "/by-group",
        summary="Get models by group",
        description="""
        Get models organized by resource group.
        
        Returns dictionary with:
        - '<group_id>': Models assigned to that group
        - 'default': Models in the Default group (accessible to all users)
        """
    )
    def get_models_by_group(
            request: Request,
            enabled_only: bool = Query(True, description="Only enabled models"),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_READ))
    ):
        """Get models organized by group"""
        admin, admin_type = admin_and_type
        admin_id = admin.id
        tenant_id = acting_tenant_id(request, admin)

        try:
            models_by_group = LLMAccessService.get_models_by_group(
                db=db,
                tenant_id=tenant_id,
                enabled_only=enabled_only
            )

            logger.debug("Retrieved models by group", context={
                "admin_id": admin_id,
                "tenant_id": tenant_id,
                "group_count": len(models_by_group)
            })

            # Convert to response format
            result = {}
            for group_id, models in models_by_group.items():
                model_responses = [
                    model_manager.to_response(model, include_groups=False)
                    for model in models
                ]

                result[group_id] = {
                    "group_id": group_id,
                    "group_name": None,  # Populated by frontend from group metadata
                    "models": model_responses,
                    "total": len(model_responses)
                }

            return result

        except Exception as e:
            logger.error("Failed to get models by group", context={
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to get models by group")

    @router.post(
        "/search",
        response_model=LLModelSearchResponse,
        summary="Search ALL LLM Models (Admin)",
        description="""
        **ADMIN ONLY** - Search ALL LLM models in tenant with advanced filtering.

        No access control filtering - returns all models matching criteria
        regardless of group assignments.

        **Search Capabilities:**
        - Text search: name, description, model_name (partial, case-insensitive)
        - Provider filtering: single or multiple providers
        - Capability filters: streaming, function calling, vision
        - Cost range filters: min/max for input/output tokens
        - Tag search: search in capabilities.tags
        - Status filters: enabled, healthy
        - Sorting: by name, cost, latency, requests, date
        - Pagination: offset/limit

        **Examples:**

        Find cheap streaming models:
        ```json
        {
            "supports_streaming": true,
            "max_cost_per_input_token": 1.0,
            "sort_by": "cost_per_input_token"
        }
        ```

        Find vision models from OpenAI or Anthropic:
        ```json
        {
            "providers": ["openai", "anthropic"],
            "supports_vision": true,
            "enabled_only": true
        }
        ```

        Find models by name:
        ```json
        {
            "name": "gpt-5",
            "sort_by": "created_at",
            "sort_order": "desc"
        }
        ```

        **Response:**
        - List of ALL matching models (no access control filtering)
        - Total count (ignoring pagination)
        - Pagination metadata
        - Applied filters summary
        """
    )
    def search_llm_models_admin(
            request: Request,
            search_request: LLModelSearchRequest,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_READ))
    ):
        """
        Search ALL LLM models with advanced filtering - ADMIN ONLY

        Args:
            search_request: Search filters and pagination
            db: Database session
            tenant_id: Current tenant ID
            # admin_user: Admin user (from auth dependency)

        Returns:
            LLModelSearchResponse with ALL matching models

        Raises:
            HTTPException 400: Invalid search parameters
            HTTPException 403: Not authorized (not admin)
            HTTPException 500: Database error
        """
        admin, admin_type = admin_and_type
        admin_id = admin.id
        tenant_id = acting_tenant_id(request, admin)

        try:
            # Initialize service
            model_search_service = LLModelSearchService()

            # Perform search (NO user_id - searches all models)
            result = block_on(model_search_service.search_models(
                db=db,
                tenant_id=tenant_id,
                search_request=search_request
            ))

            return result

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to search models: {str(e)}"
            )

    @router.post(
        "/search/with-summary",
        response_model=LLModelSearchWithSummaryResponse,
        summary="Search ALL Models with Summary Statistics (Admin)",
        description="""
        **ADMIN ONLY** - Extended search that includes summary statistics.

        Returns statistics for ALL models matching criteria:
        - Provider distribution (count per provider)
        - Capability counts (streaming, function calling, vision)
        - Cost statistics (min, max, avg)
        - Status counts (enabled, disabled, healthy, unhealthy)

        **Note:** This endpoint is slightly slower than /search because it 
        calculates additional statistics.
        """
    )
    def search_llm_models_with_summary_admin(
            request: Request,
            search_request: LLModelSearchRequest,
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_READ))
    ):
        """
        Search ALL LLM models with summary statistics - ADMIN ONLY

        Returns both search results and aggregate statistics
        useful for admin dashboards.
        """
        admin, admin_type = admin_and_type
        admin_id = admin.id
        tenant_id = acting_tenant_id(request, admin)

        try:
            # Initialize service
            model_search_service = LLModelSearchService()

            # Get search results
            search_results = block_on(model_search_service.search_models(
                db=db,
                tenant_id=tenant_id,
                search_request=search_request
            ))

            # Get summary statistics
            summary = block_on(model_search_service.get_search_summary(
                db=db,
                tenant_id=tenant_id,
                search_request=search_request
            ))

            return LLModelSearchWithSummaryResponse(
                search_results=search_results,
                summary=summary
            )

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to search models with summary: {str(e)}"
            )

    @router.get(
        "/search/quick",
        response_model=LLModelSearchResponse,
        summary="Quick Model Search - Query Params (Admin)",
        description="""
        **ADMIN ONLY** - Simplified search using query parameters instead of POST body.
        Good for simple searches and direct browser access.

        **Example:**
        ```
        GET /api/admin/llm/models/search/quick?name=gpt&provider=openai&streaming=true&limit=10
        ```
        """
    )
    def quick_search_llm_models_admin(
            request: Request,
            name: Optional[str] = Query(None, description="Search by name"),
            provider: Optional[str] = Query(None, description="Filter by provider"),
            streaming: Optional[bool] = Query(None, description="Streaming support"),
            vision: Optional[bool] = Query(None, description="Vision support"),
            functions: Optional[bool] = Query(None, description="Function calling support"),
            max_cost: Optional[float] = Query(None, description="Max cost per 1M input tokens"),
            enabled_only: bool = Query(True, description="Only enabled models"),
            offset: int = Query(0, ge=0, description="Pagination offset"),
            limit: int = Query(50, ge=1, le=500, description="Results per page"),
            sort_by: str = Query("name", description="Sort field"),
            sort_order: str = Query("asc", description="Sort order"),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_READ))
    ):
        """
        Quick search using query parameters - ADMIN ONLY

        Simpler alternative to POST /search for basic admin queries.
        """
        admin, admin_type = admin_and_type
        admin_id = admin.id
        tenant_id = acting_tenant_id(request, admin)

        try:
            # Initialize service
            model_search_service = LLModelSearchService()

            # Build search request from query params
            search_request = LLModelSearchRequest(
                name=name,
                provider=provider,
                supports_streaming=streaming,
                supports_vision=vision,
                supports_function_calling=functions,
                max_cost_per_input_token=max_cost,
                enabled_only=enabled_only,
                offset=offset,
                limit=limit,
                sort_by=sort_by,
                sort_order=sort_order
            )

            result = block_on(model_search_service.search_models(
                db=db,
                tenant_id=tenant_id,
                search_request=search_request
            ))

            return result

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to search models: {str(e)}"
            )

    @router.get(
        "/search/suggestions",
        response_model=dict,
        summary="Get Search Suggestions (Admin)",
        description="""
        **ADMIN ONLY** - Get autocomplete suggestions for search fields.

        Useful for building admin UIs with autocomplete.

        **Example:**
        ```
        GET /api/admin/llm/models/search/suggestions?field=provider
        ```

        Returns unique values for the specified field from ALL models.
        """
    )
    def get_search_suggestions_admin(
            request: Request,
            field: str = Query(..., description="Field to get suggestions for"),
            prefix: Optional[str] = Query(None, description="Filter by prefix"),
            limit: int = Query(20, ge=1, le=100),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.AI_MODEL_READ))
    ):
        """
        Get autocomplete suggestions for search fields - ADMIN ONLY

        Supported fields:
        - provider: List of providers
        - model_name: List of model names
        - name: List of display names
        - tags: List of tags from capabilities
        """
        admin, admin_type = admin_and_type
        admin_id = admin.id
        tenant_id = acting_tenant_id(request, admin)

        try:
            # Initialize service
            model_search_service = LLModelSearchService()

            suggestions = block_on(model_search_service.get_search_suggestions(
                db=db,
                tenant_id=tenant_id,
                field=field,
                prefix=prefix,
                limit=limit
            ))

            return {
                "field": field,
                "suggestions": suggestions,
                "total": len(suggestions)
            }

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to get suggestions: {str(e)}"
            )

    # Static paths must beat parameterized ones: /by-group, /user-access,
    # and /search are declared below /{model_id}, and FastAPI would match
    # them as model ids. Stable sort, as in agent_skills.
    router.routes.sort(key=lambda r: "{" in getattr(r, "path", ""))

    return router