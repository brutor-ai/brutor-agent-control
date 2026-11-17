# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Multi-step agent trace service.

Reads `proxy_logs`, reconstructs trace trees that span any
combination of LLM / MCP / A2A / Skill calls keyed by the
`root_task_id` column. Two query shapes:

  * `list_traces` — one summary row per `root_task_id` in window.
  * `get_trace`   — every span of a single root, ordered for
                    timeline rendering.

This service is a generalisation of
`A2ADelegationGraphService` from `services/a2a/delegation_graph_service.py`.
That one filters on `proxy_type='a2a'`; this one does not. The
two coexist because the A2A view has its own UI vocabulary
("steps", "edges", "graph") that ships in the Mission Control
A2A tab; the cross-proxy trace timeline ships in the new
proxy-log trace view.

All queries are tenant-scoped. System admins must pass
`tenant_id`; tenant admins are pinned at the endpoint layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import asc
from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.proxy_log import ProxyLog


# Audit values written by `services::call_chain` / `proxy_logger::apply_call_chain`
# in the Rust runtime. `KNOWN_ROOT_KINDS` is used by the API for filter
# validation (proxy_logs.py); `KNOWN_CONTINUITY` is not wired to a filter yet
# — see its own note.
KNOWN_ROOT_KINDS = {
    "llm_user",
    "agent_invocation",
    "skill_execution",
    "scheduled_job",
    "external_api",
}

# XREPO-13 — every `trace_continuity` the Rust runtime writes.
#
# Source of truth is `TraceContinuity::as_str` in
# brutor-gateway-core/src/services/a2a_delegation.rs, plus the OTel inlet's
# `CONTINUITY_EXTERNAL` (services/otel_ingest.rs). This set had four of the
# six and was described as "used by the API for filter validation" while
# nothing imported it — so wiring it up as the comment invited would have
# rejected `client_asserted` and `external` as invalid filters on rows the
# platform writes routinely. It is completed here rather than deleted so the
# vocabulary stays pinned on this side of the wire, the way ProxyType and
# ProxySubtype are in app/constants/proxy.py; it is still not wired to a
# query parameter, and the comment no longer claims it is.
KNOWN_CONTINUITY = {
    "minted",
    "verified",
    "client_dropped",
    "sig_failed",
    "client_asserted",
    "external",
}


class TraceService:
    """Read-side for the trace API. No mutation paths — audit rows
    are written by the Rust runtime."""

    def __init__(self, logger: BrutorLogger):
        self.logger = logger

    # ------------------------------------------------------------------
    # List — one summary row per root_task_id in window
    # ------------------------------------------------------------------

    def list_traces(
        self,
        db: Session,
        *,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        root_kind: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Return (summaries, total). `total` ignores pagination.

        Each summary carries enough info for the trace-list UI to
        render a clickable row without a follow-up fetch:
        root_task_id, started_at, span_count, root_kind, originating
        user, overall outcome, max depth, proxy-type set."""
        q = db.query(ProxyLog).filter(
            ProxyLog.tenant_id == tenant_id,
            ProxyLog.root_task_id.isnot(None),
        )
        if since is not None:
            q = q.filter(ProxyLog.timestamp >= since)
        if until is not None:
            q = q.filter(ProxyLog.timestamp <= until)
        if root_kind is not None:
            q = q.filter(ProxyLog.trace_root_kind == root_kind)
        if user_id is not None:
            q = q.filter(ProxyLog.gateway_user_id == user_id)

        rows: List[ProxyLog] = q.all()
        by_root: Dict[str, List[ProxyLog]] = {}
        for r in rows:
            by_root.setdefault(r.root_task_id, []).append(r)

        summaries = [
            self._summarise(root_id, steps) for root_id, steps in by_root.items()
        ]
        summaries.sort(key=lambda s: s["started_at"], reverse=True)
        total = len(summaries)
        page = summaries[offset : offset + limit]
        return page, total

    # ------------------------------------------------------------------
    # Detail — every span of one trace
    # ------------------------------------------------------------------

    def get_trace(
        self,
        db: Session,
        *,
        tenant_id: str,
        root_task_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Full ordered span list. Returns None if no spans match.

        Ordering: `delegation_depth ASC, timestamp ASC` so the
        timeline renders parents before children even when child
        spans completed first."""
        rows: List[ProxyLog] = (
            db.query(ProxyLog)
            .filter(
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.root_task_id == root_task_id,
            )
            .order_by(asc(ProxyLog.delegation_depth), asc(ProxyLog.timestamp))
            .all()
        )
        if not rows:
            return None

        spans = [self._span_row_to_dict(r) for r in rows]
        max_depth = max((r.delegation_depth or 0) for r in rows)
        proxy_types = sorted({r.proxy_type for r in rows if r.proxy_type})
        return {
            "root_task_id": root_task_id,
            "tenant_id": tenant_id,
            "spans": spans,
            "total_spans": len(spans),
            "max_depth": max_depth,
            "proxy_types": proxy_types,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _summarise(root_id: str, spans: List[ProxyLog]) -> Dict[str, Any]:
        ordered = sorted(
            spans,
            key=lambda r: (r.delegation_depth or 0, r.timestamp),
        )
        started_at = min(r.timestamp for r in ordered)
        ended_at = max(r.timestamp for r in ordered)
        # Overall success = no span failed. A trace with mixed
        # outcomes counts as failed because at least one span
        # didn't complete.
        any_failed = any(r.success is False for r in ordered)
        # Take root-kind from the root span if present, otherwise
        # any non-null value the engine wrote.
        root_kind: Optional[str] = None
        for r in ordered:
            if r.trace_root_kind:
                root_kind = r.trace_root_kind
                break
        # Take the originating user from the same row.
        user_id: Optional[str] = None
        for r in ordered:
            if r.gateway_user_id:
                user_id = r.gateway_user_id
                break
        # Continuity hint: prefer "sig_failed" if any span failed
        # verification, then "client_dropped", else "verified" or
        # "minted". The audit UI uses this to flag suspect traces.
        continuity_rank = {
            "sig_failed": 0,
            "client_dropped": 1,
            "minted": 2,
            "verified": 3,
        }
        worst_continuity = None
        for r in ordered:
            if r.trace_continuity and (
                worst_continuity is None
                or continuity_rank.get(r.trace_continuity, 99)
                < continuity_rank.get(worst_continuity, 99)
            ):
                worst_continuity = r.trace_continuity
        return {
            "root_task_id": root_id,
            "tenant_id": ordered[0].tenant_id,
            "started_at": started_at.isoformat() if started_at else None,
            "ended_at": ended_at.isoformat() if ended_at else None,
            "span_count": len(ordered),
            "max_depth": max((r.delegation_depth or 0) for r in ordered),
            "overall_success": not any_failed,
            "root_kind": root_kind,
            "gateway_user_id": user_id,
            "worst_continuity": worst_continuity,
            "proxy_types": sorted({r.proxy_type for r in ordered if r.proxy_type}),
        }

    @staticmethod
    def _span_row_to_dict(r: ProxyLog) -> Dict[str, Any]:
        return {
            "id": r.id,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "proxy_type": r.proxy_type,
            "proxy_subtype": r.proxy_subtype,
            "request_id": r.request_id,
            "parent_task_id": r.parent_task_id,
            "current_task_id": r.current_task_id,
            "delegation_depth": r.delegation_depth,
            "trace_continuity": r.trace_continuity,
            "trace_root_kind": r.trace_root_kind,
            "endpoint": r.endpoint,
            "http_method": r.http_method,
            "http_status_code": r.http_status_code,
            "duration_ms": r.duration_ms,
            "success": r.success,
            "error_message": r.error_message,
            "gateway_user_id": r.gateway_user_id,
            "idp_user_email": r.idp_user_email,
            "server_id": r.server_id,
            "mcp_capability_name": r.mcp_capability_name,
            "llm_model_used": r.llm_model_used,
            "llm_provider": r.llm_provider,
        }
