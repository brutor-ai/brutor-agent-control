# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skill Asset Model
Stores static resources from a skill's assets/ directory.

Assets are non-executable static files that skills reference during execution:
- Templates (document templates, configuration templates)
- Images (diagrams, examples, icons)
- Data files (lookup tables, schemas, CSV data)

Unlike scripts (which execute) and resources (which are reference docs for the
agent's context window), assets are binary/text blobs served as-is to the
skill runtime. Content is stored as base64 for binary files and plain text
for text-based assets.

Example files:
  assets/invoice_template.docx       - Document template
  assets/architecture_diagram.png    - Example diagram
  assets/country_codes.csv           - Lookup table
  assets/config_schema.json          - Configuration schema
  assets/prompt_template.txt         - Prompt template
"""

from typing import Dict, Any

from sqlalchemy import Column, String, Integer, DateTime, Text, Index, UniqueConstraint, ForeignKey, JSON, Enum as SQLEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base
from app.constants.agent_skill import AssetType


class AgentSkillAsset(Base):
    """
    Skill static asset stored in the database

    Each row represents one file from the skill's assets/ directory.
    Content is TEXT (plain text or base64 for binary), loaded on demand.
    """
    __tablename__ = "agent_skill_assets"

    # =========================================================================
    # PRIMARY KEY & MULTI-TENANCY
    # =========================================================================

    id = Column(String(255), primary_key=True, comment="ULID format: asa-01J...")

    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this asset belongs to"
    )

    # =========================================================================
    # SKILL REFERENCE
    # =========================================================================

    skill_id = Column(
        String(255),
        ForeignKey("agent_skills.id", ondelete="CASCADE"),
        nullable=False,
        comment="Parent skill this asset belongs to"
    )

    # =========================================================================
    # ASSET IDENTITY
    # =========================================================================

    asset_type = Column(
        SQLEnum(AssetType),
        nullable=False,
        default=AssetType.DATA,
        comment="Type classification: template, image, data"
    )

    filename = Column(
        String(255),
        nullable=False,
        comment="Filename within assets/ directory (e.g. 'invoice_template.docx')"
    )

    description = Column(
        Text,
        nullable=True,
        comment="Human-readable description of this asset"
    )

    # =========================================================================
    # CONTENT
    # =========================================================================

    content = Column(
        Text,
        nullable=False,
        comment="File content (plain text or base64 for binary files)"
    )

    content_hash = Column(
        String(64),
        nullable=False,
        comment="SHA-256 hash of content for change detection"
    )

    mime_type = Column(
        String(100),
        nullable=True,
        comment="MIME type (e.g. 'text/csv', 'image/png', 'application/json')"
    )

    size_bytes = Column(
        Integer,
        nullable=True,
        comment="Content size in bytes"
    )

    # =========================================================================
    # ENCODING
    # =========================================================================

    encoding = Column(
        String(20),
        nullable=False,
        default="utf-8",
        comment="Content encoding: 'utf-8' for text, 'base64' for binary"
    )

    vars_schema = Column(
        JSON,
        nullable=True,
        comment="JSON Schema for the variables this template accepts via "
                "skills__render_template(skill, template, vars). Advertised in the L2 "
                "manifest (templates[].vars_schema); null = free-form / not a template."
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
        back_populates="assets"
    )

    # =========================================================================
    # INDEXES
    # =========================================================================

    __table_args__ = (
        # Primary query: "Get all assets for a skill"
        Index('ix_agent_skill_assets_tenant_skill', 'tenant_id', 'skill_id'),

        # Type-filtered query: "Get all templates for a skill"
        Index('ix_agent_skill_assets_skill_type', 'skill_id', 'asset_type'),

        # Unique filename per skill
        UniqueConstraint('skill_id', 'filename',
                         name='uix_agent_skill_assets_skill_filename'),

        # Content deduplication
        Index('ix_agent_skill_assets_hash', 'content_hash'),

        {
            'comment': 'Skill static assets (templates, images, data files)'
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
            "asset_type": (
                self.asset_type.value
                if isinstance(self.asset_type, AssetType)
                else self.asset_type
            ),
            "filename": self.filename,
            "description": self.description,
            "content_hash": self.content_hash,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "encoding": self.encoding,
            "vars_schema": self.vars_schema,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        if include_content:
            data["content"] = self.content

        return data

    def to_listing_dict(self) -> Dict[str, Any]:
        """Compact representation for asset listings (no content)"""
        return {
            "id": self.id,
            "filename": self.filename,
            "description": self.description,
            "asset_type": (
                self.asset_type.value
                if isinstance(self.asset_type, AssetType)
                else self.asset_type
            ),
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "encoding": self.encoding,
        }

    def __repr__(self) -> str:
        return (
            f"<AgentSkillAsset(id='{self.id}', skill_id='{self.skill_id}', "
            f"filename='{self.filename}', type='{self.asset_type}')>"
        )
