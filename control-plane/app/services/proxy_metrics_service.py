# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Unified Proxy Metrics Service
Query and analyze proxy metrics written by the Rust proxy.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from collections import defaultdict

from sqlalchemy.orm import Session
from sqlalchemy import and_, func, desc, case, or_

from app.core.logging_system import BrutorLogger
from app.services.metrics_window import resolve_window
from app.database.models.proxy_metric import ProxyMetricEntry

# Governed / Observed / All plane filter for Mission Control. Maps the UI
# "plane" toggle onto the proxy_metrics.source discriminator.
PLANE_GOVERNED = "governed"
PLANE_OBSERVED = "observed"
PLANE_ALL = "all"
_PLANE_TO_SOURCE = {PLANE_GOVERNED: "gateway", PLANE_OBSERVED: "observed"}


def source_for_plane(plane: Optional[str]) -> Optional[str]:
    """Map a plane ('governed'|'observed'|'all'|None) to a proxy_metrics.source
    value, or None for "no filter" (all planes)."""
    if not plane:
        return None
    return _PLANE_TO_SOURCE.get(plane.lower())


class UnifiedProxyMetricsService:
    """
    Read-only service for querying proxy metrics.

    Metrics are written by the Rust proxy. This service provides
    statistics, real-time monitoring, performance analysis, and
    dashboard capabilities for the admin API.
    """

    def __init__(self, logger: Optional[BrutorLogger] = None):
        self.logger = logger or BrutorLogger("unified-metrics-service")

    # ========================================================================
    # STATISTICS (Aggregated Data)
    # ========================================================================

    async def get_statistics(
        self,
        db: Session,
        tenant_id: str,
        proxy_types: Optional[List[str]] = None,
        hours: int = 24,
        server_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get aggregated statistics for metrics

        Args:
            db: Database session
            tenant_id: Tenant ID
            proxy_types: List of proxy types to include
            hours: Hours to look back
            server_id: Filter by server ID

        Returns:
            Dictionary with aggregated statistics
        """
        try:
            start_time = datetime.now(timezone.utc) - timedelta(hours=hours)

            query = db.query(ProxyMetricEntry).filter(
                and_(
                    ProxyMetricEntry.tenant_id == tenant_id,
                    ProxyMetricEntry.timestamp >= start_time
                )
            )

            if proxy_types:
                query = query.filter(ProxyMetricEntry.proxy_type.in_(proxy_types))

            if server_id:
                query = query.filter(ProxyMetricEntry.server_id == server_id)

            metrics = query.all()

            if not metrics:
                return {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "failed_requests": 0,
                    "success_rate": 0.0,
                    "avg_duration_ms": 0.0
                }

            total_requests = len(metrics)
            successful_requests = sum(1 for m in metrics if m.success)
            failed_requests = total_requests - successful_requests
            success_rate = (successful_requests / total_requests) * 100 if total_requests > 0 else 0.0

            durations = [m.total_duration_ms for m in metrics if m.total_duration_ms]
            avg_duration_ms = sum(durations) / len(durations) if durations else 0.0

            # Breakdown by proxy type
            by_type = defaultdict(lambda: {"count": 0, "success": 0, "total_duration": 0.0})
            for metric in metrics:
                by_type[metric.proxy_type]["count"] += 1
                if metric.success:
                    by_type[metric.proxy_type]["success"] += 1
                if metric.total_duration_ms:
                    by_type[metric.proxy_type]["total_duration"] += metric.total_duration_ms

            stats_by_type = {}
            for ptype, data in by_type.items():
                count = data["count"]
                success_count = data["success"]
                stats_by_type[ptype] = {
                    "total_requests": count,
                    "successful_requests": success_count,
                    "failed_requests": count - success_count,
                    "success_rate": (success_count / count * 100) if count > 0 else 0.0,
                    "avg_duration_ms": (data["total_duration"] / count) if count > 0 else 0.0
                }

            return {
                "total_requests": total_requests,
                "successful_requests": successful_requests,
                "failed_requests": failed_requests,
                "success_rate": success_rate,
                "avg_duration_ms": avg_duration_ms,
                "by_type": stats_by_type
            }

        except Exception as e:
            self.logger.error("Failed to get statistics", context={
                "tenant_id": tenant_id,
                "error": str(e)
            })
            return {}

    # ========================================================================
    # TIME SERIES DATA (For Charts)
    # ========================================================================

    # ========================================================================
    # GATEWAY ENDPOINT METHODS
    # ========================================================================

    async def get_real_time_metrics(
        self,
        db: Session,
        tenant_id: Optional[str] = None,
        last_minutes: int = 5
    ) -> Dict[str, Any]:
        """
        Get real-time metrics for the last N minutes

        Used by: Gateway root and health endpoints

        Args:
            db: Database session
            tenant_id: Optional tenant filter (None = all tenants)
            last_minutes: Minutes to look back (default 5)

        Returns:
            Dictionary with real_time and recent_activity data
        """
        try:
            start_time = datetime.now(timezone.utc) - timedelta(minutes=last_minutes)

            query = db.query(ProxyMetricEntry).filter(
                ProxyMetricEntry.timestamp >= start_time
            )

            if tenant_id:
                query = query.filter(ProxyMetricEntry.tenant_id == tenant_id)

            metrics = query.all()

            if not metrics:
                return {
                    "real_time": {
                        "requests_per_minute": 0,
                        "current_success_rate": 100.0,
                        "active_sessions": 0,
                        "active_servers": 0,
                        "total_requests": 0,
                        "failed_requests": 0
                    },
                    "recent_activity": {
                        "top_servers": [],
                        "top_methods": [],
                        "error_spike": False
                    }
                }

            total_requests = len(metrics)
            successful_requests = sum(1 for m in metrics if m.success)
            failed_requests = total_requests - successful_requests
            success_rate = (successful_requests / total_requests * 100) if total_requests > 0 else 100.0
            requests_per_minute = total_requests / last_minutes if last_minutes > 0 else 0

            # Count unique sessions and servers
            active_sessions = len(set(
                m.mcp_data.get('session_id')
                for m in metrics
                if m.mcp_data and m.mcp_data.get('session_id')
            ))
            active_servers = len(set(m.server_id for m in metrics if m.server_id))

            # Top servers — also collect server names from mcp_data or DB
            server_counts = defaultdict(int)
            server_names = {}
            for metric in metrics:
                if metric.server_id:
                    server_counts[metric.server_id] += 1
                    # Grab server_name from mcp_data if available
                    if metric.server_id not in server_names and metric.mcp_data:
                        sn = metric.mcp_data.get('server_name')
                        if sn:
                            server_names[metric.server_id] = sn
            # Fallback: look up missing names from DB
            missing_ids = [sid for sid in server_counts if sid not in server_names]
            if missing_ids:
                from app.database.models.mcp_server import MCPServer
                servers = db.query(MCPServer.id, MCPServer.name).filter(
                    MCPServer.id.in_(missing_ids)
                ).all()
                for s in servers:
                    server_names[s.id] = s.name
            top_servers = [
                {"server_id": server_id, "server_name": server_names.get(server_id, server_id), "request_count": count}
                for server_id, count in sorted(server_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            ]

            # Top MCP methods
            method_counts = defaultdict(int)
            for metric in metrics:
                if metric.mcp_data and metric.mcp_data.get('method'):
                    method_counts[metric.mcp_data['method']] += 1
            top_methods = [
                {"method": method, "count": count}
                for method, count in sorted(method_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            ]

            # Error spike detection (more than 20% errors in last minute)
            last_minute_metrics = [m for m in metrics if m.timestamp >= datetime.now(timezone.utc) - timedelta(minutes=1)]
            error_spike = False
            if last_minute_metrics:
                recent_error_rate = sum(1 for m in last_minute_metrics if not m.success) / len(last_minute_metrics)
                error_spike = recent_error_rate > 0.2

            return {
                "real_time": {
                    "requests_per_minute": round(requests_per_minute, 2),
                    "current_success_rate": round(success_rate, 2),
                    "active_sessions": active_sessions,
                    "active_servers": active_servers,
                    "total_requests": total_requests,
                    "failed_requests": failed_requests
                },
                "recent_activity": {
                    "top_servers": top_servers,
                    "top_methods": top_methods,
                    "error_spike": error_spike
                }
            }

        except Exception as e:
            self.logger.error("Failed to get real-time metrics", context={
                "tenant_id": tenant_id,
                "error": str(e)
            })
            return {
                "real_time": {
                    "requests_per_minute": 0,
                    "current_success_rate": 100.0,
                    "active_sessions": 0,
                    "active_servers": 0,
                    "total_requests": 0,
                    "failed_requests": 0
                },
                "recent_activity": {
                    "top_servers": [],
                    "top_methods": [],
                    "error_spike": False
                }
            }

    async def get_performance_metrics(
        self,
        db: Session,
        tenant_id: Optional[str] = None,
        period_hours: int = 24,
        server_id: Optional[str] = None,
        user_type: Optional[str] = None,
        plane: Optional[str] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Get comprehensive performance metrics for a time period

        Used by: Gateway root and health endpoints

        Args:
            db: Database session
            tenant_id: Optional tenant filter (None = all tenants)
            period_hours: Hours to look back (default 24)
            server_id: Optional server filter
            user_type: Optional user type filter

        Returns:
            Dictionary with overview, performance, errors, and mcp_methods data
        """
        try:
            query = db.query(ProxyMetricEntry)

            # period_hours <= 0 means "all time" — omit the lower time bound
            # entirely (mirrors the resource-group usage endpoint's `all`).
            # NOTE: start_time must be bound in BOTH branches — it is referenced
            # unconditionally further down (the A2A and media aggregators). When
            # it was only assigned inside the `> 0` branch, the all-time path
            # raised NameError, got swallowed by the except below, and returned
            # the all-zero fallback (every KPI showing 0 while charts rendered).
            # Explicit calendar range (month-to-date / last month / custom)
            # overrides the rolling period — one precedence rule, shared
            # with every other MC surface via resolve_window().
            start_time, end_time = resolve_window(period_hours, start, end)
            if start_time is not None:
                query = query.filter(ProxyMetricEntry.timestamp >= start_time)
            if end_time is not None:
                query = query.filter(ProxyMetricEntry.timestamp < end_time)

            if tenant_id:
                query = query.filter(ProxyMetricEntry.tenant_id == tenant_id)

            if server_id:
                query = query.filter(ProxyMetricEntry.server_id == server_id)

            if user_type:
                query = query.filter(ProxyMetricEntry.end_user_type == user_type)

            # Governed / Observed / All plane filter (Mission Control source
            # toggle). None / 'all' => no filter.
            plane_source = source_for_plane(plane)
            if plane_source is not None:
                query = query.filter(ProxyMetricEntry.source == plane_source)

            # ── SQL-side aggregation ─────────────────────────────────
            #
            # This used to be `metrics = query.all()` followed by ten
            # Python loops over full ORM entities. With a year of traffic
            # that is ~340k rows × 50 columns instantiated per request —
            # 12s at 90 days, 31s at All-Time, measured — and Mission
            # Control fires it on every 30s refresh. Below are the same
            # aggregations pushed into SQL sharing one filter list; the
            # composite (tenant_id, timestamp) indexes cover them. Same
            # response shape, same semantics (quirks preserved and
            # marked inline).
            conds = []
            if start_time is not None:
                conds.append(ProxyMetricEntry.timestamp >= start_time)
            if end_time is not None:
                conds.append(ProxyMetricEntry.timestamp < end_time)
            if tenant_id:
                conds.append(ProxyMetricEntry.tenant_id == tenant_id)
            if server_id:
                conds.append(ProxyMetricEntry.server_id == server_id)
            if user_type:
                conds.append(ProxyMetricEntry.end_user_type == user_type)
            if plane_source is not None:
                conds.append(ProxyMetricEntry.source == plane_source)

            def _q(*cols):
                q = db.query(*cols)
                for c in conds:
                    q = q.filter(c)
                return q

            def one(cond):
                return func.sum(case((cond, 1), else_=0))

            # The old loop treated duration 0 as "absent" (Python
            # truthiness), so averages/percentiles skip NULL *and* 0.
            dur = ProxyMetricEntry.total_duration_ms
            dur_present = and_(dur.isnot(None), dur != 0)

            (total_requests, successful_requests, n_dur, sum_dur, min_dur,
             max_dur, sum_req_bytes, sum_resp_bytes, cache_hits,
             session_reused_count) = _q(
                func.count(ProxyMetricEntry.id),
                one(ProxyMetricEntry.success.is_(True)),
                one(dur_present),
                func.coalesce(func.sum(case((dur_present, dur), else_=0.0)), 0.0),
                func.min(case((dur_present, dur))),
                func.max(case((dur_present, dur))),
                func.coalesce(func.sum(ProxyMetricEntry.request_size_bytes), 0),
                func.coalesce(func.sum(ProxyMetricEntry.response_size_bytes), 0),
                one(ProxyMetricEntry.cache_hit.is_(True)),
                one(ProxyMetricEntry.mcp_data['session_reused'].as_boolean().is_(True)),
            ).one()
            total_requests = int(total_requests or 0)

            if not total_requests:
                return {
                    "overview": {
                        "total_requests": 0,
                        "successful_requests": 0,
                        "failed_requests": 0,
                        "success_rate": 100.0,
                        "avg_duration_ms": 0.0,
                        "min_duration_ms": 0.0,
                        "max_duration_ms": 0.0,
                        "total_data_transferred_mb": 0.0,
                        "avg_response_size_kb": 0.0,
                        "response_time_percentiles": {
                            "p50": 0.0,
                            "p75": 0.0,
                            "p90": 0.0,
                            "p95": 0.0,
                            "p99": 0.0
                        },
                        "session_reuse_rate": 0.0,
                        "cache_hit_rate": 0.0
                    },
                    "performance": {},
                    "errors": {
                        "error_rate": 0.0,
                        "error_breakdown": {},
                        "top_errors": []
                    },
                    "mcp_methods": {},
                    "mcp_tool_calls": {},
                    "top_mcp_servers": [],
                    "top_llm_models": [],
                    "llm_economics": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "estimated_cost_usd": 0.0,
                        "llm_request_count": 0,
                        "avg_cost_per_request": 0.0,
                        "avg_tokens_per_request": 0
                    },
                    "media_usage": {
                        "request_count": 0,
                        "estimated_cost_usd": 0.0,
                        "modes": {},
                    },
                    "a2a_capabilities": {},
                    "top_a2a_agents": [],
                    "a2a_overview": {
                        "total_requests": 0,
                        "successful_requests": 0,
                        "success_rate": 0.0,
                        "avg_duration_ms": 0.0,
                        "total_data_transferred_mb": 0.0,
                    },
                    "usage_patterns": {
                        "peak_hour": None,
                        "requests_by_hour": {},
                        "user_type_distribution": {}
                    }
                }

            successful_requests = int(successful_requests or 0)
            failed_requests = total_requests - successful_requests
            success_rate = (successful_requests / total_requests * 100) if total_requests > 0 else 100.0

            n_dur = int(n_dur or 0)
            avg_duration = (float(sum_dur) / n_dur) if n_dur else 0.0
            min_duration = float(min_dur or 0.0)
            max_duration = float(max_dur or 0.0)

            # Percentiles via ordered OFFSET — identical to the old
            # `sorted_durations[int(n * p)]` semantics and portable
            # (`percentile_cont` doesn't exist on the SQLite the tests
            # run on). Five one-row indexed probes beat shipping and
            # sorting 340k floats in Python.
            percentiles = {"p50": 0.0, "p75": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0}
            if n_dur:
                if db.get_bind().dialect.name == "postgresql":
                    # One pass with percentile_disc — `disc` (not `cont`)
                    # to match the old sorted-array indexing, which always
                    # returned an observed value rather than interpolating.
                    row = _q(*[
                        func.percentile_disc(f).within_group(dur.asc())
                        for f in (0.50, 0.75, 0.90, 0.95, 0.99)
                    ]).filter(dur_present).one()
                    for label, v in zip(("p50", "p75", "p90", "p95", "p99"), row):
                        percentiles[label] = float(v or 0.0)
                else:
                    # SQLite (unit tests): ordered OFFSET probes with the
                    # exact `sorted_durations[int(n * p)]` semantics the
                    # Python loop had.
                    for label, frac in (("p50", 0.50), ("p75", 0.75), ("p90", 0.90),
                                        ("p95", 0.95), ("p99", 0.99)):
                        offset = min(int(n_dur * frac), n_dur - 1)
                        row = (_q(dur).filter(dur_present)
                               .order_by(dur).limit(1).offset(offset).first())
                        percentiles[label] = float(row[0]) if row else 0.0

            total_request_bytes = int(sum_req_bytes or 0)
            total_response_bytes = int(sum_resp_bytes or 0)
            total_data_mb = (total_request_bytes + total_response_bytes) / (1024 * 1024)
            avg_response_kb = (total_response_bytes / total_requests / 1024) if total_requests else 0.0

            session_reused_count = int(session_reused_count or 0)
            session_reuse_rate = (session_reused_count / total_requests * 100) if total_requests > 0 else 0.0
            cache_hit_count = int(cache_hits or 0)
            cache_hit_rate = (cache_hit_count / total_requests * 100) if total_requests > 0 else 0.0

            # Error analysis
            error_rate = (failed_requests / total_requests * 100) if total_requests > 0 else 0.0
            error_breakdown = {
                cat: int(cnt) for cat, cnt in _q(
                    ProxyMetricEntry.error_category, func.count(ProxyMetricEntry.id)
                ).filter(
                    ProxyMetricEntry.success.is_(False),
                    ProxyMetricEntry.error_category.isnot(None),
                ).group_by(ProxyMetricEntry.error_category).all()
            }
            top_errors = sorted(error_breakdown.items(), key=lambda x: x[1], reverse=True)[:5]

            # MCP methods / tool calls — grouped on the JSON fields the
            # writer stamps. `server_name` uses MAX() where the old loop
            # took "first seen"; both mean "a representative name".
            method_col = ProxyMetricEntry.mcp_data['method'].as_string()
            tool_col = ProxyMetricEntry.mcp_data['tool_name'].as_string()
            sname_col = ProxyMetricEntry.mcp_data['server_name'].as_string()

            mcp_methods_formatted = {}
            for method, count, total_dur, succ in _q(
                method_col, func.count(ProxyMetricEntry.id),
                func.coalesce(func.sum(case((dur.isnot(None), dur), else_=0.0)), 0.0),
                one(ProxyMetricEntry.success.is_(True)),
            ).filter(method_col.isnot(None)).group_by(method_col).all():
                count = int(count)
                mcp_methods_formatted[method] = {
                    "count": count,
                    "avg_duration": (float(total_dur) / count) if count > 0 else 0.0,
                    "success_rate": (int(succ) / count * 100) if count > 0 else 0.0,
                }

            mcp_tool_calls_formatted = {}
            for tool, count, total_dur, succ, sname in _q(
                tool_col, func.count(ProxyMetricEntry.id),
                func.coalesce(func.sum(case((dur.isnot(None), dur), else_=0.0)), 0.0),
                one(ProxyMetricEntry.success.is_(True)),
                func.max(sname_col),
            ).filter(method_col == 'tools/call', tool_col.isnot(None)
            ).group_by(tool_col).all():
                count = int(count)
                mcp_tool_calls_formatted[tool] = {
                    "count": count,
                    "avg_duration": (float(total_dur) / count) if count > 0 else 0.0,
                    "success_rate": (int(succ) / count * 100) if count > 0 else 0.0,
                    "server_name": sname,
                }

            # Top servers — MCP servers vs AI models, grouped by server_id
            llm_rows = _q(
                ProxyMetricEntry.server_id, func.count(ProxyMetricEntry.id),
            ).filter(ProxyMetricEntry.server_id.isnot(None),
                     ProxyMetricEntry.proxy_type == 'llm'
            ).group_by(ProxyMetricEntry.server_id).all()
            mcp_rows = _q(
                ProxyMetricEntry.server_id, func.count(ProxyMetricEntry.id),
                func.max(sname_col),
            ).filter(ProxyMetricEntry.server_id.isnot(None),
                     ProxyMetricEntry.proxy_type != 'llm'
            ).group_by(ProxyMetricEntry.server_id).all()

            llm_model_counts = {r[0]: int(r[1]) for r in llm_rows}
            mcp_server_counts = {r[0]: int(r[1]) for r in mcp_rows}
            mcp_server_names = {r[0]: r[2] for r in mcp_rows if r[2]}

            missing_mcp = [sid for sid in mcp_server_counts if sid not in mcp_server_names]
            if missing_mcp:
                from app.database.models.mcp_server import MCPServer
                for srv in db.query(MCPServer.id, MCPServer.name).filter(MCPServer.id.in_(missing_mcp)).all():
                    mcp_server_names[srv.id] = srv.name

            llm_model_names = {}
            if llm_model_counts:
                from app.database.models.llm_model import LLModel
                for m in db.query(LLModel.id, LLModel.name, LLModel.model_name).filter(LLModel.id.in_(list(llm_model_counts.keys()))).all():
                    llm_model_names[m.id] = {"name": m.name, "model_name": m.model_name}

            top_mcp_servers = [
                {"server_id": sid, "server_name": mcp_server_names.get(sid, sid), "request_count": cnt}
                for sid, cnt in sorted(mcp_server_counts.items(), key=lambda x: x[1], reverse=True)[:8]
            ]
            top_llm_models = [
                {
                    "model_id": mid,
                    "model_name": llm_model_names.get(mid, {}).get("model_name", mid),
                    "name": llm_model_names.get(mid, {}).get("name", mid),
                    "request_count": cnt,
                }
                for mid, cnt in sorted(llm_model_counts.items(), key=lambda x: x[1], reverse=True)[:8]
            ]

            # Usage patterns — grouped on the writer-stamped columns
            requests_by_hour = {
                int(h): int(c) for h, c in
                _q(ProxyMetricEntry.request_hour, func.count(ProxyMetricEntry.id))
                .group_by(ProxyMetricEntry.request_hour).all()
                if h is not None
            }
            user_type_distribution = {
                ut: int(c) for ut, c in
                _q(ProxyMetricEntry.end_user_type, func.count(ProxyMetricEntry.id))
                .group_by(ProxyMetricEntry.end_user_type).all()
            }
            peak_hour = max(requests_by_hour.items(), key=lambda x: x[1])[0] if requests_by_hour else None

            # LLM economics — same per-row rules as the loop it replaces
            # (see the total_tokens rationale above): a row counts when
            # any token/cost figure is non-zero; total = prompt+completion
            # when either is present, else the row-level total.
            has_llm = ProxyMetricEntry.llm_data.isnot(None)
            pt = func.coalesce(ProxyMetricEntry.llm_data['prompt_tokens'].as_integer(), 0)
            ct = func.coalesce(ProxyMetricEntry.llm_data['completion_tokens'].as_integer(), 0)
            tt = func.coalesce(ProxyMetricEntry.llm_data['total_tokens'].as_integer(), 0)
            cost_j = func.coalesce(ProxyMetricEntry.llm_data['estimated_cost_usd'].as_float(), 0.0)
            qualifies = and_(has_llm, or_(pt != 0, ct != 0, tt != 0, cost_j != 0.0))

            (llm_request_count, llm_prompt_tokens, llm_completion_tokens,
             llm_total_tokens, llm_total_cost) = _q(
                one(qualifies),
                func.coalesce(func.sum(case((has_llm, pt), else_=0)), 0),
                func.coalesce(func.sum(case((has_llm, ct), else_=0)), 0),
                func.coalesce(func.sum(case(
                    (and_(has_llm, or_(pt != 0, ct != 0)), pt + ct),
                    (has_llm, tt),
                    else_=0)), 0),
                func.coalesce(func.sum(case((has_llm, cost_j), else_=0.0)), 0.0),
            ).one()
            llm_request_count = int(llm_request_count or 0)
            llm_prompt_tokens = int(llm_prompt_tokens or 0)
            llm_completion_tokens = int(llm_completion_tokens or 0)
            llm_total_tokens = int(llm_total_tokens or 0)
            llm_total_cost = float(llm_total_cost or 0.0)

            llm_economics = {
                "prompt_tokens": llm_prompt_tokens,
                "completion_tokens": llm_completion_tokens,
                "total_tokens": llm_total_tokens,
                "estimated_cost_usd": round(llm_total_cost, 6),
                "llm_request_count": llm_request_count,
                "avg_cost_per_request": round(llm_total_cost / llm_request_count, 6) if llm_request_count > 0 else 0.0,
                "avg_tokens_per_request": round(llm_total_tokens / llm_request_count) if llm_request_count > 0 else 0,
            }

            requests_by_proxy_type = {
                (ptype or "unknown"): int(c) for ptype, c in
                _q(ProxyMetricEntry.proxy_type, func.count(ProxyMetricEntry.id))
                .group_by(ProxyMetricEntry.proxy_type).all()
            }

            # ── A2A aggregation ───────────────────────────────────────
            # A2A traffic only writes to ProxyLog (not ProxyMetricEntry —
            # see brutor-gateway-core/src/routes/a2a.rs), so we run a
            # parallel query here. Mirrors the per-group aggregation in
            # resource_groups.py so Mission Control and Group Usage stay
            # consistent. Iterating in Python matches the pattern of the
            # MCP/LLM blocks above; for tenants with very high A2A
            # volume, swap to a SQL-side GROUP BY later.
            # A2A/media live only on proxy_logs (governed inline traffic) — the
            # Observed plane has no such rows, so skip these aggregations there
            # rather than leaking governed A2A/media into the Observed view.
            observed_only = bool(plane) and plane.lower() == PLANE_OBSERVED
            if observed_only:
                a2a_capabilities, top_a2a_agents, a2a_overview = {}, [], {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "success_rate": 0.0,
                    "avg_duration_ms": 0.0,
                    "total_data_transferred_mb": 0.0,
                }
            else:
                a2a_capabilities, top_a2a_agents, a2a_overview = self._aggregate_a2a(
                    db, tenant_id, start_time, server_id, end_time=end_time
                )

            # ── Media aggregation ─────────────────────────────────────
            # Image / TTS / STT / video routes write typed billing-
            # unit counters onto proxy_logs (`media_*` columns). Token
            # columns are NULL for these rows, so `llm_economics`
            # above does not double-count them. We surface a separate
            # block so dashboards can chart per-unit usage (images,
            # chars, audio-seconds, video-seconds) — token-only
            # metrics couldn't represent that work at all.
            if observed_only:
                media_usage = {
                    "request_count": 0,
                    "estimated_cost_usd": 0.0,
                    "modes": {},
                }
            else:
                media_usage = self._aggregate_media_usage(
                    db, tenant_id, start_time, server_id, end_time=end_time
                )

            return {
                "overview": {
                    "total_requests": total_requests,
                    "successful_requests": successful_requests,
                    "failed_requests": failed_requests,
                    "success_rate": round(success_rate, 2),
                    "avg_duration_ms": round(avg_duration, 2),
                    "min_duration_ms": round(min_duration, 2),
                    "max_duration_ms": round(max_duration, 2),
                    "total_data_transferred_mb": round(total_data_mb, 2),
                    "avg_response_size_kb": round(avg_response_kb, 2),
                    "response_time_percentiles": {
                        "p50": round(percentiles["p50"], 2),
                        "p75": round(percentiles["p75"], 2),
                        "p90": round(percentiles["p90"], 2),
                        "p95": round(percentiles["p95"], 2),
                        "p99": round(percentiles["p99"], 2)
                    },
                    "session_reuse_rate": round(session_reuse_rate, 2),
                    "cache_hit_rate": round(cache_hit_rate, 2),
                    "requests_by_proxy_type": dict(requests_by_proxy_type),
                },
                "performance": {
                    "avg_duration_ms": round(avg_duration, 2),
                    "percentiles": percentiles
                },
                "errors": {
                    "error_rate": round(error_rate, 2),
                    "error_breakdown": dict(error_breakdown),
                    "top_errors": top_errors
                },
                "mcp_methods": mcp_methods_formatted,
                "mcp_tool_calls": mcp_tool_calls_formatted,
                "top_mcp_servers": top_mcp_servers,
                "top_llm_models": top_llm_models,
                "llm_economics": llm_economics,
                "media_usage": media_usage,
                "a2a_capabilities": a2a_capabilities,
                "top_a2a_agents": top_a2a_agents,
                "a2a_overview": a2a_overview,
                "usage_patterns": {
                    "peak_hour": peak_hour,
                    "requests_by_hour": dict(requests_by_hour),
                    "user_type_distribution": dict(user_type_distribution)
                }
            }

        except Exception as e:
            self.logger.error("Failed to get performance metrics", context={
                "tenant_id": tenant_id,
                "period_hours": period_hours,
                "error": str(e)
            })
            return {
                "overview": {},
                "performance": {},
                "errors": {},
                "mcp_methods": {},
                "mcp_tool_calls": {},
                "top_mcp_servers": [],
                "top_llm_models": [],
                "llm_economics": {},
                "media_usage": {
                    "request_count": 0,
                    "estimated_cost_usd": 0.0,
                    "modes": {},
                },
                "a2a_capabilities": {},
                "top_a2a_agents": [],
                "a2a_overview": {},
                "usage_patterns": {}
            }

    def _aggregate_a2a(
        self,
        db: Session,
        tenant_id: Optional[str],
        start_time: Optional[datetime],
        server_id: Optional[str] = None,
        end_time: Optional[datetime] = None,
    ) -> tuple:
        """Aggregate A2A traffic from proxy_logs for the period.

        A2A only writes to ProxyLog (not ProxyMetricEntry — see
        brutor-gateway-core/src/routes/a2a.rs), so this is a separate
        query off the richer log table. Mirrors the per-group rollup
        in resource_groups.py so Mission Control and Group Usage
        agree on counts.

        Returns:
            (a2a_capabilities, top_a2a_agents, a2a_overview)
        """
        from app.database.models.proxy_log import ProxyLog
        from app.database.models.agent_card import AgentCard

        a2a_filter = [
            ProxyLog.proxy_type == "a2a",
        ]
        if start_time is not None:  # None = all time (no lower bound)
            a2a_filter.append(ProxyLog.timestamp >= start_time)
        if end_time is not None:
            a2a_filter.append(ProxyLog.timestamp < end_time)
        if tenant_id:
            a2a_filter.append(ProxyLog.tenant_id == tenant_id)
        if server_id:
            # Mission Control's "selected server" filter doesn't really
            # apply to A2A (server_id there means MCP server) — but
            # honour it anyway so a curious admin filtering by an MCP
            # server doesn't see unrelated A2A traffic in the rollup.
            a2a_filter.append(ProxyLog.server_id == server_id)

        # Grouped SQL instead of loading every A2A proxy_log entity —
        # proxy_logs rows are ~90 columns each, so the old `.all()`
        # didn't scale past demo volumes.
        def _aq(*cols):
            q = db.query(*cols)
            for c in a2a_filter:
                q = q.filter(c)
            return q

        def _one(cond):
            return func.sum(case((cond, 1), else_=0))

        (total_requests, successful, success_dur_sum, total_bytes) = _aq(
            func.count(ProxyLog.id),
            _one(ProxyLog.success.is_(True)),
            func.coalesce(func.sum(case(
                (ProxyLog.success.is_(True), func.coalesce(ProxyLog.duration_ms, 0.0)),
                else_=0.0)), 0.0),
            func.coalesce(func.sum(
                func.coalesce(ProxyLog.request_size_bytes, 0)
                + func.coalesce(ProxyLog.response_size_bytes, 0)), 0),
        ).one()
        total_requests = int(total_requests or 0)

        if not total_requests:
            return ({}, [], {
                "total_requests": 0,
                "successful_requests": 0,
                "success_rate": 0.0,
                "avg_duration_ms": 0.0,
                "total_data_transferred_mb": 0.0,
            })

        successful = int(successful or 0)
        success_rate = (successful / total_requests * 100) if total_requests else 0.0
        avg_duration = (float(success_dur_sum) / successful) if successful else 0.0

        a2a_overview = {
            "total_requests": total_requests,
            "successful_requests": successful,
            "success_rate": round(success_rate, 2),
            "avg_duration_ms": round(avg_duration, 2),
            "total_data_transferred_mb": round(int(total_bytes or 0) / (1024 * 1024), 2),
        }

        # Top agent cards (server_id == agent_card.id)
        card_rows = _aq(
            func.coalesce(ProxyLog.server_id, 'unknown'),
            func.count(ProxyLog.id),
        ).group_by(func.coalesce(ProxyLog.server_id, 'unknown')).all()

        card_ids = sorted({r[0] for r in card_rows if r[0] and r[0] != 'unknown'})
        card_names: Dict[str, str] = {}
        if card_ids:
            for c in db.query(AgentCard.id, AgentCard.name).filter(
                AgentCard.id.in_(card_ids)
            ).all():
                card_names[c.id] = c.name

        top_a2a_agents = [
            {
                "card_id": cid,
                "card_name": card_names.get(cid, cid),
                "request_count": int(cnt),
            }
            for cid, cnt in sorted(card_rows, key=lambda r: r[1], reverse=True)[:8]
        ]

        # Per-capability rollup — "(unknown)" buckets untagged rows, as
        # before. card_name: representative card via MAX(server_id).
        cap_rows = _aq(
            func.coalesce(ProxyLog.mcp_capability_name, '(unknown)'),
            func.count(ProxyLog.id),
            func.coalesce(func.sum(func.coalesce(ProxyLog.duration_ms, 0.0)), 0.0),
            _one(ProxyLog.success.is_(True)),
            func.max(ProxyLog.server_id),
        ).group_by(func.coalesce(ProxyLog.mcp_capability_name, '(unknown)')).all()

        a2a_capabilities = {
            cap: {
                "count": int(cnt),
                "avg_duration": round((float(dur_sum) / int(cnt)) if int(cnt) else 0.0, 2),
                "success_rate": round((int(succ or 0) / int(cnt) * 100) if int(cnt) else 0.0, 2),
                "card_name": card_names.get(sid) if sid else None,
            }
            for cap, cnt, dur_sum, succ, sid in cap_rows
        }

        return a2a_capabilities, top_a2a_agents, a2a_overview

    def _aggregate_media_usage(
        self,
        db: Session,
        tenant_id: Optional[str],
        start_time: Optional[datetime],
        server_id: Optional[str] = None,
        end_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Aggregate LLM Media (image / TTS / STT / video) usage from
        proxy_logs for the period.

        Token-priced metrics live in `llm_economics`; media bills in
        its own units (images, characters, seconds) carried on the
        typed `media_*` columns. Splitting them keeps per-token
        analytics honest while still surfacing a per-mode roll-up
        for the admin's LLM Media panel. Mirrors the per-group
        rollup in resource_groups.py so Mission Control and Group
        Usage agree.

        Returns:
            ``{"request_count": int, "estimated_cost_usd": float,
              "modes": {<mode>: {"request_count", "cost_usd",
              <typed-counter>...}}}``
        """
        from app.database.models.proxy_log import ProxyLog
        from sqlalchemy import or_

        media_filter = [
            ProxyLog.proxy_type == "llm",
            or_(
                ProxyLog.media_images_generated.isnot(None),
                ProxyLog.media_audio_chars.isnot(None),
                ProxyLog.media_audio_seconds_input.isnot(None),
                ProxyLog.media_video_seconds_output.isnot(None),
            ),
        ]
        if start_time is not None:  # None = all time (no lower bound)
            media_filter.append(ProxyLog.timestamp >= start_time)
        if end_time is not None:
            media_filter.append(ProxyLog.timestamp < end_time)
        if tenant_id:
            media_filter.append(ProxyLog.tenant_id == tenant_id)
        if server_id:
            # Mission-control's selected-server filter doesn't really
            # apply to media (server_id refers to MCP). Honour it for
            # consistency with the A2A aggregator.
            media_filter.append(ProxyLog.server_id == server_id)

        # Grouped SQL — one aggregate row per endpoint instead of
        # loading every media proxy_log entity.
        mq = db.query(
            ProxyLog.endpoint,
            func.count(ProxyLog.id),
            func.coalesce(func.sum(func.coalesce(ProxyLog.llm_estimated_cost_usd, 0.0)), 0.0),
            func.coalesce(func.sum(func.coalesce(ProxyLog.media_images_generated, 0)), 0),
            func.coalesce(func.sum(func.coalesce(ProxyLog.media_audio_chars, 0)), 0),
            func.coalesce(func.sum(func.coalesce(ProxyLog.media_audio_seconds_input, 0.0)), 0.0),
            func.coalesce(func.sum(func.coalesce(ProxyLog.media_video_seconds_output, 0.0)), 0.0),
        )
        for c in media_filter:
            mq = mq.filter(c)
        media_rows = mq.group_by(ProxyLog.endpoint).all()

        if not media_rows:
            return {
                "request_count": 0,
                "estimated_cost_usd": 0.0,
                "modes": {},
            }

        # Endpoint → mode mapping is canonical; the four media routes
        # write distinct endpoint strings that drive the dashboard
        # bucketing. Totals count every media row (unknown endpoints
        # included, as before); modes skip unknowns rather than
        # misbucket them.
        endpoint_to_mode = {
            "/v1/proxy/llm/images/generations": "image_generation",
            "/v1/proxy/llm/audio/speech": "audio_speech",
            "/v1/proxy/llm/audio/transcriptions": "audio_transcription",
            "/v1/proxy/llm/video/generations": "video_generation",
        }

        modes: Dict[str, Dict[str, Any]] = {}
        request_count = 0
        total_cost = 0.0
        for ep, cnt, cost, imgs, chars, aud_s, vid_s in media_rows:
            request_count += int(cnt)
            total_cost += float(cost or 0.0)
            mode = endpoint_to_mode.get(ep or "")
            if not mode:
                continue
            slot = modes.setdefault(mode, {
                "request_count": 0,
                "cost_usd": 0.0,
                "images_generated": 0,
                "audio_chars": 0,
                "audio_seconds_input": 0.0,
                "video_seconds_output": 0.0,
            })
            slot["request_count"] += int(cnt)
            slot["cost_usd"] = round(slot["cost_usd"] + float(cost or 0.0), 6)
            slot["images_generated"] += int(imgs or 0)
            slot["audio_chars"] += int(chars or 0)
            slot["audio_seconds_input"] = round(slot["audio_seconds_input"] + float(aud_s or 0.0), 2)
            slot["video_seconds_output"] = round(slot["video_seconds_output"] + float(vid_s or 0.0), 2)

        return {
            "request_count": request_count,
            "estimated_cost_usd": round(total_cost, 6),
            "modes": modes,
        }

    # ========================================================================
    # ADMIN ENDPOINT METHODS (Required by metrics.py admin endpoints)
    # ========================================================================

    async def get_server_health_metrics(
        self,
        db: Session,
        server_id: str,
        tenant_id: str,
        hours: int = 24
    ) -> Dict[str, Any]:
        """
        Get health metrics for a specific server

        Used by: /admin/metrics/servers/{server_id}/health

        Args:
            db: Database session
            server_id: Server ID to analyze
            tenant_id: Tenant ID (for access control)
            hours: Hours to look back (default 24)

        Returns:
            Dictionary with health status, trends, and performance data
        """
        try:
            start_time = datetime.now(timezone.utc) - timedelta(hours=hours)

            # Query metrics for this server
            metrics = db.query(ProxyMetricEntry).filter(
                and_(
                    ProxyMetricEntry.server_id == server_id,
                    ProxyMetricEntry.tenant_id == tenant_id,
                    ProxyMetricEntry.timestamp >= start_time
                )
            ).all()

            if not metrics:
                return {
                    "health_status": "no_data",
                    "server_id": server_id,
                    "message": "No metrics data available for this server",
                    "total_requests": 0,
                    "success_rate": 0.0,
                    "avg_response_time_ms": 0.0
                }

            total_requests = len(metrics)
            successful_requests = sum(1 for m in metrics if m.success)
            failed_requests = total_requests - successful_requests
            success_rate = (successful_requests / total_requests * 100) if total_requests > 0 else 0.0

            # Calculate health status
            if success_rate >= 99.0:
                health_status = "healthy"
            elif success_rate >= 95.0:
                health_status = "degraded"
            elif success_rate >= 90.0:
                health_status = "warning"
            else:
                health_status = "critical"

            # Response time analysis
            durations = [m.total_duration_ms for m in metrics if m.total_duration_ms]
            avg_duration = sum(durations) / len(durations) if durations else 0.0
            sorted_durations = sorted(durations)
            p95_duration = sorted_durations[int(len(sorted_durations) * 0.95)] if sorted_durations else 0.0
            p99_duration = sorted_durations[int(len(sorted_durations) * 0.99)] if sorted_durations else 0.0

            # Error breakdown
            error_breakdown = defaultdict(int)
            for metric in metrics:
                if not metric.success and metric.error_category:
                    error_breakdown[metric.error_category] += 1

            # Trend analysis (compare first half vs second half)
            midpoint = len(metrics) // 2
            first_half = metrics[:midpoint]
            second_half = metrics[midpoint:]

            first_half_success_rate = (sum(1 for m in first_half if m.success) / len(first_half) * 100) if first_half else 0.0
            second_half_success_rate = (sum(1 for m in second_half if m.success) / len(second_half) * 100) if second_half else 0.0

            trend = "stable"
            if second_half_success_rate > first_half_success_rate + 2:
                trend = "improving"
            elif second_half_success_rate < first_half_success_rate - 2:
                trend = "degrading"

            return {
                "health_status": health_status,
                "server_id": server_id,
                "period_hours": hours,
                "overview": {
                    "total_requests": total_requests,
                    "successful_requests": successful_requests,
                    "failed_requests": failed_requests,
                    "success_rate": round(success_rate, 2),
                    "error_rate": round((failed_requests / total_requests * 100) if total_requests > 0 else 0.0, 2)
                },
                "performance": {
                    "avg_response_time_ms": round(avg_duration, 2),
                    "p95_response_time_ms": round(p95_duration, 2),
                    "p99_response_time_ms": round(p99_duration, 2)
                },
                "errors": {
                    "error_breakdown": dict(error_breakdown),
                    "total_errors": failed_requests
                },
                "trend": {
                    "status": trend,
                    "first_half_success_rate": round(first_half_success_rate, 2),
                    "second_half_success_rate": round(second_half_success_rate, 2)
                }
            }

        except Exception as e:
            self.logger.error("Failed to get server health metrics", context={
                "server_id": server_id,
                "tenant_id": tenant_id,
                "error": str(e)
            })
            return {
                "health_status": "error",
                "server_id": server_id,
                "message": "Failed to retrieve health metrics",
                "error": str(e)
            }

    async def get_dashboard_metrics(
        self,
        db: Session,
        tenant_id: str
    ) -> Dict[str, Any]:
        """
        Get comprehensive dashboard metrics (combines multiple views)

        Used by: /admin/metrics/dashboard

        Args:
            db: Database session
            tenant_id: Tenant ID

        Returns:
            Dictionary with overview, real-time, performance, and alerts
        """
        try:
            # Get real-time metrics (last 5 minutes)
            real_time = await self.get_real_time_metrics(
                db=db,
                tenant_id=tenant_id,
                last_minutes=5
            )

            # Get 1-hour performance
            performance_1h = await self.get_performance_metrics(
                db=db,
                tenant_id=tenant_id,
                period_hours=1
            )

            # Get 24-hour performance
            performance_24h = await self.get_performance_metrics(
                db=db,
                tenant_id=tenant_id,
                period_hours=24
            )

            # Get statistics
            stats = await self.get_statistics(
                db=db,
                tenant_id=tenant_id,
                hours=24
            )

            # Generate alerts based on metrics
            alerts = []

            # Check error spike
            if real_time.get("recent_activity", {}).get("error_spike", False):
                alerts.append({
                    "severity": "warning",
                    "title": "Error Spike Detected",
                    "message": "Error rate >20% in the last minute",
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })

            # Check success rate
            success_rate_24h = performance_24h.get("overview", {}).get("success_rate", 100.0)
            if success_rate_24h < 95.0:
                alerts.append({
                    "severity": "critical" if success_rate_24h < 90.0 else "warning",
                    "title": "Low Success Rate",
                    "message": f"24h success rate at {success_rate_24h:.1f}%",
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })

            # Check high latency
            p95_latency = performance_24h.get("overview", {}).get("response_time_percentiles", {}).get("p95", 0)
            if p95_latency > 1000:  # >1 second at P95
                alerts.append({
                    "severity": "warning",
                    "title": "High Latency",
                    "message": f"P95 latency at {p95_latency:.0f}ms",
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })

            return {
                "overview": {
                    "total_requests_24h": performance_24h.get("overview", {}).get("total_requests", 0),
                    "success_rate_24h": success_rate_24h,
                    "avg_response_time_24h": performance_24h.get("overview", {}).get("avg_duration_ms", 0),
                    "requests_per_minute": real_time.get("real_time", {}).get("requests_per_minute", 0),
                    "active_sessions": real_time.get("real_time", {}).get("active_sessions", 0),
                    "active_servers": real_time.get("real_time", {}).get("active_servers", 0)
                },
                "real_time": real_time,
                "performance_1h": performance_1h,
                "performance_24h": performance_24h,
                "statistics": stats,
                "alerts": alerts,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

        except Exception as e:
            self.logger.error("Failed to get dashboard metrics", context={
                "tenant_id": tenant_id,
                "error": str(e)
            })
            return {
                "overview": {},
                "real_time": {},
                "performance_1h": {},
                "performance_24h": {},
                "statistics": {},
                "alerts": [],
                "error": str(e)
            }

    async def get_usage_metrics(
        self,
        db: Session,
        tenant_id: str,
        start_time: datetime,
        end_time: datetime,
        group_by: str = "day"
    ) -> Dict[str, Any]:
        """
        Get usage metrics for billing/quota tracking

        Used by: /admin/metrics/usage

        Args:
            db: Database session
            tenant_id: Tenant ID
            start_time: Start datetime
            end_time: End datetime
            group_by: Grouping interval (hour, day, week, month)

        Returns:
            Dictionary with usage data grouped by time interval
        """
        try:
            # Query metrics for period
            metrics = db.query(ProxyMetricEntry).filter(
                and_(
                    ProxyMetricEntry.tenant_id == tenant_id,
                    ProxyMetricEntry.timestamp >= start_time,
                    ProxyMetricEntry.timestamp <= end_time
                )
            ).all()

            if not metrics:
                return {
                    "total_requests": 0,
                    "total_data_mb": 0.0,
                    "estimated_cost_usd": 0.0,
                    "by_proxy_type": {},
                    "time_series": [],
                    "period": {
                        "start": start_time.isoformat(),
                        "end": end_time.isoformat(),
                        "group_by": group_by
                    }
                }

            # Calculate totals
            total_requests = len(metrics)
            total_request_bytes = sum(m.request_size_bytes for m in metrics if m.request_size_bytes)
            total_response_bytes = sum(m.response_size_bytes for m in metrics if m.response_size_bytes)
            total_data_mb = (total_request_bytes + total_response_bytes) / (1024 * 1024)

            # Calculate cost (for LLM requests)
            estimated_cost = 0.0
            for metric in metrics:
                if metric.llm_data and metric.llm_data.get('estimated_cost_usd'):
                    estimated_cost += metric.llm_data['estimated_cost_usd']

            # Breakdown by proxy type
            by_proxy_type = defaultdict(lambda: {
                "count": 0,
                "success_count": 0,
                "data_mb": 0.0,
                "cost_usd": 0.0
            })

            for metric in metrics:
                ptype = metric.proxy_type
                by_proxy_type[ptype]["count"] += 1
                if metric.success:
                    by_proxy_type[ptype]["success_count"] += 1

                req_bytes = metric.request_size_bytes or 0
                resp_bytes = metric.response_size_bytes or 0
                by_proxy_type[ptype]["data_mb"] += (req_bytes + resp_bytes) / (1024 * 1024)

                if metric.llm_data and metric.llm_data.get('estimated_cost_usd'):
                    by_proxy_type[ptype]["cost_usd"] += metric.llm_data['estimated_cost_usd']

            # Group by time period
            time_series = defaultdict(lambda: {
                "requests": 0,
                "success_count": 0,
                "data_mb": 0.0,
                "cost_usd": 0.0
            })

            for metric in metrics:
                # Determine bucket based on group_by
                if group_by == "hour":
                    bucket = metric.timestamp.replace(minute=0, second=0, microsecond=0)
                elif group_by == "day":
                    bucket = metric.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
                elif group_by == "week":
                    # Start of week (Monday)
                    days_since_monday = metric.timestamp.weekday()
                    bucket = (metric.timestamp - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
                else:  # month
                    bucket = metric.timestamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

                bucket_key = bucket.isoformat()
                time_series[bucket_key]["requests"] += 1
                if metric.success:
                    time_series[bucket_key]["success_count"] += 1

                req_bytes = metric.request_size_bytes or 0
                resp_bytes = metric.response_size_bytes or 0
                time_series[bucket_key]["data_mb"] += (req_bytes + resp_bytes) / (1024 * 1024)

                if metric.llm_data and metric.llm_data.get('estimated_cost_usd'):
                    time_series[bucket_key]["cost_usd"] += metric.llm_data['estimated_cost_usd']

            # Convert to list and sort
            time_series_list = [
                {
                    "timestamp": timestamp,
                    "requests": data["requests"],
                    "success_rate": (data["success_count"] / data["requests"] * 100) if data["requests"] > 0 else 0.0,
                    "data_mb": round(data["data_mb"], 2),
                    "cost_usd": round(data["cost_usd"], 4)
                }
                for timestamp, data in sorted(time_series.items())
            ]

            return {
                "total_requests": total_requests,
                "total_data_mb": round(total_data_mb, 2),
                "estimated_cost_usd": round(estimated_cost, 4),
                "by_proxy_type": {
                    ptype: {
                        "count": data["count"],
                        "success_rate": (data["success_count"] / data["count"] * 100) if data["count"] > 0 else 0.0,
                        "data_mb": round(data["data_mb"], 2),
                        "cost_usd": round(data["cost_usd"], 4)
                    }
                    for ptype, data in by_proxy_type.items()
                },
                "time_series": time_series_list,
                "period": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat(),
                    "group_by": group_by
                }
            }

        except Exception as e:
            self.logger.error("Failed to get usage metrics", context={
                "tenant_id": tenant_id,
                "error": str(e)
            })
            return {
                "total_requests": 0,
                "total_data_mb": 0.0,
                "estimated_cost_usd": 0.0,
                "by_proxy_type": {},
                "time_series": [],
                "error": str(e)
            }