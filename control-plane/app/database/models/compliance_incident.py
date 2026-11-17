# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# Compliance incidents — the serious-incident register

An incident is classified per framework (`frameworks`, e.g. EU AI Act
Art. 73 serious incident, DORA major incident) and carries the reporting
deadlines those frameworks impose, computed from `became_aware_at` and the
registry's `deadline_rules` (`deadlines` = `{framework_id: {rule: ISO
timestamp}}`). Staged regimes (DORA, NIS2) count later stages from
`classified_at` (when the incident was classified as major / significant)
and from each earlier stage's submission (`stage_submissions` =
`{framework_id: {stage: ISO timestamp}}`). An approaching
or missed deadline raises an `incident_deadline` inbox finding.

Every change appends a `compliance_incident_events` row and seals an
`ai.brutor.incident` record (`context.state`); the incident's newest
record is `last_record_id`. The description never enters a record — only
its digest.

Written by the control plane.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import validates

from app.database import Base
from app.database.models.evidence_columns import (
    check_vocab,
    created_at_column,
    iso,
    updated_at_column,
    utcnow,
)

INCIDENT_STATUS_OPEN = "open"
INCIDENT_STATUS_REPORTED = "reported"
INCIDENT_STATUS_CLOSED = "closed"
INCIDENT_STATUSES = frozenset({INCIDENT_STATUS_OPEN, INCIDENT_STATUS_REPORTED, INCIDENT_STATUS_CLOSED})

INCIDENT_EVENT_CREATED = "created"
INCIDENT_EVENT_UPDATED = "updated"
INCIDENT_EVENT_REPORTED = "reported"
INCIDENT_EVENT_CLOSED = "closed"
INCIDENT_EVENT_NOTE = "note"
#: A stage of a staged regime (DORA initial / intermediate / final …) was
#: submitted to the authority.
INCIDENT_EVENT_SUBMITTED = "stage_submitted"
INCIDENT_EVENTS = frozenset({
    INCIDENT_EVENT_SUBMITTED,
    INCIDENT_EVENT_CREATED,
    INCIDENT_EVENT_UPDATED,
    INCIDENT_EVENT_REPORTED,
    INCIDENT_EVENT_CLOSED,
    INCIDENT_EVENT_NOTE,
})


class ComplianceIncident(Base):
    """One incident in the register (CP-written)."""

    __tablename__ = "compliance_incidents"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'inc-'")
    tenant_id = Column(String(255), nullable=False)
    ai_system_id = Column(String(255), nullable=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False, default="", server_default="")
    classification = Column(String(64), nullable=False, comment="e.g. serious_incident, major_incident")
    severity = Column(String(16), nullable=False)
    became_aware_at = Column(DateTime(timezone=True), nullable=False)
    classified_at = Column(
        DateTime(timezone=True), nullable=True,
        comment="When the incident was classified as major/significant (operator-entered)",
    )
    frameworks = Column(JSONB, nullable=False, default=list, server_default="[]")
    deadlines = Column(JSONB, nullable=False, default=dict, server_default="{}")
    status = Column(String(16), nullable=False, default=INCIDENT_STATUS_OPEN, server_default="open")
    reported_at = Column(DateTime(timezone=True), nullable=True)
    authority = Column(String(255), nullable=True, comment="Who it was reported to")
    external_reference = Column(String(255), nullable=True)
    linked_run_ids = Column(JSONB, nullable=False, default=list, server_default="[]")
    linked_inbox_item_ids = Column(JSONB, nullable=False, default=list, server_default="[]")
    stage_submissions = Column(
        JSONB, nullable=False, default=dict, server_default="{}",
        comment="{framework_id: {stage: ISO time}} submissions of staged reports",
    )
    last_record_id = Column(String(64), nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = created_at_column()
    updated_at = updated_at_column()

    __table_args__ = (
        Index("ix_compliance_incidents_tenant", "tenant_id", "status", "became_aware_at"),
    )

    @validates("status")
    def _validate_status(self, _key, value):
        return check_vocab("status", value, INCIDENT_STATUSES)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "ai_system_id": self.ai_system_id,
            "title": self.title,
            "description": self.description or "",
            "classification": self.classification,
            "severity": self.severity,
            "became_aware_at": iso(self.became_aware_at),
            "classified_at": iso(self.classified_at),
            "frameworks": self.frameworks or [],
            "deadlines": self.deadlines or {},
            "status": self.status,
            "reported_at": iso(self.reported_at),
            "authority": self.authority,
            "external_reference": self.external_reference,
            "linked_run_ids": self.linked_run_ids or [],
            "linked_inbox_item_ids": self.linked_inbox_item_ids or [],
            "stage_submissions": self.stage_submissions or {},
            "last_record_id": self.last_record_id,
            "created_by": self.created_by,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


class ComplianceIncidentEvent(Base):
    """One change to an incident (CP-written, append-only)."""

    __tablename__ = "compliance_incident_events"

    id = Column(String(64), primary_key=True, comment="ULID, prefixed 'ince-'")
    incident_id = Column(
        String(64), ForeignKey("compliance_incidents.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id = Column(String(255), nullable=False)
    event = Column(String(16), nullable=False)
    at = Column(DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now())
    actor_id = Column(String(255), nullable=True)
    detail = Column(JSONB, nullable=False, default=dict, server_default="{}")
    record_id = Column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_compliance_incident_events_incident", "incident_id", "at"),
    )

    @validates("event")
    def _validate_event(self, _key, value):
        return check_vocab("event", value, INCIDENT_EVENTS)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "tenant_id": self.tenant_id,
            "event": self.event,
            "at": iso(self.at),
            "actor_id": self.actor_id,
            "detail": self.detail or {},
            "record_id": self.record_id,
        }


__all__ = [
    "ComplianceIncident",
    "ComplianceIncidentEvent",
    "INCIDENT_STATUSES",
    "INCIDENT_STATUS_OPEN",
    "INCIDENT_STATUS_REPORTED",
    "INCIDENT_STATUS_CLOSED",
    "INCIDENT_EVENTS",
    "INCIDENT_EVENT_CREATED",
    "INCIDENT_EVENT_UPDATED",
    "INCIDENT_EVENT_REPORTED",
    "INCIDENT_EVENT_CLOSED",
    "INCIDENT_EVENT_NOTE",
    "INCIDENT_EVENT_SUBMITTED",
]
