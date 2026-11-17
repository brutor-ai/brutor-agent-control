# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Argument Policy Database Model

AST-aware argument policies for tool/skill invocations. Where a
guardrail asks "does this *text* contain something bad", an
argument policy asks "does this *structured argument* do
something bad" — e.g. is the SQL a write, is the URL a private
IP, is the shell command outside the allowlist.

Defined once under Governance, assigned many-to-many to
Resource Groups (same pattern as guardrails, LLM models, MCP
servers, skills).

The runtime evaluator lives in the Rust proxy
(`services/argument_policy/`); this module is the control-plane
shape only.
"""

from sqlalchemy import (
    text,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


# Surfaces — keep in sync with the analyzer wires in
# brutor-gateway-core (llm_governance.rs, routes/mcp.rs, routes/skills.rs)
# and the CHECK constraint in migration `a056_argument_policies`.
SURFACE_LLM_TOOL_CALLS = "llm_output_tool_calls"
SURFACE_MCP_INPUT = "mcp_input"
SURFACE_SKILL_INPUT = "skill_input"
ALLOWED_SURFACES = (
    SURFACE_LLM_TOOL_CALLS,
    SURFACE_MCP_INPUT,
    SURFACE_SKILL_INPUT,
)

# Analyzers — registry of parsers shipped with the proxy.
ANALYZER_SQL = "sql"
ANALYZER_URL = "url"
ANALYZER_SHELL = "shell"
ANALYZER_PATH = "path"
ANALYZER_JSON = "json"
ALLOWED_ANALYZERS = (
    ANALYZER_SQL,
    ANALYZER_URL,
    ANALYZER_SHELL,
    ANALYZER_PATH,
    ANALYZER_JSON,
)

#: How hard a tripped rule bites.
#:
#: `approval_required` is the band between the other two:
#: the call is neither allowed nor refused, it is held for a human through
#: the existing approval queue. Without it a threshold policy has
#: to be a hard block, which kills the agent above the threshold rather than
#: supervising it.
#:
#: Mirrored by the Rust `Severity` enum in
#: `services/argument_policy/policy.rs` and by the CHECK constraint widened
#: in migration `a175_arg_policy_approval`. All three must agree.
SEVERITY_DENY = "deny"
SEVERITY_APPROVAL_REQUIRED = "approval_required"
SEVERITY_WARN = "warn"

ALLOWED_SEVERITIES = (SEVERITY_DENY, SEVERITY_APPROVAL_REQUIRED, SEVERITY_WARN)


# =============================================================================
# Many-to-many join (mirrors resource_group_guardrails)
# =============================================================================

resource_group_argument_policies = Table(
    "resource_group_argument_policies",
    Base.metadata,
    Column(
        "resource_group_id",
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "argument_policy_id",
        String(255),
        ForeignKey("argument_policies.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    # Reverse-lookup by policy, the same index the migration creates.
    Index("ix_rg_argpolicies_policy", "argument_policy_id"),
    comment="Associates argument policies with resource groups",
)


class ArgumentPolicy(Base):
    """Single source of truth for one AST-aware argument policy."""

    __tablename__ = "argument_policies"

    id = Column(String(255), primary_key=True)

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )

    name = Column(String(255), nullable=False)
    description = Column(String(1024), nullable=True)

    surface = Column(String(64), nullable=False)
    target = Column(String(255), nullable=False)
    argument_key = Column(String(255), nullable=False)

    analyzer = Column(String(32), nullable=False)
    analyzer_config = Column(JSONB, nullable=True)

    rules = Column(JSONB, nullable=False, default=list, server_default="[]")

    enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    # VARCHAR(32), not 16: `approval_required` is seventeen characters.
    # Mirrors migration `a175_arg_policy_approval` — an ORM narrower than the column silently
    # truncates on some drivers and errors on others.
    severity = Column(String(32), nullable=False, default="deny", server_default="deny")

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    groups = relationship(
        "ResourceGroup",
        secondary=resource_group_argument_policies,
        back_populates="argument_policies",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_argument_policies_tenant_name"),
        CheckConstraint(
            "surface IN ('llm_output_tool_calls','mcp_input','skill_input')",
            name="ck_argument_policies_surface",
        ),
        CheckConstraint(
            "analyzer IN ('sql','url','shell','path','json')",
            name="ck_argument_policies_analyzer",
        ),
        # Must mirror migration `a175_arg_policy_approval` exactly. `create_all` builds fresh
        # installs from these constraints while upgrades come from the
        # migration, so a mismatch produces two different schemas for the
        # same release — and the core's test fixture, dumped from a
        # create_all database, would then disagree with production.
        CheckConstraint(
            "severity IN ('deny','warn','approval_required')",
            name="ck_argument_policies_severity",
        ),
        Index(
            "ix_argument_policies_tenant_surface",
            "tenant_id",
            "surface",
        ),
    )

    def to_dict(self) -> dict:
        # Materialise the relationship once — both `group_ids` (used by
        # CLI / GitOps consumers) and `groups` (used by the admin UI's
        # GroupTreeDisplay component) read from the same list so they
        # never drift.
        groups = list(self.groups or [])
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "surface": self.surface,
            "target": self.target,
            "argument_key": self.argument_key,
            "analyzer": self.analyzer,
            "analyzer_config": self.analyzer_config,
            "rules": self.rules,
            "enabled": self.enabled,
            "severity": self.severity,
            "group_ids": [g.id for g in groups],
            "groups": [
                {
                    "id": g.id,
                    "name": g.name,
                    "display_name": getattr(g, "display_name", None),
                    "parent_group_id": getattr(g, "parent_group_id", None),
                    "is_active": getattr(g, "is_active", None),
                }
                for g in groups
            ],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return (
            f"<ArgumentPolicy(id={self.id!r}, tenant={self.tenant_id!r}, "
            f"name={self.name!r}, surface={self.surface!r}, "
            f"analyzer={self.analyzer!r}, target={self.target!r})>"
        )
