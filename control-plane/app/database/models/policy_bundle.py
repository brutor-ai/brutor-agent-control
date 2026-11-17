# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Policy-as-Code Database Models

Three tables support the policy-as-code feature:
- PolicyBundle: Versioned, scope-aware YAML policy definitions
- PolicyBundleEvent: Audit trail for every validate/apply/rollback action
- PolicyTemplate: Pre-built and custom policy templates

Each PolicyBundle is scoped to a specific resource group (scope_group_id = group.id).
Multiple bundles can be active simultaneously at different scopes.
Only one active bundle per (tenant_id, scope) is allowed.
"""

import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from sqlalchemy import (
    Column, String, Boolean, DateTime, Text, Integer,
    Index, UniqueConstraint, ForeignKey, JSON,
    Enum as SQLEnum, text,
)
from sqlalchemy.orm import relationship, Mapped
from sqlalchemy.sql import func

from app.database import Base
from app.constants.policy import PolicyBundleStatus, PolicyEventType


# ============================================
# PolicyBundle
# ============================================

class PolicyBundle(Base):
    """
    A versioned, scope-aware policy bundle.

    Each bundle captures a governance posture scoped to a specific resource
    group subtree. Multiple bundles can be active simultaneously at different
    scopes. Inheritance flows from parent groups → child groups with
    restrict-only merge semantics.
    """
    __tablename__ = "policy_bundles"

    id = Column(String(255), primary_key=True, comment="ULID format: pb-01J...")

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this bundle belongs to"
    )

    # =========================================================================
    # SCOPE — which part of the resource hierarchy this bundle governs
    # =========================================================================

    scope_group_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        nullable=False,
        comment="Resource group this bundle is scoped to"
    )

    scope_group_path = Column(
        String(1000),
        nullable=True,
        comment="Denormalized hierarchy_path snapshot at apply time (e.g. 'Engineering/Frontend')"
    )

    # =========================================================================
    # BUNDLE METADATA (from YAML metadata section)
    # =========================================================================

    name = Column(
        String(255),
        nullable=False,
        comment="Bundle name from YAML metadata.name"
    )

    description = Column(
        Text,
        nullable=True,
        comment="Bundle description from YAML metadata.description"
    )

    version = Column(
        String(50),
        nullable=False,
        default="1.0.0",
        comment="Semantic version from YAML metadata.version"
    )

    labels = Column(
        JSON,
        nullable=True,
        comment="Arbitrary labels from YAML metadata.labels (e.g. environment, compliance)"
    )

    # =========================================================================
    # YAML CONTENT
    # =========================================================================

    yaml_content = Column(
        Text,
        nullable=False,
        comment="The full canonical YAML policy document"
    )

    resolved_config = Column(
        JSON,
        nullable=True,
        comment="Parsed YAML as JSON with resolved references"
    )

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    status = Column(
        SQLEnum(PolicyBundleStatus),
        nullable=False,
        default=PolicyBundleStatus.DRAFT,
        comment="Bundle lifecycle: draft → validated → active → superseded | rolled_back"
    )

    is_active = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether this is the currently active bundle at its scope"
    )

    # =========================================================================
    # DIFF & CHANGE TRACKING
    # =========================================================================

    diff_summary = Column(
        JSON,
        nullable=True,
        comment="Structured diff from previous active bundle at this scope"
    )

    # =========================================================================
    # SOURCE TRACKING (for CI/CD attribution)
    # =========================================================================

    source = Column(
        String(50),
        nullable=True,
        comment="How this bundle was created: api, ci_cd, admin_ui, import"
    )

    source_ref = Column(
        String(500),
        nullable=True,
        comment="External reference: git commit SHA, CI pipeline URL, etc."
    )

    # =========================================================================
    # AUDIT
    # =========================================================================

    applied_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this bundle was applied (status → ACTIVE)"
    )

    applied_by = Column(
        String(255),
        nullable=True,
        comment="Admin who applied this bundle"
    )

    rolled_back_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this bundle was rolled back"
    )

    rolled_back_by = Column(
        String(255),
        nullable=True,
        comment="Admin who rolled back this bundle"
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    created_by = Column(
        String(255),
        nullable=True,
        comment="Admin who created this bundle"
    )

    # =========================================================================
    # RELATIONSHIPS
    # =========================================================================

    events: Mapped[List["PolicyBundleEvent"]] = relationship(
        "PolicyBundleEvent",
        back_populates="bundle",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )

    scope_group = relationship(
        "ResourceGroup",
        foreign_keys=[scope_group_id],
        lazy="selectin",
    )

    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        Index("ix_policy_bundles_tenant", "tenant_id"),
        Index("ix_policy_bundles_tenant_scope", "tenant_id", "scope_group_id"),
        Index("ix_policy_bundles_tenant_scope_active", "tenant_id", "scope_group_id", "is_active"),
        Index("ix_policy_bundles_tenant_name", "tenant_id", "name"),
        Index("ix_policy_bundles_tenant_status", "tenant_id", "status"),
        Index("ix_policy_bundles_tenant_created", "tenant_id", "created_at"),
        # Partial unique index: at most one active bundle per (tenant, scope).
        Index(
            "uq_policy_bundles_active_per_scope",
            "tenant_id",
            "scope_group_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
        {
            "comment": "Scope-aware policy bundles with versioning and lifecycle tracking"
        }
    )

    # =========================================================================
    # SERIALIZATION
    # =========================================================================

    def to_dict(self, include_yaml: bool = False) -> Dict[str, Any]:
        """Convert to API response dict"""
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "scope_group_id": self.scope_group_id,
            "scope_group_path": self.scope_group_path,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "labels": self.labels,
            "status": self.status.value if isinstance(self.status, PolicyBundleStatus) else self.status,
            "is_active": self.is_active,
            "diff_summary": self.diff_summary,
            "source": self.source,
            "source_ref": self.source_ref,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
            "applied_by": self.applied_by,
            "rolled_back_at": self.rolled_back_at.isoformat() if self.rolled_back_at else None,
            "rolled_back_by": self.rolled_back_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
        }
        if include_yaml:
            data["yaml_content"] = self.yaml_content
        return data

    def to_summary(self) -> Dict[str, Any]:
        """Lightweight summary for list views"""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "status": self.status.value if isinstance(self.status, PolicyBundleStatus) else self.status,
            "is_active": self.is_active,
            "scope_group_id": self.scope_group_id,
            "scope_group_path": self.scope_group_path,
            "source": self.source,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
            "applied_by": self.applied_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        scope = self.scope_group_path or self.scope_group_id
        return (
            f"<PolicyBundle(id='{self.id}', name='{self.name}', "
            f"version='{self.version}', status='{self.status}', "
            f"scope='{scope}', is_active={self.is_active})>"
        )


# ============================================
# PolicyBundleEvent
# ============================================

class PolicyBundleEvent(Base):
    """
    Audit trail for policy bundle operations.

    Records every validate, apply, rollback, and dry-run event
    with the admin context and change details.
    """
    __tablename__ = "policy_bundle_events"

    id = Column(String(255), primary_key=True, comment="ULID format: pbe-01J...")

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this event belongs to"
    )

    bundle_id = Column(
        String(255),
        ForeignKey("policy_bundles.id", ondelete="CASCADE"),
        nullable=True,
        comment="The bundle this event relates to (null for dry-run events)"
    )

    scope_group_id = Column(
        String(255),
        nullable=False,
        comment="Denormalized scope from bundle for fast filtering"
    )

    event_type = Column(
        SQLEnum(PolicyEventType),
        nullable=False,
        comment="Event type: validated, applied, rolled_back, dry_run, superseded"
    )

    # =========================================================================
    # CHANGE DETAILS
    # =========================================================================

    diff = Column(
        JSON,
        nullable=True,
        comment="Structured diff showing what changed"
    )

    dry_run_result = Column(
        JSON,
        nullable=True,
        comment="For dry_run events: the full simulation result"
    )

    # =========================================================================
    # ADMIN CONTEXT
    # =========================================================================

    admin_id = Column(
        String(255),
        nullable=False,
        comment="Admin who triggered this event"
    )

    admin_type = Column(
        String(20),
        nullable=False,
        comment="system_admin or tenant_admin"
    )

    ip_address = Column(
        String(50),
        nullable=True,
        comment="Client IP address"
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    # =========================================================================
    # RELATIONSHIPS
    # =========================================================================

    bundle: Mapped["PolicyBundle"] = relationship(
        "PolicyBundle",
        back_populates="events"
    )

    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        Index("ix_policy_events_tenant", "tenant_id"),
        Index("ix_policy_events_bundle", "bundle_id"),
        Index("ix_policy_events_tenant_scope", "tenant_id", "scope_group_id"),
        Index("ix_policy_events_tenant_created", "tenant_id", "created_at"),
        {
            "comment": "Audit trail for policy bundle operations"
        }
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "bundle_id": self.bundle_id,
            "scope_group_id": self.scope_group_id,
            "event_type": self.event_type.value if isinstance(self.event_type, PolicyEventType) else self.event_type,
            "diff": self.diff,
            "dry_run_result": self.dry_run_result,
            "admin_id": self.admin_id,
            "admin_type": self.admin_type,
            "ip_address": self.ip_address,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<PolicyBundleEvent(id='{self.id}', bundle_id='{self.bundle_id}', "
            f"event_type='{self.event_type}')>"
        )


# ============================================
# PolicyTemplate
# ============================================

class PolicyTemplate(Base):
    """
    Pre-built or custom policy templates.

    System templates (is_system=True) are seeded during initialization
    and available to all tenants. Tenant-scoped templates are created
    by admins for reuse within their tenant.
    """
    __tablename__ = "policy_templates"

    id = Column(String(255), primary_key=True, comment="ULID format: pt-01J...")

    name = Column(
        String(255),
        nullable=False,
        comment="Template name (unique within scope)"
    )

    description = Column(
        Text,
        nullable=True,
        comment="Template description"
    )

    category = Column(
        String(100),
        nullable=False,
        comment="Template category: compliance, security, cost, development"
    )

    tags = Column(
        JSON,
        nullable=True,
        comment="Searchable tags for template discovery"
    )

    yaml_content = Column(
        Text,
        nullable=False,
        comment="The full YAML policy template"
    )

    is_system = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="True = system-provided (available to all tenants), False = tenant-scoped"
    )

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        comment="Null for system templates, set for tenant-scoped custom templates"
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )

    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        UniqueConstraint("name", "tenant_id", name="uq_policy_template_name_tenant"),
        Index("ix_policy_templates_category", "category"),
        Index("ix_policy_templates_system", "is_system"),
        {
            "comment": "Pre-built and custom policy templates"
        }
    )

    def to_dict(self, include_yaml: bool = False) -> Dict[str, Any]:
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "tags": self.tags,
            "is_system": self.is_system,
            "tenant_id": self.tenant_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_yaml:
            data["yaml_content"] = self.yaml_content
        return data

    def __repr__(self) -> str:
        return (
            f"<PolicyTemplate(id='{self.id}', name='{self.name}', "
            f"category='{self.category}', is_system={self.is_system})>"
        )
