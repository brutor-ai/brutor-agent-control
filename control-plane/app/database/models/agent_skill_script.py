# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skill Script Model
Stores executable code from a skill's scripts/ directory.

Separate from AgentSkillResource because scripts have security implications:
- execution_mode controls whether/how the script runs
- language tracking enables correct execution environment selection
- Content hashing enables security audit of script changes

Security model:
  SANDBOX (default): Restricted environment — no network, limited filesystem,
                     resource caps on CPU/memory/time
  TRUSTED:          Full access — requires explicit admin approval per script
  DISABLED:         Script is stored for reference but never executed

Example files:
  scripts/validate_input.py     - Input validation before skill execution
  scripts/transform_data.js     - Data transformation during execution
  scripts/post_process.py       - Output post-processing
  scripts/setup.sh              - Environment setup (trusted mode only)
"""

from typing import Dict, Any

from sqlalchemy import Column, String, DateTime, Text, Index, UniqueConstraint, ForeignKey, JSON, Enum as SQLEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base
from app.constants.agent_skill import ScriptLanguage, ScriptExecutionMode


class AgentSkillScript(Base):
    """
    Skill script stored in the database

    Each row represents one executable file from the skill's scripts/ directory.
    The execution_mode defaults to SANDBOX — IT must explicitly set TRUSTED
    for elevated access.
    """
    __tablename__ = "agent_skill_scripts"

    # =========================================================================
    # PRIMARY KEY & MULTI-TENANCY
    # =========================================================================

    id = Column(String(255), primary_key=True, comment="ULID format: ass-01J...")

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this script belongs to"
    )

    # =========================================================================
    # SKILL REFERENCE
    # =========================================================================

    skill_id = Column(
        String(255),
        ForeignKey("agent_skills.id", ondelete="CASCADE"),
        nullable=False,
        comment="Parent skill this script belongs to"
    )

    # =========================================================================
    # SCRIPT IDENTITY
    # =========================================================================

    filename = Column(
        String(255),
        nullable=False,
        comment="Filename within scripts/ directory (e.g. 'validate_input.py')"
    )

    description = Column(
        Text,
        nullable=True,
        comment="Human-readable description of what this script does"
    )

    # =========================================================================
    # SCRIPT CONTENT
    # =========================================================================

    script_content = Column(
        Text,
        nullable=False,
        comment="Full script source code"
    )

    script_hash = Column(
        String(64),
        nullable=False,
        comment="SHA-256 hash of script_content for change detection and security audit"
    )

    # =========================================================================
    # EXECUTION CONFIGURATION
    # =========================================================================

    language = Column(
        SQLEnum(ScriptLanguage),
        nullable=False,
        default=ScriptLanguage.PYTHON,
        comment="Script language: python, javascript, typescript, bash"
    )

    execution_mode = Column(
        SQLEnum(ScriptExecutionMode),
        nullable=False,
        default=ScriptExecutionMode.SANDBOX,
        comment="Security mode: sandbox (restricted), trusted (full access), disabled (never runs)"
    )

    args_schema = Column(
        JSON,
        nullable=True,
        comment="JSON Schema for the args this script accepts via "
                "skills__run_script(skill, script, args). Advertised in the L2 "
                "manifest so the model knows how to call it; null = free-form args."
    )

    # =========================================================================
    # SECURITY AUDIT
    # =========================================================================

    approved_by = Column(
        String(255),
        nullable=True,
        comment="Admin who approved TRUSTED execution mode"
    )

    approved_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="When TRUSTED mode was approved"
    )

    # =========================================================================
    # AUDIT
    # =========================================================================

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
    # RELATIONSHIPS
    # =========================================================================

    skill = relationship(
        "AgentSkill",
        back_populates="scripts"
    )

    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        # Primary query: "Get all scripts for a skill"
        Index('ix_agent_skill_scripts_tenant_skill', 'tenant_id', 'skill_id'),

        # Unique filename per skill
        UniqueConstraint('skill_id', 'filename',
                         name='uix_agent_skill_scripts_skill_filename'),

        # Security audit: "Find all TRUSTED scripts"
        Index('ix_agent_skill_scripts_tenant_mode', 'tenant_id', 'execution_mode'),

        # Content deduplication and integrity
        Index('ix_agent_skill_scripts_hash', 'script_hash'),

        {
            'comment': 'Skill executable scripts with security modes'
        }
    )

    # =========================================================================
    # SECURITY METHODS
    # =========================================================================

    def is_executable(self) -> bool:
        """Check if this script is allowed to execute"""
        return self.execution_mode != ScriptExecutionMode.DISABLED

    def is_sandboxed(self) -> bool:
        """Check if this script runs in sandbox mode"""
        return self.execution_mode == ScriptExecutionMode.SANDBOX

    def is_trusted(self) -> bool:
        """Check if this script has trusted (elevated) access"""
        return self.execution_mode == ScriptExecutionMode.TRUSTED

    def approve_trusted(self, admin_id: str) -> None:
        """Approve this script for trusted execution"""
        from datetime import datetime, timezone
        self.execution_mode = ScriptExecutionMode.TRUSTED
        self.approved_by = admin_id
        self.approved_at = datetime.now(timezone.utc)

    def revoke_trusted(self) -> None:
        """Revoke trusted access, revert to sandbox"""
        self.execution_mode = ScriptExecutionMode.SANDBOX
        self.approved_by = None
        self.approved_at = None

    def disable(self) -> None:
        """Disable script execution"""
        self.execution_mode = ScriptExecutionMode.DISABLED

    # =========================================================================
    # SERIALIZATION
    # =========================================================================

    def to_dict(self, include_content: bool = False) -> Dict[str, Any]:
        """
        Convert to dictionary for API responses

        Args:
            include_content: Whether to include the full script source.
                           False for listing, True for individual fetch.
        """
        data = {
            "id": self.id,
            "skill_id": self.skill_id,
            "filename": self.filename,
            "description": self.description,
            "language": (
                self.language.value
                if isinstance(self.language, ScriptLanguage)
                else self.language
            ),
            "execution_mode": (
                self.execution_mode.value
                if isinstance(self.execution_mode, ScriptExecutionMode)
                else self.execution_mode
            ),
            "script_hash": self.script_hash,
            "args_schema": self.args_schema,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        if include_content:
            data["script_content"] = self.script_content

        return data

    def to_listing_dict(self) -> Dict[str, Any]:
        """Compact representation for script listings (no content)"""
        return {
            "id": self.id,
            "filename": self.filename,
            "language": (
                self.language.value
                if isinstance(self.language, ScriptLanguage)
                else self.language
            ),
            "execution_mode": (
                self.execution_mode.value
                if isinstance(self.execution_mode, ScriptExecutionMode)
                else self.execution_mode
            ),
        }

    def __repr__(self) -> str:
        return (
            f"<AgentSkillScript(id='{self.id}', skill_id='{self.skill_id}', "
            f"filename='{self.filename}', lang='{self.language}', mode='{self.execution_mode}')>"
        )
