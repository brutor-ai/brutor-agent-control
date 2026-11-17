# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM Router Service
Handles business logic for router management, routing groups, and monitoring
"""

from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import and_, func

import time
from collections import defaultdict, deque
from typing import Deque, Tuple

import httpx

from app.constants.proxy import proxy_internal_url, gateway_service_api_key
from app.core.logging_system import BrutorLogger
from app.constants.groups import ResourceGroupName
from app.constants.llm import LLModelMode
from app.database.init_llm_providers import upsert_provider_for_key
from app.database.models.llm_model import LLModel
from app.database.models.llm_routing_group import LLMRoutingGroup
from app.database.models.resource_group import ResourceGroup, resource_group_llm_models


# What an explicit null in a routing-group update clears: the columns where NULL
# is a real state ("no limit", "no fallbacks") and RoutingGroupResponse declares
# Optional. A null for anything else is dropped; those read back as required
# values, so storing one would fail the response after the write.
ROUTING_GROUP_CLEARABLE_FIELDS = frozenset({
    "description",
    "complexity_config",
    "complexity_floor_tier",
    "fallback_models",
    "context_window_fallbacks",
    "content_policy_fallbacks",
    "default_fallbacks",
    "max_fallbacks",
    "stream_timeout",
    "max_parallel_requests",
    "rpm_limit",
    "tpm_limit",
    "custom_metadata",
})


class LLMRouterService:
    """
    Service layer for LLM Router operations
    Provides monitoring, routing group management, and testing capabilities.
    Routing is handled by the Rust proxy — this service provides DB-based status/metrics.
    """

    # ──────────────────────────────────────────────────────────────────
    # Test-history retention
    #
    # The "Test Router" admin tab calls `test_routing` to send a
    # real chat-completion through the core proxy. We keep the
    # last N results per tenant so the Status tab can surface
    # them per-routing-group (filtered by `routing_group_id`).
    #
    # Why in-memory and not a DB table:
    #   - The data is purely operator UX — one admin clicking
    #     "Test", another admin reading "what did it pick".
    #     Surviving a control-plane restart isn't worth the
    #     migration.
    #   - The proxy already records the actual request via its
    #     normal usage_log / proxy_log pipeline. The test history
    #     here is a thin convenience overlay, not a billing
    #     record.
    #
    # Bounded by `_TEST_HISTORY_MAX_PER_TENANT` so a long-running
    # control plane doesn't grow unbounded under heavy admin
    # testing.
    # ──────────────────────────────────────────────────────────────────
    _TEST_HISTORY_MAX_PER_TENANT = 50

    def __init__(
        self,
        logger: BrutorLogger,
    ):
        """
        Initialize LLM Router Service

        Args:
            logger: Logger instance for operation tracking
        """
        self.logger = logger
        # Per-tenant ring buffer of recent test results.
        self._test_history: Dict[str, Deque[Dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self._TEST_HISTORY_MAX_PER_TENANT)
        )

    # =========================================================================
    # Routing Group CRUD Operations
    # =========================================================================

    async def get_routing_groups(
        self,
        db: Session,
        tenant_id: str,
        enabled_only: bool = False
    ) -> Dict[str, Any]:
        """
        Get all routing groups for a tenant

        Args:
            db: Database session
            tenant_id: Tenant ID
            enabled_only: Only return enabled routing groups

        Returns:
            Dictionary matching RoutingGroupListResponse
        """
        try:
            self.logger.debug("Listing routing groups", context={
                "tenant_id": tenant_id,
                "enabled_only": enabled_only
            })

            # Build query
            query = db.query(LLMRoutingGroup).filter(
                LLMRoutingGroup.tenant_id == tenant_id
            )

            if enabled_only:
                query = query.filter(LLMRoutingGroup.enabled == True)

            # Get routing groups with model count
            routing_groups = query.all()

            # Convert to list items with configuration indicators
            group_items = []
            for group in routing_groups:
                # Count all fallback configurations
                fb_models = group.fallback_models or []
                fb_cw = group.context_window_fallbacks or []
                fb_cp = group.content_policy_fallbacks or []
                fb_def = group.default_fallbacks or []
                fallback_count = len(fb_models) + len(fb_cw) + len(fb_cp) + len(fb_def)

                group_items.append({
                    "id": group.id,
                    "name": group.name,
                    "description": group.description,
                    "mode": group.mode or "chat",
                    "routing_strategy": group.routing_strategy,
                    "enabled": group.enabled,
                    "num_models": len([m for m in group.models if not getattr(m, 'is_routing', False)]) if group.models else 0,
                    # Configuration indicators
                    "num_retries": group.num_retries,
                    "cooldown_time": group.cooldown_time,
                    # Fallback details
                    "has_fallbacks": fallback_count > 0,
                    "fallback_count": fallback_count,
                    "fallback_models": fb_models or None,
                    "context_window_fallbacks": fb_cw or None,
                    "content_policy_fallbacks": fb_cp or None,
                    "default_fallbacks": fb_def or None,
                    # Feature flags
                    "alerting_enabled": group.alerting_enabled or False,
                    # Rate limits
                    "rpm_limit": group.rpm_limit,
                    "tpm_limit": group.tpm_limit,
                    "max_parallel_requests": group.max_parallel_requests,
                    # Timestamps
                    "created_at": group.created_at.isoformat() if group.created_at else None,
                    "updated_at": group.updated_at.isoformat() if group.updated_at else None
                })

            return {
                "routing_groups": group_items,
                "total": len(group_items)
            }

        except Exception as e:
            self.logger.error("Failed to list routing groups", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise

    async def get_routing_group(
        self,
        db: Session,
        tenant_id: str,
        group_id: str
    ) -> Dict[str, Any]:
        """
        Get specific routing group details

        Args:
            db: Database session
            tenant_id: Tenant ID
            group_id: Routing group ID

        Returns:
            Dictionary matching RoutingGroupResponse
        """
        try:
            self.logger.debug("Getting routing group", context={
                "tenant_id": tenant_id,
                "group_id": group_id
            })

            routing_group = db.query(LLMRoutingGroup).filter(
                and_(
                    LLMRoutingGroup.id == group_id,
                    LLMRoutingGroup.tenant_id == tenant_id
                )
            ).first()

            if not routing_group:
                raise ValueError(f"Routing group {group_id} not found")

            # Convert to response dict
            return {
                "id": routing_group.id,
                "tenant_id": routing_group.tenant_id,
                "name": routing_group.name,
                "description": routing_group.description,
                "mode": routing_group.mode or "chat",
                "routing_strategy": routing_group.routing_strategy,
                "complexity_config": routing_group.complexity_config,
                "complexity_floor_tier": routing_group.complexity_floor_tier,
                "cooldown_time": routing_group.cooldown_time or 60,
                "fallback_models": routing_group.fallback_models,
                "context_window_fallbacks": routing_group.context_window_fallbacks,
                "content_policy_fallbacks": routing_group.content_policy_fallbacks,
                "default_fallbacks": routing_group.default_fallbacks,
                "max_fallbacks": routing_group.max_fallbacks,
                "required_capabilities": routing_group.required_capabilities,
                "num_retries": routing_group.num_retries or 3,
                "retry_after": routing_group.retry_after or 5,
                "stream_timeout": routing_group.stream_timeout,
                "alerting_enabled": routing_group.alerting_enabled or False,
                "alerting_threshold": routing_group.alerting_threshold or 300,
                "alerting_webhook_url": routing_group.alerting_webhook_url,
                "alerting_config": routing_group.alerting_config,
                "routing_max_retries": routing_group.routing_max_retries or 3,
                "routing_retry_delay": routing_group.routing_retry_delay or 0.1,
                "ttl_for_blocked_models": routing_group.ttl_for_blocked_models or 300,
                "max_parallel_requests": routing_group.max_parallel_requests,
                "rpm_limit": routing_group.rpm_limit,
                "tpm_limit": routing_group.tpm_limit,
                "router_settings": routing_group.router_settings,
                "enabled": routing_group.enabled,
                "custom_metadata": routing_group.custom_metadata,
                "num_models": len([m for m in routing_group.models if not getattr(m, 'is_routing', False)]) if routing_group.models else 0,
                "created_at": routing_group.created_at.isoformat() if routing_group.created_at else None,
                "updated_at": routing_group.updated_at.isoformat() if routing_group.updated_at else None
            }

        except Exception as e:
            self.logger.error("Failed to get routing group", context={
                "tenant_id": tenant_id,
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise

    async def create_routing_group(
        self,
        db: Session,
        tenant_id: str,
        group_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Create a new routing group

        Args:
            db: Database session
            tenant_id: Tenant ID
            group_data: Routing group configuration

        Returns:
            Dictionary matching RoutingGroupResponse
        """
        try:
            self.logger.debug("Creating routing group", context={
                "tenant_id": tenant_id,
                "group_id": group_data.get("id")
            })

            # Check if routing group already exists. The id is the primary key
            # across all tenants, so a clash with another tenant's group has to
            # be caught here too or it surfaces as a 500 quoting the SQL.
            existing = db.query(LLMRoutingGroup).filter(
                LLMRoutingGroup.id == group_data["id"]
            ).first()

            if existing:
                raise ValueError(f"Routing group {group_data['id']} already exists")

            # Create routing group
            routing_group = LLMRoutingGroup(
                id=group_data["id"],
                tenant_id=tenant_id,
                name=group_data["name"],
                description=group_data.get("description"),
                # Required at creation time — there's no sensible
                # default because the dispatch endpoint depends on
                # it. Pydantic enforces presence at the request
                # layer; we still default-guard here so a direct
                # service-layer call doesn't insert NULL and trip
                # the NOT NULL constraint with a confusing 500.
                mode=group_data.get("mode", "chat"),
                routing_strategy=group_data.get("routing_strategy", "usage-based-routing-v2"),
                cooldown_time=group_data.get("cooldown_time", 60),
                fallback_models=group_data.get("fallback_models"),
                context_window_fallbacks=group_data.get("context_window_fallbacks"),
                content_policy_fallbacks=group_data.get("content_policy_fallbacks"),
                default_fallbacks=group_data.get("default_fallbacks"),
                max_fallbacks=group_data.get("max_fallbacks"),
                complexity_config=group_data.get("complexity_config"),
                complexity_floor_tier=group_data.get("complexity_floor_tier"),
                stream_timeout=group_data.get("stream_timeout"),
                required_capabilities=None,
                num_retries=group_data.get("num_retries", 3),
                retry_after=group_data.get("retry_after", 5),
                # Not read by the gateway, and not available in this release
                # (the request model refuses a value): the column defaults.
                alerting_enabled=False,
                alerting_threshold=300,
                alerting_webhook_url=None,
                alerting_config=None,
                routing_max_retries=3,
                routing_retry_delay=0.1,
                ttl_for_blocked_models=300,
                max_parallel_requests=group_data.get("max_parallel_requests"),
                rpm_limit=group_data.get("rpm_limit"),
                tpm_limit=group_data.get("tpm_limit"),
                router_settings=None,
                enabled=group_data.get("enabled", True),
                custom_metadata=group_data.get("custom_metadata"),
                # Passed only when the caller supplied one, so an omitted
                # version falls through to the column default ("1.0")
                # rather than being written as an explicit NULL.
                **({"version": group_data["version"]} if group_data.get("version") else {}),
            )

            db.add(routing_group)
            db.flush()
            db.refresh(routing_group)

            # =================================================================
            # CREATE VIRTUAL LLModel
            # =================================================================
            # A virtual model makes the routing group selectable by end-users
            # and AI agents just like any other model.  model_name = group ID
            # so the router spreads the request across member models.
            virtual_model_id = f"rg-{routing_group.id}"

            existing_virtual = db.query(LLModel).filter(
                LLModel.id == virtual_model_id
            ).first()

            if not existing_virtual:
                # Resolve (or stub) the synthetic `routing_group`
                # provider entry — every LLModel row needs a real
                # FK into `llm_providers.id` since the legacy
                # free-form `provider` string column was promoted
                # to a relationship. Passing the
                # raw string `provider="routing_group"` here used
                # to silently work; today it tries to assign a
                # `str` to the `provider` *relationship* and SQL-
                # Alchemy fails with the cryptic `'str' object has
                # no attribute '_sa_instance_state'` 500 the admin
                # UI just hit. `upsert_provider_for_key` is the
                # canonical way to materialise a stub when one
                # doesn't already exist for this tenant.
                routing_group_provider = upsert_provider_for_key(
                    db,
                    tenant_id,
                    key="routing_group",
                    display_name="Routing Group",
                    category="virtual",
                    is_system=True,
                )

                virtual_model = LLModel(
                    id=virtual_model_id,
                    tenant_id=tenant_id,
                    name=f"[Routing Group] {routing_group.name}",
                    description=routing_group.description or f"Load-balanced routing group: {routing_group.name}",
                    usage=f"Virtual AI Model — routes requests across member models using {routing_group.routing_strategy} strategy",
                    provider_id=routing_group_provider.id,
                    model_name=routing_group.id,   # routing key
                    mode=LLModelMode.ROUTING.value,
                    routing_group_id=routing_group.id,
                    enabled=routing_group.enabled if routing_group.enabled is not None else True,
                    health_status="healthy",
                    api_key=None,
                    api_url_auto_discovered=True,
                    auth_type="none",
                    supports_streaming=True,
                    supports_function_calling=True,
                    supports_vision=False,
                    drop_params=True,
                )
                db.add(virtual_model)
                db.flush()

                # Assign virtual model to Default resource group
                # so it's accessible to all users (same as regular models)
                default_group = db.query(ResourceGroup).filter(
                    ResourceGroup.tenant_id == tenant_id,
                    ResourceGroup.name == ResourceGroupName.DEFAULT.value,
                    ResourceGroup.is_system == True,
                ).first()

                if default_group:
                    db.execute(
                        resource_group_llm_models.insert().values(
                            resource_group_id=default_group.id,
                            llm_model_id=virtual_model_id
                        )
                    )

                self.logger.info("Virtual LLModel created for routing group", context={
                    "tenant_id": tenant_id,
                    "routing_group_id": routing_group.id,
                    "virtual_model_id": virtual_model_id,
                    "default_group_assigned": default_group is not None,
                    "default_group_id": default_group.id if default_group else None,
                })

            self.logger.info("Routing group created successfully", context={
                "tenant_id": tenant_id,
                "group_id": routing_group.id
            })

            # Return created group
            return await self.get_routing_group(db, tenant_id, routing_group.id)

        except Exception as e:
            self.logger.error("Failed to create routing group", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise

    async def update_routing_group(
        self,
        db: Session,
        tenant_id: str,
        group_id: str,
        update_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Update routing group configuration

        Args:
            db: Database session
            tenant_id: Tenant ID
            group_id: Routing group ID
            update_data: Configuration updates

        Returns:
            Dictionary matching RoutingGroupResponse
        """
        try:
            self.logger.debug("Updating routing group", context={
                "tenant_id": tenant_id,
                "group_id": group_id,
                "updates": list(update_data.keys())
            })

            routing_group = db.query(LLMRoutingGroup).filter(
                and_(
                    LLMRoutingGroup.id == group_id,
                    LLMRoutingGroup.tenant_id == tenant_id
                )
            ).first()

            if not routing_group:
                raise ValueError(f"Routing group {group_id} not found")

            # Mode is immutable once members are assigned. The
            # virtual model carries the mode forward to dispatch,
            # and a mid-flight mode change would mean existing
            # members suddenly serve the wrong inbound surface.
            # Cleanest contract: reject the update with a clear
            # message; admin moves members out, changes mode,
            # moves them back.
            if "mode" in update_data and update_data["mode"] is not None:
                new_mode = update_data["mode"]
                if new_mode != routing_group.mode:
                    member_count = (
                        db.query(LLModel)
                        .filter(
                            LLModel.tenant_id == tenant_id,
                            LLModel.routing_group_id == group_id,
                            LLModel.mode != LLModelMode.ROUTING.value,
                        )
                        .count()
                    )
                    if member_count > 0:
                        raise ValueError(
                            f"Cannot change mode of routing group {group_id!r} from "
                            f"{routing_group.mode!r} to {new_mode!r} while {member_count} "
                            f"member model(s) are assigned. Detach the members first "
                            f"(LLM Models page), update the group's mode, then "
                            f"reassign matching-mode models."
                        )

            # Update fields
            for field, value in update_data.items():
                if not hasattr(routing_group, field):
                    continue
                if value is not None or field in ROUTING_GROUP_CLEARABLE_FIELDS:
                    setattr(routing_group, field, value)

            routing_group.updated_at = datetime.now(timezone.utc)

            db.flush()
            db.refresh(routing_group)

            # =================================================================
            # SYNC VIRTUAL LLModel
            # =================================================================
            virtual_model_id = f"rg-{group_id}"
            virtual_model = db.query(LLModel).filter(
                LLModel.id == virtual_model_id
            ).first()

            if virtual_model:
                if "name" in update_data and update_data["name"] is not None:
                    virtual_model.name = f"[Routing Group] {update_data['name']}"
                if "description" in update_data:
                    virtual_model.description = update_data["description"]
                if "enabled" in update_data and update_data["enabled"] is not None:
                    virtual_model.enabled = update_data["enabled"]
                if "routing_strategy" in update_data and update_data["routing_strategy"] is not None:
                    virtual_model.usage = (
                        f"Virtual AI Model — routes requests across member models "
                        f"using {update_data['routing_strategy']} strategy"
                    )
                virtual_model.updated_at = datetime.now(timezone.utc)
                db.flush()

            self.logger.info("Routing group updated successfully", context={
                "tenant_id": tenant_id,
                "group_id": group_id
            })

            # Return updated group
            return await self.get_routing_group(db, tenant_id, group_id)

        except Exception as e:
            self.logger.error("Failed to update routing group", context={
                "tenant_id": tenant_id,
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise

    async def delete_routing_group(
        self,
        db: Session,
        tenant_id: str,
        group_id: str
    ) -> Dict[str, Any]:
        """
        Delete routing group.
        Automatically disconnects any assigned models (sets their routing_group_id to None)
        before deleting the routing group and its virtual model.

        Args:
            db: Database session
            tenant_id: Tenant ID
            group_id: Routing group ID

        Returns:
            Dictionary matching RoutingGroupDeleteResponse
        """
        try:
            self.logger.debug("Deleting routing group", context={
                "tenant_id": tenant_id,
                "group_id": group_id
            })

            routing_group = db.query(LLMRoutingGroup).filter(
                and_(
                    LLMRoutingGroup.id == group_id,
                    LLMRoutingGroup.tenant_id == tenant_id
                )
            ).first()

            if not routing_group:
                raise ValueError(f"Routing group {group_id} not found")

            # Disconnect any real models assigned to this routing group
            # (set routing_group_id to None so they become standalone models)
            disconnected_count = db.query(LLModel).filter(
                and_(
                    LLModel.routing_group_id == group_id,
                    LLModel.tenant_id == tenant_id,
                    LLModel.mode != LLModelMode.ROUTING.value  # Exclude virtual model
                )
            ).update(
                {LLModel.routing_group_id: None},
                synchronize_session="fetch"
            )

            if disconnected_count > 0:
                self.logger.info("Disconnected models from routing group", context={
                    "tenant_id": tenant_id,
                    "group_id": group_id,
                    "models_disconnected": disconnected_count
                })

            # Delete associated virtual LLModel first
            virtual_model_id = f"rg-{group_id}"
            virtual_model = db.query(LLModel).filter(
                LLModel.id == virtual_model_id
            ).first()
            if virtual_model:
                db.delete(virtual_model)
                self.logger.info("Deleted virtual LLModel for routing group", context={
                    "virtual_model_id": virtual_model_id,
                    "routing_group_id": group_id
                })

            # Delete routing group
            db.delete(routing_group)
            db.flush()

            self.logger.info("Routing group deleted successfully", context={
                "tenant_id": tenant_id,
                "group_id": group_id,
                "models_disconnected": disconnected_count
            })

            return {
                "success": True,
                "message": f"Routing group {group_id} deleted successfully",
                "group_id": group_id,
                "models_disconnected": disconnected_count
            }

        except Exception as e:
            self.logger.error("Failed to delete routing group", context={
                "tenant_id": tenant_id,
                "group_id": group_id,
                "error": str(e)
            }, exc_info=True)
            raise

    # =========================================================================
    # Status Operations
    # =========================================================================

    async def get_router_status(self, db: Session, tenant_id: str) -> Dict[str, Any]:
        """
        Get comprehensive router status with per-routing-group breakdowns
        and router cache statistics.

        Only includes ENABLED models and ENABLED routing groups — disabled
        items are available for configuration but not part of active stats.

        Args:
            db: Database session
            tenant_id: Tenant ID

        Returns:
            Dictionary matching the enhanced RouterStatusResponse Pydantic model
        """
        try:
            now = datetime.now(timezone.utc)

            # ------------------------------------------------------------------
            # 1. Routing is handled by the Rust proxy — always enabled
            # ------------------------------------------------------------------

            # ------------------------------------------------------------------
            # 2. No adapter-level stats — the Rust proxy handles routing
            # ------------------------------------------------------------------

            # ------------------------------------------------------------------
            # 3. Enabled real models only (exclude virtual routing models
            #    and disabled models)
            # ------------------------------------------------------------------
            enabled_models = db.query(LLModel).filter(
                and_(
                    LLModel.tenant_id == tenant_id,
                    LLModel.enabled == True,
                    LLModel.mode != LLModelMode.ROUTING.value,
                )
            ).all()

            # Deployment health
            healthy = sum(1 for m in enabled_models if m.health_status == "healthy")
            unhealthy = sum(1 for m in enabled_models if m.health_status == "unhealthy")
            unassigned = sum(1 for m in enabled_models if not m.routing_group_id)

            # Global aggregates (enabled models only)
            total_requests = sum(m.total_requests or 0 for m in enabled_models)
            total_failed = sum(m.failed_requests or 0 for m in enabled_models)
            error_rate = (total_failed / total_requests * 100) if total_requests > 0 else 0.0

            total_weighted_latency = sum(
                (m.avg_latency_ms or 0) * (m.total_requests or 0)
                for m in enabled_models
            )
            avg_latency = (total_weighted_latency / total_requests) if total_requests > 0 else 0.0

            # ------------------------------------------------------------------
            # 4. Enabled routing groups only
            # ------------------------------------------------------------------
            routing_groups_db = db.query(LLMRoutingGroup).filter(
                and_(
                    LLMRoutingGroup.tenant_id == tenant_id,
                    LLMRoutingGroup.enabled == True,
                )
            ).all()

            # Index enabled models by routing_group_id
            models_by_group: Dict[str, List] = {}
            for m in enabled_models:
                if m.routing_group_id:
                    models_by_group.setdefault(m.routing_group_id, []).append(m)

            group_summaries = []
            for group in routing_groups_db:
                # Only enabled models within this group
                group_models = models_by_group.get(group.id, [])

                g_healthy = sum(1 for m in group_models if m.health_status == "healthy")
                g_unhealthy = sum(1 for m in group_models if m.health_status == "unhealthy")
                g_total_reqs = 0
                g_total_failed = 0
                g_weighted_lat = 0.0
                model_summaries = []

                for m in group_models:
                    reqs = m.total_requests or 0
                    fails = m.failed_requests or 0
                    err = (fails / reqs * 100) if reqs > 0 else 0.0

                    g_total_reqs += reqs
                    g_total_failed += fails
                    g_weighted_lat += (m.avg_latency_ms or 0) * reqs

                    model_summaries.append({
                        "model_id": m.id,
                        "model_name": m.name,
                        # Read the provider slug, NOT the LlmProvider
                        # ORM instance. The schema promoted the
                        # legacy free-form `provider` string column to
                        # a real `llm_providers` row + relationship;
                        # `m.provider` now resolves to the joined ORM
                        # object. Putting that in a Pydantic response
                        # dict triggers a validation error ("expected
                        # str, got LlmProvider"), and the error's
                        # `__str__` then calls `repr()` on the
                        # detached instance which crashes with
                        # `DetachedInstanceError` — surfaces to the
                        # browser as a misleading CORS-header-missing
                        # 500. Reading `.key` keeps the response shape
                        # stable.
                        "provider": m.provider.key if m.provider else None,
                        "health_status": m.health_status or "unknown",
                        "routing_weight": m.routing_weight or 1,
                        "avg_latency_ms": m.avg_latency_ms,
                        "total_requests": reqs,
                        "failed_requests": fails,
                        "error_rate": round(err, 2),
                    })

                g_error_rate = (g_total_failed / g_total_reqs * 100) if g_total_reqs > 0 else 0.0
                g_avg_latency = (g_weighted_lat / g_total_reqs) if g_total_reqs > 0 else None

                group_summaries.append({
                    "id": group.id,
                    "name": group.name,
                    "routing_strategy": group.routing_strategy,
                    "total_models": len(group_models),
                    "healthy_models": g_healthy,
                    "unhealthy_models": g_unhealthy,
                    "total_requests": g_total_reqs,
                    "total_failed": g_total_failed,
                    "error_rate": round(g_error_rate, 2),
                    "avg_latency_ms": round(g_avg_latency, 1) if g_avg_latency is not None else None,
                    "alerting_enabled": group.alerting_enabled or False,
                    "has_fallbacks": bool(group.fallback_models or group.context_window_fallbacks),
                    "models": model_summaries,
                })

            # ------------------------------------------------------------------
            # 5. Build response
            # ------------------------------------------------------------------
            return {
                "enabled": True,
                "routing_strategy": "proxy-managed",

                # Deployments (enabled only)
                "total_deployments": len(enabled_models),
                "healthy_deployments": healthy,
                "unhealthy_deployments": unhealthy,
                "unassigned_deployments": unassigned,

                # Routing groups (enabled only)
                "total_routing_groups": len(routing_groups_db),
                "routing_groups": group_summaries,

                # Router cache — managed by the Rust proxy
                "router_cache": {
                    "cached_routers": 0,
                    "max_cached_routers": 0,
                    "cache_utilization": 0.0,
                    "cache_hits": 0,
                    "cache_misses": 0,
                    "hit_rate": 0.0,
                    "total_rebuilds": 0,
                    "router_ttl_seconds": 0,
                },

                # Reliability
                "fallback_enabled": True,
                "health_check_enabled": True,

                # Aggregate metrics (enabled models only)
                "total_requests": total_requests,
                "total_errors": total_failed,
                "error_rate": round(error_rate, 2),
                "avg_latency_ms": round(avg_latency, 1),

                "uptime_seconds": 0,
                "timestamp": now.isoformat(),
            }

        except Exception as e:
            self.logger.error("Failed to get router status", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise

    async def get_router_health(self, db: Session, tenant_id: str) -> Dict[str, Any]:
        """
        Quick health check for router

        Args:
            db: Database session
            tenant_id: Tenant ID

        Returns:
            Dictionary matching RouterHealthCheckResponse Pydantic model
        """
        try:
            # Count enabled models directly from DB (routing handled by Rust proxy)
            total_models = db.query(func.count(LLModel.id)).filter(
                and_(
                    LLModel.tenant_id == tenant_id,
                    LLModel.enabled == True,
                    LLModel.mode != LLModelMode.ROUTING.value,
                )
            ).scalar() or 0

            # Get routing group count
            total_groups = db.query(func.count(LLMRoutingGroup.id)).filter(
                and_(
                    LLMRoutingGroup.tenant_id == tenant_id,
                    LLMRoutingGroup.enabled == True
                )
            ).scalar() or 0

            return {
                "status": "healthy" if total_models > 0 else "degraded",
                "enabled": True,
                "deployments_available": total_models,
                "routing_groups_available": total_groups,
                "cache_available": False,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            self.logger.warning("Router health check failed", context={
                "tenant_id": tenant_id,
                "error": str(e)
            })
            return {
                "status": "unhealthy",
                "enabled": False,
                "deployments_available": 0,
                "routing_groups_available": 0,
                "cache_available": False,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

    # =========================================================================
    # Testing Operations
    # =========================================================================

    async def test_routing(
        self,
        db: Session,
        tenant_id: str,
        message: str,
        routing_group_id: str,
    ) -> Dict[str, Any]:
        """
        Send a real chat-completion through the proxy against a
        routing group. Returns which member model the router
        actually selected (read from the upstream's `model` field
        in the response), the response text, and end-to-end
        latency. The result is appended to the per-tenant test
        history so the Status tab can surface it.

        We reach the proxy at `BRUTOR_GATEWAY_INTERNAL_URL` using the
        global health-check API key. Same path the LLM-models
        admin page already uses for "Test Connection". We do NOT
        send the `X-Brutor-Internal-Health-Check` sentinel here
        — this is a real admin-initiated request and should
        count toward the routing group's metrics.

        Args:
            db: Database session
            tenant_id: Tenant ID
            message: Test message body (used as the user turn)
            routing_group_id: Routing group to dispatch through

        Returns:
            Dictionary matching `RouterTestResponse`. The
            `selected_model` field is the **actual** member model
            the router picked (post-strategy + cooldown + rate-
            limit filtering), not a guess from a DB-side mock.
        """
        try:
            self.logger.debug("Testing routing", context={
                "tenant_id": tenant_id,
                "routing_group_id": routing_group_id,
            })

            # Validate the group exists and belongs to this tenant
            # before paying the proxy round-trip cost.
            group = db.query(LLMRoutingGroup).filter(
                and_(
                    LLMRoutingGroup.id == routing_group_id,
                    LLMRoutingGroup.tenant_id == tenant_id,
                )
            ).first()
            if not group:
                raise ValueError(f"Routing group {routing_group_id!r} not found for tenant")

            # The virtual model's `model_name` is the routing
            # group id (set by `create_routing_group`); the
            # proxy resolves this to the routing group, applies
            # the strategy, and dispatches to a member model.
            model_name = group.id

            # Pick the upstream surface based on the group's
            # declared mode. The Test Router covers chat,
            # responses, and completion (the three text-shaped
            # surfaces operators most commonly route over). Other
            # modes (embedding / image / audio / video) take very
            # different request shapes — we surface a clear error
            # rather than guess. They're testable from each
            # mode's own admin page (LLM Models → Test
            # Connection) which already wires up the right
            # endpoint per mode.
            group_mode = (group.mode or "chat").lower()
            mode_to_endpoint = {
                "chat": "/v1/proxy/llm/chat/completions",
                "responses": "/v1/proxy/llm/responses",
                "completion": "/v1/proxy/llm/completions",
            }
            endpoint = mode_to_endpoint.get(group_mode)
            if endpoint is None:
                # Surface a structured error in the same shape
                # the success path produces, so the admin UI's
                # error renderer doesn't need a special case.
                timestamp = datetime.now(timezone.utc).isoformat()
                fallback = {
                    "timestamp": timestamp,
                    "routing_group_id": routing_group_id,
                    "routing_group_name": group.name,
                    "routing_strategy": group.routing_strategy,
                    "message": message,
                    "selected_model": None,
                    "selected_provider": None,
                    "response_text": None,
                    "latency_ms": 0.0,
                    "status_code": None,
                    "error": (
                        f"Test Router does not yet support {group_mode!r} routing "
                        f"groups (the request body shape differs from chat / "
                        f"responses / completion). Test individual member models "
                        f"via the LLM Models page's Test Connection action."
                    ),
                }
                self._test_history[tenant_id].appendleft(fallback)
                return fallback

            proxy_url = proxy_internal_url()
            health_check_key = gateway_service_api_key()

            headers = {
                "Content-Type": "application/json",
                "X-Tenant-ID": tenant_id,
            }
            if health_check_key:
                # Bearer + X-API-Key both because some clients
                # send one and some the other; the proxy accepts
                # either. See `middleware/tenant.rs` Step 3.
                headers["Authorization"] = f"Bearer {health_check_key}"
                headers["X-API-Key"] = health_check_key

            # Per-mode body shape. Chat takes `messages`, the
            # Responses API takes `input` (a single string is
            # the documented short form), and legacy completion
            # takes `prompt`. Token caps are tight to keep test
            # cost effectively zero.
            if group_mode == "chat":
                payload = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": message}],
                    "max_tokens": 64,
                    "temperature": 0.3,
                    "stream": False,
                }
            elif group_mode == "responses":
                payload = {
                    "model": model_name,
                    "input": message,
                    "max_output_tokens": 64,
                    "temperature": 0.3,
                    "stream": False,
                }
            else:  # completion
                payload = {
                    "model": model_name,
                    "prompt": message,
                    "max_tokens": 64,
                    "temperature": 0.3,
                    "stream": False,
                }

            timestamp = datetime.now(timezone.utc).isoformat()
            started_at = time.monotonic()
            error_message: Optional[str] = None
            response_text: Optional[str] = None
            selected_model_name: Optional[str] = None
            selected_provider: Optional[str] = None
            status_code: Optional[int] = None

            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        f"{proxy_url.rstrip('/')}{endpoint}",
                        json=payload,
                        headers=headers,
                    )
                status_code = resp.status_code
                if resp.status_code >= 400:
                    error_message = (
                        f"Proxy returned {resp.status_code}: "
                        f"{resp.text[:500]}"
                    )
                else:
                    body = resp.json()
                    selected_model_name = body.get("model") or model_name
                    # Per-mode response-text extraction. Each
                    # surface returns a different shape; we
                    # squash them all into a single string for
                    # the UI's "Response" panel. Empty string on
                    # missing/empty — the UI renders the panel
                    # only when the string is non-empty.
                    response_text = self._extract_response_text(group_mode, body) or ""
                    # The proxy doesn't echo the provider in the
                    # response body — derive it from the catalog
                    # row matching the actual model that handled
                    # the request. Best-effort; missing values
                    # render as None in the UI.
                    member = db.query(LLModel).filter(
                        and_(
                            LLModel.tenant_id == tenant_id,
                            LLModel.model_name == selected_model_name,
                        )
                    ).first()
                    if member and member.provider:
                        selected_provider = member.provider.key
            except httpx.RequestError as e:
                error_message = f"Could not reach proxy: {e!s}"

            latency_ms = (time.monotonic() - started_at) * 1000.0

            entry = {
                "timestamp": timestamp,
                "routing_group_id": routing_group_id,
                "routing_group_name": group.name,
                "routing_strategy": group.routing_strategy,
                "message": message,
                "selected_model": selected_model_name,
                "selected_provider": selected_provider,
                "response_text": response_text,
                "latency_ms": round(latency_ms, 1),
                "status_code": status_code,
                "error": error_message,
            }
            self._test_history[tenant_id].appendleft(entry)
            return entry

        except Exception as e:
            self.logger.error("Failed to test routing", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise

    @staticmethod
    def _extract_response_text(group_mode: str, body: Dict[str, Any]) -> Optional[str]:
        """Pull the visible text out of an upstream response by
        mode. Each upstream surface returns a different shape
        and the admin UI's "Response" panel needs a single
        string. Best-effort: returns None when the shape doesn't
        match anything we know how to read (the UI renders the
        panel only when the string is truthy)."""
        try:
            if group_mode == "chat":
                # OpenAI chat-completions: choices[0].message.content
                return (
                    body.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content")
                )
            if group_mode == "completion":
                # Legacy completions: choices[0].text
                return body.get("choices", [{}])[0].get("text")
            if group_mode == "responses":
                # OpenAI responses-API. Two shapes seen in the
                # wild:
                #   1. The convenience field `output_text` (some
                #      models populate it; others don't).
                #   2. `output[].content[].text` for items of
                #      type=message — the canonical structured
                #      shape we already parse in the portal's
                #      SSE translator. Walk it best-effort.
                if isinstance(body.get("output_text"), str):
                    return body["output_text"]
                output = body.get("output") or []
                texts: List[str] = []
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "message":
                        for content in item.get("content") or []:
                            if isinstance(content, dict) and isinstance(
                                content.get("text"), str
                            ):
                                texts.append(content["text"])
                return "\n".join(texts) if texts else None
        except (AttributeError, IndexError, TypeError):
            # Defensive: any unexpected shape collapses to None
            # rather than 500ing the test endpoint. The admin
            # still sees status_code + selected_model and can
            # inspect the raw response via the proxy log.
            return None
        return None

    def get_test_history(
        self,
        tenant_id: str,
        routing_group_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return the recent test results for this tenant, most
        recent first, filtered by routing group if specified.

        Bounded by the per-tenant deque (`_TEST_HISTORY_MAX_PER_
        TENANT`) and the `limit` argument — the Status tab UI
        only shows ~10 at a time so the default limit is small
        enough to keep payloads tight.
        """
        entries = list(self._test_history.get(tenant_id, ()))
        if routing_group_id:
            entries = [e for e in entries if e.get("routing_group_id") == routing_group_id]
        return entries[: max(0, int(limit))]

    # =========================================================================
    # Deployment Operations
    # =========================================================================

    async def list_deployments(
        self,
        db: Session,
        tenant_id: str,
        filter_status: Optional[str] = None,
        routing_group_id: Optional[str] = None,
        enabled_only: bool = False,
        sort_by: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        List all model deployments

        Args:
            db: Database session
            tenant_id: Tenant ID
            filter_status: Filter by health status
            routing_group_id: Filter by routing group
            enabled_only: Only return enabled models
            sort_by: Sort field

        Returns:
            Dictionary matching DeploymentListResponse Pydantic model
        """
        try:
            # Build query
            query = db.query(LLModel).filter(
                LLModel.tenant_id == tenant_id
            )

            if filter_status:
                query = query.filter(LLModel.health_status == filter_status)

            if routing_group_id:
                query = query.filter(LLModel.routing_group_id == routing_group_id)

            if enabled_only:
                query = query.filter(LLModel.enabled == True)

            # Sort if requested (except error_rate which needs calculation)
            if sort_by and sort_by != "error_rate":
                if sort_by == "name":
                    query = query.order_by(LLModel.name)
                elif sort_by == "latency":
                    query = query.order_by(LLModel.avg_latency_ms.desc())
                elif sort_by == "requests":
                    query = query.order_by(LLModel.total_requests.desc())

            models = query.all()

            # Build deployment list
            deployments = []
            for model in models:
                total_reqs = model.total_requests or 0
                failed_reqs = model.failed_requests or 0
                error_rate = (failed_reqs / total_reqs * 100) if total_reqs > 0 else 0.0

                deployments.append({
                    "model_id": model.id,
                    "model_name": model.name,
                    "provider_model_name": model.model_name,
                    # Provider slug from the relationship.
                    "provider": model.provider.key if model.provider else None,
                    "enabled": model.enabled,
                    "health_status": model.health_status or "unknown",
                    "routing_weight": model.routing_weight,
                    "routing_group_id": model.routing_group_id,
                    "routing_group_name": model.routing_group.name if model.routing_group else None,
                    "avg_latency_ms": model.avg_latency_ms,
                    "total_requests": total_reqs,
                    "error_rate": error_rate,
                    "cost_per_1k_input": model.cost_per_input_token,
                    "cost_per_1k_output": model.cost_per_output_token
                })

            # Sort by error_rate if requested
            if sort_by == "error_rate":
                deployments.sort(key=lambda d: d["error_rate"], reverse=True)

            healthy = sum(1 for d in deployments if d["health_status"] == "healthy")
            unhealthy = sum(1 for d in deployments if d["health_status"] == "unhealthy")

            return {
                "deployments": deployments,
                "total": len(deployments),
                "healthy": healthy,
                "unhealthy": unhealthy
            }

        except Exception as e:
            self.logger.error("Failed to list deployments", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise

    async def get_deployment(
        self,
        model_id: str,
        tenant_id: str,
        db: Session
    ) -> Dict[str, Any]:
        """
        Get specific deployment details

        Args:
            model_id: Model ID
            tenant_id: Tenant ID
            db: Database session

        Returns:
            Dictionary matching ModelDeployment Pydantic model
        """
        try:
            model = db.query(LLModel).filter(
                and_(
                    LLModel.id == model_id,
                    LLModel.tenant_id == tenant_id
                )
            ).first()

            if not model:
                raise ValueError(f"Model {model_id} not found")

            total_reqs = model.total_requests or 0
            failed_reqs = model.failed_requests or 0
            error_rate = (failed_reqs / total_reqs * 100) if total_reqs > 0 else 0.0

            return {
                "model_id": model.id,
                "model_name": model.name,
                "provider_model_name": model.model_name,
                # Provider slug from the relationship.
                "provider": model.provider.key if model.provider else None,
                "enabled": model.enabled,
                "health_status": model.health_status or "unknown",
                "routing_weight": model.routing_weight,
                "routing_group_id": model.routing_group_id,
                "routing_group_name": model.routing_group.name if model.routing_group else None,
                "avg_latency_ms": model.avg_latency_ms,
                "total_requests": total_reqs,
                "error_rate": error_rate,
                "cost_per_1k_input": model.cost_per_input_token,
                "cost_per_1k_output": model.cost_per_output_token
            }

        except Exception as e:
            self.logger.error("Failed to get deployment", context={
                "model_id": model_id,
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise

    # =========================================================================
    # Statistics Operations
    # =========================================================================

    async def get_router_statistics(
        self,
        tenant_id: str,
        db: Session,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        group_by_model: bool = True,
        group_by_routing_group: bool = True
    ) -> Dict[str, Any]:
        """
        Get aggregated router statistics

        Args:
            tenant_id: Tenant ID
            db: Database session
            start_time: Start time (ISO 8601)
            end_time: End time (ISO 8601)
            group_by_model: Include per-model breakdown
            group_by_routing_group: Include per-routing-group breakdown

        Returns:
            Dictionary matching RouterStatistics Pydantic model
        """
        try:
            # Get all models
            models = db.query(LLModel).filter(
                and_(
                    LLModel.tenant_id == tenant_id,
                    LLModel.enabled == True
                )
            ).all()

            # Aggregate statistics
            total_requests = sum(m.total_requests or 0 for m in models)
            failed_requests = sum(m.failed_requests or 0 for m in models)
            successful_requests = total_requests - failed_requests

            # Calculate average latency (weighted by requests)
            total_weighted_latency = sum(
                (m.avg_latency_ms or 0) * (m.total_requests or 0)
                for m in models
            )
            avg_latency = (total_weighted_latency / total_requests) if total_requests > 0 else 0.0

            # Per-model breakdown
            requests_by_model = {}
            if group_by_model:
                for m in models:
                    requests_by_model[m.name] = m.total_requests or 0

            # Per-routing-group breakdown
            requests_by_routing_group = {}
            if group_by_routing_group:
                for m in models:
                    if m.routing_group_id:
                        group_name = m.routing_group.name if m.routing_group else m.routing_group_id
                        requests_by_routing_group[group_name] = requests_by_routing_group.get(group_name, 0) + (m.total_requests or 0)

            now = datetime.now(timezone.utc)
            period_start = start_time or now.replace(hour=0, minute=0, second=0).isoformat()
            period_end = end_time or now.isoformat()

            return {
                "total_requests": total_requests,
                "successful_requests": successful_requests,
                "failed_requests": failed_requests,
                "error_rate": (failed_requests / total_requests * 100) if total_requests > 0 else 0.0,
                "avg_latency_ms": avg_latency,
                "p50_latency_ms": None,
                "p95_latency_ms": None,
                "p99_latency_ms": None,
                "requests_by_model": requests_by_model,
                "requests_by_routing_group": requests_by_routing_group,
                "cache_hits": 0,
                "cache_misses": 0,
                "cache_hit_rate": 0.0,
                "period_start": period_start,
                "period_end": period_end
            }

        except Exception as e:
            self.logger.error("Failed to get router statistics", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise

    @staticmethod
    def assert_model_mode_matches_group(
        db: Session,
        tenant_id: str,
        routing_group_id: Optional[str],
        model_mode: Optional[str],
    ) -> None:
        """Reject an LLM model save that would put a mismatched-
        mode model into a routing group.

        Called from `update_llm` and `create_llm` whenever the
        caller is about to set `LLModel.routing_group_id`. The
        check is on the GROUP's declared mode, not on existing
        members, because:

          * A group's mode is fixed at creation time (or
            constrained-immutable while members exist), so it's
            the unambiguous source of truth.
          * Member-only checks would let a brand-new empty
            group accept whatever the first model brought —
            the group's `mode` field then drifts from members,
            and the very next member assignment would have
            nothing to validate against.

        Raises ValueError with an operator-actionable message;
        the FastAPI layer converts to 400.
        """
        # Detaching from a group is always fine. Treat the
        # empty string the same as None — admin-form payloads
        # sometimes serialize a cleared dropdown that way and
        # we don't want a ""-vs-None glitch to surface as a
        # spurious "routing group '' not found" error.
        if not routing_group_id:
            return

        # Virtual routing-group models (the synthetic LLModel
        # the control plane creates with id `rg-<group_id>` and
        # `mode='routing'` to act as the dispatch entry-point)
        # are intentionally pinned to their own group at
        # creation time. Their `mode='routing'` is a marker, not
        # a real surface, so it will always mismatch any real
        # group mode (chat / responses / …). Skip them — the
        # admin UI surfaces these in the model list and operators
        # legitimately want to edit *other* fields (resource-
        # group access, description, enabled flag) on them.
        if model_mode == "routing":
            return

        group = db.query(LLMRoutingGroup).filter(
            and_(
                LLMRoutingGroup.id == routing_group_id,
                LLMRoutingGroup.tenant_id == tenant_id,
            )
        ).first()
        if not group:
            raise ValueError(
                f"Routing group {routing_group_id!r} not found for this tenant."
            )

        group_mode = group.mode or "chat"
        # `model_mode` defaults to chat for legacy rows that
        # somehow lack a value — same back-compat default the
        # rest of the codebase uses for `LLModel.mode`.
        effective_model_mode = model_mode or "chat"

        if effective_model_mode != group_mode:
            raise ValueError(
                f"Cannot assign a {effective_model_mode!r}-mode model to routing "
                f"group {routing_group_id!r} (mode = {group_mode!r}). A routing "
                f"group dispatches one inbound request to one member, so members "
                f"must share the inbound surface. Either pick a routing group "
                f"whose mode matches this model, or change this model's mode."
            )
