# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skill Model
Primary registry for Agent Skills following the agentskills.io specification.

Stores the parsed SKILL.md content (name, description, body) alongside
gateway-specific extensions in the metadata JSON field. Supports the spec's
progressive disclosure pattern: catalog returns frontmatter only, activation
loads the full SKILL.md body, deep context loads references/scripts on demand.

Integration points:
- ResourceGroup (many-to-many via resource_group_agent_skills)
- AgentSkillResource (one-to-many: references/ directory)
- AgentSkillScript (one-to-many: scripts/ directory)
- AgentSkillAsset (one-to-many: assets/ directory)
- AgentSkillExecution (one-to-many: audit trail)
- AgentSkillDependency (self-referential many-to-many: skill composition)
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, TYPE_CHECKING

from sqlalchemy import (
    text,
    Table, Column, String, Boolean, Integer, DateTime, Text,
    Index, UniqueConstraint, ForeignKey, JSON, Enum as SQLEnum
)
from sqlalchemy.orm import relationship, validates, Mapped
from sqlalchemy.sql import func

from app.database.models.resource_mixins import ResourceAuthorshipMixin, ResourceVersionMixin
from app.database import Base
from app.constants.agent_skill import SkillStatus, SkillDisclosureMode

if TYPE_CHECKING:
    # Relationship annotations only (`Mapped[List["X"]]`): SQLAlchemy resolves
    # the names through its registry, and importing them here at run time would
    # be a cycle.
    from app.database.models.skill_source import SkillSource
    from app.database.models.resource_group import ResourceGroup
    from app.database.models.agent_skill_resource import AgentSkillResource
    from app.database.models.agent_skill_script import AgentSkillScript
    from app.database.models.agent_skill_asset import AgentSkillAsset
    from app.database.models.agent_skill_execution import AgentSkillExecution
    from app.database.models.agent_skill_dependency import AgentSkillDependency


# ============================================
# Association Table: ResourceGroup <-> AgentSkill
# ============================================
# Simple many-to-many link, same pattern as resource_group_llm_models
# and resource_group_server_configs. Governance (limits, approval,
# execution mode) lives on the ResourceGroup model.

resource_group_agent_skills = Table(
    'resource_group_agent_skills',
    Base.metadata,
    Column('id', String(255), primary_key=True, default=lambda: str(uuid.uuid4())),
    Column(
        'resource_group_id',
        String(255),
        ForeignKey('resource_groups.id', ondelete='CASCADE'),
        nullable=False
    ),
    Column(
        'agent_skill_id',
        String(255),
        ForeignKey('agent_skills.id', ondelete='CASCADE'),
        nullable=False
    ),
    Column(
        'created_at',
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc)
    ),
    Column('created_by', String(255)),
    Column('limits', JSON(none_as_null=True), nullable=True, comment="Per-skill limits (execution). Effective = min(global, per_item)"),
    Column('governance', JSON(none_as_null=True), nullable=True, comment="Per-skill governance (execution_mode, approval_timeout). Effective = restrictive_merge(global, per_item)"),
    UniqueConstraint('resource_group_id', 'agent_skill_id', name='uq_group_agent_skill')
)


# ============================================
# AgentSkill Model
# ============================================

class AgentSkill(ResourceAuthorshipMixin, ResourceVersionMixin, Base):
    """
    Agent Skill registry entry

    Represents one version of one skill within a tenant. Follows the
    Agent Skills specification (agentskills.io) for field names and
    constraints, with gateway-specific extensions in the metadata JSON.

    Spec compliance:
    - name: 1-64 chars, lowercase a-z and hyphens, matches directory name
    - description: 1-1024 chars
    - license, compatibility, allowed_tools: optional spec fields
    - metadata: arbitrary key-value (spec's extensibility mechanism)

    Gateway extensions (stored in metadata JSON):
    - required_mcp_servers: list of ServerConfig IDs needed
    - required_llm_models: list of LLModel/RoutingGroup IDs needed
    - oauth_scopes_required: list of OAuth scopes needed
    - rate_limit_tier: "standard" | "premium" | "enterprise"
    - approval_required: default approval requirement
    - cost_estimate_usd: estimated cost per execution
    - tags: list of categorization tags for catalog filtering
    - icon_name: Lucide icon name for UI display
    - category: skill category for catalog grouping

    Publishing lifecycle:
        DRAFT → VALIDATED → PUBLISHED → DEPRECATED → ARCHIVED
        Published skills are immutable; changes require a new version.
    """
    __tablename__ = "agent_skills"

    # =========================================================================
    # PRIMARY KEY & MULTI-TENANCY
    # =========================================================================

    id = Column(String(255), primary_key=True, comment="ULID format: skill-01J... (legacy: ask-01J...)")

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this skill belongs to"
    )

    # =========================================================================
    # AGENT SKILLS SPEC FIELDS
    # =========================================================================

    name = Column(
        String(64),
        nullable=False,
        comment="Skill name: 1-64 chars, lowercase a-z and hyphens (spec: directory name)"
    )

    description = Column(
        String(1024),
        nullable=False,
        comment="Human-readable description of what this skill does (spec: 1-1024 chars)"
    )

    license = Column(
        String(100),
        nullable=True,
        comment="License identifier (spec: optional, e.g. 'Apache-2.0', 'MIT', 'proprietary')"
    )

    compatibility = Column(
        JSON,
        nullable=True,
        comment="List of compatible agent identifiers (spec: optional)"
    )

    allowed_tools = Column(
        JSON,
        nullable=True,
        comment="Pre-approved tool names the skill may invoke (spec: space-delimited in YAML)"
    )

    # Tenant-wide default limit/policy posture for this skill (min-wins with each
    # Resource Group). Same shapes the RG skill forms use (skill_global_limits /
    # skill_global_governance). The Rust proxy folds these into the effective
    # (RG, skill) limits/governance with min-wins. Set via Resources → Skills →
    # skill edit → Limits / Policies.
    tenant_limits = Column(JSON(none_as_null=True), nullable=True)
    tenant_governance = Column(JSON(none_as_null=True), nullable=True)

    # =========================================================================
    # SKILL.MD CONTENT (Progressive Disclosure Level 2)
    # =========================================================================

    skill_md_content = Column(
        Text,
        nullable=False,
        comment="Full SKILL.md body below the YAML frontmatter (spec recommends <5000 tokens)"
    )

    skill_md_hash = Column(
        String(64),
        nullable=False,
        comment="SHA-256 hash of skill_md_content for change detection and deduplication"
    )

    # =========================================================================
    # GATEWAY EXTENSIONS (via spec's metadata field)
    # =========================================================================

    skill_metadata = Column(
        "metadata",  # Actual column name in the database (matches spec field name)
        JSON,
        nullable=True,
        comment="""Spec's metadata field — arbitrary key-value. Gateway extensions:
        {
            "required_mcp_servers": ["sc-01J...", "sc-01J..."],
            "required_llm_models": ["llm-01J...", "rg-01J..."],
            "oauth_scopes_required": ["read:data", "write:reports"],
            "rate_limit_tier": "standard",
            "approval_required": false,
            "cost_estimate_usd": 0.05,
            "tags": ["finance", "reporting", "sql"],
            "icon_name": "BarChart3",
            "category": "data-analysis"
        }"""
    )

    # =========================================================================
    # VERSIONING & LIFECYCLE
    # =========================================================================

    version = Column(
        String(50),
        nullable=False,
        default="1.0.0",
        comment="Semantic version (major.minor.patch)"
    )

    status = Column(
        SQLEnum(SkillStatus),
        nullable=False,
        default=SkillStatus.DRAFT,
        comment="Publishing lifecycle: draft → validated → published → deprecated → archived"
    )

    enabled = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="Master switch — disabled skills are invisible to the catalog API"
    )

    allow_local_export = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="When true, a group member may download this skill's "
                "bundle for install into a local agent (brutor skills install). "
                "Default off: server-side progressive disclosure stays the norm."
    )

    # =========================================================================
    # PROGRESSIVE DISCLOSURE
    # =========================================================================

    disclosure_mode = Column(
        SQLEnum(SkillDisclosureMode),
        nullable=False,
        default=SkillDisclosureMode.PROGRESSIVE,
        comment="How this skill is consumed over MCP: "
                "progressive (L1→L3, the only supported model path), "
                "execute (legacy one-shot, deprecated), or "
                "both (transition window). See SkillDisclosureMode.",
        server_default="PROGRESSIVE",
    )

    # =========================================================================
    # PUBLISHING AUDIT
    # =========================================================================

    published_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this skill was published (status → PUBLISHED)"
    )

    published_by = Column(
        String(255),
        nullable=True,
        comment="Admin who published this skill"
    )

    deprecated_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this skill was deprecated"
    )

    deprecation_reason = Column(
        Text,
        nullable=True,
        comment="Why this skill was deprecated (e.g. 'Superseded by query-sales-data v2.0.0')"
    )

    # =========================================================================
    # VALIDATION
    # =========================================================================

    validation_errors = Column(
        JSON,
        nullable=True,
        comment="List of validation errors from last validation attempt"
    )

    last_validated_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this skill was last validated"
    )

    # =========================================================================
    # METRICS
    # =========================================================================

    execution_count = Column(
        Integer,
        nullable=False,
        default=0,
        comment="Total executions across all groups"
    )

    last_executed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When this skill was last executed"
    )

    # =========================================================================
    # WORKFLOW (DAG-based orchestration)
    # =========================================================================

    workflow_definition = Column(
        JSON,
        nullable=True,
        comment="""Full workflow DAG definition (JSON-serialized WorkflowDefinition).
        Stores nodes, edges, variables, viewport state, and lifecycle status.
        Null when no workflow has been created for this skill.
        Published workflows are immutable — edits reset status to draft."""
    )

    workflow_executions = Column(
        JSON,
        nullable=True,
        comment="""List of workflow execution records (JSON array of WorkflowExecution).
        Stores execution history including per-node status, timing, and context.
        In production, consider migrating to a separate table for high-volume skills."""
    )

    # =========================================================================
    # AUDIT
    # =========================================================================

    # created_by / updated_by come from ResourceAuthorshipMixin — one
    # definition, stamped automatically on flush.

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
    # SOURCE TRACKING — git-sourced skills
    # =========================================================================
    # Set on skills discovered by a SkillSource sync. Manual skills leave
    # all three NULL. On a re-sync, the ingest endpoint matches existing
    # rows by (source_repo_id, source_path) so the same skill discovered
    # at two different commits updates in-place instead of double-inserting.

    source_repo_id = Column(
        String(255),
        ForeignKey("skill_sources.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "Skill source this skill was ingested from. NULL = manually "
            "authored. ON DELETE SET NULL orphans the skill as 'Manual' "
            "when the source registration is removed."
        ),
    )

    source_commit_sha = Column(
        String(64),
        nullable=True,
        comment="Commit SHA this version of the skill was indexed from",
    )

    source_path = Column(
        String(1024),
        nullable=True,
        comment="Path within the source repo (e.g. 'skills/incident-report/SKILL.md')",
    )

    # =========================================================================
    # RELATIONSHIPS
    # =========================================================================

    # Many-to-one: AgentSkill -> SkillSource (provenance)
    source_repo: Mapped[Optional["SkillSource"]] = relationship(
        "SkillSource",
        back_populates="skills",
        foreign_keys=[source_repo_id],
    )

    # Many-to-many: AgentSkill <-> ResourceGroup
    resource_groups: Mapped[List["ResourceGroup"]] = relationship(
        "ResourceGroup",
        secondary="resource_group_agent_skills",
        back_populates="agent_skills",
        lazy="dynamic"
    )

    # One-to-many: AgentSkill -> AgentSkillResource (references/ directory)
    resources: Mapped[List["AgentSkillResource"]] = relationship(
        "AgentSkillResource",
        back_populates="skill",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )

    # One-to-many: AgentSkill -> AgentSkillScript (scripts/ directory)
    scripts: Mapped[List["AgentSkillScript"]] = relationship(
        "AgentSkillScript",
        back_populates="skill",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )

    # One-to-many: AgentSkill -> AgentSkillAsset (assets/ directory)
    assets: Mapped[List["AgentSkillAsset"]] = relationship(
        "AgentSkillAsset",
        back_populates="skill",
        cascade="all, delete-orphan",
        lazy="dynamic"
    )

    # One-to-many: AgentSkill -> AgentSkillExecution
    executions: Mapped[List["AgentSkillExecution"]] = relationship(
        "AgentSkillExecution",
        back_populates="skill",
        lazy="dynamic",
        foreign_keys="AgentSkillExecution.skill_id"
    )

    # Self-referential many-to-many via AgentSkillDependency
    # Skills this skill depends on
    dependencies: Mapped[List["AgentSkillDependency"]] = relationship(
        "AgentSkillDependency",
        back_populates="parent_skill",
        cascade="all, delete-orphan",
        foreign_keys="AgentSkillDependency.parent_skill_id",
        lazy="dynamic"
    )

    # Skills that depend on this skill
    dependents: Mapped[List["AgentSkillDependency"]] = relationship(
        "AgentSkillDependency",
        back_populates="dependency_skill",
        foreign_keys="AgentSkillDependency.dependency_skill_id",
        lazy="dynamic"
    )

    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        # Primary catalog query: "Get all active published skills for tenant"
        Index('ix_agent_skills_tenant_enabled_status',
              'tenant_id', 'enabled', 'status'),

        # Fan-out lookup by source repo, the same index the migration creates.
        Index('ix_agent_skills_source_repo_id', 'source_repo_id'),

        # Unique skill name per tenant (enforces one active name, version-independent)
        UniqueConstraint('tenant_id', 'name', 'version',
                         name='uix_agent_skills_tenant_name_version'),

        # Name lookup within tenant
        Index('ix_agent_skills_tenant_name', 'tenant_id', 'name'),

        # Version queries: "Get all versions of a skill"
        Index('ix_agent_skills_tenant_name_status',
              'tenant_id', 'name', 'status'),

        # Content deduplication across tenants
        Index('ix_agent_skills_hash', 'skill_md_hash'),

        # Audit and chronological queries
        Index('ix_agent_skills_tenant_created', 'tenant_id', 'created_at'),

        # Execution tracking
        Index('ix_agent_skills_tenant_last_executed', 'tenant_id', 'last_executed_at'),

        {
            'comment': 'Agent Skills registry following agentskills.io specification'
        }
    )

    # =========================================================================
    # VALIDATION
    # =========================================================================

    @validates('name')
    def validate_name(self, key, value):
        """Validate skill name follows the Agent Skills spec convention"""
        if not value:
            raise ValueError("Skill name is required")
        if len(value) > 64:
            raise ValueError("Skill name must be 1-64 characters")
        if not re.match(r'^[a-z][a-z0-9-]*[a-z0-9]$', value) and len(value) > 1:
            raise ValueError(
                "Skill name must be lowercase a-z, digits, and hyphens, "
                "starting with a letter and ending with a letter or digit"
            )
        if len(value) == 1 and not re.match(r'^[a-z]$', value):
            raise ValueError("Single-character skill name must be a lowercase letter")
        return value

    @validates('description')
    def validate_description(self, key, value):
        """Validate description follows spec constraints"""
        if not value:
            raise ValueError("Skill description is required")
        if len(value) > 1024:
            raise ValueError("Skill description must be 1-1024 characters")
        return value

    @validates('version')
    def validate_version(self, key, value):
        """Validate semantic version format"""
        if not value:
            return "1.0.0"
        if not re.match(r'^\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$', value):
            raise ValueError(
                "Version must follow semantic versioning (e.g. '1.0.0', '2.1.0-beta.1')"
            )
        return value

    # =========================================================================
    # LIFECYCLE METHODS
    # =========================================================================

    def validate_for_publishing(self) -> List[str]:
        """
        Validate skill is ready for publishing

        Returns:
            List of validation error strings (empty = valid)
        """
        errors = []

        if not self.name:
            errors.append("Skill name is required")

        if not self.description:
            errors.append("Skill description is required")

        if not self.skill_md_content:
            errors.append("SKILL.md content is required")

        if not self.skill_md_hash:
            errors.append("SKILL.md content hash is required")

        if not self.version:
            errors.append("Version is required")

        # Validate name format
        if self.name and not re.match(r'^[a-z][a-z0-9-]*[a-z0-9]$', self.name):
            errors.append(f"Skill name '{self.name}' does not follow spec convention")

        # Validate allowed_tools format (spec: list of tool name strings)
        if self.allowed_tools:
            if not isinstance(self.allowed_tools, list):
                errors.append("allowed_tools must be a list of tool name strings")
            elif not all(isinstance(t, str) for t in self.allowed_tools):
                errors.append("Each allowed_tool must be a string")

        # Validate compatibility format
        if self.compatibility:
            if not isinstance(self.compatibility, list):
                errors.append("compatibility must be a list of agent identifiers")

        return errors

    def publish(self, published_by: str) -> None:
        """Transition skill to PUBLISHED status"""
        if self.status not in (SkillStatus.DRAFT, SkillStatus.VALIDATED):
            raise ValueError(
                f"Cannot publish skill in '{self.status.value}' status. "
                f"Must be 'draft' or 'validated'."
            )
        self.status = SkillStatus.PUBLISHED
        self.published_at = datetime.now(timezone.utc)
        self.published_by = published_by
        self.enabled = True

    def deprecate(self, reason: str, deprecated_by: str) -> None:
        """Transition skill to DEPRECATED status"""
        if self.status != SkillStatus.PUBLISHED:
            raise ValueError("Can only deprecate published skills")
        self.status = SkillStatus.DEPRECATED
        self.deprecated_at = datetime.now(timezone.utc)
        self.deprecation_reason = reason
        self.updated_by = deprecated_by

    def archive(self) -> None:
        """Transition skill to ARCHIVED status (fully disabled)"""
        self.status = SkillStatus.ARCHIVED
        self.enabled = False

    def record_execution(self) -> None:
        """Increment execution counter"""
        self.execution_count += 1
        self.last_executed_at = datetime.now(timezone.utc)

    # =========================================================================
    # METADATA HELPERS
    # =========================================================================

    def get_metadata_value(self, key: str, default: Any = None) -> Any:
        """Get a value from the metadata JSON"""
        if not self.skill_metadata:
            return default
        return self.skill_metadata.get(key, default)

    def set_metadata_value(self, key: str, value: Any) -> None:
        """Set a value in the metadata JSON"""
        if not self.skill_metadata:
            self.skill_metadata = {}
        self.skill_metadata[key] = value

    def get_required_mcp_servers(self) -> List[str]:
        """Get list of required MCP server config IDs"""
        return self.get_metadata_value("required_mcp_servers", [])

    def get_required_llm_models(self) -> List[str]:
        """Get list of required LLM model/routing group IDs"""
        return self.get_metadata_value("required_llm_models", [])

    def get_tags(self) -> List[str]:
        """Get categorization tags"""
        return self.get_metadata_value("tags", [])

    def get_cost_estimate(self) -> Optional[float]:
        """Get estimated cost per execution in USD"""
        return self.get_metadata_value("cost_estimate_usd")

    def is_approval_required(self) -> bool:
        """Check if this skill requires approval by default"""
        return self.get_metadata_value("approval_required", False)

    # =========================================================================
    # WORKFLOW HELPERS
    # =========================================================================

    def has_workflow(self) -> bool:
        """Check if this skill has a workflow definition"""
        return self.workflow_definition is not None

    def get_workflow_status(self) -> Optional[str]:
        """Get the workflow lifecycle status (draft/validated/published)"""
        if not self.workflow_definition:
            return None
        return self.workflow_definition.get("status")

    def get_workflow_node_count(self) -> int:
        """Get the number of nodes in the workflow"""
        if not self.workflow_definition:
            return 0
        return len(self.workflow_definition.get("nodes", []))

    def get_workflow_execution_count(self) -> int:
        """Get the number of workflow executions"""
        if not self.workflow_executions:
            return 0
        return len(self.workflow_executions)

    # =========================================================================
    # QUERY HELPERS
    # =========================================================================

    def get_group_ids(self) -> List[str]:
        """Get list of resource group IDs this skill is assigned to"""
        return [g.id for g in self.resource_groups]

    def get_resource_filenames(self) -> List[str]:
        """Get list of reference filenames"""
        return [r.filename for r in self.resources]

    def get_script_filenames(self) -> List[str]:
        """Get list of script filenames"""
        return [s.filename for s in self.scripts]

    def get_asset_filenames(self) -> List[str]:
        """Get list of asset filenames"""
        return [a.filename for a in self.assets]

    def get_dependency_skill_ids(self) -> List[str]:
        """Get list of skill IDs this skill depends on"""
        return [d.dependency_skill_id for d in self.dependencies]

    def build_manifest(self) -> Dict[str, Any]:
        """
        Progressive disclosure L2 manifest.

        The version-pinned inventory of everything this skill exposes at L3 —
        the source of truth the `skills__load` response advertises and the L3
        allowlist (`skills__read_resource` / `skills__run_script` /
        `skills__render_template`) enforces. Derived from the child tables so it
        cannot drift from what is actually stored. Disabled scripts are omitted
        (they can never be run). Requires resources/scripts/assets to be loaded.
        """
        def _enum(v):
            return v.value if hasattr(v, "value") else v

        return {
            "resources": [
                {
                    "path": r.filename,
                    "kind": _enum(r.resource_type),
                    "mime": r.mime_type,
                    "bytes": r.size_bytes,
                }
                for r in self.resources
            ],
            "scripts": [
                {
                    "name": s.filename,
                    "language": _enum(s.language),
                    "execution_mode": _enum(s.execution_mode),
                    "args_schema": s.args_schema,
                    "description": s.description,
                }
                for s in self.scripts
                if _enum(s.execution_mode) != "disabled"
            ],
            "templates": [
                {
                    "name": a.filename,
                    "kind": _enum(a.asset_type),
                    "mime": a.mime_type,
                    "vars_schema": a.vars_schema,
                    "description": a.description,
                }
                for a in self.assets
            ],
        }

    # =========================================================================
    # SERIALIZATION
    # =========================================================================

    def to_catalog_dict(self) -> Dict[str, Any]:
        """
        Progressive disclosure Level 1: Catalog entry

        Returns minimal metadata for skill discovery.
        Agents use this to decide whether to activate the skill.
        ~50-100 tokens per skill.
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "status": self.status.value if isinstance(self.status, SkillStatus) else self.status,
            "license": self.license,
            "tags": self.get_tags(),
            "category": self.get_metadata_value("category"),
            "icon_name": self.get_metadata_value("icon_name"),
            "cost_estimate_usd": self.get_cost_estimate(),
            "approval_required": self.is_approval_required(),
            "compatibility": self.compatibility,
            "execution_count": self.execution_count,
            "has_workflow": self.has_workflow(),
            "disclosure_mode": (
                self.disclosure_mode.value
                if isinstance(self.disclosure_mode, SkillDisclosureMode)
                else self.disclosure_mode
            ),
        }

    def to_activation_dict(self) -> Dict[str, Any]:
        """
        Progressive disclosure Level 2: Full SKILL.md content

        Returns the full skill body for agent context injection.
        Up to ~5000 tokens.
        """
        data = self.to_catalog_dict()
        data.update({
            "skill_md_content": self.skill_md_content,
            "allowed_tools": self.allowed_tools,
            "resource_filenames": self.get_resource_filenames(),
            "script_filenames": self.get_script_filenames(),
            "asset_filenames": self.get_asset_filenames(),
            "dependency_skill_ids": self.get_dependency_skill_ids(),
        })
        return data

    def to_dict(self, include_content: bool = False) -> Dict[str, Any]:
        """
        Full dictionary for admin API responses

        Args:
            include_content: Whether to include SKILL.md body (large)
        """
        data = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "status": self.status.value if isinstance(self.status, SkillStatus) else self.status,
            "enabled": self.enabled,
            "allow_local_export": self.allow_local_export,
            "disclosure_mode": (
                self.disclosure_mode.value
                if isinstance(self.disclosure_mode, SkillDisclosureMode)
                else self.disclosure_mode
            ),
            "license": self.license,
            "compatibility": self.compatibility,
            "allowed_tools": self.allowed_tools,
            "tenant_limits": self.tenant_limits,
            "tenant_governance": self.tenant_governance,
            "metadata": self.skill_metadata,
            "skill_md_hash": self.skill_md_hash,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "published_by": self.published_by,
            "deprecated_at": self.deprecated_at.isoformat() if self.deprecated_at else None,
            "deprecation_reason": self.deprecation_reason,
            "validation_errors": self.validation_errors,
            "last_validated_at": self.last_validated_at.isoformat() if self.last_validated_at else None,
            "execution_count": self.execution_count,
            "last_executed_at": self.last_executed_at.isoformat() if self.last_executed_at else None,
            "group_ids": self.get_group_ids(),
            "resource_filenames": self.get_resource_filenames(),
            "script_filenames": self.get_script_filenames(),
            "asset_filenames": self.get_asset_filenames(),
            "created_by": self.created_by,
            "updated_by": self.updated_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "has_workflow": self.has_workflow(),
            "workflow_status": self.get_workflow_status(),
        }

        if include_content:
            data["skill_md_content"] = self.skill_md_content

        return data

    def get_summary(self) -> Dict[str, Any]:
        """Get skill summary without content or metadata details"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "status": self.status.value if isinstance(self.status, SkillStatus) else self.status,
            "enabled": self.enabled,
            "execution_count": self.execution_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<AgentSkill(id='{self.id}', name='{self.name}', "
            f"version='{self.version}', status='{self.status}')>"
        )