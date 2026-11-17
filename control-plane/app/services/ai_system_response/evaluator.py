# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Deciding what to do about a finding

Separated from the actuator on purpose. Everything here is a **pure
decision**: given a finding, the policies that could match it, the system's
current state and the clock, what should happen and what would it affect?

That split is what makes the closed loop reviewable. The scary part of
automatic actuation is not the database write — it is the judgement about
whether to make it, and a judgement buried in a function that also opens
transactions and sends notifications cannot be argued with over a table of
cases.

## Why so much of this file is refusals

Every rail below exists because the obvious implementation of "act
automatically on drift" destroys trust in a specific, predictable way:

- Firing on `medium` produces daily throttling of healthy systems.
- Firing repeatedly on the same standing drift ratchets a system from
  autonomous to suspended inside an afternoon.
- Firing an action that *loosens* hands a degraded system more freedom
  than it had.
- Firing on a parent silently restricts every child, and an operator who
  did not know that will not trust the feature again.

A refusal carries its reason so the UI can say *why nothing happened*.
Silence after a critical drift finding is indistinguishable from a broken
feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from app.database.models.ai_system_contract import autonomy_rank, resolve_autonomy
from app.database.models.ai_system_response_policy import (
    ACTION_SET_AUTONOMY,
    ACTUATING_ACTIONS,
    AISystemResponsePolicy,
)

# Reason codes for a refusal. Stable strings — the UI and the audit trail
# both key on them.
REFUSED_NO_POLICY = "no_matching_policy"
REFUSED_COOLDOWN = "cooldown_active"
REFUSED_ALREADY_RESPONDED = "already_responded"
REFUSED_WOULD_LOOSEN = "would_loosen"
REFUSED_NO_CHANGE = "no_change_needed"
#: A finding closed with any kind stops being a trigger, and
#: evidence a human resolution excluded (inside an incident window, at or
#: before a baseline epoch on its metric) never fires a response.
REFUSED_FINDING_CLOSED = "finding_closed"
REFUSED_EVIDENCE_EXCLUDED = "evidence_excluded"

REFUSAL_LABELS: Dict[str, str] = {
    REFUSED_NO_POLICY: "No response policy matches this finding",
    REFUSED_COOLDOWN: "A response already fired for this system recently",
    REFUSED_ALREADY_RESPONDED: "This finding has already been responded to",
    REFUSED_WOULD_LOOSEN: (
        "The policy would give this system more freedom than it currently has"
    ),
    REFUSED_NO_CHANGE: "The system is already at the level this policy targets",
    REFUSED_FINDING_CLOSED: "The finding was closed before a response was considered",
    REFUSED_EVIDENCE_EXCLUDED: (
        "The finding's evidence was excluded by a human resolution (an incident "
        "window or a baseline epoch)"
    ),
}


@dataclass
class Refusal:
    code: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


@dataclass
class BlastRadius:
    """Which systems an action actually touches.

    Autonomy resolves min-wins *down* the resource-group tree, so tightening
    a parent tightens every inheriting descendant. An operator who arms a
    policy on a team-level group without being shown that it covers eleven
    systems will not trust this feature twice.
    """

    target_id: str
    inherited_by: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return 1 + len(self.inherited_by)

    @property
    def is_wide(self) -> bool:
        """Worth an explicit confirmation rather than a quiet apply."""
        return bool(self.inherited_by)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_id": self.target_id,
            "inherited_by": self.inherited_by,
            "total": self.total,
            "is_wide": self.is_wide,
        }


@dataclass
class PlannedAction:
    kind: str
    params: Dict[str, Any] = field(default_factory=dict)
    #: How to undo it. Recorded at plan time rather than derived later,
    #: because "reversible" is only true if the inverse was captured while
    #: the previous state was still known.
    inverse: Optional[Dict[str, Any]] = None

    @property
    def actuates(self) -> bool:
        return self.kind in ACTUATING_ACTIONS

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "params": self.params, "inverse": self.inverse}


@dataclass
class ActionPlan:
    policy_id: Optional[str]
    policy_name: Optional[str]
    actions: List[PlannedAction] = field(default_factory=list)
    blast_radius: Optional[BlastRadius] = None
    refusals: List[Refusal] = field(default_factory=list)
    require_human_to_restore: bool = True

    @property
    def will_act(self) -> bool:
        return bool(self.actions)

    @property
    def actuates(self) -> bool:
        return any(a.actuates for a in self.actions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_name": self.policy_name,
            "actions": [a.to_dict() for a in self.actions],
            "blast_radius": self.blast_radius.to_dict() if self.blast_radius else None,
            "refusals": [r.to_dict() for r in self.refusals],
            "require_human_to_restore": self.require_human_to_restore,
            "will_act": self.will_act,
        }


def in_cooldown(
    policy: AISystemResponsePolicy, now: Optional[datetime] = None
) -> bool:
    """Whether this policy fired recently enough to stay quiet.

    Rate limiting is per policy, not per finding. A standing drift is
    re-detected on every sweep, and without this a system would ratchet
    from autonomous to suspended inside an afternoon while an operator was
    still reading the first alert.
    """
    if policy.last_fired_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    last = policy.last_fired_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last < timedelta(seconds=policy.response_cooldown_seconds or 0)


def plan_response(
    *,
    policies: Sequence[AISystemResponsePolicy],
    signal_kind: str,
    severity: str,
    drift_class: Optional[str] = None,
    current_autonomy: Optional[str] = None,
    inheriting_descendants: Optional[Sequence[str]] = None,
    target_system_id: str,
    already_responded: bool = False,
    finding_open: bool = True,
    evidence_exclusion: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> ActionPlan:
    """What should happen, and what it would affect.

    Pure. No database, no clock beyond what is passed in, no side effects —
    so every rail below is arguable against a table of cases.

    The **most restrictive** matching policy wins when several match. Taking
    the first match would make behaviour depend on row order, and taking the
    least restrictive would let a broad "notify on any drift" policy shadow
    a narrow "suspend on cost drift" one.
    """
    now = now or datetime.now(timezone.utc)
    refusals: List[Refusal] = []

    if not finding_open:
        return ActionPlan(
            policy_id=None,
            policy_name=None,
            refusals=[Refusal(REFUSED_FINDING_CLOSED, REFUSAL_LABELS[REFUSED_FINDING_CLOSED])],
        )
    if evidence_exclusion:
        where = evidence_exclusion.get("incident_window_id") or evidence_exclusion.get(
            "epoch_starts_at"
        )
        return ActionPlan(
            policy_id=None,
            policy_name=None,
            refusals=[
                Refusal(
                    REFUSED_EVIDENCE_EXCLUDED,
                    f"{REFUSAL_LABELS[REFUSED_EVIDENCE_EXCLUDED]} "
                    f"({evidence_exclusion.get('reason')}: {where})",
                )
            ],
        )

    if already_responded:
        return ActionPlan(
            policy_id=None,
            policy_name=None,
            refusals=[
                Refusal(
                    REFUSED_ALREADY_RESPONDED,
                    REFUSAL_LABELS[REFUSED_ALREADY_RESPONDED],
                )
            ],
        )

    matching = [
        p
        for p in policies
        if p.matches(
            signal_kind=signal_kind, severity=severity, drift_class=drift_class
        )
    ]
    if not matching:
        return ActionPlan(
            policy_id=None,
            policy_name=None,
            refusals=[Refusal(REFUSED_NO_POLICY, REFUSAL_LABELS[REFUSED_NO_POLICY])],
        )

    # Most restrictive target first; policies that only notify sort last.
    def restrictiveness(p: AISystemResponsePolicy) -> tuple:
        target = p.target_autonomy
        rank = autonomy_rank(target) if target else 99
        return (rank, p.id or "")

    policy = sorted(matching, key=restrictiveness)[0]

    if in_cooldown(policy, now):
        return ActionPlan(
            policy_id=policy.id,
            policy_name=policy.name,
            refusals=[Refusal(REFUSED_COOLDOWN, REFUSAL_LABELS[REFUSED_COOLDOWN])],
            require_human_to_restore=bool(policy.require_human_to_restore),
        )

    planned: List[PlannedAction] = []
    for action in policy.actions or []:
        kind = action.get("kind")

        if kind == ACTION_SET_AUTONOMY:
            target = action.get("level")
            # A response policy exists to tighten. One that would loosen —
            # through misconfiguration, or because the system is already
            # more restricted than the target — must not fire.
            if current_autonomy is not None and target is not None:
                if autonomy_rank(target) > autonomy_rank(current_autonomy):
                    refusals.append(
                        Refusal(
                            REFUSED_WOULD_LOOSEN,
                            f"{REFUSAL_LABELS[REFUSED_WOULD_LOOSEN]} "
                            f"({current_autonomy} → {target})",
                        )
                    )
                    continue
                if target == current_autonomy:
                    refusals.append(
                        Refusal(
                            REFUSED_NO_CHANGE,
                            f"{REFUSAL_LABELS[REFUSED_NO_CHANGE]} ({target})",
                        )
                    )
                    continue
            planned.append(
                PlannedAction(
                    kind=kind,
                    params={"level": target},
                    # Captured now, while the previous value is still known.
                    inverse={"kind": kind, "level": current_autonomy},
                )
            )
            continue

        planned.append(
            PlannedAction(
                kind=kind,
                params={k: v for k, v in action.items() if k != "kind"},
                inverse=_inverse_for(kind, action),
            )
        )

    blast = None
    if any(a.actuates for a in planned):
        blast = BlastRadius(
            target_system_id, list(inheriting_descendants or [])
        )

    return ActionPlan(
        policy_id=policy.id,
        policy_name=policy.name,
        actions=planned,
        blast_radius=blast,
        refusals=refusals,
        require_human_to_restore=bool(policy.require_human_to_restore),
    )


def _inverse_for(kind: str, action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The undo for a non-autonomy action.

    `notify` and `open_incident` have no inverse — you cannot un-send a
    page, and pretending otherwise in the audit trail would be a lie. They
    are also not actuating, so nothing depends on undoing them.
    """
    if kind == "pin_model":
        return {"kind": "pin_model", "pinned": False}
    if kind == "tighten_grants":
        return {"kind": "tighten_grants", "restore": action.get("classes")}
    return None


def resolved_autonomy_for(chain_levels: Sequence[Optional[str]]) -> str:
    """Convenience re-export so callers do not reach into the model layer."""
    return resolve_autonomy(*chain_levels)


__all__ = [
    "ActionPlan",
    "BlastRadius",
    "PlannedAction",
    "REFUSAL_LABELS",
    "REFUSED_ALREADY_RESPONDED",
    "REFUSED_COOLDOWN",
    "REFUSED_EVIDENCE_EXCLUDED",
    "REFUSED_FINDING_CLOSED",
    "REFUSED_NO_CHANGE",
    "REFUSED_NO_POLICY",
    "REFUSED_WOULD_LOOSEN",
    "Refusal",
    "in_cooldown",
    "plan_response",
    "resolved_autonomy_for",
]
