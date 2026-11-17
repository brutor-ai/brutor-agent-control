# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
A2A Delegation Graph service (Mission Control).

Reads the proxy_logs table, filtered to `proxy_type='a2a'`, and
reconstructs delegation graphs that span many steps.

Three shapes of query:

  * `list_graphs`  — summary row per root_task_id (aggregation over steps).
  * `get_graph`    — every step of a single root_task_id (full tree).
  * `compute_stats` — counters + percentiles over a window.

All queries are tenant-scoped. System admins have to supply a
tenant_id; tenant admins are pinned to their own tenant at the
endpoint layer and pass that in as `tenant_id`.

The service keeps Python-level logic out of the hot path — it returns
dicts shaped for direct consumption by the Pydantic response models,
so the endpoint is a thin adapter. This also keeps the service unit-
testable without the FastAPI router.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, asc, case, desc, func
from sqlalchemy.orm import Session

from app.core.logging_system import BrutorLogger
from app.database.models.agent_card import AgentCard
from app.database.models.agent_identity import AgentIdentity
from app.database.models.proxy_log import ProxyLog


A2A_PROXY_TYPE = "a2a"
SUBTYPE_INBOUND = "inbound"
SUBTYPE_OUTBOUND = "outbound"


class A2ADelegationGraphService:
    def __init__(self, logger: BrutorLogger):
        self.logger = logger

    # =========================================================================
    # List — one summary row per root_task_id
    # =========================================================================

    def list_graphs(
        self,
        db: Session,
        *,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        success: Optional[bool] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Return (summaries, total_count). `total_count` ignores pagination.

        Pagination + filtering is applied to the outer aggregation, so
        the caller can page through large result sets without loading
        every step. We still touch every row within the window to
        aggregate, which is acceptable given the partial index on
        `root_task_id` makes the subquery sparse.
        """
        base_q = db.query(ProxyLog).filter(
            ProxyLog.tenant_id == tenant_id,
            ProxyLog.proxy_type == A2A_PROXY_TYPE,
            ProxyLog.root_task_id.isnot(None),
        )
        if since is not None:
            base_q = base_q.filter(ProxyLog.timestamp >= since)
        if until is not None:
            base_q = base_q.filter(ProxyLog.timestamp <= until)

        # Group-by root_task_id with aggregates. Fetch everything into
        # Python once for post-filtering (success/fail combined across
        # steps of a graph) — the result set is bounded by the page
        # window and we want consistent aggregate semantics regardless
        # of DB quirks around ordering of MIN/MAX with NULLs.
        rows = base_q.all()

        names = self._resolve_names(db, tenant_id, rows)

        by_root: Dict[str, List[ProxyLog]] = {}
        for r in rows:
            by_root.setdefault(r.root_task_id, []).append(r)

        summaries: List[Dict[str, Any]] = []
        for root_id, steps in by_root.items():
            summary = self._summarise_steps(root_id, tenant_id, steps, names)
            if success is not None and summary["overall_success"] != success:
                continue
            summaries.append(summary)

        # Sort newest-first so pagination is deterministic under inserts.
        summaries.sort(key=lambda s: s["started_at"], reverse=True)
        total = len(summaries)
        page = summaries[offset : offset + limit]
        systems = self._systems_for_roots(db, tenant_id, [p["root_task_id"] for p in page])
        for summary in page:
            summary["systems"] = systems.get(summary["root_task_id"], [])
        return page, total

    @staticmethod
    def _systems_for_roots(
        db: Session, tenant_id: str, root_task_ids: List[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """The AI Systems each graph's run involved, so the
        Delegation Graphs page can link a graph to its systems.

        `root`: the run's attributed system (ai_system_runs); `segment`: every
        system the run passed through (ai_system_run_segments). Three
        batched queries for the whole page — never one per graph.
        """
        from app.database.models.ai_system_run import AISystemRun
        from app.database.models.ai_system_run_segment import AISystemRunSegment
        from app.database.models.resource_group import ResourceGroup

        if not root_task_ids:
            return {}
        pairs: List[Tuple[str, str, str]] = []
        for root, sid in db.query(AISystemRun.root_task_id, AISystemRun.ai_system_id).filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.root_task_id.in_(root_task_ids),
            AISystemRun.ai_system_id.isnot(None),
        ):
            pairs.append((root, sid, "root"))
        for root, sid in (
            db.query(AISystemRunSegment.root_task_id, AISystemRunSegment.segment_system_id)
            .filter(
                AISystemRunSegment.tenant_id == tenant_id,
                AISystemRunSegment.root_task_id.in_(root_task_ids),
            )
            .order_by(AISystemRunSegment.min_depth, AISystemRunSegment.first_action_at)
        ):
            pairs.append((root, sid, "segment"))
        if not pairs:
            return {}
        names = {
            gid: (dn or n)
            for gid, n, dn in db.query(
                ResourceGroup.id, ResourceGroup.name, ResourceGroup.display_name
            ).filter(
                ResourceGroup.tenant_id == tenant_id,
                ResourceGroup.id.in_({sid for _, sid, _ in pairs}),
            )
        }
        out: Dict[str, List[Dict[str, Any]]] = {}
        seen: set = set()
        for root, sid, role in pairs:  # roots first, then segments by depth
            if (root, sid) in seen:
                continue
            seen.add((root, sid))
            out.setdefault(root, []).append({"id": sid, "name": names.get(sid), "role": role})
        return out

    # =========================================================================
    # Detail — every step of one graph
    # =========================================================================

    def get_graph(
        self,
        db: Session,
        *,
        tenant_id: str,
        root_task_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the full tree for a root, or None if no steps match."""
        rows: List[ProxyLog] = (
            db.query(ProxyLog)
            .filter(
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.proxy_type == A2A_PROXY_TYPE,
                ProxyLog.root_task_id == root_task_id,
            )
            .order_by(asc(ProxyLog.delegation_depth), asc(ProxyLog.timestamp))
            .all()
        )
        if not rows:
            return None

        names = self._resolve_names(db, tenant_id, rows)
        steps = [self._step_row_to_dict(r, names) for r in rows]
        max_depth = max((r.delegation_depth or 0) for r in rows)
        return {
            "root_task_id": root_task_id,
            "tenant_id": tenant_id,
            "steps": steps,
            "total_steps": len(steps),
            "max_depth": max_depth,
        }

    # =========================================================================
    # Stats — counters + percentiles over a window
    # =========================================================================

    def compute_stats(
        self,
        db: Session,
        *,
        tenant_id: str,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        top_n_cards: int = 5,
    ) -> Dict[str, Any]:
        """Aggregate stats over the window.

        The counters, the graph count and the top-card ranking are all
        computed by the database. They used to be computed in Python over
        `q.all()`, and the docstring defended the Python percentile pass
        as "acceptable at admin-dashboard volumes" — which assumed a
        window. `since` and `until` both default to None at the route, so
        the *default* request has no window at all: on an estate with
        months of A2A traffic that was a full scan of `proxy_logs`
        materialised as ORM entities in the control plane's heap, in the
        same process that runs the retention sweeper and the audit export.

        Percentiles still use linear interpolation (`_percentile`'s
        definition, which matches numpy's default) rather than
        Postgres-specific `percentile_cont`, so the function stays
        portable to the SQLite the tests run on. They are now resolved
        with two `ORDER BY … OFFSET k LIMIT 2` probes against the sorted
        durations instead of sorting the whole column in Python — exact
        for the same definition, and constant memory.
        """
        base = [
            ProxyLog.tenant_id == tenant_id,
            ProxyLog.proxy_type == A2A_PROXY_TYPE,
        ]
        if since is not None:
            base.append(ProxyLog.timestamp >= since)
        if until is not None:
            base.append(ProxyLog.timestamp <= until)

        agg = db.query(
            func.count().label("total_steps"),
            func.count(func.distinct(ProxyLog.root_task_id)).label("total_graphs"),
            func.sum(
                case((ProxyLog.proxy_subtype == SUBTYPE_INBOUND, 1), else_=0)
            ).label("inbound_steps"),
            func.sum(
                case((ProxyLog.proxy_subtype == SUBTYPE_OUTBOUND, 1), else_=0)
            ).label("outbound_steps"),
            func.sum(
                case((ProxyLog.success.is_(True), 1), else_=0)
            ).label("success_steps"),
            func.max(ProxyLog.delegation_depth).label("max_depth"),
            func.avg(ProxyLog.duration_ms).label("avg_duration"),
            func.count(ProxyLog.duration_ms).label("duration_count"),
        ).filter(*base).one()

        total_steps = int(agg.total_steps or 0)

        if not total_steps:
            return {
                "tenant_id": tenant_id,
                "window_start": since,
                "window_end": until,
                "total_graphs": 0,
                "total_steps": 0,
                "inbound_steps": 0,
                "outbound_steps": 0,
                "success_steps": 0,
                "failure_steps": 0,
                "success_rate": None,
                "avg_steps_per_graph": None,
                "max_depth_observed": None,
                "avg_duration_ms": None,
                "p50_duration_ms": None,
                "p95_duration_ms": None,
                "top_cards": [],
            }

        inbound_steps = int(agg.inbound_steps or 0)
        outbound_steps = int(agg.outbound_steps or 0)
        success_steps = int(agg.success_steps or 0)
        failure_steps = total_steps - success_steps
        total_graphs = int(agg.total_graphs or 0)
        max_depth_observed = (
            int(agg.max_depth) if agg.max_depth is not None else None
        )
        duration_count = int(agg.duration_count or 0)
        avg_duration_ms = (
            float(agg.avg_duration) if agg.avg_duration is not None else None
        )
        p50 = self._duration_percentile(db, base, 0.50, duration_count)
        p95 = self._duration_percentile(db, base, 0.95, duration_count)

        # card_id is stored in server_id for A2A. The secondary sort on
        # server_id is not cosmetic: with a Python dict the tie order was
        # insertion order, which at least made repeated calls agree.
        # GROUP BY has no such guarantee, so ties are broken explicitly
        # or the "top 5" would reshuffle between page loads.
        top_cards = [
            {"card_id": card_id, "count": int(n)}
            for card_id, n in (
                db.query(ProxyLog.server_id, func.count().label("n"))
                .filter(*base, ProxyLog.server_id.isnot(None))
                .group_by(ProxyLog.server_id)
                .order_by(desc(func.count()), asc(ProxyLog.server_id))
                .limit(top_n_cards)
                .all()
            )
        ]

        return {
            "tenant_id": tenant_id,
            "window_start": since,
            "window_end": until,
            "total_graphs": total_graphs,
            "total_steps": total_steps,
            "inbound_steps": inbound_steps,
            "outbound_steps": outbound_steps,
            "success_steps": success_steps,
            "failure_steps": failure_steps,
            "success_rate": (success_steps / total_steps) if total_steps else None,
            "avg_steps_per_graph": (
                total_steps / total_graphs if total_graphs else None
            ),
            "max_depth_observed": max_depth_observed,
            "avg_duration_ms": avg_duration_ms,
            "p50_duration_ms": p50,
            "p95_duration_ms": p95,
            "top_cards": top_cards,
        }

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _duration_percentile(
        self, db: Session, base: List[Any], q: float, n: int
    ) -> Optional[float]:
        """`_percentile`'s linear interpolation, resolved in the database.

        Same result as sorting every `duration_ms` in the window and
        indexing it, but the only rows that leave the database are the two
        that straddle the requested rank. `n` is the count of non-null
        durations, already known from the aggregate above.
        """
        if n <= 0:
            return None
        pos = max(0.0, min(1.0, q)) * (n - 1)
        lower = int(pos)
        frac = pos - lower
        rows = (
            db.query(ProxyLog.duration_ms)
            .filter(*base, ProxyLog.duration_ms.isnot(None))
            .order_by(asc(ProxyLog.duration_ms))
            .offset(lower)
            .limit(2)
            .all()
        )
        if not rows:
            return None
        lo = float(rows[0][0])
        hi = float(rows[1][0]) if len(rows) > 1 else lo
        return lo + (hi - lo) * frac

    def _summarise_steps(
        self, root_id: str, tenant_id: str, steps: List[ProxyLog],
        names: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Collapse a graph's steps into a summary dict."""
        names = names or {"agents": {}, "cards": {}}
        steps_sorted = sorted(steps, key=lambda r: (r.delegation_depth or 0, r.timestamp))
        # The root step is the one with no parent (the chain's entry point) — not
        # merely the first by (depth, timestamp), since a child onward step can
        # share the parent's recorded depth and sort ahead of it.
        first = next((r for r in steps_sorted if not r.parent_task_id), steps_sorted[0])
        last = steps_sorted[-1]
        started_at = min(r.timestamp for r in steps_sorted)
        ended_at = max(r.timestamp for r in steps_sorted)

        duration_ms: Optional[float]
        if started_at and ended_at and ended_at > started_at:
            duration_ms = (ended_at - started_at).total_seconds() * 1000.0
        else:
            # Single-step graph — fall back to that step's own duration.
            duration_ms = first.duration_ms

        max_depth = max((r.delegation_depth or 0) for r in steps_sorted)
        inbound = sum(1 for r in steps_sorted if r.proxy_subtype == SUBTYPE_INBOUND)
        outbound = sum(1 for r in steps_sorted if r.proxy_subtype == SUBTYPE_OUTBOUND)
        success_steps = sum(1 for r in steps_sorted if r.success)
        failure_steps = len(steps_sorted) - success_steps
        overall_success = all(r.success for r in steps_sorted)

        return {
            "root_task_id": root_id,
            "tenant_id": tenant_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "total_steps": len(steps_sorted),
            "max_depth": max_depth,
            "inbound_steps": inbound,
            "outbound_steps": outbound,
            "success_steps": success_steps,
            "failure_steps": failure_steps,
            "overall_success": overall_success,
            "root_endpoint": first.endpoint,
            "root_card_id": first.server_id,
            "root_card_name": names["cards"].get(first.server_id),
            "root_actor_agent_id": first.actor_agent_id,
            "root_actor_agent_name": names["agents"].get(first.actor_agent_id),
            "root_correlation_id": first.correlation_id,
        }

    def _resolve_names(
        self, db: Session, tenant_id: str, rows: List[ProxyLog]
    ) -> Dict[str, Dict[str, str]]:
        """Batch-resolve the caller (agent identity) and callee (agent card)
        ids on a set of steps to their display names, so the graph can read
        'who → whom' instead of opaque ULIDs."""
        agent_ids = {r.actor_agent_id for r in rows if r.actor_agent_id}
        card_ids = {r.server_id for r in rows if r.server_id}
        agents: Dict[str, str] = {}
        cards: Dict[str, str] = {}
        if agent_ids:
            for a in (
                db.query(AgentIdentity.id, AgentIdentity.name)
                .filter(AgentIdentity.tenant_id == tenant_id,
                        AgentIdentity.id.in_(agent_ids))
                .all()
            ):
                agents[a.id] = a.name
        if card_ids:
            for c in (
                db.query(AgentCard.id, AgentCard.name)
                .filter(AgentCard.tenant_id == tenant_id,
                        AgentCard.id.in_(card_ids))
                .all()
            ):
                cards[c.id] = c.name
        return {"agents": agents, "cards": cards}

    def _step_row_to_dict(
        self, row: ProxyLog, names: Optional[Dict[str, Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        names = names or {"agents": {}, "cards": {}}
        return {
            "current_task_id": row.current_task_id,
            "parent_task_id": row.parent_task_id,
            "depth": row.delegation_depth or 0,
            "direction": row.proxy_subtype,
            "proxy_log_id": row.id,
            "timestamp": row.timestamp,
            "duration_ms": row.duration_ms,
            "http_status_code": row.http_status_code,
            "success": row.success,
            "error_message": row.error_message,
            "card_id": row.server_id,
            "endpoint": row.endpoint,
            "target_url": row.target_url,
            "correlation_id": row.correlation_id,
            # Who → whom.
            "actor_agent_id": row.actor_agent_id,
            "actor_agent_name": names["agents"].get(row.actor_agent_id),
            "subject_id": row.subject_id,
            "subject_kind": row.subject_kind,
            "card_name": names["cards"].get(row.server_id),
        }


# -----------------------------------------------------------------------------
# Pure helper — exposed for unit tests.
# -----------------------------------------------------------------------------


def _percentile(sorted_values: List[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile on a pre-sorted list.

    Matches numpy's default (linear) method so operators reading our
    p50 / p95 on a Grafana board won't need to mentally adjust.
    Returns None on an empty input.
    """
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    if q <= 0:
        return float(sorted_values[0])
    if q >= 1:
        return float(sorted_values[-1])
    pos = q * (len(sorted_values) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = pos - lower
    return float(sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * frac)
