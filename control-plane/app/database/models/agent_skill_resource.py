# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skill Resource Model
Stores the contents of a skill's references/ directory.

These are the third level of progressive disclosure in the Agent Skills spec:
  Level 1: Catalog (name + description from YAML frontmatter)
  Level 2: SKILL.md body (full instructions)
  Level 3: Reference documents (REFERENCE.md, FORMS.md, schemas, etc.)

Resources are loaded individually on demand — never included in catalog
responses. The agent decides which resources to load based on the task
context and hints in the SKILL.md body.

Example files:
  references/REFERENCE.md       - API documentation, field definitions
  references/FORMS.md           - Form templates and validation rules
  references/customer_schema.json - Data schema for input validation
  references/output_template.md  - Expected output format
"""

from typing import Dict, Any

from sqlalchemy import Column, String, Integer, DateTime, Text, Index, UniqueConstraint, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base
from app.constants.agent_skill import SkillResourceType


class AgentSkillResource(Base):
    """
    Skill reference document stored in the database

    Each row represents one file from the skill's references/ directory.
    Content is TEXT (loaded on demand), keeping catalog queries fast.
    """
    __tablename__ = "agent_skill_resources"

    # =========================================================================
    # PRIMARY KEY & MULTI-TENANCY
    # =========================================================================

    id = Column(String(255), primary_key=True, comment="ULID format: asr-01J...")

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this resource belongs to"
    )

    # =========================================================================
    # SKILL REFERENCE
    # =========================================================================

    skill_id = Column(
        String(255),
        ForeignKey("agent_skills.id", ondelete="CASCADE"),
        nullable=False,
        comment="Parent skill this resource belongs to"
    )

    # =========================================================================
    # RESOURCE IDENTITY
    # =========================================================================

    resource_type = Column(
        SQLEnum(SkillResourceType),
        nullable=False,
        default=SkillResourceType.REFERENCE,
        comment="Type classification: reference, form, schema, template, example, asset"
    )

    filename = Column(
        String(255),
        nullable=False,
        comment="Filename within references/ directory (e.g. 'REFERENCE.md', 'customer_schema.json')"
    )

    # =========================================================================
    # CONTENT
    # =========================================================================

    content = Column(
        Text,
        nullable=False,
        comment="File content (loaded on demand for progressive disclosure)"
    )

    content_hash = Column(
        String(64),
        nullable=False,
        comment="SHA-256 hash of content for change detection"
    )

    mime_type = Column(
        String(100),
        nullable=True,
        comment="MIME type (e.g. 'text/markdown', 'application/json', 'text/plain')"
    )

    size_bytes = Column(
        Integer,
        nullable=True,
        comment="Content size in bytes"
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
        back_populates="resources"
    )

    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        # Primary query: "Get all resources for a skill"
        Index('ix_agent_skill_resources_tenant_skill', 'tenant_id', 'skill_id'),

        # Type-filtered query: "Get all schemas for a skill"
        Index('ix_agent_skill_resources_skill_type', 'skill_id', 'resource_type'),

        # Unique filename per skill
        UniqueConstraint('skill_id', 'filename',
                         name='uix_agent_skill_resources_skill_filename'),

        # Content deduplication
        Index('ix_agent_skill_resources_hash', 'content_hash'),

        {
            'comment': 'Skill reference documents (progressive disclosure level 3)'
        }
    )

    # =========================================================================
    # SERIALIZATION
    # =========================================================================

    def to_dict(self, include_content: bool = False) -> Dict[str, Any]:
        """
        Convert to dictionary for API responses

        Args:
            include_content: Whether to include the full content body.
                           False for listing, True for individual fetch.
        """
        data = {
            "id": self.id,
            "skill_id": self.skill_id,
            "resource_type": (
                self.resource_type.value
                if isinstance(self.resource_type, SkillResourceType)
                else self.resource_type
            ),
            "filename": self.filename,
            "content_hash": self.content_hash,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        if include_content:
            data["content"] = self.content

        return data

    def to_listing_dict(self) -> Dict[str, Any]:
        """Compact representation for resource listings (no content)"""
        return {
            "id": self.id,
            "filename": self.filename,
            "resource_type": (
                self.resource_type.value
                if isinstance(self.resource_type, SkillResourceType)
                else self.resource_type
            ),
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
        }

    def __repr__(self) -> str:
        return (
            f"<AgentSkillResource(id='{self.id}', skill_id='{self.skill_id}', "
            f"filename='{self.filename}', type='{self.resource_type}')>"
        )
