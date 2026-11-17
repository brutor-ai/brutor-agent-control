# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Applying a response

The write half of the closed loop. [`evaluator`] decides; this makes it
real, records it, and tells the proxy.

## Why this lives in the control plane

The actuator writes `resource_groups.autonomy_level`, which is
**configuration**. The house rule is that config writes belong here so the
SQLAlchemy ORM owns column constraints rather than having them re-derived
by hand in raw SQL — the same reason skill-source sync was moved out of
gateway-core. Detection stays in the proxy where the traffic is; the write
happens here; the proxy picks the change up through the existing cache
invalidation channel.

## The audit entry is the product

Every automatic action writes to `system_operation_log` with actor
`system:drift-response`, the triggering drift event, the **previous value**
and the blast radius. That row is what a customer shows an auditor to
answer "who changed this and why" — and it is the difference between an
automated control and an unexplained config change.

Actions are applied **all-or-nothing** within one transaction. A partial
response — autonomy tightened but nothing logged — leaves a system
restricted with no record of why, which is the worst state to debug at 3am.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session
from ulid import ULID

from app.database.models.ai_system_drift import (
    DRIFT_ACKNOWLEDGED,
    DRIFT_ACTIVE,
    AISystemDriftEvent,
)
from app.database.models.ai_system_response_policy import (
    ACTION_SET_AUTONOMY,
    AISystemResponsePolicy,
)
from app.database.models.resource_group import GroupType, ResourceGroup
from app.database.models.system_operation_log import (
    ACTION_DRIFT_RESPONSE,
    ACTION_DRIFT_RESPONSE_RESTORE,
    ACTOR_TYPE_SYSTEM,
    SYSTEM_ACTOR_DRIFT_RESPONSE,
    SystemOperationLog,
)
from app.services.ai_system_response.evaluator import ActionPlan, plan_response
from app.services.ai_system_response.reach import limitation_for

log = logging.getLogger("brutor-ai-system-response")

OPEN_DRIFT_STATUSES = (DRIFT_ACTIVE, DRIFT_ACKNOWLEDGED)

#: Bound on the descendant walk, matching every other hierarchy walk in the
#: platform.
MAX_DEPTH = 10


def inheriting_descendants(db: Session, system: ResourceGroup) -> List[str]:
    """Every group below this one that inherits from it, transitively.

    This *is* the blast radius: autonomy resolves min-wins down the tree, so
    tightening a parent tightens each of these. A group that does not
    inherit stops the walk on that branch — it and its own children are
    unaffected.
    """
    found: List[str] = []
    frontier = [system.id]
    seen = {system.id}

    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        children = (
            db.query(ResourceGroup.id)
            .filter(
                ResourceGroup.tenant_id == system.tenant_id,
                ResourceGroup.parent_group_id.in_(frontier),
                ResourceGroup.inherit_resources.is_(True),
            )
            .all()
        )
        frontier = [c.id for c in children if c.id not in seen]
        seen.update(frontier)
        found.extend(frontier)

    return sorted(found)


def policies_in_scope(
    db: Session, system: ResourceGroup, *, enabled_only: bool
) -> List[AISystemResponsePolicy]:
    """Policies on this system and on the groups it inherits from.

    A policy set on a parent applies to children for the same reason limits
    do — otherwise an operator would have to attach the same policy to every
    system by hand, and the ones they forgot would be the ones that drifted.

    `enabled_only` is the difference between *what will fire* and *what an
    operator manages*. The actuator must only ever see enabled policies. A
    management listing must see disabled ones too: a policy created and left
    off — which is the safe default for a control that can restrict production
    — would otherwise vanish from the console, leaving no way to find it and
    switch it on.
    """
    chain: List[str] = [system.id]
    current = system
    for _ in range(MAX_DEPTH):
        if not current.inherit_resources or current.parent_group is None:
            break
        current = current.parent_group
        chain.append(current.id)

    return (
        db.query(AISystemResponsePolicy)
        .filter(
            AISystemResponsePolicy.tenant_id == system.tenant_id,
            AISystemResponsePolicy.ai_system_id.in_(chain),
            *([AISystemResponsePolicy.enabled.is_(True)] if enabled_only else []),
        )
        .order_by(AISystemResponsePolicy.id)
        .all()
    )


def applicable_policies(
    db: Session, system: ResourceGroup
) -> List[AISystemResponsePolicy]:
    """The policies that may actually fire. Enabled only, always."""
    return policies_in_scope(db, system, enabled_only=True)


def resolved_autonomy(db: Session, system: ResourceGroup) -> Optional[str]:
    """The level currently in force, min-wins up the inheritance chain."""
    from app.services.ai_system_closure import inheritance_chain
    from app.database.models.ai_system_contract import resolve_autonomy

    return resolve_autonomy(*[g.autonomy_level for g in inheritance_chain(system)])


def plan_for_drift(
    db: Session, event: AISystemDriftEvent, *, now: Optional[datetime] = None
) -> ActionPlan:
    """What would happen if this finding were responded to now."""
    system = (
        db.query(ResourceGroup)
        .filter(
            ResourceGroup.id == event.ai_system_id,
            ResourceGroup.tenant_id == event.tenant_id,
        )
        .first()
    )
    if system is None:
        from app.services.ai_system_response.evaluator import (
            REFUSAL_LABELS,
            REFUSED_NO_POLICY,
            Refusal,
        )

        return ActionPlan(
            policy_id=None,
            policy_name=None,
            refusals=[
                Refusal(REFUSED_NO_POLICY, "The AI System no longer exists")
            ],
        )

    from app.services.ai_system_judgement_exclusion import finding_exclusion

    return plan_response(
        policies=applicable_policies(db, system),
        signal_kind="drift",
        severity=event.severity,
        drift_class=event.drift_class,
        current_autonomy=resolved_autonomy(db, system),
        inheriting_descendants=inheriting_descendants(db, system),
        target_system_id=system.id,
        already_responded=bool(event.response_taken),
        # A closed finding is no trigger; excluded evidence
        # never fires (THE exclusion predicate).
        finding_open=bool(event.is_open),
        evidence_exclusion=finding_exclusion(db, event),
        now=now,
    )


def apply_plan(
    db: Session,
    event: AISystemDriftEvent,
    plan: ActionPlan,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Make the plan real, in one transaction.

    Returns what was done, in the shape stored on `drift_event.response_taken`.
    """
    now = now or datetime.now(timezone.utc)

    if not plan.will_act:
        # Recorded even when nothing happened: silence after a critical
        # finding is indistinguishable from a broken feature, and the UI
        # needs to be able to say *why* nothing fired.
        record = {
            "responded_at": now.isoformat(),
            "policy_id": plan.policy_id,
            "actions": [],
            "refusals": [r.to_dict() for r in plan.refusals],
        }
        event.response_taken = record
        db.flush()
        return record

    system = (
        db.query(ResourceGroup)
        .filter(
            ResourceGroup.id == event.ai_system_id,
            ResourceGroup.tenant_id == event.tenant_id,
        )
        .one()
    )

    applied: List[Dict[str, Any]] = []
    limitations: List[Dict[str, Any]] = []
    before_state: Dict[str, Any] = {}
    after_state: Dict[str, Any] = {}

    for action in plan.actions:
        if action.kind == ACTION_SET_AUTONOMY:
            before_state["autonomy_level"] = system.autonomy_level
            system.autonomy_level = action.params.get("level")
            after_state["autonomy_level"] = system.autonomy_level
            # An action that does not bind every caller is recorded as such,
            # never as if it had stopped them.
            note = limitation_for(
                db,
                event.tenant_id,
                action.params.get("level"),
                [system.id, *inheriting_descendants(db, system)],
                now=now,
            )
            if note:
                limitations.append(note)
        # notify / open_incident / pin_model / tighten_grants are recorded
        # as intent here. They are surfaced to the operator and to the
        # notification pipeline rather than silently claimed as done — an
        # audit entry that says "paged ops-oncall" when nothing was sent is
        # worse than one that says a page was requested.
        applied.append(action.to_dict())

    policy = (
        db.query(AISystemResponsePolicy)
        .filter(AISystemResponsePolicy.id == plan.policy_id)
        .first()
        if plan.policy_id
        else None
    )
    if policy is not None:
        policy.last_fired_at = now
        policy.last_fired_drift_event_id = event.id
        policy.fire_count = (policy.fire_count or 0) + 1

    db.add(
        SystemOperationLog(
            id=f"sol-{ULID()}",
            tenant_id=event.tenant_id,
            actor_admin_id=SYSTEM_ACTOR_DRIFT_RESPONSE,
            actor_type=ACTOR_TYPE_SYSTEM,
            action=ACTION_DRIFT_RESPONSE,
            scope_kind="ai_system",
            scope_id=event.ai_system_id,
            payload={
                "drift_event_id": event.id,
                "drift_class": event.drift_class,
                "severity": event.severity,
                "attributed_cause": event.attributed_cause,
                "policy_id": plan.policy_id,
                "policy_name": plan.policy_name,
                "actions": applied,
                "blast_radius": plan.blast_radius.to_dict() if plan.blast_radius else None,
                "require_human_to_restore": plan.require_human_to_restore,
                "limitations": limitations,
            },
            before_state=before_state or None,
            after_state=after_state or None,
        )
    )

    record = {
        "responded_at": now.isoformat(),
        "policy_id": plan.policy_id,
        "policy_name": plan.policy_name,
        "actions": applied,
        "blast_radius": plan.blast_radius.to_dict() if plan.blast_radius else None,
        "refusals": [r.to_dict() for r in plan.refusals],
        "limitations": limitations,
        "require_human_to_restore": plan.require_human_to_restore,
    }
    event.response_taken = record
    db.flush()

    log.info(
        "drift response applied",
        extra={
            "ai_system_id": event.ai_system_id,
            "drift_event_id": event.id,
            "policy_id": plan.policy_id,
            "blast_radius": plan.blast_radius.total if plan.blast_radius else 1,
        },
    )
    return record


def restore(
    db: Session,
    system: ResourceGroup,
    *,
    actor_admin_id: str,
    actor_type: str,
    to_level: Optional[str],
    drift_event_id: Optional[str] = None,
) -> None:
    """Undo an automatic tightening. Human-driven by design.

    Auto-restore flaps: the signal clears *because* the system was
    throttled, the throttle lifts, the signal returns. A human closing the
    loop is the feature, not a missing one — so this takes a real actor and
    refuses to be called with the system one.
    """
    if actor_type == ACTOR_TYPE_SYSTEM:
        raise ValueError(
            "restore requires a human actor. Automatic restore produces "
            "flapping: the signal clears because the system was throttled, "
            "the throttle lifts, and the signal returns."
        )

    before = {"autonomy_level": system.autonomy_level}
    system.autonomy_level = to_level

    db.add(
        SystemOperationLog(
            id=f"sol-{ULID()}",
            tenant_id=system.tenant_id,
            actor_admin_id=actor_admin_id,
            actor_type=actor_type,
            action=ACTION_DRIFT_RESPONSE_RESTORE,
            scope_kind="ai_system",
            scope_id=system.id,
            payload={"drift_event_id": drift_event_id, "restored_to": to_level},
            before_state=before,
            after_state={"autonomy_level": to_level},
        )
    )
    db.flush()


def pending_drift_events(
    db: Session, *, limit: int = 50
) -> Sequence[AISystemDriftEvent]:
    """Findings that have not yet been considered for a response.

    `response_taken` is stamped even when nothing fires, so a finding is
    considered exactly once. Without that, a drift with no matching policy
    would be re-planned on every tick forever.

    Only OPEN findings: a finding closed with any kind stops
    being a trigger, even one closed before the loop reached it.
    """
    return (
        db.query(AISystemDriftEvent)
        .join(
            ResourceGroup,
            (ResourceGroup.id == AISystemDriftEvent.ai_system_id)
            & (ResourceGroup.tenant_id == AISystemDriftEvent.tenant_id),
        )
        .filter(
            AISystemDriftEvent.response_taken.is_(None),
            AISystemDriftEvent.status.in_(OPEN_DRIFT_STATUSES),
            ResourceGroup.group_type == GroupType.AI_SYSTEM,
        )
        .order_by(AISystemDriftEvent.detected_at.asc())
        .limit(limit)
        .all()
    )


__all__ = [
    "MAX_DEPTH",
    "applicable_policies",
    "apply_plan",
    "inheriting_descendants",
    "pending_drift_events",
    "plan_for_drift",
    "resolved_autonomy",
    "restore",
]
