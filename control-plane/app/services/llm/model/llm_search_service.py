# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
LLM Model Search Service - ADMIN VERSION (Simplified)
Searches ALL models - no group-based access control
"""

from typing import Optional, List, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import or_, desc, asc, Integer

from app.database.models.llm_model import LLModel
from app.database.models.llm_provider import LlmProvider
from app.api.v1.models.admin.llm_model import (
    LLModelSearchRequest,
    LLModelSearchResponse,
    LLModelSearchSummary,
    LLModelResponse,
    LLMModelHealthStatusResponse,
    PerformanceMetricsResponse,
    LLMModelGroupInfo
)


class LLModelSearchService:
    """
    Service for searching LLM models - ADMIN VERSION

    Simplified for admin use:
    - Searches ALL models in tenant (no access control filtering)
    - No group-based restrictions
    - Admin authentication handled at router level
    """

    async def search_models(
            self,
            db: Session,
            tenant_id: str,
            search_request: LLModelSearchRequest
    ) -> LLModelSearchResponse:
        """
        Search ALL LLM models in tenant with advanced filtering

        ADMIN-ONLY: Returns all models matching criteria,
        regardless of group assignments.

        Args:
            db: Database session
            tenant_id: Tenant ID
            search_request: Search criteria

        Returns:
            LLModelSearchResponse with matching models and metadata
        """

        # Build base query - ALL models in tenant
        query = db.query(LLModel).filter(LLModel.tenant_id == tenant_id)

        # =====================================================================
        # APPLY FILTERS
        # =====================================================================

        # Text search filters (partial, case-insensitive)
        if search_request.name:
            query = query.filter(
                LLModel.name.ilike(f"%{search_request.name}%")
            )

        if search_request.description:
            query = query.filter(
                LLModel.description.ilike(f"%{search_request.description}%")
            )

        if search_request.model_name:
            query = query.filter(
                LLModel.model_name.ilike(f"%{search_request.model_name}%")
            )

        # Provider filters — `LLModel.provider` is a
        # relationship to `LlmProvider` (slug now lives in
        # `LlmProvider.key`), so direct `==` / `in_` against the
        # relationship raises "Mapped instance expected for relationship
        # comparison" at query-build time. Translate to `has(...)` so
        # SQLAlchemy emits an EXISTS subquery against the provider
        # table's `key` column.
        if search_request.provider:
            query = query.filter(
                LLModel.provider.has(LlmProvider.key == search_request.provider)
            )

        if search_request.providers:
            query = query.filter(
                LLModel.provider.has(LlmProvider.key.in_(search_request.providers))
            )

        # Capability filters
        if search_request.supports_streaming is not None:
            query = query.filter(
                LLModel.supports_streaming == search_request.supports_streaming
            )

        if search_request.supports_function_calling is not None:
            query = query.filter(
                LLModel.supports_function_calling == search_request.supports_function_calling
            )

        if search_request.supports_vision is not None:
            query = query.filter(
                LLModel.supports_vision == search_request.supports_vision
            )

        if search_request.supports_batch is not None:
            query = query.filter(
                LLModel.supports_batch == search_request.supports_batch
            )

        # Mode filter (exact match, case-insensitive). Stored on the
        # row as a normalized lowercase string (see LLModelMode enum
        # in app/constants/llm.py).
        if search_request.mode:
            query = query.filter(
                LLModel.mode == search_request.mode.lower()
            )

        # Context-window filter. The value lives inside the
        # `capabilities` JSONB column under the `context_window`
        # key (e.g. `{"context_window": 400000, ...}`) — we extract
        # it with the JSONB `->>` operator and cast to integer for
        # numeric comparison. Models without the key fall through
        # because PostgreSQL's `NULL >= n` evaluates to NULL (not
        # TRUE), so unaware seeds (older catalogue entries, embedding
        # / audio-only models) are correctly excluded from a
        # context-size search rather than silently matching.
        if search_request.min_context_window is not None:
            query = query.filter(
                LLModel.capabilities.op('->>')('context_window').cast(Integer)
                >= search_request.min_context_window
            )
        if search_request.max_context_window is not None:
            query = query.filter(
                LLModel.capabilities.op('->>')('context_window').cast(Integer)
                <= search_request.max_context_window
            )

        # Cost filters
        if search_request.min_cost_per_input_token is not None:
            query = query.filter(
                LLModel.cost_per_input_token >= search_request.min_cost_per_input_token
            )

        if search_request.max_cost_per_input_token is not None:
            query = query.filter(
                LLModel.cost_per_input_token <= search_request.max_cost_per_input_token
            )

        if search_request.min_cost_per_output_token is not None:
            query = query.filter(
                LLModel.cost_per_output_token >= search_request.min_cost_per_output_token
            )

        if search_request.max_cost_per_output_token is not None:
            query = query.filter(
                LLModel.cost_per_output_token <= search_request.max_cost_per_output_token
            )

        # Tag search (in capabilities JSON)
        if search_request.tags:
            # Search for tags in capabilities.tags array
            # This assumes tags are stored as: {"tags": ["tag1", "tag2"]}
            tag_filters = []
            for tag in search_request.tags:
                # JSON search for tag in capabilities.tags array
                # PostgreSQL JSONB operator
                tag_filters.append(
                    LLModel.capabilities.op('->>')('tags').ilike(f'%{tag}%')
                )

            if tag_filters:
                # OR logic: match any tag
                query = query.filter(or_(*tag_filters))

        # Status filters
        if search_request.enabled_only:
            query = query.filter(LLModel.enabled == True)

        if search_request.healthy_only:
            query = query.filter(LLModel.health_status == 'healthy')

        # =====================================================================
        # GET TOTAL COUNT (before pagination)
        # =====================================================================

        total_count = query.count()

        # =====================================================================
        # SORTING
        # =====================================================================

        sort_field = self._get_sort_field(search_request.sort_by)

        if search_request.sort_order == "desc":
            query = query.order_by(desc(sort_field))
        else:
            query = query.order_by(asc(sort_field))

        # =====================================================================
        # PAGINATION
        # =====================================================================

        query = query.offset(search_request.offset).limit(search_request.limit)

        # =====================================================================
        # EXECUTE QUERY
        # =====================================================================

        models = query.all()

        # =====================================================================
        # BUILD RESPONSE
        # =====================================================================

        # Convert to response models
        model_responses = [
            self._convert_to_response(model, db)
            for model in models
        ]

        # Calculate has_more
        has_more = (search_request.offset + search_request.limit) < total_count

        # Build filters summary
        filters_applied = self._build_filters_summary(search_request)

        return LLModelSearchResponse(
            models=model_responses,
            total=total_count,
            offset=search_request.offset,
            limit=search_request.limit,
            has_more=has_more,
            filters_applied=filters_applied
        )

    async def get_search_summary(
            self,
            db: Session,
            tenant_id: str,
            search_request: LLModelSearchRequest
    ) -> LLModelSearchSummary:
        """
        Get summary statistics for search results

        ADMIN-ONLY: Returns statistics for ALL models matching criteria

        Returns aggregate statistics about models matching the search criteria.
        Useful for faceted search interfaces.
        """

        # Build base query (same as search, but without pagination)
        query = db.query(LLModel).filter(LLModel.tenant_id == tenant_id)

        # Apply same filters as search_models
        # (Copy filter logic from above)

        # Text filters
        if search_request.name:
            query = query.filter(LLModel.name.ilike(f"%{search_request.name}%"))
        if search_request.description:
            query = query.filter(LLModel.description.ilike(f"%{search_request.description}%"))
        if search_request.model_name:
            query = query.filter(LLModel.model_name.ilike(f"%{search_request.model_name}%"))

        # Provider filters — same `.has()` translation as in
        # `search_models` (see comment there). Both code paths must
        # agree so the summary aggregates and the paginated list use
        # the same row set.
        if search_request.provider:
            query = query.filter(
                LLModel.provider.has(LlmProvider.key == search_request.provider)
            )
        if search_request.providers:
            query = query.filter(
                LLModel.provider.has(LlmProvider.key.in_(search_request.providers))
            )

        # Capability filters
        if search_request.supports_streaming is not None:
            query = query.filter(LLModel.supports_streaming == search_request.supports_streaming)
        if search_request.supports_function_calling is not None:
            query = query.filter(LLModel.supports_function_calling == search_request.supports_function_calling)
        if search_request.supports_vision is not None:
            query = query.filter(LLModel.supports_vision == search_request.supports_vision)
        if search_request.supports_batch is not None:
            query = query.filter(LLModel.supports_batch == search_request.supports_batch)

        # Mode + context-window filters (mirror of search_models so
        # the summary aggregates use the same row set as the
        # paginated list).
        if search_request.mode:
            query = query.filter(LLModel.mode == search_request.mode.lower())
        if search_request.min_context_window is not None:
            query = query.filter(
                LLModel.capabilities.op('->>')('context_window').cast(Integer)
                >= search_request.min_context_window
            )
        if search_request.max_context_window is not None:
            query = query.filter(
                LLModel.capabilities.op('->>')('context_window').cast(Integer)
                <= search_request.max_context_window
            )

        # Cost filters
        if search_request.min_cost_per_input_token is not None:
            query = query.filter(LLModel.cost_per_input_token >= search_request.min_cost_per_input_token)
        if search_request.max_cost_per_input_token is not None:
            query = query.filter(LLModel.cost_per_input_token <= search_request.max_cost_per_input_token)
        if search_request.min_cost_per_output_token is not None:
            query = query.filter(LLModel.cost_per_output_token >= search_request.min_cost_per_output_token)
        if search_request.max_cost_per_output_token is not None:
            query = query.filter(LLModel.cost_per_output_token <= search_request.max_cost_per_output_token)

        # Status filters
        if search_request.enabled_only:
            query = query.filter(LLModel.enabled == True)
        if search_request.healthy_only:
            query = query.filter(LLModel.health_status == 'healthy')

        # Get all matching models
        models = query.all()

        # Calculate statistics
        total = len(models)

        # Provider breakdown — provider slug lives on
        # the relationship's `key` field.
        providers: dict[str, int] = {}
        for model in models:
            provider = model.provider.key if model.provider else None
            if provider is None:
                continue
            providers[provider] = providers.get(provider, 0) + 1

        # Capability counts
        streaming_capable = sum(1 for m in models if m.supports_streaming)
        function_calling_capable = sum(1 for m in models if m.supports_function_calling)
        vision_capable = sum(1 for m in models if m.supports_vision)
        batch_capable = sum(1 for m in models if m.supports_batch)

        # Cost statistics
        input_costs = [m.cost_per_input_token for m in models if m.cost_per_input_token]
        output_costs = [m.cost_per_output_token for m in models if m.cost_per_output_token]

        cost_stats = {
            "input": {
                "min": min(input_costs) if input_costs else None,
                "max": max(input_costs) if input_costs else None,
                "avg": sum(input_costs) / len(input_costs) if input_costs else None
            },
            "output": {
                "min": min(output_costs) if output_costs else None,
                "max": max(output_costs) if output_costs else None,
                "avg": sum(output_costs) / len(output_costs) if output_costs else None
            }
        }

        # Status counts
        enabled_count = sum(1 for m in models if m.enabled)
        disabled_count = total - enabled_count

        healthy_count = sum(1 for m in models if m.health_status == 'healthy')
        unhealthy_count = sum(1 for m in models if m.health_status != 'healthy')

        return LLModelSearchSummary(
            total_models=total,
            providers=providers,
            streaming_capable=streaming_capable,
            function_calling_capable=function_calling_capable,
            vision_capable=vision_capable,
            batch_capable=batch_capable,
            cost_stats=cost_stats,
            enabled_count=enabled_count,
            disabled_count=disabled_count,
            healthy_count=healthy_count,
            unhealthy_count=unhealthy_count
        )

    async def get_search_suggestions(
            self,
            db: Session,
            tenant_id: str,
            field: str,
            prefix: Optional[str] = None,
            limit: int = 20
    ) -> List[str]:
        """
        Get autocomplete suggestions for a field

        ADMIN-ONLY: Returns suggestions from ALL models

        Args:
            db: Database session
            tenant_id: Tenant ID
            field: Field to get suggestions for (provider, model_name, name, tags)
            prefix: Optional prefix to filter by
            limit: Max number of suggestions

        Returns:
            List of unique values for the field
        """

        # Map field names to database columns. The `provider` field is
        # special: `LLModel.provider` is a relationship,
        # not a column, so distinct() and ilike() on it would raise the
        # same "Mapped instance expected" error as the search filters.
        # The actual distinct slug lives on `LlmProvider.key`, reachable
        # via a JOIN.
        valid_fields = {'provider', 'model_name', 'name', 'tags'}

        if field not in valid_fields:
            raise ValueError(
                f"Invalid field: {field}. Must be one of: {', '.join(sorted(valid_fields))}"
            )

        if field == 'tags':
            # Special handling for tags in JSON
            # This requires extracting tags from capabilities JSON
            # Implementation depends on your database (PostgreSQL has JSON functions)

            # PostgreSQL example:
            # query = db.query(
            #     func.jsonb_array_elements_text(LLModel.capabilities['tags']).label('tag')
            # ).filter(LLModel.tenant_id == tenant_id).distinct()

            # For now, return empty list if tags not implemented
            return []

        if field == 'provider':
            # JOIN through the relationship to read distinct slugs from
            # the provider table, scoped to the tenant.
            query = (
                db.query(LlmProvider.key)
                .join(LLModel, LLModel.provider_id == LlmProvider.id)
                .filter(LLModel.tenant_id == tenant_id)
                .distinct()
            )
            if prefix:
                query = query.filter(LlmProvider.key.ilike(f"{prefix}%"))
            query = query.order_by(LlmProvider.key).limit(limit)
        else:
            # `model_name` / `name` are still plain columns on LLModel.
            column = {
                'model_name': LLModel.model_name,
                'name': LLModel.name,
            }[field]
            query = db.query(column).filter(
                LLModel.tenant_id == tenant_id
            ).distinct()
            if prefix:
                query = query.filter(column.ilike(f"{prefix}%"))
            query = query.order_by(column).limit(limit)

        # Execute and extract values
        results = query.all()
        suggestions = [result[0] for result in results if result[0]]

        return suggestions

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    def _get_sort_field(self, sort_by: str):
        """Map sort_by string to database field"""
        sort_map = {
            "name": LLModel.name,
            "created_at": LLModel.created_at,
            "updated_at": LLModel.updated_at,
            "cost_per_input_token": LLModel.cost_per_input_token,
            "cost_per_output_token": LLModel.cost_per_output_token,
            "avg_latency_ms": LLModel.avg_latency_ms,
            "total_requests": LLModel.total_requests
        }

        return sort_map.get(sort_by, LLModel.name)

    def _build_filters_summary(self, search_request: LLModelSearchRequest) -> Dict[str, Any]:
        """Build summary of applied filters for response"""
        filters = {}

        if search_request.name:
            filters['name'] = search_request.name

        if search_request.description:
            filters['description'] = search_request.description

        if search_request.model_name:
            filters['model_name'] = search_request.model_name

        if search_request.provider:
            filters['provider'] = search_request.provider

        if search_request.providers:
            filters['providers'] = search_request.providers

        if search_request.supports_streaming is not None:
            filters['supports_streaming'] = search_request.supports_streaming

        if search_request.supports_function_calling is not None:
            filters['supports_function_calling'] = search_request.supports_function_calling

        if search_request.supports_vision is not None:
            filters['supports_vision'] = search_request.supports_vision

        if search_request.supports_batch is not None:
            filters['supports_batch'] = search_request.supports_batch

        # Cost filters
        cost_filters = {}
        if search_request.min_cost_per_input_token is not None:
            cost_filters['min_input'] = search_request.min_cost_per_input_token
        if search_request.max_cost_per_input_token is not None:
            cost_filters['max_input'] = search_request.max_cost_per_input_token
        if search_request.min_cost_per_output_token is not None:
            cost_filters['min_output'] = search_request.min_cost_per_output_token
        if search_request.max_cost_per_output_token is not None:
            cost_filters['max_output'] = search_request.max_cost_per_output_token

        if cost_filters:
            filters['cost_range'] = cost_filters

        if search_request.tags:
            filters['tags'] = search_request.tags

        if search_request.enabled_only:
            filters['enabled_only'] = True

        if search_request.healthy_only:
            filters['healthy_only'] = True

        filters['sort'] = {
            'by': search_request.sort_by,
            'order': search_request.sort_order
        }

        return filters

    def _convert_to_response(self, model: LLModel, db: Session) -> LLModelResponse:
        """
        Convert database model to response model
        """

        # Build group info
        groups = [
            LLMModelGroupInfo(
                id=group.id,
                name=group.name,
                display_name=group.display_name,
                group_type=group.group_type,
                is_active=group.is_active,
                parent_group_id=group.parent_group_id
            )
            for group in model.resource_groups
        ]

        # Build health status
        health_status = LLMModelHealthStatusResponse(
            status=model.health_status or 'unknown',
            last_check=model.last_health_check.isoformat() if model.last_health_check else None,
            last_success=model.last_health_success.isoformat() if model.last_health_success else None,
            consecutive_failures=model.consecutive_health_failures or 0,
            error_message=model.health_check_error,
            avg_latency_ms=model.avg_latency_ms
        )

        # Build performance metrics
        performance_metrics = PerformanceMetricsResponse(
            total_requests=model.total_requests or 0,
            failed_requests=model.failed_requests or 0,
            success_rate=0.0,
            avg_latency_ms=model.avg_latency_ms,
            total_tokens_consumed=model.total_tokens_consumed or 0
        )

        return LLModelResponse(
            id=model.id,
            name=model.name,
            mode=model.mode,
            description=model.description,
            # Provider is a relationship — surface the
            # slug + the FK + the nested row.
            provider=model.provider.key if model.provider else None,
            provider_id=model.provider_id,
            provider_info=model.provider.to_dict() if model.provider else None,
            model_name=model.model_name,
            api_url_auto_discovered=model.api_url_auto_discovered,
            api_url=model.api_url,
            auth_type=model.auth_type,
            default_temperature=model.default_temperature,
            default_max_tokens=model.default_max_tokens,
            system_prompt=model.system_prompt,
            top_p=model.top_p,
            n=model.n,
            stop_sequences=model.stop_sequences,
            seed=model.seed,
            presence_penalty=model.presence_penalty,
            frequency_penalty=model.frequency_penalty,
            logit_bias=model.logit_bias,
            response_format=model.response_format,
            logprobs=model.logprobs,
            top_logprobs=model.top_logprobs,
            stream_options=model.stream_options,
            supports_streaming=model.supports_streaming,
            supports_function_calling=model.supports_function_calling,
            supports_vision=model.supports_vision,
            supports_batch=model.supports_batch,
            tools=model.tools,
            tool_choice=model.tool_choice,
            parallel_tool_calls=model.parallel_tool_calls,
            user_identifier=model.user_identifier,
            api_version=model.api_version,
            extra_headers=model.extra_headers,
            model_metadata=model.model_metadata,
            drop_params=model.drop_params,
            additional_drop_params=model.additional_drop_params,
            routing_group_id=model.routing_group_id,
            routing_weight=model.routing_weight,
            rate_limit_rpm=model.rate_limit_rpm,
            rate_limit_tpm=model.rate_limit_tpm,
            cost_per_input_token=model.cost_per_input_token,
            cost_per_output_token=model.cost_per_output_token,
            timeout=model.timeout,
            max_concurrent_requests=model.max_concurrent_requests,
            allowed_fails=model.allowed_fails,
            cooldown_time=model.cooldown_time,
            enabled=model.enabled,
            capabilities=model.capabilities,
            groups=groups if groups else None,
            health_status=health_status,
            performance_metrics=performance_metrics,
            tenant_id=model.tenant_id,
            created_at=model.created_at.isoformat(),
            updated_at=model.updated_at.isoformat()
        )