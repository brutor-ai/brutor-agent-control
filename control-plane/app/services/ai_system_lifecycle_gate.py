# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The lifecycle gate

`lifecycle_stage` has existed as a column with nothing behind it: any value
could be set at any time, so it recorded an intention rather than a fact.
This gives each transition an evidence requirement, which is what converts
"we have governance" into "approved in three weeks instead of nine months".

## The gate refuses with a list, not a verdict

The single most important behaviour here is that a refusal **names every
missing piece of evidence at once**. A gate that reports the first failure
sends an operator round the loop once per requirement, and after the third
round trip they go looking for the override. `evaluate()` therefore always
computes the full set, and the API returns it before the user clicks
anything so the UI can show what is missing rather than what went wrong.

## Requirements are data

The default table is a module constant and the per-tenant override is a
JSON column, so a tenant can tighten or relax without a migration. High
risk tier is not an override but a floor: `eu_ai_act_risk_tier='high'`
adds human approval to every transition, and a tenant policy cannot remove
it. A configurable control that can be configured away is decoration.

## What this does not do

## The identity floor

Moving to `active` (from `approved`, re-promoting, or back from
`deprecated`) requires `workload_identity_enforced`: the identity policy at
`active` requires workload identity, and no machine caller reached the
system's subtree below that level in the last 7 days with a credential that
can still authenticate. Like the high-risk human approval it is a floor,
re-added after any tenant policy. A retired development key (revoked,
expired or deleted) clears it at once; the calls it made stay on record.

It does not decide *whether* config is allowed to change — that is
enforcement, and it already happens. It decides whether a system may move
to a stage that asserts something about its readiness. An `active` system
whose config changed still runs; it just carries an unpromoted contract,
and declared-vs-observed conformance is what surfaces that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.models.ai_asset import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_APPROVED,
    LIFECYCLE_DEPRECATED,
    LIFECYCLE_PROPOSED,
    LIFECYCLE_RETIRED,
)
from app.database.models.ai_system_liveness import AISystemLivenessExpectation
from app.database.models.ai_system_run import AISystemRun
from app.database.models.resource_group import ResourceGroup
from app.services import ai_system_contract_service as contracts

# ---------------------------------------------------------------------------
# Requirement vocabulary
# ---------------------------------------------------------------------------

REQ_CONTRACT_MINTED = "contract_minted"
REQ_OWNER_SET = "owner_set"
REQ_INTENDED_USE = "intended_use_set"
REQ_NO_UNAPPROVED_WIDENING = "no_unapproved_widening"
REQ_LIVENESS_SET = "liveness_expectation_set"
REQ_APPROVER_RECORDED = "approver_recorded"
REQ_HUMAN_APPROVAL = "human_approval"
REQ_SUCCESSOR_NAMED = "successor_named"
REQ_NO_RECENT_RUNS = "no_recent_runs"
REQ_VALID_TRANSITION = "valid_transition"
REQ_REPLAY_PASSED = "replay_passed"
REQ_IMPACT_ASSESSMENT = "impact_assessment_on_file"
REQ_WORKLOAD_IDENTITY = "workload_identity_enforced"

#: How far back the identity floor looks for calls below the level.
IDENTITY_LOOKBACK_DAYS = 7

#: The transitions the identity floor holds: every way into `active`.
IDENTITY_FLOOR_TRANSITIONS = (
    f"{LIFECYCLE_APPROVED}->{LIFECYCLE_ACTIVE}",
    f"{LIFECYCLE_ACTIVE}->{LIFECYCLE_ACTIVE}",
    f"{LIFECYCLE_DEPRECATED}->{LIFECYCLE_ACTIVE}",
)

#: How many offending credentials a requirement names.
OFFENDING_CREDENTIALS_LIMIT = 20

#: Human-readable, in the second person, because these strings are what an
#: operator reads when the gate refuses. "REQ_OWNER_SET failed" tells them
#: nothing they can act on.
REQUIREMENT_LABELS: Dict[str, str] = {
    REQ_CONTRACT_MINTED: "A contract has been minted for the current configuration",
    REQ_OWNER_SET: "An accountable owner is named",
    REQ_INTENDED_USE: "Intended use is described",
    REQ_NO_UNAPPROVED_WIDENING: "No unapproved widening of permissions since the last approved contract",
    REQ_LIVENESS_SET: "A liveness expectation is set, or liveness is explicitly opted out",
    REQ_APPROVER_RECORDED: "An approver is recorded on the contract",
    REQ_HUMAN_APPROVAL: "A human has approved this transition",
    REQ_SUCCESSOR_NAMED: "A successor is named, or 'no successor' is stated explicitly",
    REQ_NO_RECENT_RUNS: "The system has produced no runs for the required quiet period",
    REQ_VALID_TRANSITION: "This is a recognised lifecycle transition",
    REQ_REPLAY_PASSED: "The bound replay suite passed against the current contract",
    REQ_IMPACT_ASSESSMENT: "An unexpired impact assessment is attached as evidence",
    REQ_WORKLOAD_IDENTITY: (
        "Machine callers authenticate with workload identity: the identity policy requires it, "
        "and no call in the last 7 days arrived below that level"
    ),
}

#: Days of silence required before a deprecated system may be retired.
DEFAULT_RETIREMENT_QUIET_DAYS = 30

#: Keyed by `(from_stage, to_stage)`.
DEFAULT_REQUIREMENTS: Dict[str, List[str]] = {
    f"{LIFECYCLE_PROPOSED}->{LIFECYCLE_APPROVED}": [
        REQ_CONTRACT_MINTED,
        REQ_OWNER_SET,
        REQ_INTENDED_USE,
    ],
    f"{LIFECYCLE_APPROVED}->{LIFECYCLE_ACTIVE}": [
        REQ_CONTRACT_MINTED,
        REQ_APPROVER_RECORDED,
        REQ_LIVENESS_SET,
        REQ_REPLAY_PASSED,
        REQ_WORKLOAD_IDENTITY,
    ],
    f"{LIFECYCLE_ACTIVE}->{LIFECYCLE_ACTIVE}": [
        REQ_NO_UNAPPROVED_WIDENING,
        REQ_WORKLOAD_IDENTITY,
    ],
    f"{LIFECYCLE_DEPRECATED}->{LIFECYCLE_ACTIVE}": [
        REQ_WORKLOAD_IDENTITY,
    ],
    f"{LIFECYCLE_ACTIVE}->{LIFECYCLE_DEPRECATED}": [
        REQ_SUCCESSOR_NAMED,
    ],
    f"{LIFECYCLE_DEPRECATED}->{LIFECYCLE_RETIRED}": [
        REQ_NO_RECENT_RUNS,
    ],
}

#: Transitions that are always allowed regardless of evidence — going
#: *backwards* is how an operator corrects a mistake, and a gate that traps
#: a system in `active` is a gate people route around. Back into `active`
#: from `deprecated` is not among them: it puts the system in production
#: again, under the identity floor.
ALWAYS_ALLOWED = frozenset({
    f"{LIFECYCLE_APPROVED}->{LIFECYCLE_PROPOSED}",
    f"{LIFECYCLE_ACTIVE}->{LIFECYCLE_APPROVED}",
    f"{LIFECYCLE_RETIRED}->{LIFECYCLE_DEPRECATED}",
})

RISK_TIER_HIGH = "high"


@dataclass
class Requirement:
    key: str
    label: str
    satisfied: bool
    detail: Optional[str] = None
    #: `workload_identity_enforced` when unmet: the credentials below the
    #: level, newest first (at most 20), and how many there are.
    offending_credentials: Optional[List[Dict[str, Any]]] = None
    offending_total: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "satisfied": self.satisfied,
            "detail": self.detail,
            "offending_credentials": self.offending_credentials,
            "offending_total": self.offending_total,
        }


@dataclass
class GateDecision:
    from_stage: Optional[str]
    to_stage: str
    allowed: bool
    requirements: List[Requirement] = field(default_factory=list)

    @property
    def unmet(self) -> List[Requirement]:
        return [r for r in self.requirements if not r.satisfied]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "allowed": self.allowed,
            "requirements": [r.to_dict() for r in self.requirements],
            "unmet": [r.to_dict() for r in self.unmet],
        }

    def refusal_message(self) -> str:
        missing = "; ".join(r.label for r in self.unmet)
        return (
            f"Cannot move from {self.from_stage or 'unset'} to {self.to_stage}: "
            f"{missing}"
        )


class GateRefused(Exception):
    def __init__(self, decision: GateDecision):
        super().__init__(decision.refusal_message())
        self.decision = decision


# ---------------------------------------------------------------------------
# Requirement resolution
# ---------------------------------------------------------------------------


def is_known_transition(from_stage: Optional[str], to_stage: str) -> bool:
    """Whether the platform recognises this move at all.

    Everything not named in the requirement table, the always-allowed set,
    or a no-op is a **stage skip** — `proposed → retired`, say. Those are
    refused rather than waved through: an unlisted transition falling
    through to "no requirements" would mean the way to bypass every gate is
    to jump two stages at once, which is worse than having no gate, because
    the fleet board would still report the system as governed.
    """
    stage = from_stage or LIFECYCLE_PROPOSED
    if stage == to_stage:
        return True
    key = f"{stage}->{to_stage}"
    return key in DEFAULT_REQUIREMENTS or key in ALWAYS_ALLOWED


def validate_tenant_policy(policy: Optional[Dict[str, Any]]) -> List[str]:
    """Check a tenant gate policy before it is stored. Returns problems.

    `required_keys` is deliberately forgiving at evaluation time — an unknown
    transition key is ignored, an unknown requirement is filtered out. That is
    right for evaluation (a policy written against a future release must not
    break promotion today) and wrong for *writing*: an operator who mistypes
    `proposed->aproved` would save successfully, see no error, and believe a
    requirement is enforced that silently does nothing.

    So the write path validates what the read path tolerates. Returns a list
    of human-readable problems; empty means acceptable.
    """
    if policy is None:
        return []
    problems: List[str] = []
    if not isinstance(policy, dict):
        return ["Gate policy must be an object keyed 'from_stage->to_stage'."]

    for key, value in policy.items():
        if key not in DEFAULT_REQUIREMENTS and key not in ALWAYS_ALLOWED:
            problems.append(
                f"'{key}' is not a recognised transition. Known transitions: "
                + ", ".join(sorted(DEFAULT_REQUIREMENTS))
            )
            continue
        if not isinstance(value, list):
            problems.append(
                f"'{key}' must map to a list of requirement keys, got "
                f"{type(value).__name__}."
            )
            continue
        for req in value:
            if req not in REQUIREMENT_LABELS:
                problems.append(
                    f"'{key}' names unknown requirement '{req}'. Known "
                    "requirements: " + ", ".join(sorted(REQUIREMENT_LABELS))
                )
    return problems


def required_keys(
    system: ResourceGroup,
    to_stage: str,
    *,
    tenant_policy: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Which requirements apply, tenant overrides and risk floor included."""
    from_stage = system.lifecycle_stage or LIFECYCLE_PROPOSED
    if not is_known_transition(from_stage, to_stage):
        # The only requirement, and it cannot be satisfied — the caller has
        # to go via the intervening stage.
        return [REQ_VALID_TRANSITION]

    key = f"{from_stage}->{to_stage}"
    if key in ALWAYS_ALLOWED:
        return []

    keys = list(DEFAULT_REQUIREMENTS.get(key, []))
    if tenant_policy:
        override = tenant_policy.get(key)
        if isinstance(override, list):
            keys = [k for k in override if k in REQUIREMENT_LABELS]

    # A floor, not an override: high-risk systems always need a human, and
    # no tenant policy can take that away.
    if system.eu_ai_act_risk_tier == RISK_TIER_HIGH:
        if REQ_HUMAN_APPROVAL not in keys:
            keys.append(REQ_HUMAN_APPROVAL)
        # Same floor semantics for the impact assessment, but only at the
        # approval step: approval is the review where a document like an
        # Article 27 FRIA is actually read. Repeating the demand on every
        # later transition would be evidence theatre — the artifact does
        # not get truer by being checked twice. (Lower tiers can opt in
        # per-transition via the tenant policy, because the key is in
        # REQUIREMENT_LABELS like any other.)
        if (
            key == f"{LIFECYCLE_PROPOSED}->{LIFECYCLE_APPROVED}"
            and REQ_IMPACT_ASSESSMENT not in keys
        ):
            keys.append(REQ_IMPACT_ASSESSMENT)

    # A floor too: every way into `active` needs workload identity, whatever
    # the tenant policy says.
    if key in IDENTITY_FLOOR_TRANSITIONS and REQ_WORKLOAD_IDENTITY not in keys:
        keys.append(REQ_WORKLOAD_IDENTITY)

    return keys


def evaluate(
    db: Session,
    system: ResourceGroup,
    to_stage: str,
    *,
    tenant_policy: Optional[Dict[str, Any]] = None,
    approved_by: Optional[str] = None,
    successor_id: Optional[str] = None,
    no_successor: bool = False,
) -> GateDecision:
    """Every requirement, satisfied or not — never short-circuited.

    Short-circuiting would return the *first* problem and send the operator
    round the loop once per requirement.
    """
    from_stage = system.lifecycle_stage
    keys = required_keys(system, to_stage, tenant_policy=tenant_policy)

    checks = {
        REQ_CONTRACT_MINTED: lambda: _check_contract(db, system),
        REQ_OWNER_SET: lambda: _simple(
            bool(system.owner), "No owner set on this AI System"
        ),
        REQ_INTENDED_USE: lambda: _simple(
            bool(system.intended_use and system.intended_use.strip()),
            "Intended use is empty",
        ),
        REQ_NO_UNAPPROVED_WIDENING: lambda: _check_widening(db, system),
        REQ_LIVENESS_SET: lambda: _check_liveness(db, system),
        REQ_APPROVER_RECORDED: lambda: _check_approver(db, system),
        REQ_HUMAN_APPROVAL: lambda: _simple(
            bool(approved_by), "This transition must be approved by a named person"
        ),
        REQ_SUCCESSOR_NAMED: lambda: _simple(
            bool(successor_id)
            or no_successor
            or bool(getattr(system, "successor_system_id", None))
            or bool(getattr(system, "no_successor_declared", False)),
            "Name a successor system, or state explicitly that there is none",
        ),
        REQ_NO_RECENT_RUNS: lambda: _check_quiet(db, system),
        REQ_REPLAY_PASSED: lambda: _check_replay(db, system),
        REQ_IMPACT_ASSESSMENT: lambda: _check_impact_assessment(db, system),
        REQ_WORKLOAD_IDENTITY: lambda: _check_workload_identity(db, system, to_stage),
        REQ_VALID_TRANSITION: lambda: (
            False,
            f"{from_stage or LIFECYCLE_PROPOSED} → {to_stage} skips a "
            f"lifecycle stage; move through the intervening stage instead",
        ),
    }

    requirements = []
    for key in keys:
        satisfied, detail, *offending = checks[key]()
        requirement = Requirement(
            key=key,
            label=REQUIREMENT_LABELS[key],
            satisfied=satisfied,
            detail=None if satisfied else detail,
        )
        if offending and not satisfied:
            requirement.offending_credentials, requirement.offending_total = offending
        requirements.append(requirement)

    return GateDecision(
        from_stage=from_stage,
        to_stage=to_stage,
        allowed=all(r.satisfied for r in requirements),
        requirements=requirements,
    )


def _simple(ok: bool, detail: str):
    return ok, detail


def _check_contract(db: Session, system: ResourceGroup):
    latest = contracts.latest_contract(db, system.tenant_id, system.id)
    if latest is None:
        return False, "No contract has been minted for this system"
    if contracts.is_config_drifted(db, system):
        return (
            False,
            f"Configuration has changed since contract v{latest.version} was "
            f"minted; mint a new one",
        )
    return True, None


def _check_approver(db: Session, system: ResourceGroup):
    latest = contracts.latest_contract(db, system.tenant_id, system.id)
    if latest is None:
        return False, "No contract to approve"
    if not latest.is_approved:
        return False, f"Contract v{latest.version} has not been approved"
    return True, None


def _check_widening(db: Session, system: ResourceGroup):
    latest = contracts.latest_contract(db, system.tenant_id, system.id)
    if latest is None:
        return False, "No contract has been minted for this system"
    summary = latest.diff_summary or {}
    if summary.get("has_widened_permissions") and not latest.is_approved:
        widened = summary.get("widened") or []
        detail = "; ".join(str(w.get("detail")) for w in widened[:3])
        return False, f"Unapproved widening in contract v{latest.version}: {detail}"
    return True, None


def _check_liveness(db: Session, system: ResourceGroup):
    # An expectation row satisfies this whether or not it is `enabled`:
    # a disabled row *is* the explicit opt-out. What the gate refuses is
    # the absence of a decision, because "nobody ever considered whether
    # this system going silent matters" is the state that produces the
    # 3am surprise.
    expectation = (
        db.query(AISystemLivenessExpectation)
        .filter(
            AISystemLivenessExpectation.tenant_id == system.tenant_id,
            AISystemLivenessExpectation.ai_system_id == system.id,
        )
        .first()
    )
    if expectation is None:
        return (
            False,
            "No liveness expectation — set one, or disable it explicitly, so "
            "'this system stopped working' is a question the platform can answer",
        )
    return True, None


def _check_replay(db: Session, system: ResourceGroup):
    """Whether the bound replay suite has passed against the live contract.

    Three states, and the distinction between them is the point:

    * **No suite bound** — satisfied, but the detail says so. Requiring a
      suite would block every system before anyone has recorded traffic to
      build one from, and a gate that cannot be satisfied on day one is a
      gate operators disable on day two. What it must not do is stay
      *silent*: "nothing to replay" and "replay passed" are different facts.
    * **Bound but not replayable** — refused. A suite marked
      `replayable = false` describes a system whose behaviour cannot be
      reproduced; treating that as a pass would let the least testable
      systems through the strictest gate.
    * **Bound and runnable** — needs a completed run at or above the
      threshold with zero outcome changes on critical cases.
    """
    from app.database.models.ai_system_replay import (
        REPLAY_RUNNING,
        AISystemReplayRun,
        AISystemReplaySuite,
    )

    suite_id = getattr(system, "replay_suite_id", None)
    if not suite_id:
        return True, None

    suite = (
        db.query(AISystemReplaySuite)
        .filter(
            AISystemReplaySuite.id == suite_id,
            AISystemReplaySuite.tenant_id == system.tenant_id,
        )
        .first()
    )
    if suite is None:
        return False, f"Replay suite {suite_id!r} no longer exists"

    if not suite.replayable:
        reason = suite.not_replayable_reason or "no reason recorded"
        return (
            False,
            f"Suite {suite.name!r} is marked not replayable ({reason}). "
            f"Unbind it or record why this system may be promoted without "
            f"replay evidence — an un-runnable suite is not a pass.",
        )
    if not suite.enabled:
        return False, f"Replay suite {suite.name!r} is disabled"

    latest = (
        db.query(AISystemReplayRun)
        .filter(
            AISystemReplayRun.tenant_id == system.tenant_id,
            AISystemReplayRun.suite_id == suite.id,
        )
        .order_by(AISystemReplayRun.started_at.desc())
        .first()
    )
    if latest is None:
        return False, f"Suite {suite.name!r} has never been run"

    if latest.status == REPLAY_RUNNING and latest.finished_at is None:
        # Still a non-pass — a run in flight proves nothing yet — but say
        # so. "Passed not scored, below the threshold" read as a failed
        # replay while the proxy had not even started it.
        started = latest.started_at.strftime("%H:%M:%S") if latest.started_at else "just now"
        return (
            False,
            f"Replay of {suite.name!r} is still running (started {started}); "
            f"the gate re-evaluates when it finishes",
        )

    if latest.passes(suite.pass_threshold):
        return True, None

    if latest.critical_outcome_changed:
        return (
            False,
            f"{latest.critical_outcome_changed} critical case(s) ended "
            f"differently under this contract",
        )
    rate = latest.pass_rate
    shown = "not scored" if rate is None else f"{rate:.0%}"
    return (
        False,
        f"Last replay passed {shown}, below the {suite.pass_threshold:.0%} "
        f"threshold for {suite.name!r}",
    )


def _check_impact_assessment(db: Session, system: ResourceGroup):
    """An unexpired `impact_assessment` evidence row exists for this system.

    Expired is distinguished from absent in the detail, because the fix is
    different: absent means go do the assessment; expired means the one you
    have needs renewing — and its title tells you which document that is.
    """
    from app.database.models.ai_system_evidence import (
        EVIDENCE_IMPACT_ASSESSMENT,
        AISystemEvidence,
    )

    rows = (
        db.query(AISystemEvidence)
        .filter(
            AISystemEvidence.tenant_id == system.tenant_id,
            AISystemEvidence.ai_system_id == system.id,
            AISystemEvidence.kind == EVIDENCE_IMPACT_ASSESSMENT,
        )
        .all()
    )
    if not rows:
        return (
            False,
            "No impact assessment is attached. Attach one under Evidence — "
            "the document stays in your document system; the platform "
            "records where it lives and how to verify it",
        )
    live = [r for r in rows if not r.is_expired]
    if not live:
        latest = max(rows, key=lambda r: r.valid_until or r.created_at)
        expired_on = (
            latest.valid_until.date().isoformat() if latest.valid_until else "unknown"
        )
        return (
            False,
            f"The impact assessment on file ({latest.title!r}) expired on "
            f"{expired_on}; attach a renewed assessment",
        )
    return True, None


#: The query of the identity floor: machine calls of the subtree in the
#: window below the level (or personal keys where the policy refuses them),
#: not refused, made with a credential that can still authenticate (a key
#: neither revoked nor expired nor deleted; a workload token whose agent is
#: neither revoked nor expired; a kind with no such record counts).
_IDENTITY_FLOOR_SQL = """
SELECT pl.credential_ref, pl.credential_kind, pl.credential_assurance,
       COUNT(*) AS calls, MAX(pl.timestamp) AS last_seen,
       COUNT(*) OVER () AS offending_total
  FROM proxy_logs pl
  LEFT JOIN api_keys ak ON ak.id = pl.api_key_id AND ak.tenant_id = pl.tenant_id
  LEFT JOIN agent_identities ag ON ag.id = pl.actor_agent_id AND ag.tenant_id = pl.tenant_id
 WHERE pl.tenant_id = :tenant_id
   AND pl.resource_group_id IN (SELECT id FROM subtree)
   AND {window}
   AND pl.credential_kind IN ('api_key', 'workload_token', 'x509_svid', 'brutor_token')
   AND (pl.credential_policy_outcome IS NULL OR pl.credential_policy_outcome <> 'refused')
   AND (pl.credential_assurance IN :below
        OR (pl.credential_kind = 'api_key' AND ak.scope = 'user' AND NOT :allow_user_keys))
   AND (
        (pl.credential_kind = 'api_key' AND ak.id IS NOT NULL AND NOT ak.revoked
         AND (ak.expires_at IS NULL OR ak.expires_at > {now}))
     OR (pl.credential_kind = 'workload_token'
         AND (pl.actor_agent_id IS NULL
              OR (ag.id IS NOT NULL AND ag.revoked_at IS NULL
                  AND (ag.expires_at IS NULL OR ag.expires_at > {now}))))
     OR pl.credential_kind IN ('x509_svid', 'brutor_token')
   )
 GROUP BY pl.credential_ref, pl.credential_kind, pl.credential_assurance
 ORDER BY last_seen DESC, pl.credential_ref
 LIMIT :limit
"""


def _iso(value: Any) -> Optional[str]:
    """A timestamp from either database as ISO-8601 (SQLite returns text)."""
    from datetime import datetime, timezone

    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _check_workload_identity(db: Session, system: ResourceGroup, to_stage: str):
    """The identity floor: the policy at `to_stage` requires workload
    identity, and no live credential reached the subtree below its level in
    the look-back window."""
    from sqlalchemy import bindparam, text

    from app.services import identity_policy as ip

    try:
        effective = ip.resolve_group(db, system, to_stage=to_stage)
    except Exception as exc:  # noqa: BLE001 — a gate that cannot decide refuses
        return False, f"The identity policy could not be resolved: {exc}"
    policy = effective["effective"]
    required = policy["min_assurance"]
    lowered = bool(effective["sources"]["min_assurance"].get("lowered_by_exception"))
    if not (
        (policy["mode"] == "enforce" and ip.level_rank(required) >= ip.level_rank("workload"))
        or (required == ip.EXCEPTION_LEVEL and lowered)
    ):
        return (
            False,
            f"The identity policy at {to_stage} requires {required} in {policy['mode']} mode; "
            "production requires workload identity, enforced",
        )

    window, params = ip.since_clause(db, "pl.timestamp", IDENTITY_LOOKBACK_DAYS)
    now, now_params = ip.now_clause(db)
    statement = text(ip.SUBTREE_CTE + _IDENTITY_FLOOR_SQL.format(window=window, now=now)).bindparams(
        bindparam("below", expanding=True)
    )
    rows = db.execute(
        statement,
        {
            "tenant_id": system.tenant_id,
            "ai_system_id": system.id,
            "below": ip.levels_below(required),
            "allow_user_keys": bool(policy["allow_user_keys"]),
            "limit": OFFENDING_CREDENTIALS_LIMIT,
            **params,
            **now_params,
        },
    ).fetchall()
    if not rows:
        return True, None
    offending = [
        {
            "credential_ref": r[0],
            "credential_kind": r[1],
            "credential_assurance": r[2],
            "calls": int(r[3]),
            "last_seen": _iso(r[4]),
        }
        for r in rows
    ]
    total = int(rows[0][5])
    named = "; ".join(
        f"{c['credential_ref']} ({c['calls']} calls, last {c['last_seen']})" for c in offending[:3]
    )
    detail = (
        f"{total} credential(s) reached this system below {required} in the last "
        f"{IDENTITY_LOOKBACK_DAYS} days: {named}"
    )
    return False, detail, offending, total


def retirement_readiness(db: Session, system: ResourceGroup) -> Dict[str, Any]:
    """When this system becomes retirable, not merely whether it is yet.

    "Has run in the last 30 days" is a true answer that leaves an operator
    with nothing to do but remember to come back. Deprecated systems then
    sit forever, because nobody re-checks a page that only ever said no.
    So return the date instead, and let the callers count down to it.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    window = timedelta(days=DEFAULT_RETIREMENT_QUIET_DAYS)

    last_run = (
        db.query(func.max(AISystemRun.started_at))
        .filter(
            AISystemRun.tenant_id == system.tenant_id,
            AISystemRun.ai_system_id == system.id,
        )
        .scalar()
    )

    if last_run is None:
        # Never ran, so there is nothing to break by retiring it.
        return {
            "quiet_days_required": DEFAULT_RETIREMENT_QUIET_DAYS,
            "last_run_at": None,
            "retirable_at": None,
            "retirable": True,
            "days_remaining": 0,
        }

    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)

    retirable_at = last_run + window
    remaining = retirable_at - now
    retirable = remaining.total_seconds() <= 0

    return {
        "quiet_days_required": DEFAULT_RETIREMENT_QUIET_DAYS,
        "last_run_at": last_run.isoformat(),
        "retirable_at": retirable_at.isoformat(),
        "retirable": retirable,
        # Ceil, so "0 days" only ever means "now", never "later today".
        "days_remaining": 0 if retirable else -(-remaining.days // 1) + (
            1 if remaining.seconds else 0
        ),
    }


def _check_quiet(db: Session, system: ResourceGroup):
    r = retirement_readiness(db, system)
    if r["retirable"]:
        return True, None
    days = r["days_remaining"]
    return (
        False,
        f"This system ran {_ago(r['last_run_at'])}; retiring it would break a "
        f"live caller. Retirable in {days} day{'s' if days != 1 else ''}, "
        f"on {r['retirable_at'][:10]}.",
    )


def _ago(iso: str) -> str:
    from datetime import datetime, timezone

    delta = datetime.now(timezone.utc) - datetime.fromisoformat(iso)
    days = delta.days
    if days <= 0:
        return "today"
    return f"{days} day{'s' if days != 1 else ''} ago"


__all__ = [
    "ALWAYS_ALLOWED",
    "retirement_readiness",
    "DEFAULT_REQUIREMENTS",
    "DEFAULT_RETIREMENT_QUIET_DAYS",
    "GateDecision",
    "GateRefused",
    "REQUIREMENT_LABELS",
    "REQ_IMPACT_ASSESSMENT",
    "REQ_REPLAY_PASSED",
    "REQ_WORKLOAD_IDENTITY",
    "IDENTITY_FLOOR_TRANSITIONS",
    "IDENTITY_LOOKBACK_DAYS",
    "is_known_transition",
    "Requirement",
    "evaluate",
    "required_keys",
]
