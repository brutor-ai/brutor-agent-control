# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# AI System dependencies — declared composition edges

An AI System is a composite: its runs may delegate to other agents. A
delegate that is a **member** of the system (a component) needs no edge —
its actions are the system's actions. Anything else the system relies on is
a **dependency**, declared here with a role:

  * ``shared_service`` — another AI System in the tenant, separately owned
    and approved, assured from its run segments.
  * ``external`` — a third-party agent card with no Brutor AI System behind
    it. Observed only at the boundary; everything past it is *assumed, not
    tested* and is recorded in ``assumptions``.

An edge observed in the ledger but not declared here is a composition
finding (``composition.undeclared_dependency``), exactly as an undeclared
client is today. The inverse side stays declarative: the serving system
accepts callers through ``resource_groups.intended_client_systems``.

Authorship is ``declared_by`` / ``declared_at`` / ``updated_by`` /
``updated_at`` — stamped by the endpoint from the acting admin, never from
the request body.
"""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, String,
    Text, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from app.database import Base

ROLE_SHARED_SERVICE = "shared_service"
ROLE_EXTERNAL = "external"
DEPENDENCY_ROLES = (ROLE_SHARED_SERVICE, ROLE_EXTERNAL)

CRITICALITY_CRITICAL = "critical"
CRITICALITY_DEGRADABLE = "degradable"
DEPENDENCY_CRITICALITIES = (CRITICALITY_CRITICAL, CRITICALITY_DEGRADABLE)


class AISystemDependency(Base):
    """One declared edge from a depending (calling) AI System."""

    __tablename__ = "ai_system_dependencies"

    id = Column(String(64), primary_key=True)  # dep-<ULID>

    tenant_id = Column(
        String(255), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    ai_system_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        nullable=False,
        comment="The depending (calling) AI System",
    )
    role = Column(String(32), nullable=False, comment="shared_service | external")
    depends_on_system_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        nullable=True,
        comment="Target AI System (required for shared_service)",
    )
    agent_card_id = Column(
        String(255),
        ForeignKey("agent_cards.id", ondelete="CASCADE"),
        nullable=True,
        comment="Target agent card (required for external; optional for shared_service)",
    )
    criticality = Column(
        String(16), nullable=False, default=CRITICALITY_CRITICAL,
        server_default=CRITICALITY_CRITICAL,
    )
    fallback = Column(Text, nullable=True, comment="Required when degradable")
    contributes_to_decision = Column(
        Boolean, nullable=False, default=True, server_default=text("true"),
    )
    separability_rationale = Column(
        Text, nullable=True, comment="Required when contributes_to_decision = false",
    )
    # The separability claim is SEALED as an
    # ai.brutor.declaration; its record id and seal time live
    # here, cleared whenever the claim changes or is withdrawn.
    separability_record_id = Column(
        String(64),
        nullable=True,
        comment="Sealed ai.brutor.declaration of the separability rationale",
    )
    separability_sealed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When the separability rationale was sealed",
    )
    contract_pin = Column(
        String(64),
        nullable=True,
        comment="NULL = follow the target's active contract; else a contract id of the target",
    )
    supplier_legal_entity = Column(
        String(255),
        nullable=True,
        comment="Legal entity providing the dependency when it is not the "
        "deployer (required for external); drives supplier_information",
    )
    assumptions = Column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'"),
        comment="The root's side of an assume-guarantee contract",
    )
    # The delegate outcomes ("<outcome>[.<variant>]")
    # the caller is built to handle from this dependency. An answer outside
    # the list is the caller's `unexpected_delegate_outcome` finding. NULL =
    # not declared (no finding); part of the contract closure when set.
    expected_outcomes = Column(
        JSONB(none_as_null=True), nullable=True,
        comment="Delegate outcome ids the caller handles; NULL = not declared",
    )
    declared_by = Column(String(255), nullable=True)
    declared_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_by = Column(String(255), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "ai_system_id", "depends_on_system_id",
            name="uq_ai_system_dependencies_system_target",
        ),
        UniqueConstraint(
            "tenant_id", "ai_system_id", "agent_card_id",
            name="uq_ai_system_dependencies_system_card",
        ),
        CheckConstraint(
            "ai_system_id <> depends_on_system_id",
            name="ck_ai_system_dependencies_no_self_edge",
        ),
        CheckConstraint(
            "role IN ('shared_service', 'external')",
            name="ck_ai_system_dependencies_role",
        ),
        CheckConstraint(
            "criticality IN ('critical', 'degradable')",
            name="ck_ai_system_dependencies_criticality",
        ),
        Index("ix_ai_system_dependencies_target", "tenant_id", "depends_on_system_id"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ai_system_id": self.ai_system_id,
            "role": self.role,
            "depends_on_system_id": self.depends_on_system_id,
            "agent_card_id": self.agent_card_id,
            "criticality": self.criticality,
            "fallback": self.fallback,
            "contributes_to_decision": bool(self.contributes_to_decision)
            if self.contributes_to_decision is not None else True,
            "separability_rationale": self.separability_rationale,
            "separability_record_id": self.separability_record_id,
            "separability_sealed_at": (
                self.separability_sealed_at.isoformat() if self.separability_sealed_at else None
            ),
            "contract_pin": self.contract_pin,
            "supplier_legal_entity": self.supplier_legal_entity,
            "assumptions": self.assumptions or {},
            "expected_outcomes": self.expected_outcomes,
            "declared_by": self.declared_by,
            "declared_at": self.declared_at.isoformat() if self.declared_at else None,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


__all__ = [
    "AISystemDependency",
    "ROLE_SHARED_SERVICE",
    "ROLE_EXTERNAL",
    "DEPENDENCY_ROLES",
    "CRITICALITY_CRITICAL",
    "CRITICALITY_DEGRADABLE",
    "DEPENDENCY_CRITICALITIES",
]
