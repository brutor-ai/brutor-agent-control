# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# evidence.*, docs.*, profile.*, incidents.*

Organisational duties are evidenced by pointer and declaration — never
claimed. A pointer is a document produced outside the platform; its content
is not evaluated. A DECLARED statement cites the sealed profile
declaration; an unsealed declaration is `undetermined`, not `declared`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text

from app.services.compliance import offering
from app.services.compliance.catalogs.base import EvidenceSpec
from app.services.compliance.resolvers._base import (
    DECLARED,
    MET,
    NOT_EVALUABLE,
    NOT_MET,
    UNDETERMINED,
    Outcome,
    ResolverContext,
    aware,
    counted,
    declaration_record,
    dotted,
    present,
    profile_dict,
    resolver,
    window_params,
)


@resolver("evidence.pointer_on_file")
def pointer_on_file(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    kind = str(spec.params.get("kind") or "")
    if not kind:
        return Outcome(NOT_EVALUABLE, detail="The statement names no evidence kind.")
    rows = ctx.db.execute(
        text(
            "SELECT id, title, valid_until, independent FROM ai_system_evidence "
            "WHERE tenant_id = :t AND ai_system_id = :sid AND kind = :k"
        ),
        {"t": ctx.tenant_id, "sid": ctx.system_id, "k": kind},
    ).fetchall()
    current = [r for r in rows if r[2] is None or aware(r[2]) > ctx.now]
    refs = [{"type": "evidence_pointer", "id": r[0], "expired": r not in current} for r in rows]
    facts = {"kind": kind, "on_file": len(rows), "unexpired": len(current),
             "independent": sum(1 for r in current if r[3])}
    if not rows:
        return Outcome(NOT_MET, 0, 0, f"No {kind.replace('_', ' ')} is on file.", facts=facts)
    if not current:
        return Outcome(NOT_MET, 0, len(rows), f"Every {kind.replace('_', ' ')} on file has expired.",
                       facts=facts, refs=refs)
    return Outcome(MET, len(current), len(rows),
                   f"{len(current)} of {len(rows)} {kind.replace('_', ' ')} pointer(s) on file are unexpired.",
                   facts=facts, refs=refs)


def _active_contract(ctx: ResolverContext) -> Optional[Any]:
    return ctx.db.execute(
        text(
            "SELECT id, version, contract_hash FROM ai_system_contracts "
            "WHERE tenant_id = :t AND ai_system_id = :sid AND status = 'active' "
            "ORDER BY version DESC LIMIT 1"
        ),
        {"t": ctx.tenant_id, "sid": ctx.system_id},
    ).first()


@resolver("docs.technical_documentation")
def technical_documentation(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    framework = str(spec.params.get("framework") or ctx.framework_id or "")
    contract = _active_contract(ctx)
    if contract is None:
        return Outcome(NOT_EVALUABLE, detail="No active contract — there is no version to document.")
    rows = ctx.db.execute(
        text(
            "SELECT id, contract_version, record_id, digest, created_at FROM compliance_documentation_exports "
            "WHERE tenant_id = :t AND ai_system_id = :sid AND framework_id = :f ORDER BY created_at DESC"
        ),
        {"t": ctx.tenant_id, "sid": ctx.system_id, "f": framework},
    ).fetchall()
    current = [r for r in rows if r[1] == contract[1]]
    sealed = [r for r in current if r[2]]
    facts = {"framework_id": framework, "active_contract_version": contract[1],
             "exports": len(rows), "for_active_version": len(current), "sealed": len(sealed)}
    refs = [{"type": "documentation_export", "id": r[0], "contract_version": r[1]} for r in rows[:5]]
    if sealed:
        return Outcome(MET, 1, 1, f"Documentation export for contract v{contract[1]} on file, digest sealed.",
                       citations=[sealed[0][2]], facts=facts, refs=refs)
    if current:
        return Outcome(NOT_MET, 0, 1, f"A documentation export for contract v{contract[1]} exists but its "
                                      f"digest is not sealed.", facts=facts, refs=refs)
    older = f" (latest export is for v{rows[0][1]})" if rows else ""
    return Outcome(NOT_MET, 0, 1, f"No documentation export for the active contract v{contract[1]}{older}.",
                   facts=facts, refs=refs)


@resolver("docs.registration_recorded")
def registration_recorded(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    framework = str(spec.params.get("framework") or ctx.framework_id or "")
    regs = profile_dict(ctx)["core"].get("external_registrations") or []
    hits = [r for r in regs if isinstance(r, dict) and r.get("framework_id") == framework and present(r.get("id"))]
    decl = declaration_record(ctx)
    facts = {"framework_id": framework, "registrations": len(hits), "sealed": bool(decl)}
    if not hits:
        return Outcome(UNDETERMINED, detail="No external registration identifier is recorded for this framework.",
                       facts=facts)
    if not decl:
        return Outcome(UNDETERMINED, detail="A registration identifier is recorded but the declaration is not "
                                            "sealed.", facts=facts)
    return Outcome(DECLARED, detail=f"Registration recorded ({hits[0].get('registry') or 'registry'}); sealed.",
                   citations=[decl], facts=facts)


@resolver("profile.field_declared")
def field_declared(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    path = str(spec.params.get("path") or "")
    if not path:
        return Outcome(NOT_EVALUABLE, detail="The statement names no profile field.")
    found, value = dotted(profile_dict(ctx), path)
    decl = declaration_record(ctx)
    facts = {"path": path, "declared": found and present(value), "sealed": bool(decl)}
    if not (found and present(value)):
        return Outcome(UNDETERMINED, detail=f"Not declared on the compliance profile ({path}).", facts=facts)
    if not decl:
        return Outcome(UNDETERMINED, detail="Declared on the profile, but the declaration is not sealed.",
                       facts=facts)
    return Outcome(DECLARED, detail="Declared on the compliance profile; sealed.", citations=[decl], facts=facts)


# ---------------------------------------------------------------------------
# incidents.*
# ---------------------------------------------------------------------------


def _parse_due(value: Any) -> Optional[datetime]:
    if isinstance(value, dict):
        for key in ("due_at", "deadline", "at", "due"):
            if value.get(key):
                return _parse_due(value[key])
        return None
    if isinstance(value, str):
        try:
            return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _stored_dues(stored: Any) -> List[datetime]:
    """Every due time in a stored per-framework entry. The canonical shape
    is `{rule: ISO due}` (`incidents.compute_deadlines`); a bare ISO string,
    a list of them, or `{due_at: …}` objects are read too."""
    if isinstance(stored, dict):
        direct = _parse_due(stored)
        if direct is not None:  # legacy `{due_at: …}` object
            return [direct]
        values: Iterable[Any] = stored.values()
    elif isinstance(stored, list):
        values = stored
    else:
        values = [stored]
    return [d for d in (_parse_due(v) for v in values) if d is not None]


def incident_deadline(incident: Dict[str, Any], framework_id: str) -> Optional[datetime]:
    """The reporting deadline of one incident under one framework.

    The deadlines stored on the incident (`{framework_id: {rule: ISO}}`,
    frozen when it was created or last re-classified) are authoritative:
    the earliest rule is the report stage, later ones are follow-ups
    (`incidents.deadline_status`). A catalog change after the incident was
    raised must not move its clock, so the catalog is consulted only for an
    incident that has no stored entry for the framework at all. A stored
    empty entry means "in scope, no statutory clock" — no deadline.

    A staged framework (DORA, NIS2) is the exception: its first stage's due
    time is computed from `deadline_stages` and the incident's anchors
    (`staged_rows`), because rows stored before stages were recorded
    measured every stage from awareness."""
    staged = staged_rows(incident, framework_id, now=None)
    if staged is not None:
        return _parse_due(staged[0]["due_at"]) if staged else None
    deadlines = incident.get("deadlines") or {}
    if isinstance(deadlines, dict) and framework_id in deadlines:
        dues = _stored_dues(deadlines[framework_id])
        return min(dues) if dues else None
    catalog = offering.catalog(framework_id)
    rule = catalog.deadline_for(incident.get("classification")) if catalog else None
    aware_at = aware(incident.get("became_aware_at"))
    if rule is None or aware_at is None:
        return None
    return aware_at + rule


def staged_rows(
    incident: Dict[str, Any], framework_id: str, *, now: Optional[datetime]
) -> Optional[List[Dict[str, Any]]]:
    """Every stage of a staged framework for one incident, with status
    (`incidents.staged_status`); None when the framework is not staged."""
    catalog = offering.catalog(framework_id)
    aware_at = aware(incident.get("became_aware_at"))
    if catalog is None or not catalog.staged or aware_at is None:
        return None
    from app.services.compliance.incidents import staged_status

    return staged_status(
        catalog,
        became_aware_at=aware_at,
        classified_at=aware(incident.get("classified_at")),
        stage_submissions=incident.get("stage_submissions") or {},
        reported_at=aware(incident.get("reported_at")),
        closed=incident.get("status") == "closed",
        now=now or datetime.now(timezone.utc),
    )


#: Staged-stage statuses that count against "reported within deadline".
_STAGE_LATE = frozenset({"reported_late", "overdue"})


def _staged_outcome(rows: List[Dict[str, Any]], now: datetime) -> Optional[str]:
    """`late` | `on_time` | `pending` for one staged incident: on time =
    every SUBMITTED stage was on time and no unsubmitted stage is past its
    due time (open or closed unsubmitted); pending = nothing submitted and
    nothing overdue yet. None = no stage has a clock."""
    if not rows:
        return None
    for r in rows:
        if r["status"] in _STAGE_LATE:
            return "late"
        due = _parse_due(r["due_at"])
        if r["status"] == "closed_unsubmitted" and due is not None and now > due:
            return "late"
    if any(r["status"] == "reported_on_time" for r in rows):
        return "on_time"
    return "pending"


def _incidents(ctx: ResolverContext, framework_id: Optional[str]) -> List[Dict[str, Any]]:
    rows = ctx.db.execute(
        text(
            "SELECT id, classification, became_aware_at, frameworks, deadlines, status, reported_at, "
            "classified_at, stage_submissions "
            "FROM compliance_incidents WHERE tenant_id = :t AND ai_system_id = :sid"
        ),
        {"t": ctx.tenant_id, "sid": ctx.system_id},
    ).fetchall()
    out = []
    for r in rows:
        fws = r[3] if isinstance(r[3], list) else []
        if framework_id and fws and framework_id not in fws:
            continue
        out.append({"id": r[0], "classification": r[1], "became_aware_at": aware(r[2]), "frameworks": fws,
                    "deadlines": r[4] or {}, "status": r[5], "reported_at": aware(r[6]),
                    "classified_at": aware(r[7]), "stage_submissions": r[8] if isinstance(r[8], dict) else {}})
    return out


@resolver("incidents.reported_within_deadline")
def reported_within_deadline(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    framework = str(spec.params.get("framework") or ctx.framework_id or "")
    incidents = [i for i in _incidents(ctx, framework)
                 if ctx.window_start <= i["became_aware_at"] < ctx.window_end]
    on_time, late, pending, no_rule = [], [], [], []
    for inc in incidents:
        staged = staged_rows(inc, framework, now=ctx.now)
        if staged is not None:
            verdict = _staged_outcome(staged, ctx.now)
            {"late": late, "on_time": on_time, "pending": pending, None: no_rule}[verdict].append(inc["id"])
            continue
        due = incident_deadline(inc, framework)
        if due is None:
            no_rule.append(inc["id"])
            continue
        if inc["reported_at"] is not None:
            (on_time if inc["reported_at"] <= due else late).append(inc["id"])
        elif ctx.now > due:
            late.append(inc["id"])
        else:
            pending.append(inc["id"])
    facts = {"framework_id": framework, "incidents": len(incidents), "reported_on_time": len(on_time),
             "late_or_overdue": len(late), "within_deadline_not_yet_reported": len(pending),
             "no_deadline_rule": len(no_rule)}
    refs = [{"type": "incident", "id": i, "outcome": "late"} for i in late] + \
           [{"type": "incident", "id": i, "outcome": "pending"} for i in pending]
    m = len(on_time) + len(late)
    if no_rule and m == 0:
        return Outcome(UNDETERMINED, detail="The framework defines no deadline rule for these incidents.",
                       facts=facts, refs=refs)
    out = counted(len(on_time), m, what="incidents were reported within their deadline", facts=facts, refs=refs,
                  empty="No incident became known in the window with a deadline that has passed or been "
                        "met — nothing to evaluate.")
    if pending:
        out.detail += f" {len(pending)} still within their deadline."
    return out


@resolver("incidents.register_exists")
def register_exists(ctx: ResolverContext, spec: EvidenceSpec) -> Outcome:
    used = ctx.db.execute(
        text("SELECT count(*) FROM compliance_incidents WHERE tenant_id = :t"), {"t": ctx.tenant_id}
    ).scalar() or 0
    framework = ctx.framework_id or ""
    open_ = [i for i in _incidents(ctx, framework or None) if i["status"] == "open"]
    overdue = []
    for inc in open_:
        staged = staged_rows(inc, framework, now=ctx.now) if framework else None
        if staged is not None:
            if any(r["status"] == "overdue" for r in staged):
                overdue.append(inc["id"])
            continue
        due = incident_deadline(inc, framework) if framework else None
        if due is not None and inc["reported_at"] is None and ctx.now > due:
            overdue.append(inc["id"])
    facts = {"tenant_incidents": int(used), "open_for_system": len(open_), "open_past_deadline": len(overdue)}
    refs = [{"type": "incident", "id": i, "outcome": "overdue"} for i in overdue]
    if not used:
        return Outcome(UNDETERMINED, 0, 0, "The incident register has never been used — an empty register "
                                           "cannot be told apart from an unused one.", facts=facts)
    n = len(open_) - len(overdue)
    if overdue:
        return Outcome(NOT_MET, n, len(open_), f"{len(overdue)} open incident(s) are past their reporting "
                                              f"deadline.", facts=facts, refs=refs)
    return Outcome(MET, n, len(open_), f"Register kept ({used} incident(s) recorded); {len(open_)} open for "
                                       f"this system, none past its deadline.", facts=facts, refs=refs)


__all__ = [
    "field_declared",
    "incident_deadline",
    "pointer_on_file",
    "register_exists",
    "registration_recorded",
    "reported_within_deadline",
    "staged_rows",
    "technical_documentation",
]
