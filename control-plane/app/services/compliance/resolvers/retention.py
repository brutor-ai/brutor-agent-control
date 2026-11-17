# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# retention.*

`retention.floor{floor_days, scope?}` — the effective retention (tenant
policy composed with every framework floor, `retention_floors.py`) is at
least `floor_days`, and no prune in the window removed rows younger than
the floor. A prune's rows are bracketed by the surviving neighbours in
their chain: every removed row is no newer than the next survivor and no
older than the previous one, so a prune is *provably* old enough when
`pruned_at − next survivor ≥ floor`, *provably* too young when
`pruned_at − previous survivor < floor`, and undetermined in between.

`scope = "documentation"` (HIPAA §164.316(b)(2)) evaluates the documentation
artefacts — declarations, reports, documentation exports — which no sweeper
deletes.

`retention.declared_prunes` — every gap in the row chains touching the
window is a declared prune (the core's `pruned` vs `gap` verdict).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.database.models.tenant import Tenant
from app.services.compliance.catalogs.base import EvidenceSpec
from app.services.compliance.resolvers._base import (
    MET,
    NOT_EVALUABLE,
    NOT_MET,
    UNDETERMINED,
    Outcome,
    ResolverContext,
    aware,
    resolver,
    window_params,
)
from app.services.compliance.resolvers.records import chain_reports
from app.services.compliance.retention_floors import compose, system_floor
from app.services.data_retention.policy import effective_policy

SCOPE_DOCUMENTATION = "documentation"


def _neighbour_ts(ctx: ResolverContext, chain_id: str, seq: int, after: bool) -> Optional[Any]:
    op, order = (">", "ASC") if after else ("<", "DESC")
    row = ctx.db.execute(
        text(
            f"SELECT timestamp FROM proxy_logs WHERE chain_id = :c AND chain_seq {op} :s "
            f"ORDER BY chain_seq {order} LIMIT 1"
        ),
        {"c": chain_id, "s": seq},
    ).first()
    return aware(row[0]) if row else None


def classify_prune(pruned_at, prev_ts, next_ts, floor: timedelta) -> str:
    """`old_enough` | `too_young` | `unknown` for one pruned run (pure)."""
    if next_ts is not None and pruned_at - next_ts >= floor:
        return "old_enough"
    if prev_ts is not None and pruned_at - prev_ts < floor:
        return "too_young"
    return "unknown"


def _documentation_floor(ctx: ResolverContext, floor_days: int) -> Outcome:
    counts = ctx.db.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM action_records WHERE tenant_id = :t AND ai_system_id = :sid
                 AND record->'context'->>'record_type' IN
                     ('ai.brutor.declaration','ai.brutor.report','ai.brutor.documentation')),
              (SELECT count(*) FROM compliance_reports WHERE tenant_id = :t
                 AND (ai_system_id = :sid OR ai_system_id IS NULL)),
              (SELECT count(*) FROM compliance_documentation_exports WHERE tenant_id = :t AND ai_system_id = :sid)
            """
        ),
        {"t": ctx.tenant_id, "sid": ctx.system_id},
    ).first()
    total = sum(int(c or 0) for c in counts)
    facts = {"floor_days": floor_days, "declaration_and_report_records": int(counts[0] or 0),
             "reports": int(counts[1] or 0), "documentation_exports": int(counts[2] or 0)}
    if total == 0:
        return Outcome(NOT_EVALUABLE, 0, 0, "No documentation artefacts exist yet — nothing to retain.", facts=facts)
    return Outcome(
        MET, total, total,
        f"{total} documentation artefacts on file; declarations, reports and documentation exports are "
        f"never removed by the retention sweeper, so they are kept at least {floor_days} days.",
        facts=facts,
    )


@resolver("retention.floor")
def retention_floor(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    try:
        floor_days = int(spec.params.get("floor_days"))
    except (TypeError, ValueError):
        return Outcome(NOT_EVALUABLE, detail="The statement names no floor_days.")
    if spec.params.get("scope") == SCOPE_DOCUMENTATION:
        return _documentation_floor(ctx, floor_days)

    tenant = ctx.db.get(Tenant, ctx.tenant_id)
    if tenant is None:
        return Outcome(NOT_EVALUABLE, detail="Tenant not found.")
    policy = effective_policy(tenant)
    sf = system_floor(ctx.db, tenant, ctx.system, ctx.profile)
    effective = compose(policy, max(sf.floor_days or 0, 0) or None)
    log_days = effective.log_days if effective else None
    run_days = effective.run_days if effective else None
    facts: Dict[str, Any] = {
        "floor_days": floor_days,
        "policy": policy.model_dump() if policy else None,
        "framework_floors": [s.to_dict() for s in sf.sources],
        "effective_log_days": log_days,
        "effective_run_days": run_days,
    }
    # The two retention windows the floor governs, each checked.
    windows = (("ledger rows", log_days), ("runs", run_days))
    short = [name for name, days in windows if days is not None and days < floor_days]
    windows_ok = len(windows) - len(short)

    prunes = ctx.db.execute(
        text(
            "SELECT id, chain_id, seq_min, seq_max, runs, reason, pruned_at FROM audit_chain_prunes "
            "WHERE (tenant_id = :t OR tenant_id IS NULL) AND pruned_at >= :ws AND pruned_at < :we"
        ),
        window_params(ctx),
    ).fetchall()
    floor = timedelta(days=floor_days)
    verdicts: Dict[str, int] = {"old_enough": 0, "too_young": 0, "unknown": 0}
    refs: List[Dict[str, Any]] = []
    for pid, chain_id, seq_min, seq_max, runs, reason, pruned_at in prunes:
        pruned_at = aware(pruned_at)
        prev_ts = _neighbour_ts(ctx, chain_id, int(seq_min), after=False)
        next_ts = _neighbour_ts(ctx, chain_id, int(seq_max), after=True)
        v = classify_prune(pruned_at, prev_ts, next_ts, floor)
        verdicts[v] += 1
        if v != "old_enough":
            refs.append({"type": "audit_chain_prune", "id": pid, "reason": reason, "classification": v})
    facts["prunes"] = verdicts
    # n of m = retention windows at or above the floor + prunes provably
    # older than it, of windows + prunes in the window.
    m = len(windows) + len(prunes)
    n = windows_ok + verdicts["old_enough"]

    if short:
        return Outcome(
            NOT_MET, n, m,
            f"Effective retention for {' and '.join(short)} is below the {floor_days}-day floor.",
            facts=facts, refs=refs,
        )
    if verdicts["too_young"]:
        return Outcome(
            NOT_MET, n, m,
            f"{verdicts['too_young']} prune(s) in the window removed rows younger than {floor_days} days.",
            facts=facts, refs=refs,
        )
    if verdicts["unknown"]:
        return Outcome(
            UNDETERMINED, n, m,
            f"{verdicts['unknown']} prune(s) in the window could not be bracketed against the "
            f"{floor_days}-day floor.",
            facts=facts, refs=refs,
        )
    keep = "kept indefinitely" if log_days is None else f"kept {log_days} days"
    detail = f"Ledger rows are {keep} (floor {floor_days} days)."
    runs_keep = "kept indefinitely" if run_days is None else f"kept {run_days} days"
    detail += f" Runs are {runs_keep}."
    detail += (f" {verdicts['old_enough']} of {len(prunes)} prunes in the window removed only rows older than "
               f"the floor." if prunes else " No prunes in the window.")
    return Outcome(MET, n, m, detail, facts=facts, refs=refs)


@resolver("retention.declared_prunes")
def declared_prunes(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    reports = [r for r in chain_reports(ctx) if r.get("chain_id")]
    if not reports:
        return Outcome(NOT_EVALUABLE, 0, 0, "No chained ledger rows in the window — nothing to check.")
    checked = [r for r in reports if r.get("verdict")]
    undeclared = [r for r in checked if r.get("verdict") == "gap" or r.get("undeclared_gaps")]
    pruned_rows = sum(int(r.get("pruned_rows") or 0) for r in checked)
    facts = {"chains": reports, "pruned_rows_declared": pruned_rows}
    if undeclared:
        return Outcome(
            NOT_MET, len(checked) - len(undeclared), len(reports),
            f"{len(undeclared)} of {len(reports)} row chains have gaps no prune record declares.",
            facts=facts,
        )
    if len(checked) < len(reports):
        return Outcome(
            NOT_EVALUABLE, len(checked), len(reports),
            f"{len(reports) - len(checked)} row chain(s) could not be checked (verifier unavailable).",
            facts=facts,
        )
    return Outcome(
        MET, len(checked), len(reports),
        f"{len(checked)} of {len(reports)} row chains have no undeclared gap; {pruned_rows} pruned row(s) declared.",
        facts=facts,
    )


__all__ = ["classify_prune", "declared_prunes", "retention_floor"]
