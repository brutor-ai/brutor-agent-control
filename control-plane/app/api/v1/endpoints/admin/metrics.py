# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Proxy Metrics Administration Endpoints with multi-tenant support for production monitoring and analytics
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

from fastapi.responses import StreamingResponse
from fastapi import APIRouter, HTTPException, Depends, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session
from sqlalchemy import func, text, Integer

from app.core.sync_bridge import block_on
from app.constants.tenant import TenantResourceTypes
from app.database import get_db
from app.database.models.proxy_metric import ProxyMetricEntry
from app.core.admin_user.dependencies import (
    require_permission,
    require_tenant_resource_access
)
from app.core.admin_user.permissions import Permission
from app.core.admin_user.utils import extract_user_context
from app.core.logging_system import BrutorLogger
from app.services.proxy_metrics_service import (
    UnifiedProxyMetricsService,
    source_for_plane,
)
from app.services.agent_metrics_service import AgentMetricsService
from app.services.cost_metrics_service import CostMetricsService
from app.services.governance_metrics_service import GovernanceMetricsService


def _metrics_start_time(db, tenant_id, end_time, period_hours):
    """Resolve the lower time bound for a metrics window.

    `period_hours <= 0` means "all time": floor at the tenant's earliest
    `proxy_metrics` row so `date_bin` bucketing still has a valid origin
    (mirrors how the resource-group usage endpoint implements `all`). Falls
    back to a 24h window when the tenant has no metrics yet.
    """
    if period_hours and period_hours > 0:
        return end_time - timedelta(hours=period_hours)
    row = db.execute(
        text("SELECT MIN(timestamp) AS min_ts FROM proxy_metrics WHERE tenant_id = :tid"),
        {"tid": tenant_id},
    ).fetchone()
    return row.min_ts if row and row.min_ts else end_time - timedelta(hours=24)


# Mission Control's "All AI Systems" selector sends the sentinel "all" (and can
# leak "undefined"/"" from the client). Those are NOT real Resource-Group ids, so
# treating them as a scope filter (`WHERE resource_group_id = 'all'`) matches
# nothing and silently zeroes the Cost / Governance / Agents tabs even though the
# data is there. Normalise any sentinel to None (= no scope).
_GROUP_ID_SENTINELS = {"", "all", "none", "null", "undefined", "*"}


def _clean_group_id(group_id: Optional[str]) -> Optional[str]:
    """Return a real Resource-Group id, or None for an 'all'/blank sentinel."""
    if group_id and group_id.strip().lower() not in _GROUP_ID_SENTINELS:
        return group_id
    return None


def create_proxy_metrics_admin_router(
    metrics_service: UnifiedProxyMetricsService,
    logger: Optional[BrutorLogger] = None
) -> APIRouter:
    """
    Create router for proxy metrics admin endpoints with multi-tenant support

    Args:
        metrics_service: Proxy metrics service instance
        logger: Optional logger instance

    Returns:
        APIRouter configured with metrics endpoints
    """
    if logger is None:
        logger = BrutorLogger("metrics-admin")

    cost_service = CostMetricsService(logger=logger)
    governance_service = GovernanceMetricsService(logger=logger)
    agent_service = AgentMetricsService(logger=logger)

    router = APIRouter(prefix="/admin/metrics", tags=["Proxy Metrics Administration"])

    @router.get(
        "/performance",
        summary="Get performance metrics",
        description="Get comprehensive performance metrics for the tenant"
    )
    def get_performance_metrics(
        request: Request,
        period_hours: int = Query(24, ge=0, le=2160, description="Period in hours; 0 = all time, max 2160 (90 days)"),
        start: Optional[datetime] = Query(None, description="Explicit window start (ISO). Overrides period_hours"),
        end: Optional[datetime] = Query(None, description="Explicit window end (ISO, exclusive)"),
        server_id: Optional[str] = Query(None, description="Filter by server ID"),
        user_type: Optional[str] = Query(None, description="Filter by user type"),
        plane: Optional[str] = Query(None, description="Provenance plane: governed | observed | all"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.METRICS)),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        """
        Get comprehensive performance metrics

        Query Parameters:
        - period_hours: Time period to analyze (1-168 hours)
        - server_id: Optional server ID filter
        - user_type: Optional user type filter

        Returns:
        - Detailed performance analysis including request volume, success rates,
          response time percentiles, session efficiency, and error analysis
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Get metrics with tenant filter
            metrics = block_on(metrics_service.get_performance_metrics(
                db=db,
                period_hours=period_hours,
                server_id=server_id,
                user_type=user_type,
                plane=plane,
                tenant_id=tenant_id,
                start=start,
                end=end,
            ))

            logger.debug("Performance metrics accessed", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "period_hours": period_hours,
                "server_id": server_id,
                "user_type": user_type
            })

            return {
                "performance_metrics": metrics,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tenant_id": tenant_id,
                "accessed_by": extract_user_context(admin),
                "query_parameters": {
                    "period_hours": period_hours,
                    "server_id": server_id,
                    "user_type": user_type
                }
            }

        except Exception as e:
            logger.error("Failed to retrieve performance metrics", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve performance metrics"
            )

    @router.get(
        "/real-time",
        summary="Get real-time metrics",
        description="Get real-time metrics for monitoring dashboards"
    )
    def get_real_time_metrics(
        request: Request,
        last_minutes: int = Query(5, ge=1, le=60, description="Minutes of recent data"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.METRICS)),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        """
        Get real-time metrics

        Query Parameters:
        - last_minutes: Minutes of recent data to analyze (1-60)

        Returns:
        - Live metrics including current throughput, active sessions,
          real-time success rates, and error spike detection
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Get real-time metrics with tenant filter
            metrics = block_on(metrics_service.get_real_time_metrics(
                db=db,
                last_minutes=last_minutes,
                tenant_id=tenant_id
            ))

            logger.debug("Real-time metrics accessed", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "last_minutes": last_minutes
            })

            return {
                "real_time_metrics": metrics,
                "refresh_recommended_seconds": 30,
                "tenant_id": tenant_id,
                "accessed_by": extract_user_context(admin)
            }

        except Exception as e:
            logger.error("Failed to retrieve real-time metrics", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve real-time metrics"
            )

    @router.get(
        "/servers/{server_id}/health",
        summary="Get server health metrics",
        description="Get detailed health metrics for a specific server"
    )
    def get_server_health_metrics(
        request: Request,
        server_id: str,
        hours: int = Query(24, ge=1, le=168, description="Hours of data for health analysis"),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.METRICS)
        ),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        """
        Get server health metrics

        Path Parameters:
        - server_id: Server ID

        Query Parameters:
        - hours: Hours of data to analyze (1-168)

        Returns:
        - Server-specific health analysis including health status,
          success rate trends, performance trends, and error patterns
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Verify server belongs to tenant
            from app.database.models.mcp_server import MCPServer
            server = db.query(MCPServer).filter(
                MCPServer.id == server_id,
                MCPServer.tenant_id == tenant_id
            ).first()

            if not server:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Server '{server_id}' not found"
                )

            # Get health metrics with tenant filter
            health_metrics = block_on(metrics_service.get_server_health_metrics(
                db=db,
                server_id=server_id,
                hours=hours,
                tenant_id=tenant_id
            ))

            logger.debug(f"Server health metrics accessed: {server_id}", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "hours": hours
            })

            return {
                "server_health": health_metrics,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tenant_id": tenant_id,
                "accessed_by": extract_user_context(admin)
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to retrieve server health metrics: {server_id}", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve server health metrics"
            )

    @router.get(
        "/dashboard",
        summary="Get dashboard metrics",
        description="Get comprehensive metrics for monitoring dashboard"
    )
    def get_dashboard_metrics(
        request: Request,
        period_hours: int = Query(24, ge=1, le=168),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.METRICS)
        ),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        """
        Get dashboard metrics

        Query Parameters:
        - period_hours: Time period to analyze (1-168 hours)

        Returns:
        - Comprehensive dashboard metrics including performance overview,
          server status, usage patterns, and alerts
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Get dashboard metrics with tenant filter
            metrics = block_on(metrics_service.get_dashboard_metrics(
                db=db,
                tenant_id=tenant_id
            ))

            logger.debug("Dashboard metrics accessed", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "period_hours": period_hours
            })

            return {
                "dashboard": metrics,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tenant_id": tenant_id,
                "accessed_by": extract_user_context(admin)
            }

        except Exception as e:
            logger.error("Failed to retrieve dashboard metrics", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve dashboard metrics"
            )

    @router.get(
        "/usage",
        summary="Get usage metrics",
        description="Get detailed usage metrics for billing and quota tracking"
    )
    def get_usage_metrics(
        request: Request,
        start_date: Optional[str] = Query(None, description="Start date (ISO format)"),
        end_date: Optional[str] = Query(None, description="End date (ISO format)"),
        group_by: str = Query("day", description="Grouping: hour, day, week, month"),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.METRICS)
        ),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        """
        Get usage metrics

        Query Parameters:
        - start_date: Start date (defaults to 30 days ago)
        - end_date: End date (defaults to now)
        - group_by: Grouping interval (hour, day, week, month)

        Returns:
        - Usage metrics with request counts, data transfer, and costs
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Parse dates or use defaults
            if start_date:
                start_time = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
            else:
                start_time = datetime.now(timezone.utc) - timedelta(days=30)

            if end_date:
                end_time = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
            else:
                end_time = datetime.now(timezone.utc)

            # Get usage metrics with tenant filter
            usage = block_on(metrics_service.get_usage_metrics(
                db=db,
                start_time=start_time,
                end_time=end_time,
                group_by=group_by,
                tenant_id=tenant_id
            ))

            logger.debug("Usage metrics accessed", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "admin_type": admin_type_value,
                "start_date": start_date,
                "end_date": end_date
            })

            return {
                "usage": usage,
                "period": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat(),
                    "group_by": group_by
                },
                "tenant_id": tenant_id,
                "accessed_by": extract_user_context(admin)
            }

        except Exception as e:
            logger.error("Failed to retrieve usage metrics", context={
                "tenant_id": tenant_id,
                "admin_id": admin_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve usage metrics"
            )

    @router.get(
        "/timeseries/requests",
        summary="Get request time-series",
        description="Get time-series data for request volume charting"
    )
    def get_requests_timeseries(
        request: Request,
        period_hours: int = Query(24, ge=0, le=2160),
        start: Optional[datetime] = Query(None, description="Explicit window start (ISO). Overrides period_hours"),
        end: Optional[datetime] = Query(None, description="Explicit window end (ISO, exclusive)"),
        interval_minutes: int = Query(60, ge=5, le=1440),
        server_id: Optional[str] = Query(None),
        plane: Optional[str] = Query(None, description="Provenance plane: governed | observed | all"),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.METRICS)
        ),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        """
        Get request time-series data

        Query Parameters:
        - period_hours: Time period (1-720 hours / 30 days)
        - interval_minutes: Data point interval (5-1440 minutes)
        - server_id: Optional server filter

        Returns:
        - Time-series data for request volume charting
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Explicit calendar range (MTD / last month / custom) wins
            # over the rolling preset — same precedence as resolve_window.
            if start is not None:
                start_time = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
                end_time = (end if end.tzinfo else end.replace(tzinfo=timezone.utc)) if end is not None else datetime.now(timezone.utc)
            else:
                end_time = datetime.now(timezone.utc)
                start_time = _metrics_start_time(db, tenant_id, end_time, period_hours)

            # Validate server belongs to tenant if filtered
            if server_id:
                from app.database.models.mcp_server import MCPServer
                server = db.query(MCPServer).filter(
                    MCPServer.id == server_id,
                    MCPServer.tenant_id == tenant_id
                ).first()
                if not server:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Server '{server_id}' not found"
                    )

            # Clean raw SQL with date_bin for reliable time bucketing
            server_filter = "AND server_id = :server_id" if server_id else ""
            plane_source = source_for_plane(plane)
            source_filter = "AND source = :source" if plane_source else ""
            sql = text(f"""
                SELECT
                    date_bin(interval '{interval_minutes} minutes', timestamp, :origin) AS time_bucket,
                    COUNT(*)::int AS total_requests,
                    SUM(CASE WHEN success THEN 1 ELSE 0 END)::int AS successful_requests,
                    AVG(total_duration_ms) AS avg_duration
                FROM proxy_metrics
                WHERE timestamp >= :start_time
                    AND timestamp <= :end_time
                    AND tenant_id = :tenant_id
                    {server_filter}
                    {source_filter}
                GROUP BY time_bucket
                ORDER BY time_bucket
            """)

            params = {
                "origin": start_time,
                "start_time": start_time,
                "end_time": end_time,
                "tenant_id": tenant_id,
            }
            if server_id:
                params["server_id"] = server_id
            if plane_source:
                params["source"] = plane_source

            results = db.execute(sql, params).fetchall()

            logger.debug(f"Retrieved time-series data: {len(results)} points", context={
                "tenant_id": tenant_id,
                "server_id": server_id
            })

            return {
                "period_hours": period_hours,
                "interval_minutes": interval_minutes,
                "server_id": server_id,
                "tenant_id": tenant_id,
                "data_points": len(results),
                "data": [
                    {
                        "time": row.time_bucket.isoformat() if row.time_bucket else None,
                        "totalRequests": row.total_requests or 0,
                        "successfulRequests": row.successful_requests or 0,
                        "failedRequests": (row.total_requests or 0) - (row.successful_requests or 0),
                        "successRate": ((row.successful_requests or 0) / row.total_requests * 100) if row.total_requests and row.total_requests > 0 else 0.0,
                        "avgDuration": float(row.avg_duration) if row.avg_duration else 0.0
                    }
                    for row in results
                ]
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to retrieve time-series data", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve time-series data"
            )

    @router.get(
        "/timeseries/latency",
        summary="Get latency percentiles time-series",
        description="Get time-series data for latency percentile charting (P50, P95, P99)"
    )
    def get_latency_timeseries(
        request: Request,
        period_hours: int = Query(24, ge=0, le=2160),
        start: Optional[datetime] = Query(None, description="Explicit window start (ISO). Overrides period_hours"),
        end: Optional[datetime] = Query(None, description="Explicit window end (ISO, exclusive)"),
        interval_minutes: int = Query(60, ge=5, le=1440),
        server_id: Optional[str] = Query(None),
        plane: Optional[str] = Query(None, description="Provenance plane: governed | observed | all"),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.METRICS)
        ),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        """
        Get latency percentiles time-series data

        Query Parameters:
        - period_hours: Time period (1-720 hours / 30 days)
        - interval_minutes: Data point interval (5-1440 minutes)
        - server_id: Optional server filter

        Returns:
        - Time-series data for latency percentile charting (P50, P95, P99)
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Explicit calendar range (MTD / last month / custom) wins
            # over the rolling preset — same precedence as resolve_window.
            if start is not None:
                start_time = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
                end_time = (end if end.tzinfo else end.replace(tzinfo=timezone.utc)) if end is not None else datetime.now(timezone.utc)
            else:
                end_time = datetime.now(timezone.utc)
                start_time = _metrics_start_time(db, tenant_id, end_time, period_hours)

            # Validate server belongs to tenant if filtered
            if server_id:
                from app.database.models.mcp_server import MCPServer
                server = db.query(MCPServer).filter(
                    MCPServer.id == server_id,
                    MCPServer.tenant_id == tenant_id
                ).first()
                if not server:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Server '{server_id}' not found"
                    )

            # Clean raw SQL with date_bin + percentile_cont for P50/P95/P99
            server_filter = "AND server_id = :server_id" if server_id else ""
            plane_source = source_for_plane(plane)
            source_filter = "AND source = :source" if plane_source else ""
            sql = text(f"""
                SELECT
                    date_bin(interval '{interval_minutes} minutes', timestamp, :origin) AS time_bucket,
                    AVG(total_duration_ms) AS avg_latency,
                    MIN(total_duration_ms) AS min_latency,
                    MAX(total_duration_ms) AS max_latency,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY total_duration_ms) AS p50,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY total_duration_ms) AS p95,
                    percentile_cont(0.99) WITHIN GROUP (ORDER BY total_duration_ms) AS p99,
                    COUNT(*)::int AS sample_count
                FROM proxy_metrics
                WHERE timestamp >= :start_time
                    AND timestamp <= :end_time
                    AND tenant_id = :tenant_id
                    {server_filter}
                    {source_filter}
                GROUP BY time_bucket
                ORDER BY time_bucket
            """)

            params = {
                "origin": start_time,
                "start_time": start_time,
                "end_time": end_time,
                "tenant_id": tenant_id,
            }
            if server_id:
                params["server_id"] = server_id
            if plane_source:
                params["source"] = plane_source

            results = db.execute(sql, params).fetchall()

            logger.debug(f"Retrieved latency time-series data: {len(results)} points", context={
                "tenant_id": tenant_id,
                "server_id": server_id
            })

            return {
                "period_hours": period_hours,
                "interval_minutes": interval_minutes,
                "server_id": server_id,
                "tenant_id": tenant_id,
                "data_points": len(results),
                "data": [
                    {
                        "time": row.time_bucket.isoformat() if row.time_bucket else None,
                        "avgLatency": float(row.avg_latency) if row.avg_latency else 0.0,
                        "minLatency": float(row.min_latency) if row.min_latency else 0.0,
                        "maxLatency": float(row.max_latency) if row.max_latency else 0.0,
                        "p50": float(row.p50) if row.p50 else 0.0,
                        "p95": float(row.p95) if row.p95 else 0.0,
                        "p99": float(row.p99) if row.p99 else 0.0,
                        "sampleCount": row.sample_count or 0
                    }
                    for row in results
                ]
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to retrieve latency time-series data", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve latency time-series data"
            )

    @router.get(
        "/timeseries/error-rate",
        summary="Get error rate time-series",
        description="Get time-series data for error rate charting"
    )
    def get_error_rate_timeseries(
        request: Request,
        period_hours: int = Query(24, ge=0, le=2160),
        start: Optional[datetime] = Query(None, description="Explicit window start (ISO). Overrides period_hours"),
        end: Optional[datetime] = Query(None, description="Explicit window end (ISO, exclusive)"),
        interval_minutes: int = Query(60, ge=5, le=1440),
        server_id: Optional[str] = Query(None),
        plane: Optional[str] = Query(None, description="Provenance plane: governed | observed | all"),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.METRICS)
        ),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        """
        Get error rate time-series data

        Query Parameters:
        - period_hours: Time period (1-720 hours)
        - interval_minutes: Data point interval (5-1440 minutes)
        - server_id: Optional server filter

        Returns:
        - Time-series data for error rate charting
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Explicit calendar range (MTD / last month / custom) wins
            # over the rolling preset — same precedence as resolve_window.
            if start is not None:
                start_time = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
                end_time = (end if end.tzinfo else end.replace(tzinfo=timezone.utc)) if end is not None else datetime.now(timezone.utc)
            else:
                end_time = datetime.now(timezone.utc)
                start_time = _metrics_start_time(db, tenant_id, end_time, period_hours)

            # Validate server belongs to tenant if filtered
            if server_id:
                from app.database.models.mcp_server import MCPServer
                server = db.query(MCPServer).filter(
                    MCPServer.id == server_id,
                    MCPServer.tenant_id == tenant_id
                ).first()
                if not server:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Server '{server_id}' not found"
                    )

            # Clean raw SQL with date_bin for reliable time bucketing
            server_filter = "AND server_id = :server_id" if server_id else ""
            plane_source = source_for_plane(plane)
            source_filter = "AND source = :source" if plane_source else ""
            sql = text(f"""
                SELECT
                    date_bin(interval '{interval_minutes} minutes', timestamp, :origin) AS time_bucket,
                    COUNT(*)::int AS total,
                    SUM(CASE WHEN NOT success THEN 1 ELSE 0 END)::int AS error_count
                FROM proxy_metrics
                WHERE timestamp >= :start_time
                    AND timestamp <= :end_time
                    AND tenant_id = :tenant_id
                    {server_filter}
                    {source_filter}
                GROUP BY time_bucket
                ORDER BY time_bucket
            """)

            params = {
                "origin": start_time,
                "start_time": start_time,
                "end_time": end_time,
                "tenant_id": tenant_id,
            }
            if server_id:
                params["server_id"] = server_id
            if plane_source:
                params["source"] = plane_source

            results = db.execute(sql, params).fetchall()

            return {
                "period_hours": period_hours,
                "interval_minutes": interval_minutes,
                "server_id": server_id,
                "tenant_id": tenant_id,
                "data_points": len(results),
                "data": [
                    {
                        "time": row.time_bucket.isoformat() if row.time_bucket else None,
                        "errorRate": (row.error_count / row.total * 100) if row.total and row.total > 0 else 0.0,
                        "errorCount": row.error_count or 0,
                        "totalRequests": row.total or 0
                    }
                    for row in results
                ]
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to retrieve error time-series", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve error time-series"
            )

    @router.get(
        "/heatmap/hourly",
        summary="Get hourly heatmap",
        description="Get request volume heatmap by hour and day for calendar visualization"
    )
    def get_hourly_heatmap(
        request: Request,
        days: int = Query(7, ge=1, le=90, description="Number of days to analyze"),
        server_id: Optional[str] = Query(None),
        plane: Optional[str] = Query(None, description="Provenance plane: governed | observed | all"),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.METRICS)
        ),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        """
        Get hourly heatmap data

        Query Parameters:
        - days: Number of days to analyze (1-90)
        - server_id: Optional server filter

        Returns:
        - Heatmap data for calendar-style visualization
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(days=days)

            # Build query with tenant filter
            query = db.query(
                ProxyMetricEntry.request_day_of_week.label('day_of_week'),
                ProxyMetricEntry.request_hour.label('hour'),
                func.count(ProxyMetricEntry.id).label('request_count')
            ).filter(
                ProxyMetricEntry.timestamp >= start_time,
                ProxyMetricEntry.timestamp <= end_time,
                ProxyMetricEntry.tenant_id == tenant_id  # Tenant filter
            )

            plane_source = source_for_plane(plane)
            if plane_source:
                query = query.filter(ProxyMetricEntry.source == plane_source)

            if server_id:
                # Verify server belongs to tenant
                from app.database.models.mcp_server import MCPServer
                server = db.query(MCPServer).filter(
                    MCPServer.id == server_id,
                    MCPServer.tenant_id == tenant_id
                ).first()

                if not server:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Server '{server_id}' not found"
                    )

                query = query.filter(ProxyMetricEntry.server_id == server_id)

            results = query.group_by(
                ProxyMetricEntry.request_day_of_week,
                ProxyMetricEntry.request_hour
            ).all()

            # Create 7x24 matrix
            heatmap_data = {}
            for day in range(7):
                heatmap_data[day] = {}
                for hour in range(24):
                    heatmap_data[day][hour] = 0

            # Fill in actual data
            max_value = 0
            for day_of_week, hour, count in results:
                if day_of_week is not None and hour is not None:
                    heatmap_data[day_of_week][hour] = count
                    max_value = max(max_value, count)

            # Format for response
            day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

            return {
                "days_analyzed": days,
                "server_id": server_id,
                "tenant_id": tenant_id,
                "max_value": max_value,
                "heatmap": [
                    {
                        "day_of_week": day,
                        "day_name": day_names[day],
                        "hourly_data": [
                            {
                                "hour": hour,
                                "value": heatmap_data[day][hour],
                                "intensity": (heatmap_data[day][hour] / max_value * 100) if max_value > 0 else 0
                            }
                            for hour in range(24)
                        ]
                    }
                    for day in range(7)
                ]
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to retrieve heatmap data", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve heatmap data"
            )

    @router.get(
        "/methods/performance",
        summary="Get methods performance",
        description="Get detailed performance metrics grouped by MCP method"
    )
    def get_methods_performance(
        request: Request,
        period_hours: int = Query(24, ge=0, le=2160),
        limit: int = Query(20, ge=1, le=100, description="Number of top methods"),
        server_id: Optional[str] = Query(None),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.METRICS)
        ),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        """
        Get methods performance metrics

        Query Parameters:
        - period_hours: Time period (1-720 hours)
        - limit: Number of top methods (1-100)
        - server_id: Optional server filter

        Returns:
        - Performance metrics by MCP method including count, durations, and success rates
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            end_time = datetime.now(timezone.utc)
            start_time = _metrics_start_time(db, tenant_id, end_time, period_hours)

            # The method is in the row's `mcp_data`: `ProxyMetricEntry` has
            # no `mcp_method` column.
            method_col = ProxyMetricEntry.mcp_data['method'].as_string()

            # Build query with tenant filter
            query = db.query(
                method_col,
                func.count(ProxyMetricEntry.id).label('count'),
                func.avg(ProxyMetricEntry.total_duration_ms).label('avg_duration'),
                func.min(ProxyMetricEntry.total_duration_ms).label('min_duration'),
                func.max(ProxyMetricEntry.total_duration_ms).label('max_duration'),
                (func.sum(func.cast(ProxyMetricEntry.success, Integer)) * 100.0 /
                 func.count(ProxyMetricEntry.id)).label('success_rate'),
                func.sum(func.cast(~ProxyMetricEntry.success, Integer)).label('error_count')
            ).filter(
                ProxyMetricEntry.timestamp >= start_time,
                ProxyMetricEntry.timestamp <= end_time,
                ProxyMetricEntry.tenant_id == tenant_id,  # Tenant filter
                method_col.isnot(None)
            )

            if server_id:
                # Verify server belongs to tenant
                from app.database.models.mcp_server import MCPServer
                server = db.query(MCPServer).filter(
                    MCPServer.id == server_id,
                    MCPServer.tenant_id == tenant_id
                ).first()

                if not server:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Server '{server_id}' not found"
                    )

                query = query.filter(ProxyMetricEntry.server_id == server_id)

            results = query.group_by(method_col) \
                .order_by(func.count(ProxyMetricEntry.id).desc()) \
                .limit(limit) \
                .all()

            return {
                "period_hours": period_hours,
                "server_id": server_id,
                "tenant_id": tenant_id,
                "methods_count": len(results),
                "methods": [
                    {
                        "method": method,
                        "count": count,
                        "avg_duration_ms": float(avg_duration) if avg_duration is not None else 0.0,
                        "min_duration_ms": float(min_duration) if min_duration is not None else 0.0,
                        "max_duration_ms": float(max_duration) if max_duration is not None else 0.0,
                        "success_rate": float(success_rate) if success_rate is not None else 0.0,
                        "error_count": error_count or 0
                    }
                    for method, count, avg_duration, min_duration, max_duration, success_rate, error_count in results
                ]
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to retrieve methods performance", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve methods performance"
            )

    @router.get(
        "/servers/status",
        summary="Get servers status",
        description="Get real-time status of all servers for the tenant"
    )
    def get_servers_status(
        request: Request,
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.METRICS)
        ),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ) -> Dict[str, Any]:
        """
        Get servers status

        Returns:
        - Real-time status of all servers including health status,
          latency, and request rate
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            # Get metrics from last 5 minutes
            recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)

            # Query per-server metrics with tenant filter
            query = db.query(
                ProxyMetricEntry.server_id,
                func.count(ProxyMetricEntry.id).label('request_count'),
                func.avg(ProxyMetricEntry.total_duration_ms).label('avg_latency'),
                (func.sum(func.cast(ProxyMetricEntry.success, Integer)) * 100.0 /
                 func.count(ProxyMetricEntry.id)).label('success_rate'),
                func.max(ProxyMetricEntry.timestamp).label('last_request')
            ).filter(
                ProxyMetricEntry.timestamp >= recent_time,
                ProxyMetricEntry.tenant_id == tenant_id,  # Tenant filter
                ProxyMetricEntry.server_id.isnot(None)
            ).group_by(ProxyMetricEntry.server_id).all()

            # Determine health status
            def get_health_status(success_rate, avg_latency, last_request):
                if last_request is None:
                    return 'unknown'

                seconds_since_last = (datetime.now(timezone.utc) - last_request).total_seconds()
                if seconds_since_last > 300:  # No requests in 5 minutes
                    return 'idle'

                if success_rate and success_rate < 90:
                    return 'unhealthy'
                if success_rate and success_rate < 99 or (avg_latency and avg_latency > 5000):
                    return 'degraded'
                return 'healthy'

            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "tenant_id": tenant_id,
                "window_minutes": 5,
                "servers": [
                    {
                        "server_id": server_id,
                        "status": get_health_status(success_rate, avg_latency, last_request),
                        "request_count": request_count,
                        "requests_per_minute": request_count / 5.0,
                        "avg_latency_ms": float(avg_latency) if avg_latency is not None else 0.0,
                        "success_rate": float(success_rate) if success_rate is not None else 0.0,
                        "last_request": last_request.isoformat() if last_request else None
                    }
                    for server_id, request_count, avg_latency, success_rate, last_request in query
                ]
            }

        except Exception as e:
            logger.error("Failed to retrieve servers status", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to retrieve servers status"
            )

    @router.get(
        "/cost/breakdown",
        summary="Cost breakdown (FinOps)",
        description="Cost & tokens by provider/model/team/user with governed/observed split",
    )
    def get_cost_breakdown(
        request: Request,
        period_hours: int = Query(720, ge=0, le=8760, description="Period in hours; 0 = all time"),
        start: Optional[datetime] = Query(None, description="Explicit window start (ISO). Overrides period_hours — FinOps lives in calendar months"),
        end: Optional[datetime] = Query(None, description="Explicit window end (ISO, exclusive). Only with start; open-ended when omitted"),
        compare: bool = Query(False, description="Also return the preceding equal-length window (totals + per-row prev values). Ignored for all-time"),
        plane: Optional[str] = Query(None, description="Provenance plane: governed | observed | all"),
        top_n: int = Query(10, ge=1, le=50, description="Top-N rows per dimension"),
        group_id: Optional[str] = Query(None, description="Scope to one Resource Group (AI Systems are RGs)"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.METRICS)),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db),
    ) -> Dict[str, Any]:
        """FinOps cost attribution over the unified usage ledger."""
        _admin, tenant_id, _t = admin_and_tenant
        try:
            return cost_service.breakdown(
                db, tenant_id=tenant_id, period_hours=period_hours,
                plane=plane, top_n=top_n, group_id=_clean_group_id(group_id),
                start=start, end=end, compare=compare,
            )
        except Exception as e:
            logger.error("Failed to compute cost breakdown", context={
                "tenant_id": tenant_id, "error": str(e)}, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to compute cost breakdown",
            )

    @router.get(
        "/cost/monthly",
        summary="Monthly LLM economics series (FinOps)",
        description="Per-calendar-month LLM cost/requests/tokens + unit cost per 1k requests",
    )
    def get_cost_monthly(
        request: Request,
        months: int = Query(12, ge=1, le=36, description="How many calendar months back (including the current one)"),
        plane: Optional[str] = Query(None, description="Provenance plane: governed | observed | all"),
        group_id: Optional[str] = Query(None, description="Scope to one Resource Group (AI Systems are RGs)"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.METRICS)),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db),
    ) -> Dict[str, Any]:
        """Monthly LLM series for the Economics tab's trend charts."""
        _admin, tenant_id, _t = admin_and_tenant
        try:
            return cost_service.monthly(
                db, tenant_id=tenant_id, months=months,
                plane=plane, group_id=_clean_group_id(group_id),
            )
        except Exception as e:
            logger.error("Failed to compute monthly cost series", context={
                "tenant_id": tenant_id, "error": str(e)}, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to compute monthly cost series",
            )

    @router.get(
        "/cost/budgets",
        summary="Budget burn-down (FinOps)",
        description="Per-resource-group budget utilisation vs limits + month-end projection",
    )
    def get_cost_budgets(
        request: Request,
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.METRICS)),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db),
    ) -> Dict[str, Any]:
        """Budget burn-down vs llm_global_limits.budget with run-rate projection."""
        _admin, tenant_id, _t = admin_and_tenant
        try:
            return cost_service.budgets(db, tenant_id=tenant_id)
        except Exception as e:
            logger.error("Failed to compute budget burn-down", context={
                "tenant_id": tenant_id, "error": str(e)}, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to compute budget burn-down",
            )

    @router.get(
        "/cost/series",
        summary="Cost composition over time (FinOps)",
        description=(
            "Daily cost per provider/model/team for the stacked-area chart. "
            "Always daily buckets; the client rolls days up to weeks/months "
            "with the shared grain rule. Tail beyond top_n is returned as "
            "'other' so the bands still sum to the real total."
        ),
    )
    def get_cost_series(
        request: Request,
        dimension: str = Query("provider", description="provider | model | team"),
        period_hours: int = Query(2160, ge=0, le=8760, description="Period in hours; 0 = all time"),
        start: Optional[datetime] = Query(None, description="Explicit window start (ISO). Overrides period_hours"),
        end: Optional[datetime] = Query(None, description="Explicit window end (ISO, exclusive)"),
        plane: Optional[str] = Query(None, description="Provenance plane: governed | observed | all"),
        group_id: Optional[str] = Query(None, description="Scope to one Resource Group (AI Systems are RGs)"),
        top_n: int = Query(5, ge=1, le=12, description="Distinct bands before the tail folds into 'other'"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.METRICS)),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db),
    ) -> Dict[str, Any]:
        """Composition-over-time feed for the Cost tab."""
        _admin, tenant_id, _t = admin_and_tenant
        if dimension not in ("provider", "model", "team"):
            raise HTTPException(status_code=422, detail="dimension must be provider | model | team")
        try:
            return cost_service.series(
                db, tenant_id=tenant_id, dimension=dimension,
                period_hours=period_hours, plane=plane,
                group_id=_clean_group_id(group_id), start=start, end=end,
                top_n=top_n,
            )
        except Exception as e:
            logger.error("Failed to compute cost series", context={
                "tenant_id": tenant_id, "error": str(e)}, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to compute cost series",
            )

    @router.get(
        "/cost/savings",
        summary="Cost avoided by the reducers (FinOps)",
        description=(
            "Gateway-cache savings (estimated — hits x the window's average "
            "non-cached request cost, basis included) and provider prompt-cache "
            "savings (measured from the ledger), kept apart because their "
            "evidence differs in kind. Governed traffic only."
        ),
    )
    def get_cost_savings(
        request: Request,
        period_hours: int = Query(2160, ge=0, le=8760, description="Period in hours; 0 = all time"),
        start: Optional[datetime] = Query(None, description="Explicit window start (ISO). Overrides period_hours"),
        end: Optional[datetime] = Query(None, description="Explicit window end (ISO, exclusive)"),
        group_id: Optional[str] = Query(None, description="Scope to one Resource Group (AI Systems are RGs)"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.METRICS)),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db),
    ) -> Dict[str, Any]:
        """What the reducers avoided spending, honestly labelled."""
        _admin, tenant_id, _t = admin_and_tenant
        try:
            return cost_service.savings(
                db, tenant_id=tenant_id, period_hours=period_hours,
                group_id=_clean_group_id(group_id), start=start, end=end,
            )
        except Exception as e:
            logger.error("Failed to compute cost savings", context={
                "tenant_id": tenant_id, "error": str(e)}, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to compute cost savings",
            )

    @router.get(
        "/cost/export/focus",
        summary="FOCUS cost export (FinOps Open Cost & Usage Specification)",
        description=(
            "Daily-grain cost rows as FOCUS-conformant CSV — one row per "
            "day x provider x model x resource group. A documented subset "
            "of FOCUS 1.x columns (see CostMetricsService.FOCUS_COLUMNS): "
            "columns we cannot answer truthfully are omitted, not faked. "
            "Ingestible by any FOCUS-aware FinOps platform."
        ),
    )
    def export_cost_focus(
        request: Request,
        period_hours: int = Query(720, ge=0, le=8760, description="Period in hours; 0 = all time"),
        start: Optional[datetime] = Query(None, description="Explicit window start (ISO). Overrides period_hours"),
        end: Optional[datetime] = Query(None, description="Explicit window end (ISO, exclusive)"),
        plane: Optional[str] = Query(None, description="Provenance plane: governed | observed | all"),
        group_id: Optional[str] = Query(None, description="Scope to one or more Resource Groups (comma-separated)"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.METRICS)),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db),
    ) -> StreamingResponse:
        """Stream the FOCUS CSV — constant memory even over a year."""
        _admin, tenant_id, _t = admin_and_tenant
        cols = cost_service.FOCUS_COLUMNS

        def _iter():
            import csv as _csv
            import io as _io
            buf = _io.StringIO()
            w = _csv.DictWriter(buf, fieldnames=cols)
            w.writeheader()
            yield buf.getvalue(); buf.seek(0); buf.truncate(0)
            for row in cost_service.focus_rows(
                db, tenant_id=tenant_id, period_hours=period_hours,
                plane=plane, group_id=_clean_group_id(group_id),
                start=start, end=end,
            ):
                w.writerow(row)
                yield buf.getvalue(); buf.seek(0); buf.truncate(0)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        return StreamingResponse(
            _iter(),
            media_type="text/csv",
            headers={
                "Content-Disposition":
                    f'attachment; filename="brutor-focus-{stamp}.csv"',
            },
        )

    @router.get(
        "/governance/posture",
        summary="Governance posture",
        description="Guardrail blocks / policy denials / PII detections + shadow-AI signals",
    )
    def get_governance_posture(
        request: Request,
        period_hours: int = Query(720, ge=0, le=8760, description="Period in hours; 0 = all time"),
        start: Optional[datetime] = Query(None, description="Explicit window start (ISO). Overrides period_hours"),
        end: Optional[datetime] = Query(None, description="Explicit window end (ISO, exclusive)"),
        top_n: int = Query(10, ge=1, le=50),
        group_id: Optional[str] = Query(None, description="Scope to one Resource Group (AI Systems are RGs)"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.METRICS)),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db),
    ) -> Dict[str, Any]:
        """Governance-posture aggregation (proxy_logs outcomes + shadow-AI)."""
        _admin, tenant_id, _t = admin_and_tenant
        try:
            return governance_service.posture(
                db, tenant_id=tenant_id, period_hours=period_hours, top_n=top_n,
                group_id=_clean_group_id(group_id), start=start, end=end,
            )
        except Exception as e:
            logger.error("Failed to compute governance posture", context={
                "tenant_id": tenant_id, "error": str(e)}, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to compute governance posture",
            )

    @router.get(
        "/agents/activity",
        summary="Agent activity",
        description="Per-agent runtime-authz activity: top agents, deny/approval rates, denied feed",
    )
    def get_agent_activity(
        request: Request,
        period_hours: int = Query(720, ge=0, le=8760, description="Period in hours; 0 = all time"),
        top_n: int = Query(10, ge=1, le=50),
        group_id: Optional[str] = Query(None, description="Scope to one Resource Group (AI Systems are RGs)"),
        admin_and_tenant: tuple = Depends(require_tenant_resource_access(TenantResourceTypes.METRICS)),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db),
    ) -> Dict[str, Any]:
        """Per-agent activity aggregation (proxy_logs actor_agent_id outcomes)."""
        _admin, tenant_id, _t = admin_and_tenant
        try:
            return agent_service.activity(
                db, tenant_id=tenant_id, period_hours=period_hours, top_n=top_n,
                group_id=_clean_group_id(group_id),
            )
        except Exception as e:
            logger.error("Failed to compute agent activity", context={
                "tenant_id": tenant_id, "error": str(e)}, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to compute agent activity",
            )

    @router.get(
        "/export",
        summary="Export metrics data",
        description="Export metrics data in CSV or JSON format"
    )
    def export_metrics(
        request: Request,
        format: str = Query("json", description="Export format: json or csv"),
        period_hours: int = Query(24, ge=0, le=2160),
        server_id: Optional[str] = Query(None),
        plane: Optional[str] = Query(None, description="Provenance plane: governed | observed | all"),
        admin_and_tenant: tuple = Depends(
            require_tenant_resource_access(TenantResourceTypes.METRICS)
        ),
        _: Any = Depends(require_permission(Permission.PROXY_STATS_READ)),
        db: Session = Depends(get_db)
    ):
        """
        Export metrics data

        Query Parameters:
        - format: Export format (json or csv)
        - period_hours: Time period (1-720 hours)
        - server_id: Optional server filter

        Returns:
        - Exported metrics data in requested format
        """
        admin, tenant_id, admin_type = admin_and_tenant

        # Eager load admin attributes for exception handlers
        admin_id = admin.id
        admin_type_value = admin_type.value if hasattr(admin_type, 'value') else str(admin_type)

        try:
            end_time = datetime.now(timezone.utc)
            start_time = _metrics_start_time(db, tenant_id, end_time, period_hours)

            # Query metrics with tenant filter
            query = db.query(ProxyMetricEntry).filter(
                ProxyMetricEntry.timestamp >= start_time,
                ProxyMetricEntry.timestamp <= end_time,
                ProxyMetricEntry.tenant_id == tenant_id  # Tenant filter
            )

            plane_source = source_for_plane(plane)
            if plane_source:
                query = query.filter(ProxyMetricEntry.source == plane_source)

            if server_id:
                # Verify server belongs to tenant
                from app.database.models.mcp_server import MCPServer
                server = db.query(MCPServer).filter(
                    MCPServer.id == server_id,
                    MCPServer.tenant_id == tenant_id
                ).first()

                if not server:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Server '{server_id}' not found"
                    )

                query = query.filter(ProxyMetricEntry.server_id == server_id)

            metrics = query.order_by(ProxyMetricEntry.timestamp.desc()).limit(10000).all()

            if format.lower() == "csv":
                # Generate CSV
                import csv
                from io import StringIO

                output = StringIO()
                writer = csv.writer(output)

                # Write header
                writer.writerow([
                    'timestamp', 'server_id', 'user_type', 'mcp_method',
                    'success', 'total_duration_ms', 'status_code', 'error_type'
                ])

                # Write data
                for metric in metrics:
                    writer.writerow([
                        metric.timestamp.isoformat(),
                        metric.server_id,
                        metric.user_type,
                        metric.mcp_method,
                        metric.success,
                        metric.total_duration_ms,
                        metric.status_code,
                        metric.error_type
                    ])

                logger.debug(f"Exported {len(metrics)} metrics to CSV", context={
                    "tenant_id": tenant_id,
                    "admin_id": admin_id
                })

                return Response(
                    content=output.getvalue(),
                    media_type="text/csv",
                    headers={
                        "Content-Disposition": f"attachment; filename=metrics_{tenant_id}_{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
                    }
                )
            else:
                # Return JSON
                data = [metric.to_dict() for metric in metrics]

                logger.debug(f"Exported {len(metrics)} metrics to JSON", context={
                    "tenant_id": tenant_id,
                    "admin_id": admin_id
                })

                return {
                    "metrics": data,
                    "count": len(data),
                    "period": {
                        "start": start_time.isoformat(),
                        "end": end_time.isoformat()
                    },
                    "tenant_id": tenant_id,
                    "exported_by": extract_user_context(admin)
                }

        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to export metrics", context={
                "tenant_id": tenant_id,
                "error": str(e)
            }, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to export metrics"
            )

    return router