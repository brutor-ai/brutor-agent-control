# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The closed loop

Observability tells you something changed. This acts on it. That is the
part a sidecar cannot do, because acting requires being in the request
path, and it is the reason this is one product rather than a dashboard
bolted to a proxy.

A policy is a trigger and a set of actions:

```yaml
on:
  drift_class: behavioural
  severity_at_least: high
then:
  - notify: [ops-oncall, system-owner]
  - set_autonomy: approval_required
  - pin_model: true
  - tighten_grants: [side_effecting]
  - open_incident: true
require_human_to_restore: true
```

## The constraints are the design

Every one of these exists because the obvious implementation destroys
trust:

- **Nothing automatic is irreversible.** Every action has an inverse and
  every action is written to `system_operation_log` with actor
  `system:drift-response` and the triggering `drift_event_id`.
- **`suspended` is never automatic unless explicitly opted in**, per
  system, with `allow_suspend`. Auto-killing a production system on a
  statistical signal loses more trust than any missed detection ever
  recovers.
- **Restore is human by default.** Auto-restore produces flapping: the
  signal clears because the system was throttled, the throttle lifts, the
  signal returns. A human closing the loop is a feature.
- **Actuation is rate-limited.** At most one automatic transition per
  system per `response_cooldown_seconds`. A drift that persists across
  sweeps must not ratchet a system down four levels in an hour.
- **Blast radius is explicit.** Autonomy resolves min-wins *down* the
  resource-group tree, so an action on a parent silently affects every
  child. The affected set is computed before the policy is armed, shown in
  the UI, and recorded on the audit entry.

## Rubric scores never actuate

The rule is explicit and this module enforces it structurally: the trigger
vocabulary has no rubric term. Sampled judge-model scores inform humans;
deterministic signals actuate. A probabilistic score wired to an automatic
enforcement action is how you get a system throttled because a judge model
had a bad afternoon.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base
from app.database.models.ai_system_contract import (
    AUTONOMY_LEVELS,
    autonomy_rank,
    AUTONOMY_SUSPENDED,
)
from app.database.models.ai_system_drift import DRIFT_CLASSES

# ---------------------------------------------------------------------------
# Trigger vocabulary
# ---------------------------------------------------------------------------

#: Signal families a policy may fire on. Deliberately does **not** include
#: rubric scores — see the module docstring.
SIGNAL_DRIFT = "drift"
SIGNAL_LIVENESS = "liveness"
SIGNAL_KINDS = frozenset({SIGNAL_DRIFT, SIGNAL_LIVENESS})

SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"
SEVERITY_CRITICAL = "critical"

#: Ordered least- to most-severe, so "at least high" is a comparison
#: rather than a set membership test that has to be kept in sync.
SEVERITY_RANK: Dict[str, int] = {
    SEVERITY_LOW: 0,
    SEVERITY_MEDIUM: 1,
    SEVERITY_HIGH: 2,
    SEVERITY_CRITICAL: 3,
}
SEVERITIES = frozenset(SEVERITY_RANK)

#: Default trigger floor. `medium` and below is a "look at this when you
#: get a chance" signal; wiring it to an actuator produces exactly the
#: over-reaction that makes teams disable the feature.
DEFAULT_SEVERITY_AT_LEAST = SEVERITY_HIGH

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

ACTION_NOTIFY = "notify"
ACTION_SET_AUTONOMY = "set_autonomy"
ACTION_PIN_MODEL = "pin_model"
ACTION_TIGHTEN_GRANTS = "tighten_grants"
ACTION_OPEN_INCIDENT = "open_incident"

ACTION_KINDS = frozenset({
    ACTION_NOTIFY,
    ACTION_SET_AUTONOMY,
    ACTION_PIN_MODEL,
    ACTION_TIGHTEN_GRANTS,
    ACTION_OPEN_INCIDENT,
})

#: Actions that change what the system may do, as opposed to telling
#: somebody. Only these need a blast-radius preview and a human to restore.
ACTUATING_ACTIONS = frozenset({
    ACTION_SET_AUTONOMY, ACTION_PIN_MODEL, ACTION_TIGHTEN_GRANTS,
})

GRANT_CLASS_SIDE_EFFECTING = "side_effecting"
GRANT_CLASSES = frozenset({GRANT_CLASS_SIDE_EFFECTING})

#: One automatic transition per system per hour. A drift that persists
#: across sweeps must not ratchet a system down four levels before anyone
#: has read the first alert.
DEFAULT_RESPONSE_COOLDOWN_SECONDS = 3600


class PolicyValidationError(ValueError):
    """Raised when an action set is not one the platform will arm."""


def validate_actions(actions: Any, *, allow_suspend: bool) -> List[Dict[str, Any]]:
    """Check an action list, refusing anything unsafe rather than coercing it.

    Silently dropping an unknown action would arm a policy that does less
    than its author believes — the worst possible failure for a control
    somebody is relying on during an incident.
    """
    if not isinstance(actions, list) or not actions:
        raise PolicyValidationError("actions must be a non-empty list")

    checked: List[Dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, dict) or "kind" not in action:
            raise PolicyValidationError(f"action must be an object with 'kind': {action!r}")
        kind = action["kind"]
        if kind not in ACTION_KINDS:
            raise PolicyValidationError(
                f"unknown action {kind!r}; expected one of {sorted(ACTION_KINDS)}"
            )

        if kind == ACTION_SET_AUTONOMY:
            level = action.get("level")
            if level not in AUTONOMY_LEVELS:
                raise PolicyValidationError(
                    f"set_autonomy level must be one of {sorted(AUTONOMY_LEVELS)}, got {level!r}"
                )
            if level == AUTONOMY_SUSPENDED and not allow_suspend:
                raise PolicyValidationError(
                    "set_autonomy: suspended requires allow_suspend on this policy. "
                    "Auto-killing a production system on a statistical signal is "
                    "opt-in for a reason."
                )

        if kind == ACTION_NOTIFY:
            targets = action.get("targets")
            if not isinstance(targets, list) or not targets:
                raise PolicyValidationError("notify requires a non-empty targets list")

        if kind == ACTION_TIGHTEN_GRANTS:
            classes = action.get("classes") or [GRANT_CLASS_SIDE_EFFECTING]
            unknown = [c for c in classes if c not in GRANT_CLASSES]
            if unknown:
                raise PolicyValidationError(
                    f"unknown grant class(es) {unknown}; expected {sorted(GRANT_CLASSES)}"
                )

        checked.append(action)

    return checked


def describe_action(action: Any) -> str:
    """One stored action as a short phrase: what a reader of the policy sees.

    The single place an action is turned into words, so a view cannot read an
    action under a key it does not have (the Composition list once printed
    "on drift → None" by reading `type`; actions are keyed by `kind`). Every
    member of `ACTION_KINDS` has a phrase; anything else is named as it is
    stored rather than hidden, because a policy that shows less than it does
    is the failure this module exists to prevent.
    """
    if not isinstance(action, dict) or not action.get("kind"):
        return f"unrecognised action {action!r}"
    kind = action["kind"]
    if kind == ACTION_SET_AUTONOMY:
        return f"set autonomy to {action.get('level')}"
    if kind == ACTION_NOTIFY:
        return "notify " + ", ".join(str(t) for t in action.get("targets") or [])
    if kind == ACTION_PIN_MODEL:
        return "pin the model"
    if kind == ACTION_TIGHTEN_GRANTS:
        classes = action.get("classes") or [GRANT_CLASS_SIDE_EFFECTING]
        return "tighten grants (" + ", ".join(str(c) for c in classes) + ")"
    if kind == ACTION_OPEN_INCIDENT:
        return "open an incident"
    return f"unrecognised action {kind!r}"


def actuates(actions: List[Dict[str, Any]]) -> bool:
    """Whether this policy changes behaviour rather than only notifying."""
    return any(a.get("kind") in ACTUATING_ACTIONS for a in actions or [])


class AISystemResponsePolicy(Base):
    """What to do automatically when a signal fires."""

    __tablename__ = "ai_system_response_policies"

    id = Column(String(64), primary_key=True)
    tenant_id = Column(String(255), nullable=False, index=True)
    ai_system_id = Column(
        String(255),
        nullable=False,
        index=True,
        comment=(
            "The resource group this policy is attached to. Actions resolve "
            "min-wins down the tree, so a policy on a parent affects every "
            "inheriting child — which is what the blast-radius preview is for."
        ),
    )

    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")

    # --- trigger -----------------------------------------------------------

    signal_kind = Column(
        String(16), nullable=False, default=SIGNAL_DRIFT, server_default=SIGNAL_DRIFT
    )
    drift_class = Column(
        String(24),
        nullable=True,
        comment="NULL matches any drift class.",
    )
    severity_at_least = Column(
        String(16),
        nullable=False,
        default=DEFAULT_SEVERITY_AT_LEAST,
        server_default=DEFAULT_SEVERITY_AT_LEAST,
    )

    # --- actions -----------------------------------------------------------

    actions = Column(
        JSONB,
        nullable=False,
        comment="Ordered list of {kind, ...}. Validated by validate_actions().",
    )

    allow_suspend = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment=(
            "Opt-in per policy. Without it, `set_autonomy: suspended` is "
            "refused at validation rather than at fire time — an operator "
            "should find out when they write the policy, not during an "
            "incident."
        ),
    )

    require_human_to_restore = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment=(
            "Auto-restore produces flapping: the signal clears because the "
            "system was throttled, the throttle lifts, the signal returns."
        ),
    )

    response_cooldown_seconds = Column(
        Integer,
        nullable=False,
        default=DEFAULT_RESPONSE_COOLDOWN_SECONDS,
        server_default=str(DEFAULT_RESPONSE_COOLDOWN_SECONDS),
    )

    # --- state -------------------------------------------------------------

    last_fired_at = Column(DateTime(timezone=True), nullable=True)
    last_fired_drift_event_id = Column(String(64), nullable=True)
    fire_count = Column(Integer, nullable=False, default=0, server_default="0")

    created_by = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["ai_system_id"], ["resource_groups.id"], ondelete="CASCADE"
        ),
        UniqueConstraint(
            "tenant_id", "ai_system_id", "name", name="uq_response_policies_name"
        ),
        Index(
            "ix_response_policies_trigger",
            "tenant_id", "ai_system_id", "signal_kind", "enabled",
        ),
        {
            "comment": (
                "Automatic, reversible, rate-limited "
                "response to a drift or liveness finding."
            )
        },
    )

    @validates("signal_kind")
    def _v_signal(self, _k: str, v: str) -> str:
        if v not in SIGNAL_KINDS:
            raise ValueError(
                f"signal_kind must be one of {sorted(SIGNAL_KINDS)}, got {v!r}"
            )
        return v

    @validates("severity_at_least")
    def _v_severity(self, _k: str, v: str) -> str:
        if v not in SEVERITIES:
            raise ValueError(
                f"severity_at_least must be one of {sorted(SEVERITIES)}, got {v!r}"
            )
        return v

    @validates("drift_class")
    def _v_drift_class(self, _k: str, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in DRIFT_CLASSES:
            raise ValueError(
                f"drift_class must be one of {sorted(DRIFT_CLASSES)}, got {v!r}"
            )
        return v

    # --- matching ----------------------------------------------------------

    def matches(
        self,
        *,
        signal_kind: str,
        severity: str,
        drift_class: Optional[str] = None,
    ) -> bool:
        """Whether a finding trips this policy.

        An unknown severity does **not** match. Treating it as maximal would
        let a future severity name fire every actuating policy in the fleet
        the moment it shipped.
        """
        if not self.enabled:
            return False
        if signal_kind != self.signal_kind:
            return False
        if self.drift_class is not None and drift_class != self.drift_class:
            return False
        if severity not in SEVERITY_RANK:
            return False
        return SEVERITY_RANK[severity] >= SEVERITY_RANK[self.severity_at_least]

    @property
    def is_actuating(self) -> bool:
        return actuates(self.actions or [])

    @property
    def target_autonomy(self) -> Optional[str]:
        for action in self.actions or []:
            if action.get("kind") == ACTION_SET_AUTONOMY:
                return action.get("level")
        return None

    def would_loosen(self, current_level: Optional[str]) -> bool:
        """Whether firing would make the system *more* permissive.

        A response policy exists to tighten. One that loosens — through a
        misconfiguration, or because the system is already more restricted
        than the policy's target — must not fire, or an incident response
        would hand a degraded system more freedom than it had.
        """
        target = self.target_autonomy
        if target is None or current_level is None:
            return False
        return autonomy_rank(target) > autonomy_rank(current_level)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "ai_system_id": self.ai_system_id,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "signal_kind": self.signal_kind,
            "drift_class": self.drift_class,
            "severity_at_least": self.severity_at_least,
            "actions": self.actions,
            "allow_suspend": self.allow_suspend,
            "require_human_to_restore": self.require_human_to_restore,
            "response_cooldown_seconds": self.response_cooldown_seconds,
            "last_fired_at": self.last_fired_at.isoformat() if self.last_fired_at else None,
            "last_fired_drift_event_id": self.last_fired_drift_event_id,
            "fire_count": self.fire_count,
            "is_actuating": self.is_actuating,
            "target_autonomy": self.target_autonomy,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
