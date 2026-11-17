# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Agent implementation checks, in the assurance check library

The credential decides WHO acts; these checks watch WHAT software acts and
WHICH release of it, against what an approver declared on the agent identity
(`implementation_name`, `approved_versions`, `require_release` — part of the
contract closure of every AI System the identity is a member of):

| code | fires when | severity | clears when |
|---|---|---|---|
| `agent.unapproved_release` | a run in the last 7 days whose version matched none of the `approved_versions` IN EFFECT WHEN IT STARTED (non-empty) | high on a high-risk system, else medium | no such run remains in the 7-day window (approving the version stops new ones) |
| `agent.name_mismatch` | the current release's name differs from `implementation_name` | high | the declared name matches again |
| `agent.release_missing` | `require_release` and the identity's latest run carries no name/version | medium | runs carry it again |
| `agent.undeclared_change` | same (name, version, build), a new implementation fingerprint after the release had ≥ 20 runs on others | high | acknowledged, or a new version is declared |

## Judged by when it ran

A release is unapproved only if it RAN while not approved. Each run is
judged by the declaration in effect at its `started_at`, read from
`agent_identity_approval_history` (periods [valid_from, valid_to), see
`agent_implementation.approved_versions_at`). Replacing ['0.1.0'] with
['0.2.0'] therefore does not turn 0.1.0 — approved the whole time it ran —
into a finding, while 0.2.0 runs before the approval still are one.

## One fact, one computation

`agent_findings(facts)` is the ONLY implementation. The inbox items raised
by `run_agent_checks`, the drift findings a response policy acts on and the
assurance report's composition section all read it. The inbox fingerprint is
`agent:<identity>:<code>:<release>` — one item per identity, rule and
release; an item whose finding no longer occurs is resolved automatically
(disposition `fixed`), like the composition checks.

## Responses

Each finding is ALSO an open drift finding on every AI System the identity
is a member of: `drift_class='agent'`, cause `agent_release`,
`metric_key='<code>:<identity>'`, severity per system (an unapproved release
is `high` on a high-risk system). The closed loop considers it like any
other finding, so a response policy on `drift_class: agent` — "an unapproved
release on a high-risk system ⇒ approval_required" — needs no new mechanism.
It resolves itself when the finding clears. It is not the system's own
behavioural drift (`is_own_drift`).

## Lifecycle (`agent.new_release`)

Every release row the core writes becomes one lifecycle event on each AI
System the identity was a member of when the release was first seen —
exactly once (a partial unique index backs the check).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ulid import ULID

from app.constants.agent_implementation import (
    DRIFT_CAUSE_AGENT_RELEASE,
    LIFECYCLE_AGENT_NEW_RELEASE,
)
from app.database.models.agent_identity import AgentIdentity, ResourceGroupAgentMember
from app.database.models.agent_implementation_release import AgentImplementationRelease
from app.database.models.ai_system_drift import DRIFT_AGENT, AISystemDriftEvent
from app.database.models.ai_system_lifecycle_event import AISystemLifecycleEvent, new_event_id
from app.database.models.ai_system_run import AISystemRun
from app.database.models.assurance_inbox import (
    DISPOSITION_FIXED,
    INBOX_STATE_ACKNOWLEDGED,
    INBOX_STATE_RESOLVED,
    AssuranceInboxItem,
)
from app.database.models.resource_group import GroupType, ResourceGroup
from app.services.agent_implementation import (
    ApprovalTimeline,
    approval_of,
    approval_timelines,
    approved_versions_at,
    release_key,
)
from app.services.ai_system_judgement_exclusion import evidence_exclusion
from app.services.drift_resolution import auto_clear

log = logging.getLogger("brutor.assurance-checks.agent")

FINDING_UNAPPROVED_RELEASE = "agent.unapproved_release"
FINDING_NAME_MISMATCH = "agent.name_mismatch"
FINDING_RELEASE_MISSING = "agent.release_missing"
FINDING_UNDECLARED_CHANGE = "agent.undeclared_change"
AGENT_FINDING_CODES = (
    FINDING_UNAPPROVED_RELEASE,
    FINDING_NAME_MISMATCH,
    FINDING_RELEASE_MISSING,
    FINDING_UNDECLARED_CHANGE,
)

#: Severities (the drift vocabulary a response policy compares).
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
#: … and the inbox's.
INBOX_SEVERITY = {SEVERITY_HIGH: "critical", SEVERITY_MEDIUM: "warning"}

#: Inbox category every agent finding is raised under.
INBOX_CATEGORY_AGENT = "agent"
#: Who resolves an item / drift finding that no longer occurs, and who
#: records a new-release lifecycle event.
SYSTEM_ACTOR_AGENT_CHECKS = "system:agent-checks"
SYSTEM_ACTOR_AGENT_RELEASES = "system:agent-releases"

#: An unapproved release clears when no run of it that was unapproved when
#: it ran lies within this many days.
UNAPPROVED_RELEASE_WINDOW_DAYS = 7
#: A release must have had this many runs on its established fingerprints
#: before a new fingerprint is a change rather than the release settling.
MIN_RUNS_FOR_UNDECLARED_CHANGE = 20
#: The tier at and above which an unapproved release is `high`.
HIGH_RISK_TIER = "high"

#: `ai_system_drift_events.metric_key` width.
_METRIC_KEY_WIDTH = 64


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def fingerprint(agent_id: str, code: str, release: Optional[str]) -> str:
    """Inbox dedup key: one item per (identity, rule, release)."""
    return f"agent:{agent_id}:{code}:{release or '-'}"


def metric_key(code: str, agent_id: str) -> str:
    """The drift finding's key: `<code>:<identity>` (every code starts
    `agent.`, which is what `is_agent_finding` recognises). An identity id
    too long for the column is replaced by a stable digest of it."""
    key = f"{code}:{agent_id}"
    if len(key) <= _METRIC_KEY_WIDTH:
        return key
    room = _METRIC_KEY_WIDTH - len(code) - 1
    return f"{code}:{hashlib.sha256(agent_id.encode()).hexdigest()[:room]}"


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


@dataclass
class AgentFacts:
    """Everything the agent rules read for one tenant, loaded once."""

    tenant_id: str
    now: datetime
    identities: Dict[str, AgentIdentity]
    #: agent id → the AI Systems it is a member of, with when it joined.
    memberships: Dict[str, Dict[str, Optional[datetime]]]
    #: AI System id → effective EU AI Act tier.
    tiers: Dict[str, Optional[str]]
    #: agent id → its releases, most recently seen first.
    releases: Dict[str, List[AgentImplementationRelease]]
    #: agent id → (name, version) of its latest run (require_release only).
    latest_run: Dict[str, Tuple[Optional[str], Optional[str], Optional[datetime]]] = field(
        default_factory=dict
    )
    #: agent id → its approval periods, oldest first.
    approval_timelines: Dict[str, ApprovalTimeline] = field(default_factory=dict)
    #: agent id → its runs in the unapproved-release window that carried a
    #: version: (started_at, name, version, build), oldest first.
    window_runs: Dict[str, List[Tuple[datetime, Optional[str], str, Optional[str]]]] = field(
        default_factory=dict
    )

    def current(self, agent_id: str) -> Optional[AgentImplementationRelease]:
        rows = self.releases.get(agent_id) or []
        return rows[0] if rows else None

    def systems_of(self, agent_id: str) -> List[str]:
        return sorted(self.memberships.get(agent_id) or {})

    def is_high_risk(self, sid: str) -> bool:
        from app.services.ai_system_composition import tier_rank

        return tier_rank(self.tiers.get(sid)) >= tier_rank(HIGH_RISK_TIER)


def load_agent_facts(db: Session, tenant_id: str, *, now: Optional[datetime] = None) -> AgentFacts:
    from app.services.ai_system_composition import load_tier_facts

    now = now or datetime.now(timezone.utc)
    identities = {
        a.id: a for a in db.query(AgentIdentity).filter(AgentIdentity.tenant_id == tenant_id)
    }
    ai_systems = {
        gid for (gid,) in db.query(ResourceGroup.id).filter(
            ResourceGroup.tenant_id == tenant_id,
            ResourceGroup.group_type == GroupType.AI_SYSTEM,
        )
    }
    memberships: Dict[str, Dict[str, Optional[datetime]]] = {}
    for agent_id, group_id, joined in db.query(
        ResourceGroupAgentMember.agent_id,
        ResourceGroupAgentMember.group_id,
        ResourceGroupAgentMember.joined_at,
    ).filter(ResourceGroupAgentMember.tenant_id == tenant_id):
        if group_id in ai_systems:
            memberships.setdefault(agent_id, {})[group_id] = _aware(joined)

    releases: Dict[str, List[AgentImplementationRelease]] = {}
    for r in db.query(AgentImplementationRelease).filter(
        AgentImplementationRelease.tenant_id == tenant_id
    ):
        releases.setdefault(r.agent_id, []).append(r)
    for rows in releases.values():
        rows.sort(key=lambda r: (_aware(r.last_seen_at), _aware(r.first_seen_at), r.id), reverse=True)

    facts = AgentFacts(
        tenant_id=tenant_id,
        now=now,
        identities=identities,
        memberships=memberships,
        tiers=load_tier_facts(db, tenant_id).effective,
        releases=releases,
    )
    for agent_id, agent in identities.items():
        if not agent.require_release:
            continue
        latest = (
            db.query(
                AISystemRun.agent_impl_name,
                AISystemRun.agent_impl_version,
                AISystemRun.started_at,
            )
            .filter(
                AISystemRun.tenant_id == tenant_id,
                AISystemRun.agent_id == agent_id,
                AISystemRun.is_replay.is_(False),
            )
            .order_by(AISystemRun.started_at.desc(), AISystemRun.id.desc())
            .first()
        )
        if latest is not None:
            facts.latest_run[agent_id] = (latest[0], latest[1], _aware(latest[2]))

    # The approval history, and the runs
    # it judges. Only identities that ever declared a non-empty approval can
    # have an unapproved run, so only theirs are read.
    facts.approval_timelines = approval_timelines(db, tenant_id)
    constrained = sorted(
        agent_id for agent_id, agent in identities.items()
        if (agent.approved_versions or [])
        or any(p[2] for p in facts.approval_timelines.get(agent_id, []))
    )
    if constrained:
        window_start = now - timedelta(days=UNAPPROVED_RELEASE_WINDOW_DAYS)
        for agent_id, started, name, version, build in (
            db.query(
                AISystemRun.agent_id,
                AISystemRun.started_at,
                AISystemRun.agent_impl_name,
                AISystemRun.agent_impl_version,
                AISystemRun.agent_impl_build,
            )
            .filter(
                AISystemRun.tenant_id == tenant_id,
                AISystemRun.agent_id.in_(constrained),
                AISystemRun.is_replay.is_(False),
                AISystemRun.agent_impl_version.isnot(None),
                AISystemRun.started_at >= window_start,
            )
            .order_by(AISystemRun.started_at.asc(), AISystemRun.id.asc())
        ):
            facts.window_runs.setdefault(agent_id, []).append(
                (_aware(started), name, version, build or None)
            )
    return facts


def unapproved_runs(
    facts: AgentFacts, agent_id: str
) -> Dict[Tuple[Optional[str], str, Optional[str]], List[Tuple[datetime, List[str]]]]:
    """The identity's runs in the window that were NOT approved when they
    started, grouped by release `(name, version, build)`: each run's start
    and the `approved_versions` in effect then. A run under an empty
    declaration (observe only) is never unapproved."""
    agent = facts.identities[agent_id]
    timeline = facts.approval_timelines.get(agent_id)
    out: Dict[Tuple[Optional[str], str, Optional[str]], List[Tuple[datetime, List[str]]]] = {}
    for started, name, version, build in facts.window_runs.get(agent_id, []):
        in_effect = approved_versions_at(timeline, started, agent.approved_versions)
        if approval_of(version, in_effect) is False:
            out.setdefault((name, version, build), []).append((started, in_effect))
    return out


def _fingerprint_change(
    db: Session, tenant_id: str, agent_id: str, release: AgentImplementationRelease
) -> Optional[Dict[str, Any]]:
    """The first run of `release` whose implementation fingerprint is new
    after the release had ≥ MIN_RUNS_FOR_UNDECLARED_CHANGE runs on other
    fingerprints — or None. Runs without a fingerprint say nothing."""
    q = (
        db.query(AISystemRun.id, AISystemRun.implementation_fingerprint, AISystemRun.started_at)
        .filter(
            AISystemRun.tenant_id == tenant_id,
            AISystemRun.agent_id == agent_id,
            AISystemRun.is_replay.is_(False),
            AISystemRun.agent_impl_name == release.name,
            AISystemRun.agent_impl_version == release.version,
            AISystemRun.implementation_fingerprint.isnot(None),
        )
    )
    # '' is the release table's "no build"; the run leaves it NULL.
    if release.build:
        q = q.filter(AISystemRun.agent_impl_build == release.build)
    else:
        q = q.filter((AISystemRun.agent_impl_build.is_(None)) | (AISystemRun.agent_impl_build == ""))
    counts: Dict[str, int] = {}
    for run_id, fp, started in q.order_by(AISystemRun.started_at.asc(), AISystemRun.id.asc()):
        if fp not in counts and sum(counts.values()) >= MIN_RUNS_FOR_UNDECLARED_CHANGE:
            established = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
            return {
                "run_id": run_id,
                "new_fingerprint": fp,
                "established_fingerprint": established[0],
                "runs_before": sum(counts.values()),
                "changed_at": _aware(started).isoformat() if started else None,
            }
        counts[fp] = counts.get(fp, 0) + 1
    return None


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


def _release_block(r: AgentImplementationRelease, approved_versions) -> Dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "version": r.version,
        "build": r.build or None,
        "trust": r.trust,
        "approved": approval_of(r.version, approved_versions),
        "first_seen_at": _aware(r.first_seen_at).isoformat() if r.first_seen_at else None,
        "last_seen_at": _aware(r.last_seen_at).isoformat() if r.last_seen_at else None,
        "run_count": int(r.run_count or 0),
    }


def _finding(
    facts: AgentFacts,
    agent: AgentIdentity,
    code: str,
    *,
    release: Optional[AgentImplementationRelease],
    message: str,
    severity_for,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    systems = {sid: severity_for(sid) for sid in facts.systems_of(agent.id)}
    overall = (
        SEVERITY_HIGH if SEVERITY_HIGH in systems.values()
        else (severity_for(None) if not systems else SEVERITY_MEDIUM)
    )
    key = release_key(release.name, release.version, release.build) if release else None
    return {
        "code": code,
        "severity": overall,
        "agent_id": agent.id,
        "agent_name": agent.name,
        "release": _release_block(release, agent.approved_versions) if release else None,
        "release_key": key,
        "systems": systems,
        "message": message,
        "evidence": evidence or {},
    }


def agent_findings(db: Session, facts: AgentFacts) -> List[Dict[str, Any]]:
    """Every current agent finding of the tenant. Deterministic order:
    identity, then rule, then release."""
    out: List[Dict[str, Any]] = []
    for agent_id in sorted(facts.identities):
        agent = facts.identities[agent_id]
        who = agent.name or agent_id
        approved = list(agent.approved_versions or [])
        current = facts.current(agent_id)

        # -- unapproved release: a run in the window that was not approved
        # WHEN IT RAN. Today's declaration
        # never re-judges a run made under an earlier one.
        by_release = {
            (r.name, r.version, r.build or None): r for r in facts.releases.get(agent_id) or []
        }
        for key, runs in sorted(
            unapproved_runs(facts, agent_id).items(),
            key=lambda kv: (by_release[kv[0]].id if kv[0] in by_release else "~", str(kv[0])),
        ):
            r = by_release.get(key)
            if r is None:
                # The core writes the release row in the same finaliser as the
                # run; a run without one is not yet a release to name.
                continue
            label = release_key(r.name, r.version, r.build)
            first_at, in_effect = runs[0]
            out.append(_finding(
                facts, agent, FINDING_UNAPPROVED_RELEASE, release=r,
                severity_for=lambda sid: (
                    SEVERITY_HIGH if sid is not None and facts.is_high_risk(sid)
                    else SEVERITY_MEDIUM
                ),
                message=(
                    f"{who} ran release {label} ({r.trust}) at {first_at.isoformat()} "
                    f"while it was not among the approved versions in effect then "
                    f"({', '.join(in_effect)}); {len(runs)} such run(s) in the last "
                    f"{UNAPPROVED_RELEASE_WINDOW_DAYS} days. Approve the version, "
                    f"or roll the agent back."
                ),
                evidence={
                    "approved_versions": approved,
                    "approved_versions_when_run": in_effect,
                    "first_unapproved_run_at": first_at.isoformat(),
                    "last_unapproved_run_at": runs[-1][0].isoformat(),
                    "unapproved_runs": len(runs),
                },
            ))

        # -- name mismatch: the current release is some other software.
        if agent.implementation_name and current is not None and current.name != agent.implementation_name:
            out.append(_finding(
                facts, agent, FINDING_NAME_MISMATCH, release=current,
                severity_for=lambda _sid: SEVERITY_HIGH,
                message=(
                    f"{who} declares itself as {current.name!r} ({current.trust}), but its "
                    f"implementation is declared as {agent.implementation_name!r}."
                ),
                evidence={"implementation_name": agent.implementation_name},
            ))

        # -- release missing: the latest run declared nothing.
        if agent.require_release and agent_id in facts.latest_run:
            name, version, started = facts.latest_run[agent_id]
            if not name or not version:
                out.append(_finding(
                    facts, agent, FINDING_RELEASE_MISSING, release=None,
                    severity_for=lambda _sid: SEVERITY_MEDIUM,
                    message=(
                        f"{who} must declare its release, but its latest run carried "
                        f"no implementation {'name' if not name else 'version'}."
                    ),
                    evidence={"latest_run_at": started.isoformat() if started else None},
                ))

        # -- undeclared change: same release, different software. Only the
        # current VERSION: declaring a new version clears it.
        if current is not None:
            for r in sorted(facts.releases.get(agent_id) or [], key=lambda r: r.id):
                if r.name != current.name or r.version != current.version:
                    continue
                if len(r.fingerprints or []) < 2:
                    continue  # one fingerprint cannot have changed
                change = _fingerprint_change(db, facts.tenant_id, agent_id, r)
                if change is None:
                    continue
                label = release_key(r.name, r.version, r.build)
                out.append(_finding(
                    facts, agent, FINDING_UNDECLARED_CHANGE, release=r,
                    severity_for=lambda _sid: SEVERITY_HIGH,
                    message=(
                        f"{who} still declares release {label}, but after "
                        f"{change['runs_before']} runs it started behaving like different "
                        f"software (new implementation fingerprint) — an implementation "
                        f"change nobody declared. Acknowledge it, or release a new version."
                    ),
                    evidence={"no_version_change": True, **change},
                ))
    return out


def findings_for_system(findings: List[Dict[str, Any]], sid: str) -> List[Dict[str, Any]]:
    """The findings of a system's member agents, with the system's own
    severity — what the assurance report's composition section lists."""
    return [
        {**f, "severity": f["systems"][sid]}
        for f in findings
        if sid in f["systems"]
    ]


# ---------------------------------------------------------------------------
# Into the inbox, the closed loop and the lifecycle record
# ---------------------------------------------------------------------------


def _acknowledged_fingerprints(db: Session, tenant_id: str, fps: Set[str]) -> Set[str]:
    """Undeclared-change fingerprints a HUMAN acknowledged or resolved: the
    RFC clears the finding on acknowledgement, so it must not be re-raised
    (a new item) or kept open as a drift finding."""
    if not fps:
        return set()
    out = set()
    for fp, state, resolved_by in db.query(
        AssuranceInboxItem.fingerprint,
        AssuranceInboxItem.state,
        AssuranceInboxItem.resolved_by,
    ).filter(
        AssuranceInboxItem.tenant_id == tenant_id,
        AssuranceInboxItem.fingerprint.in_(sorted(fps)),
    ):
        if state == INBOX_STATE_ACKNOWLEDGED or (
            state == INBOX_STATE_RESOLVED and resolved_by != SYSTEM_ACTOR_AGENT_CHECKS
        ):
            out.add(fp)
    return out


def emit_new_release_events(db: Session, facts: AgentFacts) -> int:
    """One `agent.new_release` lifecycle event per (release row, AI System
    the identity was a member of when the release was first seen). Returns
    how many were written. Idempotent: existing events are skipped, and a
    concurrent writer loses on the partial unique index, not twice."""
    existing = {
        (sid, subject)
        for sid, subject in db.query(
            AISystemLifecycleEvent.ai_system_id, AISystemLifecycleEvent.subject_id
        ).filter(
            AISystemLifecycleEvent.tenant_id == facts.tenant_id,
            AISystemLifecycleEvent.event_type == LIFECYCLE_AGENT_NEW_RELEASE,
        )
    }
    written = 0
    for agent_id in sorted(facts.releases):
        agent = facts.identities.get(agent_id)
        systems = facts.memberships.get(agent_id) or {}
        for r in sorted(facts.releases[agent_id], key=lambda r: (_aware(r.first_seen_at), r.id)):
            first = _aware(r.first_seen_at)
            label = release_key(r.name, r.version, r.build)
            for sid in sorted(systems):
                joined = systems[sid]
                if (sid, r.id) in existing or (joined is not None and first is not None and first < joined):
                    continue
                event = AISystemLifecycleEvent(
                    id=new_event_id(),
                    tenant_id=facts.tenant_id,
                    ai_system_id=sid,
                    event_type=LIFECYCLE_AGENT_NEW_RELEASE,
                    subject_id=r.id,
                    from_stage=None,
                    to_stage=None,
                    recorded_by=SYSTEM_ACTOR_AGENT_RELEASES,
                    note=f"New release {label} of {getattr(agent, 'name', None) or agent_id} ({r.trust})",
                    details={
                        "agent_id": agent_id,
                        "agent_name": getattr(agent, "name", None),
                        "release_id": r.id,
                        "name": r.name,
                        "version": r.version,
                        "build": r.build or None,
                        "trust": r.trust,
                        "approved": approval_of(r.version, getattr(agent, "approved_versions", None)),
                    },
                    created_at=first or facts.now,
                )
                try:
                    with db.begin_nested():
                        db.add(event)
                        db.flush()
                except IntegrityError:
                    continue  # another instance recorded it first
                existing.add((sid, r.id))
                written += 1
    return written


def _evidence_at(f: Dict[str, Any], now: datetime) -> datetime:
    """When a finding's evidence arose: the first run that was unapproved
    when it ran, the undeclared change's first run, the latest run that
    declared nothing, else the release's first sighting."""
    ev = f.get("evidence") or {}
    raw = (
        ev.get("first_unapproved_run_at")
        or ev.get("changed_at")
        or ev.get("latest_run_at")
        or (f.get("release") or {}).get("first_seen_at")
    )
    return _aware(datetime.fromisoformat(raw)) if raw else now


def _sync_drift_findings(
    db: Session, facts: AgentFacts, findings: List[Dict[str, Any]]
) -> Tuple[int, int]:
    """Open a `drift_class='agent'` finding per (system, current finding)
    and resolve the open ones that no longer occur. Returns (opened, resolved)."""
    wanted: Dict[Tuple[str, str], Dict[str, Any]] = {}
    #: The NEWEST evidence behind each (system, key): several
    #: releases can share one finding, and a new one must re-open it.
    evidence_at: Dict[Tuple[str, str], datetime] = {}
    for f in findings:
        for sid, severity in f["systems"].items():
            key = (sid, metric_key(f["code"], f["agent_id"]))
            prev = wanted.get(key)
            # Several releases of one identity can be unapproved at once; the
            # system has ONE finding per (rule, identity), at the worst severity.
            if prev is None or (severity == SEVERITY_HIGH and prev["severity"] != SEVERITY_HIGH):
                wanted[key] = {**f, "severity": severity}
            at = _evidence_at(f, facts.now)
            evidence_at[key] = max(evidence_at.get(key, at), at)
    open_events = {
        (ev.ai_system_id, ev.metric_key): ev
        for ev in db.query(AISystemDriftEvent).filter(
            AISystemDriftEvent.tenant_id == facts.tenant_id,
            AISystemDriftEvent.drift_class == DRIFT_AGENT,
            AISystemDriftEvent.status.in_(("active", "acknowledged")),
        )
    }
    opened = 0
    for (sid, key), f in sorted(wanted.items()):
        if (sid, key) in open_events:
            continue
        # A human resolution of this finding is honoured: evidence
        # at/before its epoch (or inside an incident window) does not re-open
        # it; a new offending release, first seen later, does.
        # Windows excuse performance, never facts: only the finding's own
        # epoch (a human accepted it) settles an agent finding.
        if evidence_exclusion(db, facts.tenant_id, sid, key, evidence_at[(sid, key)], windows=False):
            continue
        db.add(AISystemDriftEvent(
            id=f"drift-{ULID()}",
            tenant_id=facts.tenant_id,
            ai_system_id=sid,
            actor_id=f["agent_id"],
            drift_class=DRIFT_AGENT,
            metric_key=key,
            detected_at=facts.now,
            change_point_at=facts.now,
            direction="shift",
            severity=f["severity"],
            attributed_cause=DRIFT_CAUSE_AGENT_RELEASE,
            cause_confidence=1.0,
            cause_evidence={
                "code": f["code"],
                "agent_id": f["agent_id"],
                "agent_name": f["agent_name"],
                "release": f["release"],
                "message": f["message"],
                **f["evidence"],
            },
        ))
        opened += 1
    resolved = 0
    for (sid, key), ev in open_events.items():
        if (sid, key) in wanted:
            continue
        auto_clear(
            ev,
            by=SYSTEM_ACTOR_AGENT_CHECKS,
            note="The agent check no longer finds this (approved, matched or redeclared).",
            at=facts.now,
        )
        resolved += 1
    return opened, resolved


def run_agent_checks(
    db: Session,
    tenant_id: str,
    *,
    now: Optional[datetime] = None,
    inbox=None,
) -> Dict[str, int]:
    """Raise every current agent finding of a tenant into the inbox, keep
    the matching drift findings open for the closed loop, resolve what no
    longer occurs, and record new releases on the lifecycle.

    Returns `{raised, resolved, drift_opened, drift_resolved, release_events}`.
    Flushes; the caller commits.
    """
    from app.services.assurance_inbox import AssuranceInboxService

    inbox = inbox or AssuranceInboxService()
    facts = load_agent_facts(db, tenant_id, now=now)
    findings = agent_findings(db, facts)

    change_fps = {
        fingerprint(f["agent_id"], f["code"], f["release_key"])
        for f in findings if f["code"] == FINDING_UNDECLARED_CHANGE
    }
    acknowledged = _acknowledged_fingerprints(db, tenant_id, change_fps)
    live = [
        f for f in findings
        if fingerprint(f["agent_id"], f["code"], f["release_key"]) not in acknowledged
    ]

    current: Set[str] = set(acknowledged)
    raised = 0
    for f in live:
        fp = fingerprint(f["agent_id"], f["code"], f["release_key"])
        current.add(fp)
        systems = f["systems"]
        # One item per identity; it names the system where it matters most.
        primary = next(
            (sid for sid in sorted(systems) if systems[sid] == f["severity"]), None
        )
        item = inbox.raise_item(
            db,
            tenant_id=tenant_id,
            category=INBOX_CATEGORY_AGENT,
            severity=INBOX_SEVERITY[f["severity"]],
            fingerprint=fp,
            title=f["message"],
            ai_system_id=primary,
            evidence={
                "code": f["code"],
                "agent_id": f["agent_id"],
                "release": f["release"],
                "systems": sorted(systems),
                **f["evidence"],
            },
        )
        raised += int(item is not None)

    resolved = 0
    for item in (
        db.query(AssuranceInboxItem)
        .filter(
            AssuranceInboxItem.tenant_id == tenant_id,
            AssuranceInboxItem.category == INBOX_CATEGORY_AGENT,
            AssuranceInboxItem.state != INBOX_STATE_RESOLVED,
        )
        .all()
    ):
        if item.fingerprint in current:
            continue
        inbox.resolve(
            db,
            item,
            admin=SYSTEM_ACTOR_AGENT_CHECKS,
            disposition=DISPOSITION_FIXED,
            note=(
                "Cleared automatically: the agent check no longer finds this "
                "(version approved, name matches, release declared, or a new "
                "version released)."
            ),
        )
        resolved += 1

    drift_opened, drift_resolved = _sync_drift_findings(db, facts, live)
    release_events = emit_new_release_events(db, facts)
    db.flush()
    return {
        "raised": raised,
        "resolved": resolved,
        "drift_opened": drift_opened,
        "drift_resolved": drift_resolved,
        "release_events": release_events,
    }


def run_agent_checks_all(db: Session) -> Dict[str, int]:
    """Every tenant that has an agent identity. One failing tenant does not
    stop the rest; each tenant commits on its own."""
    totals = {"raised": 0, "resolved": 0, "drift_opened": 0, "drift_resolved": 0,
              "release_events": 0}
    tenants = [t for (t,) in db.query(AgentIdentity.tenant_id).distinct().all()]
    for tenant_id in sorted(tenants):
        try:
            stats = run_agent_checks(db, tenant_id)
            db.commit()
        except Exception:  # noqa: BLE001 — one tenant must not stop the tick
            db.rollback()
            log.exception("agent checks failed for tenant %s", tenant_id)
            continue
        for k in totals:
            totals[k] += stats[k]
    return totals


__all__ = [
    "AGENT_FINDING_CODES",
    "AgentFacts",
    "FINDING_NAME_MISMATCH",
    "FINDING_RELEASE_MISSING",
    "FINDING_UNAPPROVED_RELEASE",
    "FINDING_UNDECLARED_CHANGE",
    "INBOX_CATEGORY_AGENT",
    "MIN_RUNS_FOR_UNDECLARED_CHANGE",
    "SYSTEM_ACTOR_AGENT_CHECKS",
    "SYSTEM_ACTOR_AGENT_RELEASES",
    "UNAPPROVED_RELEASE_WINDOW_DAYS",
    "agent_findings",
    "emit_new_release_events",
    "findings_for_system",
    "fingerprint",
    "load_agent_facts",
    "metric_key",
    "run_agent_checks",
    "run_agent_checks_all",
    "unapproved_runs",
]
