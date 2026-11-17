# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# monitoring.*, change.*

`monitoring.daily_health` counts the days in the window on which the
platform evaluated the system: an assurance capture, a drift detection, a
baseline, liveness or health computation dated that day. Health and
liveness rows are materialised (one current row per system), so days
before the latest computation are evidenced by captures and detections
only — said in `facts`, not hidden.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Set

from sqlalchemy import text

from app.services.compliance.catalogs.base import EvidenceSpec
from app.services.compliance.resolvers._base import (
    MET,
    NOT_EVALUABLE,
    NOT_MET,
    Outcome,
    ResolverContext,
    counted,
    resolver,
    window_params,
)
from app.services.compliance.resolvers.oversight import abort_holders
from app.services.compliance.resolvers.records import epoch_gaps

#: (table, timestamp column) pairs dated per system.
HEALTH_SOURCES = (
    ("ai_system_assurance_reports", "created_at"),
    ("ai_system_drift_events", "detected_at"),
    ("ai_system_baselines", "computed_at"),
    ("ai_system_health", "computed_at"),
    ("ai_system_liveness_expectations", "last_checked_at"),
)


@resolver("monitoring.daily_health")
def daily_health(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    days = ctx.days()
    covered: Set[date] = set()
    per_source: Dict[str, int] = {}
    for table, column in HEALTH_SOURCES:
        rows = ctx.db.execute(
            text(
                f"SELECT DISTINCT (({column}) AT TIME ZONE 'UTC')::date FROM {table} "
                f"WHERE tenant_id = :t AND ai_system_id = :sid AND {column} >= :ws AND {column} < :we"
            ),
            {**window_params(ctx), "sid": ctx.system_id},
        ).fetchall()
        dates = {r[0] for r in rows if r[0] is not None}
        per_source[table] = len(dates)
        covered |= dates
    in_window = [d for d in days if d in covered]
    missing = [d.isoformat() for d in days if d not in covered]
    facts = {"days_by_source": per_source, "missing_days": missing[:31]}
    return counted(len(in_window), len(days), what="days had a health, drift or liveness evaluation",
                   facts=facts, empty="The window covers no complete day yet.")


@resolver("monitoring.suspend_available")
def suspend_available(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    holders = abort_holders(ctx)
    interventions = ctx.db.execute(
        text(
            "SELECT DISTINCT ri.id FROM run_interventions ri "
            "JOIN proxy_logs p ON p.tenant_id = ri.tenant_id AND p.root_task_id = ri.root_task_id "
            "WHERE ri.tenant_id = :t AND ri.requested_at >= :ws AND ri.requested_at < :we "
            "AND p.resource_group_id = ANY(:gids)"
        ),
        {**window_params(ctx), "gids": ctx.group_ids()},
    ).fetchall()
    ids = [r[0] for r in interventions]
    sealed: Dict[str, str] = {}
    if ids:
        for rid, iid in ctx.db.execute(
            text(
                "SELECT record_id, record->'context'->>'intervention_id' FROM action_records "
                "WHERE tenant_id = :t AND record->'context'->>'record_type' = 'ai.brutor.run-abort' "
                "AND record->'context'->>'intervention_id' = ANY(:ids)"
            ),
            {"t": ctx.tenant_id, "ids": ids},
        ).fetchall():
            sealed.setdefault(iid, rid)
    suspensions = ctx.db.execute(
        text(
            "SELECT count(*) FROM system_operation_log WHERE tenant_id = :t AND scope_kind = 'ai_system' "
            "AND scope_id = :sid AND after_state->>'autonomy_level' = 'suspended' "
            "AND created_at >= :ws AND created_at < :we"
        ),
        {**window_params(ctx), "sid": ctx.system_id},
    ).scalar() or 0
    facts = {
        "run_abort_holders": holders,
        "aborts": len(ids),
        "aborts_sealed": len(sealed),
        "suspensions_logged": int(suspensions),
    }
    citations = list(sealed.values())
    refs = [{"type": "run_intervention", "id": i} for i in ids if i not in sealed]
    if holders == 0:
        return Outcome(NOT_MET, len(sealed), len(ids),
                       "No active administrator holds run:abort — the stop control is not available.",
                       citations=citations, facts=facts, refs=refs)
    if len(sealed) < len(ids):
        return Outcome(NOT_MET, len(sealed), len(ids),
                       f"{len(ids) - len(sealed)} of {len(ids)} run aborts in the window have no sealed record.",
                       citations=citations, facts=facts, refs=refs)
    detail = (f"Suspension and run abort are available ({holders} administrator(s) hold run:abort); "
              f"{len(sealed)} of {len(ids)} aborts in the window sealed.")
    if suspensions:
        detail += f" {suspensions} suspension(s) recorded in the operations log."
    return Outcome(MET, len(sealed), len(ids), detail, citations=citations, facts=facts, refs=refs)


@resolver("change.promotions_sealed")
def promotions_sealed(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    g = epoch_gaps(ctx)
    facts = {"promotions": g["promotions"], "missing": g["missing"], "without_approver": g["without_approver"]}
    return counted(g["sealed"], g["promotions"],
                   what="contract promotions have a sealed epoch-boundary record naming the approver",
                   citations=g["citations"], facts=facts,
                   empty="No contract was promoted in the window — nothing to evaluate.")


@resolver("change.no_unapproved_widening")
def no_unapproved_widening(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    rows = ctx.db.execute(
        text(
            "SELECT id, version, diff_summary, approved_by, promoted_at, status FROM ai_system_contracts "
            "WHERE tenant_id = :t AND ai_system_id = :sid AND ("
            "  (promoted_at >= :ws AND promoted_at < :we) OR status = 'active')"
        ),
        {**window_params(ctx), "sid": ctx.system_id},
    ).fetchall()
    if not rows:
        return Outcome(NOT_EVALUABLE, 0, 0, "No contract governs the system — widening cannot be assessed.")
    bad: List[Dict[str, Any]] = []
    for cid, version, summary, approved_by, _at, _status in rows:
        summary = summary or {}
        if summary.get("has_widened_permissions") and not approved_by:
            bad.append({"type": "contract", "id": cid, "version": version})
    n = len(rows) - len(bad)
    if bad:
        return Outcome(NOT_MET, n, len(rows),
                       f"{len(bad)} contract version(s) widened permissions without an approval.",
                       refs=bad)
    return Outcome(MET, n, len(rows),
                   f"{n} of {len(rows)} contract versions in force during the window carry no unapproved widening.")


__all__ = ["daily_health", "no_unapproved_widening", "promotions_sealed", "suspend_available"]
