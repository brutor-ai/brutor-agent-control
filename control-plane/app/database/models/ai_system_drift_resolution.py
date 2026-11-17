# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Baseline epochs and incident windows

Two declared facts every signal that grades samples must honour (the rule:
`app/constants/drift_resolution.py`; the control-plane predicate:
`app/services/ai_system_judgement_exclusion.py`; the core's: its SQL
fragment, pinned against the same literals).

* **Baseline epoch** — samples of (system, metric, basis, caller) taken
  before `starts_at` no longer describe normal. Written by an
  `accepted_change` resolution (at the finding's change point: the new
  behaviour is the new normal, and the baseline re-learns from there) or a
  `false_positive` one (at the finding's detection time: the evidence the
  detector misread is not read again). An epoch on caller 'all' covers every
  caller of the metric.

* **Incident window** — a period [starts_at, ends_at) whose samples are
  excluded from judgement (baselines, drift, envelope terms, conformance
  rates, health rate components, the down rule, connection lines) but KEPT
  as evidence: recorded, sealed, exportable, and listed in the assurance
  report with how many runs it excluded. For one AI System, or every system
  in the tenant (`ai_system_id` NULL — a provider outage).

* **Measurement-change epoch** — kind `measurement_change`: a
  platform upgrade changed how the metric is MEASURED (registry
  `app/constants/measurement_changes.py`, applied once per installation at
  control-plane startup). Same effect as any epoch; no drift event, not a
  human declaration, so not sealed — `measurement_change_id` names the
  registry entry and `platform_measurement_changes` records when it applied.

Human epochs and windows are sealed declarations: `record_id` / `sealed_at` are the
`ai.brutor.declaration` that recorded them (NULL = the seal failed and a
capture-gap item was raised). Authorship (`declared_by`, `updated_by`) is
stamped from the acting admin, never from a request body.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import validates
from sqlalchemy.sql import func

from app.constants.drift_resolution import (
    EPOCH_ALL_BUCKETS,
    EPOCH_ALL_CALLERS,
    EPOCH_KINDS,
    INCIDENT_CAUSE_CATEGORIES,
    INCIDENT_RESOURCE_TYPES,
)
from app.database import Base
from app.database.models.ai_system_drift import BASES, BASIS_RUNS


def _iso(dt) -> Any:
    return dt.isoformat() if dt else None


class AISystemBaselineEpoch(Base):
    """From `starts_at` on, (system, metric, basis, caller) learns afresh."""

    __tablename__ = "ai_system_baseline_epochs"

    id = Column(String(64), primary_key=True)  # epoch-<ULID>
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    ai_system_id = Column(
        String(255), ForeignKey("resource_groups.id", ondelete="CASCADE"), nullable=False
    )
    metric_key = Column(String(64), nullable=False)
    basis = Column(String(16), nullable=False, default=BASIS_RUNS, server_default=BASIS_RUNS)
    caller_system_id = Column(
        String(255),
        nullable=False,
        default=EPOCH_ALL_CALLERS,
        server_default=EPOCH_ALL_CALLERS,
        comment="'all' covers every caller of the metric; else one calling AI System",
    )
    # Baselines are learned per outcome bucket (the full
    # outcome id) as well as on 'all'. A `catalogue_change` epoch names the
    # one bucket whose meaning changed; every other kind covers 'all'.
    bucket = Column(
        String(128),
        nullable=False,
        default=EPOCH_ALL_BUCKETS,
        server_default=EPOCH_ALL_BUCKETS,
        comment="'all' covers every bucket of the metric; else one outcome bucket",
    )
    starts_at = Column(DateTime(timezone=True), nullable=False)
    kind = Column(
        String(32),
        nullable=False,
        comment="accepted_change | false_positive | measurement_change | catalogue_change",
    )
    drift_event_id = Column(
        String(64),
        ForeignKey("ai_system_drift_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    # A `measurement_change` epoch names the platform registry entry
    # (`app/constants/measurement_changes.py`) it came from. It has no
    # drift event and is not a human declaration: its authority is the
    # versioned registry plus `platform_measurement_changes`, so it is not
    # sealed (`record_id` NULL is expected for this kind, not a capture gap).
    measurement_change_id = Column(
        String(64),
        nullable=True,
        comment="The platform registry entry a measurement_change epoch came from",
    )
    reason = Column(Text, nullable=True)
    declared_by = Column(String(255), nullable=True)
    declared_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    record_id = Column(String(64), nullable=True)
    sealed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "ai_system_id", "metric_key", "basis", "caller_system_id",
            "bucket", "starts_at",
            name="uq_ai_system_baseline_epochs_key",
        ),
        CheckConstraint(
            "basis IN ('runs', 'segments')", name="ck_ai_system_baseline_epochs_basis"
        ),
        CheckConstraint(
            "kind IN ('accepted_change', 'false_positive', 'measurement_change', "
            "'catalogue_change')",
            name="ck_ai_system_baseline_epochs_kind",
        ),
        {"comment": "Samples before starts_at no longer describe normal"},
    )

    @validates("basis")
    def _validate_basis(self, _key, value):
        if value is not None and value not in BASES:
            raise ValueError(f"basis must be one of {sorted(BASES)}, got {value!r}")
        return value

    @validates("kind")
    def _validate_kind(self, _key, value):
        if value not in EPOCH_KINDS:
            raise ValueError(f"kind must be one of {list(EPOCH_KINDS)}, got {value!r}")
        return value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ai_system_id": self.ai_system_id,
            "metric_key": self.metric_key,
            "basis": self.basis or BASIS_RUNS,
            "caller_system_id": self.caller_system_id or EPOCH_ALL_CALLERS,
            "bucket": self.bucket or EPOCH_ALL_BUCKETS,
            "starts_at": _iso(self.starts_at),
            "kind": self.kind,
            "drift_event_id": self.drift_event_id,
            "measurement_change_id": self.measurement_change_id,
            "reason": self.reason,
            "declared_by": self.declared_by,
            "declared_at": _iso(self.declared_at),
            "record_id": self.record_id,
            "sealed_at": _iso(self.sealed_at),
        }


class AISystemIncidentWindow(Base):
    """A declared period excluded from judgement, kept as evidence."""

    __tablename__ = "ai_system_incident_windows"

    id = Column(String(64), primary_key=True)  # iw-<ULID>
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    ai_system_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        nullable=True,
        comment="NULL = every AI System in the tenant",
    )
    resource_type = Column(String(32), nullable=True, comment="Display-only narrowing")
    resource_id = Column(String(255), nullable=True)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False, comment="Exclusive")
    cause_category = Column(String(32), nullable=False)
    reason = Column(Text, nullable=False)
    drift_event_id = Column(
        String(64),
        ForeignKey("ai_system_drift_events.id", ondelete="SET NULL"),
        nullable=True,
        comment="The cause_fixed resolution that declared it, if any",
    )
    declared_by = Column(String(255), nullable=True)
    declared_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_by = Column(String(255), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    record_id = Column(String(64), nullable=True)
    sealed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_ai_system_incident_windows_bounds"),
        CheckConstraint(
            "cause_category IN ('provider_outage', 'platform_incident', "
            "'maintenance', 'other')",
            name="ck_ai_system_incident_windows_cause",
        ),
        CheckConstraint(
            "resource_type IS NULL OR resource_type IN ('model', 'provider', "
            "'mcp_server', 'skill', 'knowledge_base', 'agent')",
            name="ck_ai_system_incident_windows_resource_type",
        ),
        CheckConstraint(
            "(resource_type IS NULL) = (resource_id IS NULL)",
            name="ck_ai_system_incident_windows_resource",
        ),
        Index(
            "ix_ai_system_incident_windows_scope", "tenant_id", "ai_system_id", "starts_at"
        ),
        {"comment": "Samples in [starts_at, ends_at) are excluded from judgement"},
    )

    @validates("cause_category")
    def _validate_cause(self, _key, value):
        if value not in INCIDENT_CAUSE_CATEGORIES:
            raise ValueError(
                f"cause_category must be one of {list(INCIDENT_CAUSE_CATEGORIES)}, got {value!r}"
            )
        return value

    @validates("resource_type")
    def _validate_resource_type(self, _key, value):
        if value is not None and value not in INCIDENT_RESOURCE_TYPES:
            raise ValueError(
                f"resource_type must be one of {list(INCIDENT_RESOURCE_TYPES)}, got {value!r}"
            )
        return value

    @property
    def is_tenant_wide(self) -> bool:
        return self.ai_system_id is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ai_system_id": self.ai_system_id,
            "tenant_wide": self.is_tenant_wide,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "starts_at": _iso(self.starts_at),
            "ends_at": _iso(self.ends_at),
            "cause_category": self.cause_category,
            "reason": self.reason,
            "drift_event_id": self.drift_event_id,
            "declared_by": self.declared_by,
            "declared_at": _iso(self.declared_at),
            "updated_by": self.updated_by,
            "updated_at": _iso(self.updated_at),
            "record_id": self.record_id,
            "sealed_at": _iso(self.sealed_at),
        }


WINDOW_LINK_DECLARED = "declared"
WINDOW_LINK_ATTACHED = "attached"
WINDOW_LINK_ROLES = (WINDOW_LINK_DECLARED, WINDOW_LINK_ATTACHED)


class AISystemIncidentWindowFinding(Base):
    """A `cause_fixed` finding closed against a window: the finding that
    declared it, or one attached to it later (N identical findings of one
    incident close against ONE window). Sealed like the resolution."""

    __tablename__ = "ai_system_incident_window_findings"

    incident_window_id = Column(
        String(64),
        ForeignKey("ai_system_incident_windows.id", ondelete="CASCADE"),
        primary_key=True,
    )
    drift_event_id = Column(
        String(64),
        ForeignKey("ai_system_drift_events.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(String(16), nullable=False)
    attached_by = Column(String(255), nullable=True)
    attached_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    record_id = Column(String(64), nullable=True)
    sealed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "role IN ('declared', 'attached')",
            name="ck_ai_system_incident_window_findings_role",
        ),
        Index("ix_ai_system_incident_window_findings_event", "drift_event_id"),
        {"comment": "The cause_fixed findings a window closed"},
    )

    @validates("role")
    def _validate_role(self, _key, value):
        if value not in WINDOW_LINK_ROLES:
            raise ValueError(f"role must be one of {list(WINDOW_LINK_ROLES)}, got {value!r}")
        return value


__all__ = [
    "AISystemBaselineEpoch",
    "AISystemIncidentWindow",
    "AISystemIncidentWindowFinding",
    "WINDOW_LINK_ATTACHED",
    "WINDOW_LINK_DECLARED",
    "WINDOW_LINK_ROLES",
]
