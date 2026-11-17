# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Declared vs observed

`intended_clients` was first added with the comment *"compared against the
runtime 'accessed by' rollup"*. Nothing ever built the rollup. This does.

The declaration is typed: `intended_client_labels` are compared
against the `client_label` of the system's own runs, and
`intended_client_systems` against the root systems of its run segments — the
callers of a shared service, which has no runs of its own. And "models used"
is the system's OWN share of its runs: a delegate's model calls happen inside
the root's runs, and counting them as the root's made a composition finding
fire (or hide) on the wrong system. Models used through dependencies are
reported separately, per dependency.

Three comparisons, and the framing is what makes them worth anything: they
compare what an operator **declared** the system is for against what the
gateway **observed** it doing. That sentence is only expressible because
the lifecycle is keyed on a declared *system* rather than on an agent
credential — a credential has no intended use to diverge from.

## Why over-provisioning is a finding and not a metric

A resource granted and never used is a least-privilege finding: it is
blast radius nobody is getting value from. Reporting it as a percentage
buries it; reporting it as a list of names an operator can act on does not.

The reverse — a client calling that was never declared — is the sharper
finding, and on a high-risk system it is the one a risk owner reads first.

## The honest limit

We see clients that come **through the gateway**. A system also reachable
by some other path would show a clean client conformance here and still be
wide open, so every report states the scope rather than implying total
coverage. The claim is *"We detect declared-vs-observed divergence"*, never *"we
know every client that touched it"*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import and_, func, or_, text
from sqlalchemy.orm import Session

from app.constants.outcomes import (
    CONFORMANCE_DECLARED,
    CONFORMANCE_UNDECLARED,
    CONFORMANCE_UNREPORTED,
    EXPECT_FORBIDS_CAPABILITIES,
    EXPECT_REQUIRES_CAPABILITIES,
)
from app.database.models.ai_system_run import AISystemRun
from app.database.models.ai_system_run_segment import AISystemRunSegment
from app.database.models.proxy_log import ProxyLog
from app.database.models.resource_group import ResourceGroup
from app.services import ai_system_outcome_catalogue as catalogue_rules
from app.services.ai_system_down_rule import CALL_ANSWERED, classify_segment_outcome
from app.services.ai_system_outcomes import COMPLETED_TERMINAL_STATES

#: A finding's weight in the conformance score. Undeclared access is a
#: security question; unused grants are a hygiene question. Scoring them the
#: same would let ten stale bindings outrank one unknown caller.
UNDECLARED_CLIENT_PENALTY = 0.25
UNUSED_RESOURCE_PENALTY = 0.05

#: Ending somewhere the contract never approved is a goal-hijack
#: signal at the result level (OWASP Agentic Top 10 2026, ASI01): weighted
#: like an undeclared caller. The other outcome findings are evidence and
#: hygiene questions. One penalty per distinct subject, never per run.
OUTCOME_FINDING_PENALTY: Dict[str, float] = {
    "undeclared_outcome": 0.25,
    "outcome_unreported": 0.1,
    "outcome_evidence_mismatch": 0.1,
    "unexpected_delegate_outcome": 0.1,
    # A declared outcome not yet observed is a precision hint — the contract
    # may be broader than the system needs — not a fault. It stays a (low)
    # finding but does not lower the score: penalising it marks a system down
    # for declaring a rare-but-legitimate end, and pushes owners to declare
    # fewer outcomes, so the rare end later arrives undeclared.
    "unused_outcome": 0.0,
}
#: The newest this-many declared runs with an `expect` are checked for
#: evidence per evaluation — enough to evidence a pattern, bounded so a busy
#: system's report stays one query.
MAX_EVIDENCE_RUNS = 2000
#: Run ids quoted per evidence finding.
EVIDENCE_EXAMPLES = 3

#: Below this many observed calls there is not enough traffic to say a
#: declared client is *missing* rather than merely quiet this week.
MIN_CALLS_FOR_CLIENT_FINDING = 20


@dataclass
class ConformanceFinding:
    kind: str
    detail: str
    severity: str = "medium"
    subject: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "severity": self.severity,
            "subject": self.subject,
        }


@dataclass
class ConformanceReport:
    declared_clients: List[str] = field(default_factory=list)
    observed_clients: List[str] = field(default_factory=list)
    undeclared_clients: List[str] = field(default_factory=list)
    unused_declared_clients: List[str] = field(default_factory=list)
    granted_resources: List[str] = field(default_factory=list)
    used_resources: List[str] = field(default_factory=list)
    unused_resources: List[str] = field(default_factory=list)
    #: Callers that are AI Systems (from run segments' roots).
    declared_client_systems: List[str] = field(default_factory=list)
    observed_client_systems: Dict[str, int] = field(default_factory=dict)
    undeclared_client_systems: List[str] = field(default_factory=list)
    #: Models used inside this system's runs by its dependencies,
    #: `{dependency system id: [model, ...]}`. Never part of `used_resources`.
    models_used_via_dependencies: Dict[str, List[str]] = field(default_factory=dict)
    #: Runs made by the system's own member agents: counted
    #: as observed traffic, never judged against the intended client labels.
    member_agent_runs: int = 0
    #: The outcome catalogue of the ACTIVE contract (reportable
    #: ids) against what the runs reported. `observed_outcomes`: {full id:
    #: runs} of every reported id; `undeclared_outcomes`: the reported ids it
    #: does not declare; `unreported_runs`: completed runs with no outcome;
    #: `unused_outcomes`: declared ids never reported in the window.
    declared_outcomes: List[str] = field(default_factory=list)
    observed_outcomes: Dict[str, int] = field(default_factory=dict)
    undeclared_outcomes: Dict[str, int] = field(default_factory=dict)
    unreported_runs: int = 0
    unused_outcomes: List[str] = field(default_factory=list)
    #: {outcome id: {"runs", "run_ids", "violations"}} — runs whose evidence
    #: broke the outcome's merged `expect` (cheap outcomes need evidence).
    evidence_mismatches: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: {delegate system id: {outcome id ("" = none): calls}} — answers
    #: outside the dependency's `expected_outcomes`.
    unexpected_delegate_outcomes: Dict[str, Dict[str, int]] = field(default_factory=dict)
    findings: List[ConformanceFinding] = field(default_factory=list)
    score: Optional[float] = None
    observed_call_count: int = 0
    #: Stated on every report rather than assumed. See the module docs.
    scope_note: str = (
        "Covers traffic routed through the gateway. A system reachable by "
        "another path would not appear here."
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "declared_clients": self.declared_clients,
            "observed_clients": self.observed_clients,
            "undeclared_clients": self.undeclared_clients,
            "unused_declared_clients": self.unused_declared_clients,
            "granted_resources": self.granted_resources,
            "used_resources": self.used_resources,
            "unused_resources": self.unused_resources,
            "declared_client_systems": self.declared_client_systems,
            "observed_client_systems": self.observed_client_systems,
            "undeclared_client_systems": self.undeclared_client_systems,
            "models_used_via_dependencies": self.models_used_via_dependencies,
            "member_agent_runs": self.member_agent_runs,
            "declared_outcomes": self.declared_outcomes,
            "observed_outcomes": self.observed_outcomes,
            "undeclared_outcomes": self.undeclared_outcomes,
            "unreported_runs": self.unreported_runs,
            "unused_outcomes": self.unused_outcomes,
            "evidence_mismatches": self.evidence_mismatches,
            "unexpected_delegate_outcomes": self.unexpected_delegate_outcomes,
            "findings": [f.to_dict() for f in self.findings],
            "score": self.score,
            "observed_call_count": self.observed_call_count,
            "scope_note": self.scope_note,
        }


#: A system is never its own client. A run whose actor is
#: one of the system's MEMBER agent identities is the system doing its own
#: work; it is judged on implementation and release (the agent checks), not
#: against `intended_client_labels`, which keep meaning channels (portal,
#: SDK integrations, named apps). `client_label` itself is the normalised
#: value the core writes (X-Brutor-Client, else the first User-Agent product
#: name without its version).
_MEMBER_AGENT_RUN = """
       r.agent_id IS NOT NULL
       AND r.agent_id IN (
           SELECT m.agent_id FROM resource_group_agent_members m
            WHERE m.tenant_id = :tenant_id AND m.group_id = :ai_system_id)
"""

#: Incident windows excuse PERFORMANCE, never FACTS.
#: Declared-vs-observed is a fact — an undeclared caller seen only during an
#: outage is still an undeclared caller — so NOTHING in this module applies
#: the judgement exclusion (static guard:
#: tests/test_judgement_exclusion_single_predicate.py::FACT_MODULES).

_CLIENTS_SQL = text(
    f"""
    SELECT COALESCE(r.client_label, 'unlabelled') AS client, COUNT(*) AS calls
      FROM ai_system_runs r
     WHERE r.tenant_id = :tenant_id
       AND r.ai_system_id = :ai_system_id
       AND NOT r.is_replay
       AND r.started_at >= :cutoff
       AND NOT ({_MEMBER_AGENT_RUN})
     GROUP BY 1
     ORDER BY 2 DESC
    """
)

_MEMBER_RUNS_SQL = text(
    f"""
    SELECT COUNT(*) AS runs
      FROM ai_system_runs r
     WHERE r.tenant_id = :tenant_id
       AND r.ai_system_id = :ai_system_id
       AND NOT r.is_replay
       AND r.started_at >= :cutoff
       AND ({_MEMBER_AGENT_RUN})
    """
)

def _cutoff(period_hours: int) -> datetime:
    if period_hours <= 0:
        # All time. A fixed early date rather than None keeps the SQL
        # single-shaped instead of branching on a nullable bind.
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - timedelta(hours=period_hours)


def member_agent_runs(
    db: Session, tenant_id: str, ai_system_id: str, period_hours: int
) -> int:
    """Runs of the system made by its own member agents in the window — the
    system at work. Counted toward `observed_call_count` (the system is
    measured) but never judged as a client."""
    return int(db.execute(
        _MEMBER_RUNS_SQL,
        {
            "tenant_id": tenant_id,
            "ai_system_id": ai_system_id,
            "cutoff": _cutoff(period_hours),
        },
    ).scalar() or 0)


def observed_clients(
    db: Session, tenant_id: str, ai_system_id: str, period_hours: int
) -> Dict[str, int]:
    """`{client label: runs}` of the system's runs whose actor is NOT one of
    its member agents."""
    rows = db.execute(
        _CLIENTS_SQL,
        {
            "tenant_id": tenant_id,
            "ai_system_id": ai_system_id,
            "cutoff": _cutoff(period_hours),
        },
    ).mappings()
    return {r["client"]: int(r["calls"]) for r in rows}


def own_rows(db: Session, tenant_id: str, ai_system_id: str, *columns):
    """A query over the proxy_logs rows that are this system's OWN steps.

    THE own-rows rule — conformance's "models used" and the
    Composition tab's per-connection status both read it, so the two can
    never disagree about which calls were the system's own.

    A run rooted here also carries its delegates' steps (root-plus-via). A
    step belongs to this system unless its resource group resolves to a
    DIFFERENT AI System — the same attribution the core run ledger uses to
    cut segments (nearest AI System up the group tree). Steps with no group
    cannot belong to another system and stay the root's. No time filter:
    each caller applies its own window.
    """
    from app.services.ai_system_composition import nearest_ai_system_map

    foreign_groups = [
        rg for rg, sid in nearest_ai_system_map(db, tenant_id).items()
        if sid is not None and sid != ai_system_id
    ]
    q = (
        db.query(*columns)
        .join(
            AISystemRun,
            and_(
                AISystemRun.tenant_id == ProxyLog.tenant_id,
                AISystemRun.root_task_id == ProxyLog.root_task_id,
            ),
        )
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id == ai_system_id,
        )
    )
    if foreign_groups:
        q = q.filter(
            or_(
                ProxyLog.resource_group_id.is_(None),
                ProxyLog.resource_group_id.notin_(foreign_groups),
            )
        )
    return q


def _granted_name_by_model_name(db: Session, granted_models: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """`llm_model.model_name` → the display name a grant carries, for the
    models granted in the closure."""
    from app.database.models.llm_model import LLModel

    by_id = {m.get("id"): (m.get("name") or m.get("id")) for m in granted_models if m.get("id")}
    if not by_id:
        return {}
    rows = db.query(LLModel.id, LLModel.model_name).filter(LLModel.id.in_(list(by_id))).all()
    return {model_name: by_id[mid] for mid, model_name in rows if model_name}


def used_models(
    db: Session, tenant_id: str, ai_system_id: str, period_hours: int
) -> List[str]:
    """Models this system's OWN steps requested (`own_rows`), in its runs in
    the window."""
    q = own_rows(db, tenant_id, ai_system_id, ProxyLog.llm_model_requested).filter(
        ProxyLog.llm_model_requested.isnot(None),
        AISystemRun.started_at >= _cutoff(period_hours),
    )
    return sorted({name for (name,) in q.distinct().all() if name})


def models_used_via_dependencies(
    db: Session, tenant_id: str, ai_system_id: str, period_hours: int
) -> Dict[str, List[str]]:
    """`{dependency system id: sorted models}` from the segments of this
    system's runs — the core ledger's own attribution of delegated work."""
    out: Dict[str, set] = {}
    rows = (
        db.query(AISystemRunSegment.segment_system_id, AISystemRunSegment.models_used)
        .filter(
            AISystemRunSegment.tenant_id == tenant_id,
            AISystemRunSegment.root_system_id == ai_system_id,
            AISystemRunSegment.first_action_at >= _cutoff(period_hours),
        )
        .all()
    )
    for sid, models in rows:
        if isinstance(models, dict):
            out.setdefault(sid, set()).update(k for k in models if k)
    return {sid: sorted(names) for sid, names in sorted(out.items())}


def models_used_serving(
    db: Session, tenant_id: str, ai_system_id: str, period_hours: int
) -> List[str]:
    """Models this system's steps requested while SERVING other systems'
    runs — its segments. A shared service's work lives only there; leaving
    it out read every model it uses as an unused grant."""
    names: set = set()
    for (models,) in (
        db.query(AISystemRunSegment.models_used)
        .filter(
            AISystemRunSegment.tenant_id == tenant_id,
            AISystemRunSegment.segment_system_id == ai_system_id,
            AISystemRunSegment.first_action_at >= _cutoff(period_hours),
        )
        .all()
    ):
        if isinstance(models, dict):
            names.update(k for k in models if k)
    return sorted(names)


def observed_client_systems(
    db: Session, tenant_id: str, ai_system_id: str, period_hours: int
) -> Dict[str, int]:
    """`{caller system id: inbound calls}` from this system's segments.

    The caller is the IMMEDIATE calling AI System (`caller_system_id`)
    — in A → B → C, C's caller is B, which is who C accepts
    (or not) in `intended_client_systems`. A segment with no caller system
    (the caller is in no AI System, or was deleted) is not a caller anyone
    can declare, so it is not counted as one.
    """
    rows = (
        db.query(AISystemRunSegment.caller_system_id, func.count(AISystemRunSegment.id))
        .filter(
            AISystemRunSegment.tenant_id == tenant_id,
            AISystemRunSegment.segment_system_id == ai_system_id,
            AISystemRunSegment.caller_system_id.isnot(None),
            AISystemRunSegment.first_action_at >= _cutoff(period_hours),
        )
        .group_by(AISystemRunSegment.caller_system_id)
        .all()
    )
    return {sid: int(n) for sid, n in rows}


# ---------------------------------------------------------------------------
# Declared outcomes: facts
# ---------------------------------------------------------------------------


def observed_outcome_counts(
    db: Session, tenant_id: str, ai_system_id: str, period_hours: int
) -> Dict[tuple, int]:
    """`{(outcome_id or None, conformance): runs}` over the system's finished
    runs in the window (a fact — no judgement exclusion)."""
    rows = (
        db.query(
            AISystemRun.outcome_id,
            AISystemRun.outcome_conformance,
            AISystemRun.terminal_state,
            func.count(AISystemRun.id),
        )
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id == ai_system_id,
            AISystemRun.is_replay.is_(False),
            AISystemRun.started_at >= _cutoff(period_hours),
        )
        .group_by(
            AISystemRun.outcome_id, AISystemRun.outcome_conformance, AISystemRun.terminal_state,
        )
        .all()
    )
    out: Dict[tuple, int] = {}
    for outcome_id, conformance, state, n in rows:
        # "Unreported" is a finding only on a run that COMPLETED: an errored
        # run usually could not know its outcome.
        if conformance == CONFORMANCE_UNREPORTED and state not in COMPLETED_TERMINAL_STATES:
            continue
        key = (outcome_id, conformance)
        out[key] = out.get(key, 0) + int(n)
    return out


def evidence_mismatches(
    db: Session,
    tenant_id: str,
    ai_system_id: str,
    period_hours: int,
    catalogue: Optional[List[Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    """Evidence: declared runs whose facts break their outcome's merged `expect`
    (action_count, llm_call_count, total_cost_usd and the capabilities the
    run's steps used). `{outcome id: {runs, run_ids, violations}}`.

    Each run is held to the catalogue it was JUDGED against — the contract
    stamped on the run (`contract_id`), else `catalogue` (the active one) —
    the core's rule, so a promotion never re-grades history."""
    from app.database.models.ai_system_contract import AISystemContract

    runs = (
        db.query(
            AISystemRun.id,
            AISystemRun.root_task_id,
            AISystemRun.outcome_id,
            AISystemRun.contract_id,
            AISystemRun.action_count,
            AISystemRun.llm_call_count,
            AISystemRun.total_cost_usd,
        )
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.ai_system_id == ai_system_id,
            AISystemRun.is_replay.is_(False),
            AISystemRun.started_at >= _cutoff(period_hours),
            AISystemRun.outcome_conformance == CONFORMANCE_DECLARED,
        )
        .order_by(AISystemRun.started_at.desc(), AISystemRun.id.desc())
        .limit(MAX_EVIDENCE_RUNS)
        .all()
    )
    stamped = sorted({r.contract_id for r in runs if r.contract_id})
    catalogues: Dict[Optional[str], Any] = {None: catalogue}
    if stamped:
        for cid, closure in db.query(AISystemContract.id, AISystemContract.resolved_closure).filter(
            AISystemContract.tenant_id == tenant_id, AISystemContract.id.in_(stamped)
        ):
            catalogues[cid] = (closure or {}).get("outcomes")

    def expect_of(r) -> Optional[Dict[str, Any]]:
        cat = catalogues.get(r.contract_id, catalogue) if r.contract_id else catalogue
        return catalogue_rules.merged_expect(cat, r.outcome_id)

    checked = [(r, exp) for r in runs if (exp := expect_of(r))]
    needs_caps = {
        r.root_task_id
        for r, exp in checked
        if EXPECT_REQUIRES_CAPABILITIES in exp or EXPECT_FORBIDS_CAPABILITIES in exp
    }
    caps: Dict[str, set] = {}
    if needs_caps:
        for root, cap in (
            db.query(ProxyLog.root_task_id, ProxyLog.mcp_capability_name)
            .filter(
                ProxyLog.tenant_id == tenant_id,
                ProxyLog.root_task_id.in_(sorted(needs_caps)),
                ProxyLog.mcp_capability_name.isnot(None),
            )
            .distinct()
            .all()
        ):
            caps.setdefault(root, set()).add(cap)
    out: Dict[str, Dict[str, Any]] = {}
    for r, exp in checked:
        broken = catalogue_rules.expect_violations(
            exp,
            action_count=int(r.action_count or 0),
            llm_call_count=int(r.llm_call_count or 0),
            total_cost_usd=float(r.total_cost_usd or 0),
            capabilities=caps.get(r.root_task_id, ()),
        )
        if not broken:
            continue
        entry = out.setdefault(r.outcome_id, {"runs": 0, "run_ids": [], "violations": []})
        entry["runs"] += 1
        if len(entry["run_ids"]) < EVIDENCE_EXAMPLES:
            entry["run_ids"].append(r.id)
        for v in broken:
            if v not in entry["violations"]:
                entry["violations"].append(v)
    return out


def delegate_outcomes(
    db: Session, tenant_id: str, ai_system_id: str, period_hours: int
) -> Dict[str, Dict[str, int]]:
    """`{delegate system id: {outcome id ("" = none): answered calls}}` —
    what this system's calls into its dependencies came back with. Only
    ANSWERED calls (THE segment classification): a delegate that did not
    answer reported nothing to judge."""
    rows = (
        db.query(
            AISystemRunSegment.segment_system_id,
            AISystemRunSegment.outcome,
            AISystemRunSegment.outcome_id,
            func.count(AISystemRunSegment.id),
        )
        .filter(
            AISystemRunSegment.tenant_id == tenant_id,
            AISystemRunSegment.caller_system_id == ai_system_id,
            AISystemRunSegment.first_action_at >= _cutoff(period_hours),
        )
        .group_by(
            AISystemRunSegment.segment_system_id,
            AISystemRunSegment.outcome,
            AISystemRunSegment.outcome_id,
        )
        .all()
    )
    out: Dict[str, Dict[str, int]] = {}
    for target, outcome, outcome_id, n in rows:
        if classify_segment_outcome(outcome) != CALL_ANSWERED:
            continue
        per = out.setdefault(target, {})
        per[outcome_id or ""] = per.get(outcome_id or "", 0) + int(n)
    return out


def expected_delegate_outcomes(db: Session, system: ResourceGroup) -> Dict[str, List[str]]:
    """`{delegate system id: expected_outcomes}` of the system's declared
    shared-service dependencies that set the list (NULL = not declared)."""
    from app.database.models.ai_system_dependency import AISystemDependency

    return {
        e.depends_on_system_id: list(e.expected_outcomes)
        for e in db.query(AISystemDependency).filter(
            AISystemDependency.tenant_id == system.tenant_id,
            AISystemDependency.ai_system_id == system.id,
            AISystemDependency.depends_on_system_id.isnot(None),
        )
        if e.expected_outcomes is not None
    }


def _expected(outcome_id: str, expected: Sequence[str]) -> bool:
    """An expected entry `<outcome>` covers every variant of it."""
    if not outcome_id:
        return False
    if outcome_id in expected:
        return True
    return outcome_id.partition(".")[0] in expected


def _outcome_findings(
    report: "ConformanceReport",
    *,
    declared_ids: Optional[Sequence[str]],
    outcome_counts: Dict[tuple, int],
    evidence: Dict[str, Dict[str, Any]],
    delegates: Dict[str, Dict[str, int]],
    expected: Dict[str, List[str]],
    total_calls: int,
) -> None:
    report.declared_outcomes = sorted(declared_ids or [])
    for (outcome_id, conformance), n in sorted(
        outcome_counts.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))
    ):
        if outcome_id:
            report.observed_outcomes[outcome_id] = report.observed_outcomes.get(outcome_id, 0) + n
        if conformance == CONFORMANCE_UNDECLARED and outcome_id is not None:
            report.undeclared_outcomes[outcome_id] = report.undeclared_outcomes.get(outcome_id, 0) + n
        elif conformance == CONFORMANCE_UNREPORTED:
            report.unreported_runs += n

    for outcome_id, n in sorted(report.undeclared_outcomes.items()):
        report.findings.append(ConformanceFinding(
            kind="undeclared_outcome",
            subject=outcome_id,
            severity="high",
            detail=(
                f"{n} run(s) ended in {outcome_id!r}, an outcome the active contract does not "
                f"declare — the system ended somewhere it was never approved to."
            ),
        ))
    if report.unreported_runs:
        report.findings.append(ConformanceFinding(
            kind="outcome_unreported",
            severity="medium",
            detail=(
                f"{report.unreported_runs} completed run(s) reported no outcome "
                f"(X-Brutor-Outcome-Id): a completed run without a declared outcome is a "
                f"deviation, not a success."
            ),
        ))
    for outcome_id, ev in sorted(evidence.items()):
        report.evidence_mismatches[outcome_id] = ev
        report.findings.append(ConformanceFinding(
            kind="outcome_evidence_mismatch",
            subject=outcome_id,
            severity="medium",
            detail=(
                f"{ev['runs']} run(s) reporting {outcome_id!r} broke its declared evidence: "
                f"{'; '.join(ev['violations'])} (e.g. {', '.join(ev['run_ids'])})."
            ),
        ))
    if declared_ids and total_calls >= MIN_CALLS_FOR_CLIENT_FINDING:
        report.unused_outcomes = [d for d in sorted(declared_ids) if d not in report.observed_outcomes]
        for outcome_id in report.unused_outcomes:
            report.findings.append(ConformanceFinding(
                kind="unused_outcome",
                subject=outcome_id,
                severity="low",
                detail=(
                    f"{outcome_id!r} is declared but no run ended there in this window — "
                    f"an approved path nobody exercises."
                ),
            ))
    for target in sorted(expected):
        allowed = expected[target]
        off = {
            oid: n for oid, n in sorted((delegates.get(target) or {}).items())
            if not _expected(oid, allowed)
        }
        if not off:
            continue
        report.unexpected_delegate_outcomes[target] = off
        shown = ", ".join(f"{oid or '(none)'} ×{n}" for oid, n in off.items())
        report.findings.append(ConformanceFinding(
            kind="unexpected_delegate_outcome",
            subject=target,
            severity="medium",
            detail=(
                f"Dependency {target!r} answered with outcome(s) this system is not declared "
                f"to handle ({shown}); expected {sorted(allowed)}. The composition has "
                f"drifted from what was signed off."
            ),
        ))


def evaluate(
    *,
    declared_clients: Optional[Sequence[str]],
    observed: Dict[str, int],
    granted_resources: Sequence[str],
    used: Sequence[str],
    risk_tier: Optional[str] = None,
    declared_client_systems: Optional[Sequence[str]] = None,
    observed_systems: Optional[Dict[str, int]] = None,
    via_dependencies: Optional[Dict[str, List[str]]] = None,
    member_agent_runs: int = 0,
    declared_outcome_ids: Optional[Sequence[str]] = None,
    outcome_counts: Optional[Dict[tuple, int]] = None,
    evidence: Optional[Dict[str, Dict[str, Any]]] = None,
    delegate_outcome_counts: Optional[Dict[str, Dict[str, int]]] = None,
    expected_delegate: Optional[Dict[str, List[str]]] = None,
) -> ConformanceReport:
    """The judgement, as a pure function over already-gathered facts.

    `declared_clients` / `observed` are client labels of the runs NOT made
    by the system's own member agents; `member_agent_runs`
    counts those, so a system called only by its own agent is measured
    (its grants are judged) without being judged as its own client.
    `declared_client_systems` / `observed_systems` are caller AI Systems
    (segments). A shared service is called only through segments, so its
    inbound calls count toward `observed_call_count` — it is measured.
    """
    declared = sorted({c for c in (declared_clients or []) if c})
    observed_names = sorted(observed)
    observed_systems = dict(observed_systems or {})
    declared_systems = sorted({c for c in (declared_client_systems or []) if c})
    total_calls = (
        sum(observed.values()) + sum(observed_systems.values()) + int(member_agent_runs or 0)
    )

    granted = sorted({r for r in granted_resources if r})
    used_names = sorted({r for r in used if r})

    report = ConformanceReport(
        declared_clients=declared,
        observed_clients=observed_names,
        granted_resources=granted,
        used_resources=used_names,
        observed_call_count=total_calls,
        declared_client_systems=declared_systems,
        observed_client_systems=observed_systems,
        models_used_via_dependencies=dict(via_dependencies or {}),
        member_agent_runs=int(member_agent_runs or 0),
    )

    # --- caller systems -----------------------------------
    report.undeclared_client_systems = sorted(
        sid for sid in observed_systems if sid not in declared_systems
    )
    for caller in report.undeclared_client_systems:
        report.findings.append(
            ConformanceFinding(
                kind="undeclared_client_system",
                subject=caller,
                severity="high" if risk_tier == "high" else "medium",
                detail=(
                    f"AI System {caller!r} called this system but is not in its "
                    f"intended client systems."
                ),
            )
        )

    # --- client conformance ---------------------------------------------
    if declared:
        report.undeclared_clients = [c for c in observed_names if c not in declared]
        # Only meaningful with enough traffic to say a client is *absent*
        # rather than merely quiet this week.
        if total_calls >= MIN_CALLS_FOR_CLIENT_FINDING:
            report.unused_declared_clients = [
                c for c in declared if c not in observed
            ]

        for client in report.undeclared_clients:
            # On a high-risk system an undeclared caller is the finding a
            # risk owner reads first, so it is escalated rather than
            # uniformly "medium".
            severity = "high" if risk_tier == "high" else "medium"
            report.findings.append(
                ConformanceFinding(
                    kind="undeclared_client",
                    subject=client,
                    severity=severity,
                    detail=(
                        f"{client!r} called this system but is not in its "
                        f"declared intended clients."
                    ),
                )
            )
        for client in report.unused_declared_clients:
            report.findings.append(
                ConformanceFinding(
                    kind="declared_client_absent",
                    subject=client,
                    severity="low",
                    detail=(
                        f"{client!r} is declared as an intended client but has "
                        f"not called in this window."
                    ),
                )
            )
    elif observed_names and not declared_systems:
        report.findings.append(
            ConformanceFinding(
                kind="no_declared_clients",
                severity="low",
                detail=(
                    "No intended clients are declared, so there is nothing to "
                    "compare observed callers against."
                ),
            )
        )

    # --- composition conformance ----------------------------------------
    report.unused_resources = [r for r in granted if r not in used_names]
    if total_calls >= MIN_CALLS_FOR_CLIENT_FINDING:
        for resource in report.unused_resources:
            report.findings.append(
                ConformanceFinding(
                    kind="unused_grant",
                    subject=resource,
                    severity="low",
                    detail=(
                        f"{resource!r} is granted to this system but was never "
                        f"used — blast radius nobody is getting value from."
                    ),
                )
            )

    # --- declared outcomes -------------------------------------
    _outcome_findings(
        report,
        declared_ids=declared_outcome_ids,
        outcome_counts=dict(outcome_counts or {}),
        evidence=dict(evidence or {}),
        delegates=dict(delegate_outcome_counts or {}),
        expected=dict(expected_delegate or {}),
        total_calls=total_calls,
    )

    report.score = _score(report)
    return report


def _score(report: ConformanceReport) -> Optional[float]:
    """1.0 is "everything observed was declared, everything granted was used".

    `None` — not 1.0 — when nothing was observed. A system with no traffic
    has not demonstrated conformance; it has demonstrated nothing, and
    scoring that as perfect is exactly the kind of confident number the
    report must never show.
    """
    if report.observed_call_count == 0:
        return None

    penalty = (
        (len(report.undeclared_clients) + len(report.undeclared_client_systems))
        * UNDECLARED_CLIENT_PENALTY
        + len(report.unused_resources) * UNUSED_RESOURCE_PENALTY
        + sum(OUTCOME_FINDING_PENALTY.get(f.kind, 0.0) for f in report.findings)
    )
    return max(0.0, min(1.0, 1.0 - penalty))


def for_system(
    db: Session,
    system: ResourceGroup,
    *,
    period_hours: int = 720,
    granted_resources: Optional[Sequence[str]] = None,
) -> ConformanceReport:
    """Gather the facts and evaluate them."""
    observed = observed_clients(db, system.tenant_id, system.id, period_hours)
    # Own steps in its own runs, plus its steps in the runs it served.
    used = sorted(
        set(used_models(db, system.tenant_id, system.id, period_hours))
        | set(models_used_serving(db, system.tenant_id, system.id, period_hours))
    )
    callers = observed_client_systems(db, system.tenant_id, system.id, period_hours)
    via = models_used_via_dependencies(db, system.tenant_id, system.id, period_hours)
    own_agent_runs = member_agent_runs(db, system.tenant_id, system.id, period_hours)

    # Outcomes are judged against the ACTIVE contract's catalogue
    # (what the core judged them against), never an unapproved edit.
    from app.services.ai_system_contract_service import current_contract

    active = current_contract(db, system.tenant_id, system.id)
    catalogue = (active.resolved_closure or {}).get("outcomes") if active else None
    outcome_counts = observed_outcome_counts(db, system.tenant_id, system.id, period_hours)
    evidence = evidence_mismatches(db, system.tenant_id, system.id, period_hours, catalogue)
    expected = expected_delegate_outcomes(db, system)
    delegates = (
        delegate_outcomes(db, system.tenant_id, system.id, period_hours) if expected else {}
    )

    if granted_resources is None:
        from app.services.ai_system_closure import resolve_closure

        closure = resolve_closure(db, system)
        granted_models = closure.get("composition", {}).get("llm_models", [])
        granted_resources = [m.get("name") or m.get("id") for m in granted_models]
        # A grant is named by its display name, a step by the model string it
        # sent (`llm_model.model_name`): translate the step's string to the
        # grant it used, or every model in use reads "granted but never used".
        used = sorted({_granted_name_by_model_name(db, granted_models).get(u, u) for u in used})

    return evaluate(
        declared_clients=system.intended_client_labels,
        observed=observed,
        granted_resources=granted_resources or [],
        used=used,
        risk_tier=system.eu_ai_act_risk_tier,
        declared_client_systems=system.intended_client_systems,
        observed_systems=callers,
        via_dependencies=via,
        member_agent_runs=own_agent_runs,
        declared_outcome_ids=catalogue_rules.all_ids(catalogue),
        outcome_counts=outcome_counts,
        evidence=evidence,
        delegate_outcome_counts=delegates,
        expected_delegate=expected,
    )


__all__ = [
    "MIN_CALLS_FOR_CLIENT_FINDING",
    "OUTCOME_FINDING_PENALTY",
    "delegate_outcomes",
    "evidence_mismatches",
    "expected_delegate_outcomes",
    "observed_outcome_counts",
    "UNDECLARED_CLIENT_PENALTY",
    "UNUSED_RESOURCE_PENALTY",
    "ConformanceFinding",
    "ConformanceReport",
    "evaluate",
    "for_system",
    "member_agent_runs",
    "models_used_serving",
    "models_used_via_dependencies",
    "observed_client_systems",
    "observed_clients",
    "own_rows",
    "used_models",
]
