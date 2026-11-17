# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Evidence Reports — daily, weekly, monthly, per framework

One set of records, many audits: a report evaluates a framework's
obligations in force for the period against the shared resolvers
(`app/services/compliance/resolvers/`), so the same month rendered under
the EU AI Act, ISO/IEC 42001 and SOC 2 shows the same row wherever the
frameworks cite the same `(resolver, params)`.

Rows (one per system × obligation × statement) carry the statement's
basis, status, `n of m`, detail, limit and citations. The summary keeps
the bases apart — **recomputed and judged counts are never summed** —
states human agreement beside judged rows, witness plurality (tree-head
grades in the period), declared prunes, what the catalog does not cover,
and a human-signed scope census placeholder (completeness is never
proven; a person has to sign for the scope).

Each report is persisted in `compliance_reports` (unique per tenant,
framework, system-or-estate, cadence, period start — generation is
idempotent on that key) and sealed as an `ai.brutor.report` record whose
payload digest covers the rows. A monthly report is also emitted as an
evidence bundle rooted at the report record, with the rows in
`extensions["ai.brutor/evidence-report/v1"]` (`framework_id` inside).
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from ulid import ULID

from app.database.models.action_record import ActionRecord
from app.database.models.compliance_report import (
    REPORT_CADENCES,
    REPORT_STATUS_FAILED,
    REPORT_STATUS_READY,
    ComplianceReport,
)
from app.database.models.evidence_log import EvidenceTreeHead
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.compliance import sealing
from app.services.compliance import offering
from app.services.compliance.catalogs.base import (
    NOT_LEGAL_ADVICE,
    STATUS_NOT_APPLICABLE,
    Framework,
)
from app.services.evidence import core_client

log = logging.getLogger("brutor.compliance.reports")

REPORT_EXTENSION = "ai.brutor/evidence-report/v1"
REPORT_SCHEMA_VERSION = 1

#: Cap on the explicit record ids a monthly bundle selects (the report
#: record plus the rows' citations); the core refuses > 10 000.
MAX_BUNDLE_RECORDS = 5000

BASES = ("recomputed", "judged", "indicator", "declared")

SCOPE_CENSUS_NOTE = (
    "Completeness is never proven by the records: a person accountable for the scope must sign "
    "that the systems listed are all the AI systems in scope. Not signed in this report."
)


class ReportError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


def period_for(cadence: str, day: date) -> Tuple[date, date]:
    """Inclusive `(start, end)` of the period containing `day`."""
    if cadence == "daily":
        return day, day
    if cadence == "weekly":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    if cadence == "monthly":
        start = day.replace(day=1)
        return start, day.replace(day=calendar.monthrange(day.year, day.month)[1])
    raise ReportError(f"cadence must be one of {sorted(REPORT_CADENCES)}")


def previous_period(cadence: str, today: date) -> Tuple[date, date]:
    """The last COMPLETE period before `today`."""
    start, _ = period_for(cadence, today)
    return period_for(cadence, start - timedelta(days=1))


def window_of(start: date, end: date) -> Tuple[datetime, datetime]:
    return (
        datetime.combine(start, time.min, tzinfo=timezone.utc),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def systems_in_scope(db: Session, tenant_id: str, ai_system_id: Optional[str]) -> List[ResourceGroup]:
    """Estate-wide: every active AI System; per system: that one."""
    from app.services.compliance.evaluation import active_systems

    return sorted(active_systems(db, tenant_id, ai_system_id), key=lambda s: s.id)


def _agreement_of(result: Any) -> Optional[Dict[str, Any]]:
    facts = getattr(result, "facts", None) or {}
    agreements = facts.get("agreement")
    return agreements if agreements else None


def evaluate_rows(
    db: Session,
    tenant_id: str,
    framework: Framework,
    systems: Sequence[ResourceGroup],
    start: datetime,
    end: datetime,
    *,
    catalog_version: Optional[str] = None,
    now: Optional[datetime] = None,
    obligation_keys: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Every statement of every obligation in force in `[start, end)` that
    applies to each system — one row per system × obligation × statement
    (× catalog-version segment, when the window spans a version change).

    Built on the console's engine (`evaluation.SystemEvaluator`: version in
    force per segment, one resolver run per `(resolver, params)`), so a
    report row and the Obligations page never disagree. `catalog_version`
    is informational; segments carry their own."""
    from app.services.compliance.evaluation import SystemEvaluator, load_profiles

    period_end_day = (end - timedelta(microseconds=1)).date()
    profiles = load_profiles(db, tenant_id)
    rows: List[Dict[str, Any]] = []
    for system in systems:
        ev = SystemEvaluator(db, tenant_id, framework, system, profiles.get(system.id), (start, end), now=now)
        for obligation in framework.obligations:
            if obligation_keys is not None and obligation.key not in obligation_keys:
                continue
            res = ev.obligation(obligation)
            if not res["present_in_window"]:
                continue  # not in the catalog text in force during the period
            if date.fromisoformat(res["applies_from"]) > period_end_day:
                continue  # not in force in the period
            if res["applicability"] == STATUS_NOT_APPLICABLE:
                continue
            base = {
                "ai_system_id": system.id,
                "ai_system_name": system.display_name or system.name,
                "obligation_key": obligation.key,
                "obligation_title": obligation.title,
                "reference": obligation.reference,
                "provision_note": NOT_LEGAL_ADVICE,
                "applicability": res["applicability"],
                "applicability_reason": res["reason"],
                "applies_from": res["applies_from"],
                "obligation_limit": obligation.limit,
            }
            if not obligation.evidence:
                rows.append({**base, "statement_key": None, "basis": None, "status": "not_covered",
                             "detail": "No evidence statement in the catalog for this obligation."})
                continue
            for result in res["statements"]:
                row = {
                    **base,
                    "statement_key": result.key,
                    "statement": result.statement,
                    "resolver": result.resolver,
                    "params": dict(result.params),
                    "basis": result.basis,
                    "status": result.status,
                    "n": result.n,
                    "m": result.m,
                    "detail": result.detail,
                    "limit": result.limit,
                    "citations": list(result.citations[:20]),
                    "catalog_version": result.catalog_version,
                }
                if result.basis == "judged":
                    # Stated BESIDE the judged row, never folded into n / m.
                    row["human_agreement"] = _agreement_of(result)
                rows.append(row)
    return rows


def summarize_rows(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts per basis × status. Bases are never summed together."""
    by_basis: Dict[str, Dict[str, int]] = {b: {} for b in BASES}
    not_covered: List[Dict[str, str]] = []
    for r in rows:
        basis = r.get("basis")
        if basis is None:
            not_covered.append({"ai_system_id": r["ai_system_id"], "obligation_key": r["obligation_key"]})
            continue
        bucket = by_basis.setdefault(basis, {})
        bucket[r["status"]] = bucket.get(r["status"], 0) + 1
    obligations = {(r["ai_system_id"], r["obligation_key"]) for r in rows}
    limits = sorted({r["obligation_limit"] for r in rows if r.get("obligation_limit")})
    return {
        "obligations_in_scope": len(obligations),
        "statements": sum(1 for r in rows if r.get("basis")),
        "recomputed": by_basis["recomputed"],
        "judged": by_basis["judged"],
        "indicator": by_basis["indicator"],
        "declared": by_basis["declared"],
        "not_covered": not_covered,
        "stated_limits": limits,
    }


def witness_plurality(db: Session, tenant_id: str, start: datetime, end: datetime) -> Dict[str, Any]:
    heads = (
        db.query(EvidenceTreeHead.grade, func.count(EvidenceTreeHead.id))
        .filter(
            EvidenceTreeHead.tenant_id == tenant_id,
            EvidenceTreeHead.issued_at >= start,
            EvidenceTreeHead.issued_at < end,
        )
        .group_by(EvidenceTreeHead.grade)
        .all()
    )
    latest = (
        db.query(EvidenceTreeHead)
        .filter(EvidenceTreeHead.tenant_id == tenant_id, EvidenceTreeHead.issued_at < end)
        .order_by(EvidenceTreeHead.issued_at.desc(), EvidenceTreeHead.tree_size.desc())
        .first()
    )
    return {
        "heads_by_grade": {g: int(c) for g, c in heads},
        "latest_head": (
            {"id": latest.id, "tree_size": int(latest.tree_size), "root": latest.root, "grade": latest.grade}
            if latest is not None
            else None
        ),
    }


def runs_sealed(db: Session, tenant_id: str, system_ids: Sequence[str], start: datetime, end: datetime) -> int:
    if not system_ids:
        return 0
    return int(
        db.query(func.count(func.distinct(ActionRecord.root_task_id)))
        .filter(
            ActionRecord.tenant_id == tenant_id,
            ActionRecord.ai_system_id.in_(list(system_ids)),
            ActionRecord.root_task_id.isnot(None),
            ActionRecord.timestamp >= start,
            ActionRecord.timestamp < end,
        )
        .scalar()
        or 0
    )


def _declared_prunes(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"ai_system_id": r["ai_system_id"], "status": r["status"], "detail": r.get("detail")}
        for r in rows
        if r.get("resolver") == "retention.declared_prunes"
    ]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


@dataclass
class Generated:
    report: ComplianceReport
    created: bool
    seal: Optional[sealing.SealOutcome]
    bundle_error: Optional[str] = None


def _existing(db: Session, tenant_id: str, framework_id: str, ai_system_id: Optional[str],
              cadence: str, period_start: date) -> Optional[ComplianceReport]:
    q = db.query(ComplianceReport).filter(
        ComplianceReport.tenant_id == tenant_id,
        ComplianceReport.framework_id == framework_id,
        ComplianceReport.cadence == cadence,
        ComplianceReport.period_start == period_start,
    )
    q = q.filter(ComplianceReport.ai_system_id == ai_system_id) if ai_system_id else q.filter(
        ComplianceReport.ai_system_id.is_(None)
    )
    return q.first()


def generate(
    db: Session,
    tenant_id: str,
    framework_id: str,
    *,
    cadence: str,
    day: date,
    ai_system_id: Optional[str] = None,
    force: bool = False,
    actor: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Generated:
    """Generate (or return) the report for the period containing `day`.
    Idempotent on the period key; `force` regenerates in place (new rows,
    new record)."""
    known = offering.any_catalog(framework_id)
    if known is None:
        raise ReportError(f"Unknown framework {framework_id!r}", 404)
    reason = offering.unavailable_reason(known)
    if reason is not None:
        raise ReportError(reason, 409)
    framework = known
    if cadence not in REPORT_CADENCES:
        raise ReportError(f"cadence must be one of {sorted(REPORT_CADENCES)}")
    systems = systems_in_scope(db, tenant_id, ai_system_id)
    if ai_system_id and not systems:
        raise ReportError(f"AI System {ai_system_id!r} not found", 404)

    p_start, p_end = period_for(cadence, day)
    row = _existing(db, tenant_id, framework_id, ai_system_id, cadence, p_start)
    if row is not None and row.status == REPORT_STATUS_READY and not force:
        return Generated(report=row, created=False, seal=None)

    version = framework.version_on(p_end)
    catalog_version = (version.catalog_version if version else framework.current_version) or "unversioned"
    created = row is None
    if created:
        row = ComplianceReport(
            id=f"rpt-{ULID()}".lower(),
            tenant_id=tenant_id,
            framework_id=framework_id,
            ai_system_id=ai_system_id,
            cadence=cadence,
            period_start=p_start,
            period_end=p_end,
            catalog_version=catalog_version,
            status="generating",
            generated_by=actor,
        )
        db.add(row)
    # Committed before evaluation: a resolver that fails rolls the session
    # back (engine contract), which must not take the report row with it.
    row.status = "generating"
    db.commit()
    report_id = row.id

    start, end = window_of(p_start, p_end)
    try:
        rows = evaluate_rows(
            db, tenant_id, framework, systems, start, end, catalog_version=version.catalog_version if version else None,
            now=now,
        )
        summary = summarize_rows(rows)
        summary.update({
            "schema_version": REPORT_SCHEMA_VERSION,
            "framework": {
                "id": framework.id,
                "title": framework.title,
                "kind": framework.kind,
                "reference": framework.reference,
                "verified_against_instrument": framework.verified_against_instrument,
            },
            "catalog_version": catalog_version,
            "systems": [{"id": s.id, "name": s.display_name or s.name} for s in systems],
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "witness": witness_plurality(db, tenant_id, start, end),
            "runs_sealed": runs_sealed(db, tenant_id, [s.id for s in systems], start, end),
            "declared_prunes": _declared_prunes(rows),
            "scope_census": {"human_signed": False, "signed_by": None, "signed_at": None,
                             "note": SCOPE_CENSUS_NOTE},
            "provision_note": NOT_LEGAL_ADVICE,
        })
    except Exception as exc:  # noqa: BLE001 — a failed report is stored as failed, never half-ready
        log.exception("report generation failed for %s/%s", tenant_id, framework_id)
        db.rollback()
        row = db.get(ComplianceReport, report_id)
        row.status = REPORT_STATUS_FAILED
        row.error = f"{exc.__class__.__name__}: {str(exc)[:500]}"
        db.flush()
        return Generated(report=row, created=created, seal=None)

    row.rows = rows
    row.summary = summary
    row.catalog_version = catalog_version
    row.status = REPORT_STATUS_READY
    row.error = None
    row.generated_by = actor or row.generated_by
    flag_modified(row, "rows")
    flag_modified(row, "summary")
    db.flush()

    outcome = sealing.seal(
        tenant_id,
        sealing.RECORD_REPORT,
        ai_system_id=ai_system_id,
        context={
            "framework_id": framework_id,
            "catalog_version": catalog_version,
            "report_id": row.id,
            "cadence": cadence,
            "period_start": p_start.isoformat(),
            "period_end": p_end.isoformat(),
        },
        payload={"report_id": row.id, "framework_id": framework_id, "summary": summary, "rows": rows},
    )
    row.record_id = outcome.record_id
    bundle_error = None
    if cadence == "monthly":
        bundle_error = attach_bundle(row, actor=actor)
    if bundle_error:
        summary = dict(row.summary or {})
        summary["bundle_error"] = bundle_error
        row.summary = summary
        flag_modified(row, "summary")
    db.flush()
    return Generated(report=row, created=created, seal=outcome, bundle_error=bundle_error)


def bundle_extension(row: ComplianceReport) -> Dict[str, Any]:
    return {
        REPORT_EXTENSION: {
            "framework_id": row.framework_id,
            "catalog_version": row.catalog_version,
            "report_id": row.id,
            "report_record_id": row.record_id,
            "ai_system_id": row.ai_system_id,
            "cadence": row.cadence,
            "period_start": row.period_start.isoformat(),
            "period_end": row.period_end.isoformat(),
            "summary": row.summary or {},
            "rows": row.rows or [],
        }
    }


def attach_bundle(row: ComplianceReport, *, actor: Optional[str] = None) -> Optional[str]:
    """Build the monthly evidence bundle rooted at the report record.
    Returns an error code when it could not be built (the report stays
    ready; the console says the bundle is missing and why)."""
    if not row.record_id:
        return "report_not_sealed"
    cited: List[str] = [row.record_id]
    for r in row.rows or []:
        for rid in r.get("citations") or []:
            if rid not in cited and sealing.reference("x", rid) is not None:
                cited.append(rid)
            if len(cited) >= MAX_BUNDLE_RECORDS:
                break
    result = core_client.build_bundle(
        row.tenant_id,
        {"record_ids": cited},
        framework_id=row.framework_id,
        catalog_version=row.catalog_version,
        extensions=bundle_extension(row),
        closure_depth=1,
        requested_by=actor,
    )
    if core_client.is_error(result):
        log.warning("report bundle failed for %s: %s %s", row.id, result.get("error"), result.get("detail"))
        return str(result.get("error"))
    row.bundle_id = result.get("bundle_id")
    return None


def latest_for_system(db: Session, tenant_id: str, ai_system_id: str) -> List[ComplianceReport]:
    """The newest ready report per framework for a system (monthly first,
    then the most recent of any cadence) — what the Assurance Report cites."""
    rows = (
        db.query(ComplianceReport)
        .filter(
            ComplianceReport.tenant_id == tenant_id,
            ComplianceReport.ai_system_id == ai_system_id,
            ComplianceReport.status == REPORT_STATUS_READY,
        )
        .order_by(ComplianceReport.period_end.desc(), ComplianceReport.created_at.desc())
        .all()
    )
    chosen: Dict[str, ComplianceReport] = {}
    for r in rows:
        prior = chosen.get(r.framework_id)
        if prior is None or (prior.cadence != "monthly" and r.cadence == "monthly" and r.period_end >= prior.period_start):
            chosen[r.framework_id] = r
    return [chosen[k] for k in sorted(chosen)]


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def scheduled_targets(db: Session) -> List[Tuple[str, str, Optional[str]]]:
    """`(tenant_id, framework_id, ai_system_id|None)` the scheduler reports
    on: every framework a tenant has enabled that has a catalog, estate-wide
    and per system whose compliance profile declares a block for it."""
    from app.database.models.ai_system_compliance_profile import AISystemComplianceProfile
    from app.database.models.compliance_framework import ComplianceFramework
    from app.database.models.tenant import Tenant
    from app.services import compliance_registry

    frameworks = db.query(ComplianceFramework).all()
    targets: List[Tuple[str, str, Optional[str]]] = []
    for tenant in db.query(Tenant).filter(Tenant.is_active.is_(True)).all():
        enabled = [f.id for f in frameworks if offering.catalog(f.id) and compliance_registry.is_enabled(tenant, f)]
        if not enabled:
            continue
        profiles = db.query(AISystemComplianceProfile).filter(AISystemComplianceProfile.tenant_id == tenant.id).all()
        for fid in enabled:
            targets.append((tenant.id, fid, None))
            for p in profiles:
                if isinstance(p.frameworks, dict) and fid in p.frameworks:
                    targets.append((tenant.id, fid, p.ai_system_id))
    return targets


def run_scheduled(db: Session, *, today: Optional[date] = None) -> Dict[str, int]:
    """Generate yesterday's daily, last week's weekly and last month's
    monthly reports for every target. Idempotent on the period key; each
    report commits on its own."""
    today = today or datetime.now(timezone.utc).date()
    stats = {"generated": 0, "existing": 0, "failed": 0}
    for tenant_id, framework_id, ai_system_id in scheduled_targets(db):
        for cadence in ("daily", "weekly", "monthly"):
            p_start, _ = previous_period(cadence, today)
            try:
                result = generate(db, tenant_id, framework_id, cadence=cadence, day=p_start,
                                  ai_system_id=ai_system_id, actor="scheduler")
                db.commit()
                if result.report.status == REPORT_STATUS_FAILED:
                    stats["failed"] += 1
                elif result.seal is None:
                    stats["existing"] += 1
                else:
                    stats["generated"] += 1
            except Exception:  # noqa: BLE001 — one report must not stop the rest
                db.rollback()
                stats["failed"] += 1
                log.exception("scheduled report failed: %s %s %s %s", tenant_id, framework_id, ai_system_id, cadence)
    return stats


__all__ = [
    "Generated",
    "REPORT_EXTENSION",
    "ReportError",
    "attach_bundle",
    "bundle_extension",
    "evaluate_rows",
    "generate",
    "latest_for_system",
    "period_for",
    "previous_period",
    "run_scheduled",
    "scheduled_targets",
    "summarize_rows",
    "window_of",
]
