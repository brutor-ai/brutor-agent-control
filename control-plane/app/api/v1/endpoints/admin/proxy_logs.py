# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Unified Proxy Logs Admin Endpoints
Query and analyze logs across all proxy types with multi-tenant support
Complete and updated version with aligned response structures
"""

from typing import Any, Dict, List, Literal, Optional, Union
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.constants.proxy import PROXY_TYPES, ProxyTypeName
from app.core.sync_bridge import block_on
from app.constants.admin import AdminType
from app.database import get_db
from app.database.models.admin import TenantAdmin
from app.core.admin_user.admin_principal import AdminPrincipal
from app.core.admin_user.permissions import Permission
from app.core.admin_user.dependencies import (
    require_permission,
    get_audit_logger,
    acting_tenant_id,
    get_current_admin,
    resolve_tenant_id,
)
from app.api.v1.models.admin.proxy_log import (
    LogQueryResponse, LogQueryRequest, ThreadQueryResponse,
    UserActivityResponse, StatisticsResponse, CleanupResponse, CleanupRequest,
    ProxyLogPaginationInfo, AdminAccessInfo,
    FacetsRequest, FacetsResponse, FacetValue, SuggestRequest, SuggestResponse,
)
from app.core.admin_user.services.audit_logger import UnifiedAdminAuditLogger
from app.core.logging_system import BrutorLogger
from app.services.proxy_log_service import UnifiedProxyLogService
from app.services.admin_export import (
    PROXY_LOG_CSV_HEADERS,
    assert_within_cap,
    build_meta,
    export_filename,
    flatten_proxy_log,
    media_type_for,
    stream_csv,
    stream_jsonl,
)
from app.middleware.trusted_proxies import client_ip


def _can_read_bodies(admin) -> bool:
    """Bodies are a separate grant from rows.

    A caller with only `proxy:logs:read` gets every record-layer field and a
    `body_state` of `withheld`; the body text itself, and free-text search
    over it, need `proxy:logs:read_bodies`.
    """
    return bool(admin.has_permission(Permission.PROXY_LOGS_READ_BODIES.value))


def log_query_arguments(body: LogQueryRequest) -> Dict[str, Any]:
    """What `UnifiedProxyLogService.query_logs` is asked for a request body:
    the time range, every filter and the order. The list and the export both
    call the service with exactly this, so a filter the list has the export
    has too; the caller adds the tenant, the page and whether bodies are
    included."""
    if body.hours_ago:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=body.hours_ago)
    else:
        start_time = body.start_time
        end_time = body.end_time
    return dict(
        start_time=start_time,
        end_time=end_time,
        proxy_types=body.proxy_types,
        proxy_subtype=body.proxy_subtype,
        gateway_user_id=body.gateway_user_id,
        idp_user_id=body.idp_user_id,
        idp_user_username=body.idp_user_username,
        idp_user_email=body.idp_user_email,
        session_id=body.session_id,
        server_id=body.server_id,
        idp_server_id=body.idp_server_id,
        correlation_id=body.correlation_id,
        parent_request_id=body.parent_request_id,
        request_id=body.request_id,
        success=body.success,
        client_ip=body.client_ip,
        min_duration_ms=body.min_duration_ms,
        max_duration_ms=body.max_duration_ms,
        mcp_method=body.mcp_method,
        mcp_capability_name=body.mcp_capability_name,
        llm_provider=body.llm_provider,
        llm_model_requested=body.llm_model_requested,
        llm_model_used=body.llm_model_used,
        min_tokens=body.min_tokens,
        max_cost_usd=body.max_cost_usd,
        llm_streaming=body.llm_streaming,
        llm_function_calling=body.llm_function_calling,
        llm_used_fallback=body.llm_used_fallback,
        oauth_flow_type=body.oauth_flow_type,
        # One exact compliance tag (e.g. "eu_ai_act.high").
        compliance_tag=body.compliance_tag,
        # The list-valued filters: OR within a column, AND across columns
        # (`LogQueryRequest.coalesce_filters`); `compliance_tags` is matched
        # by overlap.
        multi_value_filters=body.coalesce_filters(),
        # Substring search over the body previews, the target URL and the
        # error message.
        q=body.q,
        sort_by=body.sort_by,
        sort_order=body.sort_order,
    )


def create_unified_proxy_logs_admin_router(
        logger: BrutorLogger,
        log_service: UnifiedProxyLogService
) -> APIRouter:
    """
    Create admin router for unified proxy logs

    Args:
        logger: Brutor logging system
        log_service: Unified proxy log service

    Returns:
        FastAPI APIRouter with multi-tenant support and audit logging
    """

    router = APIRouter(prefix="/admin/proxy-logs", tags=["Proxy Logs"])

    # Helper function to build AdminAccessInfo from the admin object
    def build_admin_access_info(request, admin_obj) -> AdminAccessInfo:
        """
        Build AdminAccessInfo from the admin object for audit logging

        Note: extract_user_context returns different field names, so we
        build AdminAccessInfo directly from the admin object instead.
        """
        return AdminAccessInfo(
            admin_id=admin_obj.id,
            admin_username=admin_obj.username if hasattr(admin_obj, 'username') else 'unknown',
            tenant_id=acting_tenant_id(request, admin_obj)
        )

    @router.post(
        "/query",
        response_model=LogQueryResponse,
        summary="Query unified proxy logs",
        description="""
        Query logs across all proxy types (MCP, LLM, OAuth, Virtual MCP).
        
        Tenant isolation:
        - Tenant admins: Only see their tenant's logs
        - System admins: Can filter by tenant_id or see all
        
        Features:
        - Time range filtering
        - Proxy type filtering
        - User filtering
        - Correlation ID tracking (request threading)
        - Success/error filtering
        - Pagination and sorting
        """
    )
    def query_logs(
            request_body: LogQueryRequest,
            request: Request,
            tenant_id: Optional[str] = Query(None, description="Filter by tenant (system admins only)"),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.PROXY_LOGS_READ))
    ):
        """Query unified proxy logs with tenant isolation"""
        admin_obj, admin_type = admin_and_type

        # Eager load admin attributes
        effective_tenant_id = None
        admin_id = admin_obj.id
        admin_tenant_id = acting_tenant_id(request, admin_obj)
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Determine effective tenant_id based on admin type
            # The principal decides which tenant.
            effective_tenant_id = resolve_tenant_id(admin_obj, admin_type, tenant_id)

            arguments = log_query_arguments(request_body)
            start_time = arguments["start_time"]
            end_time = arguments["end_time"]

            logger.debug("Querying proxy logs", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": effective_tenant_id,
                "proxy_types": request_body.proxy_types,
                "correlation_id": request_body.correlation_id,
                "limit": request_body.limit
            })

            result = block_on(log_service.query_logs(
                include_bodies=_can_read_bodies(admin_obj),
                db=db,
                tenant_id=effective_tenant_id,
                limit=request_body.limit,
                offset=request_body.offset,
                **arguments,
            ))

            logger.debug("Proxy logs queried successfully", context={
                "admin_id": admin_id,
                "tenant_id": effective_tenant_id,
                "results": result['total']
            })

            # Build filters_applied dictionary
            filters_applied = {}
            if start_time:
                filters_applied['start_time'] = start_time.isoformat()
            if end_time:
                filters_applied['end_time'] = end_time.isoformat()
            if request_body.hours_ago:
                filters_applied['hours_ago'] = request_body.hours_ago
            if request_body.proxy_types:
                filters_applied['proxy_types'] = request_body.proxy_types
            if request_body.proxy_subtype:
                filters_applied['proxy_subtype'] = request_body.proxy_subtype
            if request_body.server_id:
                filters_applied['server_id'] = request_body.server_id
            if request_body.idp_server_id:
                filters_applied['idp_server_id'] = request_body.idp_server_id
            if request_body.idp_user_id:
                filters_applied['idp_user_id'] = request_body.idp_user_id
            if request_body.idp_user_username:
                filters_applied['idp_user_username'] = request_body.idp_user_username
            if request_body.session_id:
                filters_applied['session_id'] = request_body.session_id
            if request_body.correlation_id:
                filters_applied['correlation_id'] = request_body.correlation_id
            if request_body.parent_request_id:
                filters_applied['parent_request_id'] = request_body.parent_request_id
            if request_body.request_id:
                filters_applied['request_id'] = request_body.request_id
            if request_body.success is not None:
                filters_applied['success'] = request_body.success
            if request_body.client_ip:
                filters_applied['client_ip'] = request_body.client_ip
            # MCP filters
            if request_body.mcp_method:
                filters_applied['mcp_method'] = request_body.mcp_method
            if request_body.mcp_capability_name:
                filters_applied['mcp_capability_name'] = request_body.mcp_capability_name
            # LLM filters
            if request_body.llm_provider:
                filters_applied['llm_provider'] = request_body.llm_provider
            if request_body.llm_model_requested:
                filters_applied['llm_model_requested'] = request_body.llm_model_requested
            if request_body.llm_model_used:
                filters_applied['llm_model_used'] = request_body.llm_model_used
            if request_body.min_tokens is not None:
                filters_applied['min_tokens'] = request_body.min_tokens
            if request_body.max_cost_usd is not None:
                filters_applied['max_cost_usd'] = request_body.max_cost_usd
            if request_body.llm_streaming is not None:
                filters_applied['llm_streaming'] = request_body.llm_streaming
            if request_body.llm_function_calling is not None:
                filters_applied['llm_function_calling'] = request_body.llm_function_calling
            if request_body.llm_used_fallback is not None:
                filters_applied['llm_used_fallback'] = request_body.llm_used_fallback
            # OAuth filters
            if request_body.oauth_flow_type:
                filters_applied['oauth_flow_type'] = request_body.oauth_flow_type

            # Build response with nested pagination
            return LogQueryResponse(
                logs=result['logs'],
                pagination=ProxyLogPaginationInfo(
                    total=result['total'],
                    offset=result['offset'],
                    limit=result['limit'],
                    returned=len(result['logs']),
                    has_more=result['has_more']
                ),
                filters_applied=filters_applied,
                accessed_by=build_admin_access_info(request, admin_obj),
                timestamp=datetime.now(timezone.utc).isoformat()
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to query logs", context={
                "admin_id": admin_id,
                "tenant_id": effective_tenant_id if 'effective_tenant_id' in locals() else None,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to query logs: {str(e)}"
            )

    @router.get(
        "/thread/{correlation_id}",
        response_model=ThreadQueryResponse,
        summary="Get request thread",
        description="""
        Get all requests in a correlated thread.
        
        Track complete request flows using correlation ID:
        - OAuth discovery → token exchange → MCP call
        - LLM request → multiple MCP tool calls
        - Any multi-step flow across proxies
        
        Tenant isolation enforced automatically.
        """
    )
    def get_request_thread(
            correlation_id: str,
            request: Request,
            tenant_id: Optional[str] = Query(None, description="Filter by tenant (system admins only)"),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.PROXY_LOGS_READ))
    ):
        """Get all requests in a correlated thread"""
        admin_obj, admin_type = admin_and_type

        # Eager load admin attributes
        admin_id = admin_obj.id
        admin_tenant_id = acting_tenant_id(request, admin_obj)
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Determine effective tenant_id
            # The principal decides which tenant.
            effective_tenant_id = resolve_tenant_id(admin_obj, admin_type, tenant_id)

            logger.debug("Fetching request thread", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": effective_tenant_id,
                "correlation_id": correlation_id
            })

            # Get thread
            logs = block_on(log_service.get_thread(
                include_bodies=_can_read_bodies(admin_obj),
                db=db,
                tenant_id=effective_tenant_id,
                correlation_id=correlation_id
            ))

            logger.debug("Request thread retrieved", context={
                "admin_id": admin_id,
                "tenant_id": effective_tenant_id,
                "correlation_id": correlation_id,
                "thread_length": len(logs)
            })

            return ThreadQueryResponse(
                correlation_id=correlation_id,
                thread_length=len(logs),
                logs=logs,
                accessed_by=build_admin_access_info(request, admin_obj),
                timestamp=datetime.now(timezone.utc).isoformat()
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get thread", context={
                "admin_id": admin_id,
                "correlation_id": correlation_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get thread: {str(e)}"
            )

    @router.get(
        "/user/{user_id}/activity",
        response_model=UserActivityResponse,
        summary="Get user activity",
        description="""
        Get all activity for a user across all proxies.
        
        Shows chronological view of user's requests across:
        - MCP servers
        - LLM servers
        - OAuth flows
        - Virtual MCP servers
        
        Tenant isolation enforced automatically.
        """
    )
    def get_user_activity(
            user_id: str,
            request: Request,
            hours_ago: int = Query(default=24, ge=1, le=720, description="Activity from last N hours"),
            proxy_types: Optional[str] = Query(None, description="Comma-separated proxy types filter"),
            tenant_id: Optional[str] = Query(None, description="Filter by tenant (system admins only)"),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.PROXY_LOGS_READ))
    ):
        """Get all activity for a user across all proxies"""
        admin_obj, admin_type = admin_and_type

        # Eager load admin attributes
        admin_id = admin_obj.id
        admin_tenant_id = acting_tenant_id(request, admin_obj)
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Determine effective tenant_id
            # The principal decides which tenant.
            effective_tenant_id = resolve_tenant_id(admin_obj, admin_type, tenant_id)

            # Parse proxy types filter
            proxy_types_list = None
            if proxy_types:
                proxy_types_list = [pt.strip() for pt in proxy_types.split(',')]
                # The types the gateway writes, and nothing else.
                unknown = sorted(set(proxy_types_list) - set(PROXY_TYPES))
                if unknown:
                    raise HTTPException(
                        status_code=422,
                        detail=f"proxy_types: not a proxy type: {', '.join(unknown)}. "
                               f"The proxy types are {', '.join(PROXY_TYPES)}.",
                    )

            # Calculate time range
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=hours_ago)

            logger.debug("Fetching user activity", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": effective_tenant_id,
                "user_id": user_id,
                "hours_ago": hours_ago
            })

            # Get activity. The service returns an envelope
            # {'logs': [...], 'total_requests': ..., ...}, not a bare list.
            activity = block_on(log_service.get_user_activity(
                include_bodies=_can_read_bodies(admin_obj),
                db=db,
                tenant_id=effective_tenant_id,
                user_id=user_id,
                start_time=start_time,
                end_time=end_time,
                proxy_types=proxy_types_list
            ))
            logs = (
                activity.get("logs", [])
                if isinstance(activity, dict)
                else activity
            )

            logger.debug("User activity retrieved", context={
                "admin_id": admin_id,
                "tenant_id": effective_tenant_id,
                "user_id": user_id,
                "total_requests": len(logs)
            })

            # Extract username from first log if available
            username = None
            if logs and len(logs) > 0:
                # Try multiple possible field names for username
                first_log = logs[0]
                username = (
                    first_log.get('username') or
                    first_log.get('end_user_username') or
                    first_log.get('user', {}).get('username')
                )

            # Calculate success/failure counts
            successful_requests = sum(1 for log in logs if log.get('success', False))
            failed_requests = len(logs) - successful_requests

            return UserActivityResponse(
                gateway_user_id=user_id,
                idp_user_id=user_id,
                idp_user_username=username,
                period_start=start_time.isoformat(),
                period_end=end_time.isoformat(),
                total_requests=len(logs),
                successful_requests=successful_requests,
                failed_requests=failed_requests,
                logs=logs,
                accessed_by=build_admin_access_info(request, admin_obj),
                timestamp=datetime.now(timezone.utc).isoformat()
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get user activity", context={
                "admin_id": admin_id,
                "user_id": user_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get user activity: {str(e)}"
            )

    @router.get(
        "/statistics",
        response_model=StatisticsResponse,
        summary="Get proxy statistics",
        description="""
        Get aggregated statistics across all proxy types.
        
        Includes:
        - Total requests by proxy type
        - Success rates by proxy type  
        - Error breakdown
        - LLM token usage and costs (if applicable)
        - MCP tool usage (if applicable)
        
        Tenant isolation enforced automatically.
        """
    )
    def get_statistics(
            request: Request,
            hours_ago: int = Query(default=24, ge=1, le=720, description="Statistics from last N hours"),
            proxy_types: Optional[str] = Query(None, description="Comma-separated proxy types filter"),
            tenant_id: Optional[str] = Query(None, description="Filter by tenant (system admins only)"),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.PROXY_STATS_READ))
    ):
        """Get aggregated proxy statistics"""
        admin_obj, admin_type = admin_and_type

        # Eager load admin attributes
        admin_id = admin_obj.id
        admin_tenant_id = acting_tenant_id(request, admin_obj)
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Determine effective tenant_id
            # The principal decides which tenant.
            effective_tenant_id = resolve_tenant_id(admin_obj, admin_type, tenant_id)

            # Parse proxy types filter
            proxy_types_list = None
            if proxy_types:
                proxy_types_list = [pt.strip() for pt in proxy_types.split(',')]
                # The types the gateway writes, and nothing else.
                unknown = sorted(set(proxy_types_list) - set(PROXY_TYPES))
                if unknown:
                    raise HTTPException(
                        status_code=422,
                        detail=f"proxy_types: not a proxy type: {', '.join(unknown)}. "
                               f"The proxy types are {', '.join(PROXY_TYPES)}.",
                    )

            # Calculate time range
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=hours_ago)

            logger.debug("Fetching proxy statistics", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": effective_tenant_id,
                "hours_ago": hours_ago
            })

            # Get statistics
            stats = block_on(log_service.get_statistics(
                db=db,
                tenant_id=effective_tenant_id,
                start_time=start_time,
                end_time=end_time,
                proxy_types=proxy_types_list
            ))

            logger.debug("Proxy statistics retrieved", context={
                "admin_id": admin_id,
                "tenant_id": effective_tenant_id,
                "total_requests": stats.get('total_requests', 0)
            })

            # Ensure stats includes timestamp if service doesn't provide it
            if 'timestamp' not in stats:
                stats['timestamp'] = datetime.now(timezone.utc).isoformat()

            # Ensure accessed_by is included
            return StatisticsResponse(
                **stats,
                accessed_by=build_admin_access_info(request, admin_obj)
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get statistics", context={
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to get statistics: {str(e)}"
            )

    @router.post(
        "/export",
        summary="Export proxy logs (CSV or JSONL, streamed)",
        description="""
        Stream every proxy log row that matches the filters as CSV or JSONL,
        with a self-describing metadata header. The body is the body of
        ``/query`` and every filter means what it means there, so the file
        holds the rows the list shows for the same filters. ``limit`` and
        ``offset`` page the list and are not used: the export is the whole
        matching set, in the order ``sort_by`` and ``sort_order`` give.

        Same tenant isolation rules as ``/query``:

        - Tenant admins are pinned to their tenant.
        - System admins must pass ``tenant_id``.

        Output format:
        - ``format=csv`` → ``text/csv``; metadata is emitted as ``# key: value``
          comment lines, followed by the column header and rows.
        - ``format=jsonl`` → ``application/x-ndjson``; first line is a
          ``{"_meta": {...}}`` envelope, then one JSON record per line.

        At most 50,000 rows: a larger matching set is refused with HTTP 413;
        narrow the filters.

        Body text, and the free-text filter ``q`` over it, need
        ``proxy:logs:read_bodies`` as they do in ``/query``.

        Changes nothing but the admin audit trail, where each export is
        recorded with its filters (``action = "export_proxy_logs"``).
        """,
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "The file, as an attachment.",
                "content": {"text/csv": {}, "application/x-ndjson": {}},
            },
            413: {"description": "More than 50,000 rows match."},
        },
    )
    def export_logs(
            request_body: LogQueryRequest,
            request: Request,
            tenant_id: Optional[str] = Query(None, description="Filter by tenant (system admins only)"),
            format: Literal["csv", "jsonl"] = Query("csv", description="Output format"),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            audit_logger: UnifiedAdminAuditLogger = Depends(get_audit_logger),
            admin: AdminPrincipal = Depends(require_permission(Permission.PROXY_LOGS_READ))
    ):
        """Stream proxy logs as CSV or JSONL with metadata header and audit trail."""
        admin_obj, admin_type = admin_and_type
        admin_id = admin_obj.id
        admin_username = getattr(admin_obj, 'username', 'unknown')

        effective_tenant_id = resolve_tenant_id(admin_obj, admin_type, tenant_id)

        arguments = log_query_arguments(request_body)
        resolved_start = arguments["start_time"]
        resolved_end = arguments["end_time"]

        def matching(limit: int) -> dict:
            return block_on(log_service.query_logs(
                include_bodies=_can_read_bodies(admin_obj),
                db=db,
                tenant_id=effective_tenant_id,
                limit=limit,
                offset=0,
                **arguments,
            ))

        # Count first, so that an oversize export is refused cheaply.
        total = matching(1).get('total', 0)
        assert_within_cap(total, resource="proxy log rows")

        logs = matching(total).get('logs', []) if total else []
        row_count = len(logs)

        # What was asked, as it was asked: the filters of the body that were
        # set, the resolved time range and the order.
        filters = {
            "tenant_id": effective_tenant_id,
            **request_body.model_dump(exclude_unset=True, exclude={"limit", "offset"}),
            "start_time": resolved_start,
            "end_time": resolved_end,
            "sort_by": request_body.sort_by,
            "sort_order": request_body.sort_order,
        }

        meta = build_meta(
            resource="proxy_logs",
            format_type=format,
            exported_by=f"{admin_username} ({admin_id})",
            filters=filters,
            row_count=row_count,
        )

        # Audit the export *before* streaming so the entry is persisted even
        # if the client aborts mid-download.
        try:
            audit_logger.log(
                action="export_proxy_logs",
                db=db,
                admin=admin_obj,
                success=True,
                resource_type="proxy_logs",
                tenant_id=effective_tenant_id,
                affected_tenant_id=effective_tenant_id,
                details={
                    "format": format,
                    "row_count": row_count,
                    "filters": meta["filters"],
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get("user-agent"),
                request_method=request.method,
                request_path=str(request.url.path),
            )
        except Exception as e:
            logger.warning("Failed to record proxy log export in audit trail", context={
                "admin_id": admin_id,
                "error": str(e),
            })

        filename = export_filename(
            "proxy-logs", format,
            start_time=resolved_start, end_time=resolved_end,
        )
        headers = {"Content-Disposition": f"attachment; filename={filename}"}

        if format == "jsonl":
            generator = stream_jsonl(records=logs, meta=meta)
        else:
            generator = stream_csv(
                records=logs,
                headers=PROXY_LOG_CSV_HEADERS,
                meta=meta,
                row_extractor=lambda log: flatten_proxy_log(log, tenant_id=effective_tenant_id),
            )

        return StreamingResponse(
            generator,
            media_type=media_type_for(format),
            headers=headers,
        )

    @router.post(
        "/cleanup",
        response_model=CleanupResponse,
        summary="Cleanup old logs",
        description="""
        Delete logs older than specified days.
        
        Tenant isolation:
        - Tenant admins: Delete only their tenant's logs
        - System admins: Must specify tenant_id
        
        The deleted rows are declared in the audit chain's prune record
        (reason `manual`), so the chain verifies with a declared prune and
        not a gap. The operation is also written to the admin audit log.
        """
    )
    def cleanup_old_logs(
            cleanup_request: CleanupRequest,
            request: Request,
            tenant_id: Optional[str] = Query(None, description="Tenant to cleanup (system admins only)"),
            db: Session = Depends(get_db),
            admin_and_type: tuple = Depends(get_current_admin),
            admin: AdminPrincipal = Depends(require_permission(Permission.PROXY_MANAGEMENT)),
            audit: UnifiedAdminAuditLogger = Depends(get_audit_logger)
    ):
        """Delete old logs"""
        admin_obj, admin_type = admin_and_type

        # Eager load admin attributes
        admin_id = admin_obj.id
        admin_tenant_id = acting_tenant_id(request, admin_obj)
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Determine effective tenant_id
            # The principal decides which tenant.
            effective_tenant_id = resolve_tenant_id(admin_obj, admin_type, tenant_id)

            logger.debug("Cleaning up old logs", context={
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "tenant_id": effective_tenant_id,
                "days_to_keep": cleanup_request.days_to_keep
            })

            # Cleanup logs
            deleted_count = block_on(log_service.cleanup_old_logs(
                db=db,
                tenant_id=effective_tenant_id,
                days_to_keep=cleanup_request.days_to_keep,
                actor=str(admin_id),
            ))

            logger.info("Logs cleaned up successfully", context={
                "admin_id": admin_id,
                "tenant_id": effective_tenant_id,
                "deleted_count": deleted_count
            })

            # Audit cleanup operation
            audit.log(
                action="proxy_logs_cleanup",
                admin=admin_obj,
                admin_type=admin_type,
                tenant_id=effective_tenant_id,
                success=True,
                resource_type="proxy_logs",
                resource_id=None,
                details={
                    "deleted_count": deleted_count,
                    "days_to_keep": cleanup_request.days_to_keep
                },
                ip_address=client_ip(request),
                user_agent=request.headers.get('user-agent'),
                request_method=request.method,
                request_path=str(request.url.path),
                db=db
            )

            return CleanupResponse(
                message=f"Successfully deleted {deleted_count} logs older than {cleanup_request.days_to_keep} days",
                deleted_count=deleted_count,
                days_to_keep=cleanup_request.days_to_keep,
                tenant_id=effective_tenant_id,
                deleted_by=build_admin_access_info(request, admin_obj),
                timestamp=datetime.now(timezone.utc).isoformat()
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to cleanup logs", context={
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to cleanup logs: {str(e)}"
            )

    # =========================================================================
    # Multi-step agent trace API
    # =========================================================================
    #
    # Two endpoints keyed by the `root_task_id` column on proxy_logs:
    #
    #   GET  /traces              — paginated list of recent traces
    #   GET  /traces/{root_id}    — every span of a single trace
    #
    # Tenant-scoped: tenant admins are pinned to their tenant; system
    # admins must pass ?tenant_id=. The service layer
    # (`app/services/trace_service.py`) does the joins so this router
    # is a thin adapter.

    from app.services.trace_service import TraceService, KNOWN_ROOT_KINDS
    _trace_service = TraceService(logger=logger)

    @router.get(
        "/traces",
        summary="List recent traces",
        description=(
            "Paginated list of distinct `root_task_id` values matching "
            "filters. Each entry carries the span count, root kind, "
            "originating user, and overall outcome for the trace-list UI."
        ),
    )
    def list_traces(
        request: Request,
        tenant_id: Optional[str] = Query(None),
        since: Optional[datetime] = Query(None),
        until: Optional[datetime] = Query(None),
        root_kind: Optional[str] = Query(None),
        user_id: Optional[str] = Query(None),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.PROXY_LOGS_READ)
        ),
    ):
        admin_obj, admin_type = admin_and_type
        # Tenant-scoping copy of the pattern used by query_logs above.
        # The principal decides which tenant.
        effective_tenant_id = resolve_tenant_id(admin_obj, admin_type, tenant_id)

        if root_kind is not None and root_kind not in KNOWN_ROOT_KINDS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Unknown root_kind {root_kind!r}; expected one of "
                    f"{sorted(KNOWN_ROOT_KINDS)}"
                ),
            )

        traces, total = _trace_service.list_traces(
            db,
            tenant_id=effective_tenant_id,
            limit=limit,
            offset=offset,
            since=since,
            until=until,
            root_kind=root_kind,
            user_id=user_id,
        )
        return {
            "tenant_id": effective_tenant_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "traces": traces,
        }

    @router.get(
        "/traces/{root_task_id}",
        summary="Get trace by root_task_id",
        description=(
            "Return every span of a trace, ordered by delegation_depth + "
            "timestamp so parent spans render before children. 404 if no "
            "spans match the (tenant_id, root_task_id) pair."
        ),
    )
    def get_trace(
        root_task_id: str,
        request: Request,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        admin: AdminPrincipal = Depends(
            require_permission(Permission.PROXY_LOGS_READ)
        ),
    ):
        admin_obj, admin_type = admin_and_type
        # The principal decides which tenant.
        effective_tenant_id = resolve_tenant_id(admin_obj, admin_type, tenant_id)

        trace = _trace_service.get_trace(
            db,
            tenant_id=effective_tenant_id,
            root_task_id=root_task_id,
        )
        if trace is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No trace found for root_task_id={root_task_id!r}",
            )
        return trace

    # =====================================================================
    # FACETS + TYPEAHEAD — Datadog-style filter discovery for the
    # admin UI's faceted filter sidebar + FacetMultiSelect dropdowns.
    # Both endpoints take the same filter envelope as /query so the
    # value-counts the operator sees reflect exactly the table view
    # they're about to load.
    # =====================================================================

    def _resolve_tenant_for_log_read(
        admin_obj,
        admin_type,
        admin_tenant_id: Optional[str],
        tenant_id_query: Optional[str],
    ) -> str:
        """Tenant-scope resolver shared by /facets, /suggest, and the
        existing /query handler. Tenant admins are pinned to their
        tenant; system admins must pass `tenant_id` explicitly.

        Pulled into a tiny helper so the new endpoints don't drift
        from the long-form auth block above — that would let a system
        admin facet across tenants they aren't supposed to see, which
        is a subtle but real RBAC bug."""
        # The principal decides which tenant.
        return resolve_tenant_id(admin_obj, admin_type, tenant_id_query)

    @router.post(
        "/facets",
        response_model=FacetsResponse,
        summary="Top-N values + counts per facetable column",
        description=(
            "Returns the most-frequent values for every facetable "
            "column scoped to the same filter envelope as /query. "
            "Used by the admin UI's filter sidebar (Datadog-style) "
            "and the FacetMultiSelect dropdown on every filter input. "
            "Counts always reflect the active scope so what the "
            "operator sees in the dropdown matches the table page."
        ),
    )
    def get_facets(
        request_body: FacetsRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _: AdminPrincipal = Depends(require_permission(Permission.PROXY_LOGS_READ)),
    ):
        admin_obj, admin_type = admin_and_type
        effective_tenant_id = _resolve_tenant_for_log_read(
            admin_obj, admin_type,
            getattr(admin_obj, 'tenant_id', None), tenant_id,
        )
        # Compute the effective time window in the same way /query
        # does so the operator can't accidentally widen scope by
        # hitting /facets with the same body that hit /query.
        start_time = request_body.start_time
        end_time = request_body.end_time
        if request_body.hours_ago and not (start_time or end_time):
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=request_body.hours_ago)

        scope_filters: Dict[str, Any] = {
            "start_time": start_time,
            "end_time": end_time,
            "proxy_types": request_body.proxy_types,
            "success": request_body.success,
            "multi_value_filters": request_body.coalesce_filters(),
            "q": request_body.q,
        }
        try:
            result = block_on(log_service.facets(
                include_bodies=_can_read_bodies(admin_obj),
                db=db,
                tenant_id=effective_tenant_id,
                scope_filters=scope_filters,
                fields=request_body.fields,
                per_field_limit=request_body.per_field_limit,
            ))
            # Resolve human-readable display names for the ID-shaped
            # facets in the same round trip — keeps `value` as the
            # filter key and gives the UI a per-field {id: name} map
            # to render in the sidebar / dropdown / chips.
            display_names = log_service.resolve_display_names(
                db=db,
                tenant_id=effective_tenant_id,
                facets_result=result,
            )
            return FacetsResponse(
                facets={
                    field: [FacetValue(value=row["value"], count=row["count"]) for row in rows]
                    for field, rows in result.items()
                },
                display_names=display_names,
            )
        except Exception as e:
            logger.error("Failed to compute facets", context={
                "tenant_id": effective_tenant_id, "error": str(e),
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to compute facets: {str(e)}",
            )

    @router.post(
        "/suggest",
        response_model=SuggestResponse,
        summary="Typeahead for a single facetable column",
        description=(
            "Returns prefix-matching values for the named column, "
            "scoped to the same filter envelope as /query. Used by "
            "the FacetMultiSelect dropdown when the operator types "
            "to narrow the value list."
        ),
    )
    def get_suggestions(
        request_body: SuggestRequest,
        tenant_id: Optional[str] = Query(None),
        db: Session = Depends(get_db),
        admin_and_type: tuple = Depends(get_current_admin),
        _: AdminPrincipal = Depends(require_permission(Permission.PROXY_LOGS_READ)),
    ):
        admin_obj, admin_type = admin_and_type
        effective_tenant_id = _resolve_tenant_for_log_read(
            admin_obj, admin_type,
            getattr(admin_obj, 'tenant_id', None), tenant_id,
        )
        start_time = request_body.start_time
        end_time = request_body.end_time
        if request_body.hours_ago and not (start_time or end_time):
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=request_body.hours_ago)

        scope_filters: Dict[str, Any] = {
            "start_time": start_time,
            "end_time": end_time,
            "proxy_types": request_body.proxy_types,
            "success": request_body.success,
            "multi_value_filters": request_body.coalesce_filters(),
            "q": request_body.q,
        }
        try:
            rows = block_on(log_service.suggest(
                include_bodies=_can_read_bodies(admin_obj),
                db=db,
                tenant_id=effective_tenant_id,
                field=request_body.field,
                prefix=request_body.prefix,
                scope_filters=scope_filters,
                limit=request_body.suggest_limit,
            ))
            return SuggestResponse(
                suggestions=[FacetValue(value=r["value"], count=r["count"]) for r in rows]
            )
        except ValueError as e:
            # Service raised for an unknown field name. 422 lines up
            # with how the rest of the proxy_logs router treats
            # operator-supplied-but-invalid params.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(e),
            )
        except Exception as e:
            logger.error("Failed to compute suggestions", context={
                "tenant_id": effective_tenant_id,
                "field": request_body.field,
                "error": str(e),
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to compute suggestions: {str(e)}",
            )

    @router.get(
        "/health",
        summary="Get logging system health",
        description="Check if the unified logging system is operational (public endpoint)"
    )
    def get_health(db: Session = Depends(get_db)):
        """Get logging system health status"""
        try:
            health = block_on(log_service.get_system_health(db=db))

            # Ensure timestamp is included
            if isinstance(health, dict) and 'timestamp' not in health:
                health['timestamp'] = datetime.now(timezone.utc).isoformat()

            return health
        except Exception as e:
            logger.error("Health check failed", context={
                "error": str(e)
            }, exc_info=True)
            return {
                "status": "unhealthy",
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

    return router