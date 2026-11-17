# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skill Dependency Model
Tracks skill composition — when one skill requires or optionally chains
to another skill.

Enables multi-step workflows:
  Sequential:   Skill A → Skill B → Skill C (execution_order)
  Conditional:  If condition then Skill X else Skill Y (condition JSON)
  Required:     Skill B must succeed before Skill A can complete
  Optional:     Skill C failure doesn't block Skill A

The gateway resolves the dependency graph at execution time:
1. Build DAG from parent → dependency relationships
2. Detect cycles (reject circular dependencies)
3. Check authorization for ALL skills in the chain
4. Aggregate resource requirements
5. Execute in order, passing context between steps
6. Track via correlation_id and parent_execution_id in AgentSkillExecution

Example:
  "Generate Quarterly Report" skill depends on:
    1. "Query Sales Data" (required, order=1)
    2. "Query Customer Metrics" (required, order=1)  — parallel with #1
    3. "Summarize Findings" (required, order=2)       — after both complete
    4. "Email Report" (optional, order=3)              — best-effort delivery
"""

from typing import Dict, Any

from sqlalchemy import (
    Column, String, Integer, DateTime, Index, UniqueConstraint,
    ForeignKey, JSON, Enum as SQLEnum
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base
from app.constants.agent_skill import DependencyType


class AgentSkillDependency(Base):
    """
    Dependency relationship between two skills within a tenant

    Defines that parent_skill depends on dependency_skill with a
    specified type (required/optional/conditional) and execution order.

    One dependency per (tenant, parent, dependency) — enforced by unique constraint.
    """
    __tablename__ = "agent_skill_dependencies"

    # =========================================================================
    # PRIMARY KEY & MULTI-TENANCY
    # =========================================================================

    id = Column(String(255), primary_key=True, comment="ULID format: asd-01J...")

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this dependency belongs to"
    )

    # =========================================================================
    # DEPENDENCY REFERENCES
    # =========================================================================

    parent_skill_id = Column(
        String(255),
        ForeignKey("agent_skills.id", ondelete="CASCADE"),
        nullable=False,
        comment="The skill that HAS the dependency (the composite/parent skill)"
    )

    dependency_skill_id = Column(
        String(255),
        ForeignKey("agent_skills.id", ondelete="CASCADE"),
        nullable=False,
        comment="The skill that IS the dependency (the required/optional sub-skill)"
    )

    # =========================================================================
    # DEPENDENCY CONFIGURATION
    # =========================================================================

    dependency_type = Column(
        SQLEnum(DependencyType),
        nullable=False,
        default=DependencyType.REQUIRED,
        comment="How critical this dependency is: required, optional, conditional"
    )

    execution_order = Column(
        Integer,
        nullable=False,
        default=0,
        comment="""Execution ordering within the parent skill's dependency graph.
        Same order = parallel execution. Sequential order = 0, 1, 2...
        Example: order=0 for data gathering, order=1 for processing, order=2 for output"""
    )

    condition = Column(
        JSON,
        nullable=True,
        comment="""Condition for CONDITIONAL dependencies. Evaluated at runtime.
        Example: {"when": "input.data_source == 'snowflake'",
                  "unless": "input.skip_export == true"}
        NULL for required/optional dependencies."""
    )

    description = Column(
        String(500),
        nullable=True,
        comment="Human-readable description of why this dependency exists"
    )

    # =========================================================================
    # AUDIT
    # =========================================================================

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    created_by = Column(
        String(255),
        nullable=True,
        comment="Admin who created this dependency"
    )

    # =========================================================================
    # RELATIONSHIPS
    # =========================================================================

    parent_skill = relationship(
        "AgentSkill",
        back_populates="dependencies",
        foreign_keys=[parent_skill_id]
    )

    dependency_skill = relationship(
        "AgentSkill",
        back_populates="dependents",
        foreign_keys=[dependency_skill_id]
    )

    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        # One dependency relationship per (tenant, parent, dependency)
        UniqueConstraint('tenant_id', 'parent_skill_id', 'dependency_skill_id',
                         name='uix_agent_skill_deps_tenant_parent_dependency'),

        # Forward traversal: "What does this skill depend on?"
        Index('ix_agent_skill_deps_tenant_parent',
              'tenant_id', 'parent_skill_id'),

        # Reverse traversal: "What skills depend on this skill?"
        Index('ix_agent_skill_deps_tenant_dependency',
              'tenant_id', 'dependency_skill_id'),

        # Execution planning: "Get ordered dependencies for a skill"
        Index('ix_agent_skill_deps_parent_order',
              'parent_skill_id', 'execution_order'),

        {
            'comment': 'Skill composition and chaining relationships'
        }
    )

    # =========================================================================
    # QUERY HELPERS
    # =========================================================================

    def is_required(self) -> bool:
        """Check if this is a required dependency"""
        return self.dependency_type == DependencyType.REQUIRED

    def is_optional(self) -> bool:
        """Check if this is an optional dependency"""
        return self.dependency_type == DependencyType.OPTIONAL

    def is_conditional(self) -> bool:
        """Check if this is a conditional dependency"""
        return self.dependency_type == DependencyType.CONDITIONAL

    def evaluate_condition(self, context: Dict[str, Any]) -> bool:
        """
        Evaluate whether a conditional dependency should execute

        Args:
            context: Execution context with input params, runtime state, etc.

        Returns:
            True if dependency should execute. Always True for non-conditional types.
        """
        if self.dependency_type != DependencyType.CONDITIONAL:
            return True  # Required and optional always evaluate to True

        if not self.condition:
            return True  # No condition defined = always execute

        # Evaluate "when" condition (must be True to execute)
        when_expr = self.condition.get("when")
        if when_expr and not self._evaluate_expression(when_expr, context):
            return False

        # Evaluate "unless" condition (must be False to execute)
        unless_expr = self.condition.get("unless")
        if unless_expr and self._evaluate_expression(unless_expr, context):
            return False

        return True

    @staticmethod
    def _evaluate_expression(expression: str, context: Dict[str, Any]) -> bool:
        """
        Safely evaluate a simple expression against context

        Supports dot-notation access: "input.data_source == 'snowflake'"
        This is deliberately limited — complex conditions should use
        plugin hooks (SKILL_RESOURCE_RESOLUTION) instead.

        Args:
            expression: Simple comparison expression
            context: Dictionary to evaluate against

        Returns:
            Boolean result of evaluation
        """
        # Simple key-value checks only — no arbitrary code execution
        # For production, consider a safe expression evaluator library
        try:
            # Handle "key == value" comparisons
            if "==" in expression:
                left, right = [s.strip() for s in expression.split("==", 1)]
                left_val = AgentSkillDependency._resolve_dotted_key(left, context)
                # Strip quotes from right side
                right_val = right.strip("'\"")
                return str(left_val) == right_val

            # Handle "key != value" comparisons
            if "!=" in expression:
                left, right = [s.strip() for s in expression.split("!=", 1)]
                left_val = AgentSkillDependency._resolve_dotted_key(left, context)
                right_val = right.strip("'\"")
                return str(left_val) != right_val

            # Handle bare truthy check "key"
            val = AgentSkillDependency._resolve_dotted_key(expression.strip(), context)
            return bool(val)

        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _resolve_dotted_key(key: str, context: Dict[str, Any]) -> Any:
        """Resolve a dotted key path like 'input.data_source' against a dict"""
        current = context
        for part in key.split("."):
            if isinstance(current, dict):
                current = current[part]
            else:
                raise KeyError(f"Cannot resolve '{part}' in non-dict value")
        return current

    # =========================================================================
    # SERIALIZATION
    # =========================================================================

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "parent_skill_id": self.parent_skill_id,
            "dependency_skill_id": self.dependency_skill_id,
            "dependency_type": (
                self.dependency_type.value
                if isinstance(self.dependency_type, DependencyType)
                else self.dependency_type
            ),
            "execution_order": self.execution_order,
            "condition": self.condition,
            "description": self.description,
            "parent_skill_name": (
                self.parent_skill.name if self.parent_skill else None
            ),
            "dependency_skill_name": (
                self.dependency_skill.name if self.dependency_skill else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
        }

    def __repr__(self) -> str:
        return (
            f"<AgentSkillDependency(parent='{self.parent_skill_id}', "
            f"dependency='{self.dependency_skill_id}', "
            f"type='{self.dependency_type}', order={self.execution_order})>"
        )
