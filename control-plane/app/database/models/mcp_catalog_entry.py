# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
MCP Server Catalog Entry Database Model

Stores pre-built MCP server configurations that admins can browse, search,
and import to quickly set up MCP servers with proper OAuth, IdP, and
deployment configuration. Follows the same pattern as LLModel for the
Model Catalog.
"""

from sqlalchemy import (
    Column, String, Boolean, JSON, DateTime, LargeBinary, Text,
    ForeignKey, Index, Integer, UniqueConstraint, text,
)
from sqlalchemy.sql import func
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from app.database import Base


# =============================================================================
# Trust tier constants
#
# Mirror the A2A agent-card model's tier vocabulary so operators
# reading audit logs across A2A + catalog see consistent labels.
# Kept as module constants rather than a SQLAlchemy enum so we can
# add new tiers (e.g. `cve_revoked`) without a schema migration.
# =============================================================================

TRUST_TIER_UNSIGNED = "unsigned"
TRUST_TIER_TENANT_SIGNED = "tenant_signed"
TRUST_TIER_PARTNER_SIGNED = "partner_signed"
TRUST_TIER_REVOKED = "revoked"

VALID_TRUST_TIERS = frozenset({
    TRUST_TIER_UNSIGNED,
    TRUST_TIER_TENANT_SIGNED,
    TRUST_TIER_PARTNER_SIGNED,
    TRUST_TIER_REVOKED,
})


class MCPCatalogEntry(Base):
    """MCP Server Catalog Entry — a template for quickly creating MCP server configurations."""

    __tablename__ = "mcp_catalog_entries"

    # Primary Key
    # Format:
    #   - GLOBAL system entries: "mcp-cat-system-{slug}" (tenant_id NULL,
    #     source="system") — curated by system admins, browseable by every
    #     tenant.
    #   - Per-tenant custom entries: "mcp-cat-{ULID}" (tenant_id set,
    #     source="custom") — managed by the owning tenant's admins.
    id = Column(String(255), primary_key=True)

    # Multi-tenancy
    #
    # NULL means a GLOBAL entry (source="system"): platform-wide, curated by
    # system admins and visible to all tenants in browse. A non-NULL value
    # is a per-tenant custom entry owned by that tenant.
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        comment="Owning tenant; NULL = global system entry (all tenants)",
    )

    # Basic Information
    name = Column(String(200), nullable=False)
    slug = Column(String(200), nullable=False, comment="URL-safe identifier derived from name/filename")
    short_description = Column(Text, nullable=True)
    description = Column(Text, nullable=True, comment="Full markdown description")

    # Icon
    icon_url = Column(String(500), nullable=True, comment="External icon URL")
    icon_data = Column(LargeBinary, nullable=True, comment="Custom uploaded icon binary")
    icon_mime_type = Column(String(100), nullable=True)
    icon_size = Column(Integer, nullable=True)
    icon_hash = Column(String(64), nullable=True)

    # Source & Documentation
    repo_url = Column(String(500), nullable=True, comment="Source repository URL")

    # Runtime Configuration
    runtime = Column(String(20), nullable=False, comment="'remote' or 'containerized'")
    remote_config = Column(JSON, nullable=True, comment="Remote config: {fixedURL?, hostname?, headers?}")
    containerized_config = Column(JSON, nullable=True, comment="Docker config: {image, port, path, args?}")
    env_vars = Column(JSON, nullable=True, default=list, comment="Environment variables: [{key, name, required, sensitive, description}]")

    # MCP Capabilities Preview
    tool_preview = Column(JSON, nullable=True, default=list, comment="Tool previews: [{name, description, params}]")

    # Categorization
    categories = Column(JSON, nullable=True, default=list, comment='Category tags: ["Business", "Communication"]')
    metadata_extra = Column(JSON, nullable=True, comment="Extra metadata: {allow_multiple?, unsupported_tools?}")

    # Authentication Pattern
    auth_pattern = Column(String(50), nullable=False, default="none", comment="'oauth', 'api_key', 'header', 'none'")

    # Source tracking
    source = Column(String(20), nullable=False, default="system", comment="'system' for seeded, 'custom' for admin-created")

    # Status
    enabled = Column(Boolean, default=True, nullable=False)

    # =========================================================================
    # Trust tier + signature
    #
    # Reuses the A2A Ed25519 signing primitive — same canonical JSON,
    # same tenant signing key, same wire format on `signature`. See
    # `app/services/mcp/catalog_signing.py` for the sign/verify
    # functions and its migration for column rationale.
    # =========================================================================
    signature = Column(
        Text, nullable=True,
        comment="Base64-encoded Ed25519 signature over canonical JSON",
    )
    signature_algorithm = Column(
        String(50), nullable=True,
        comment="'ed25519' today; reserved for partner-signed RSA/EC",
    )
    signed_by = Column(
        String(50), nullable=True,
        comment="Admin user ID who signed this entry",
    )
    signed_at = Column(
        DateTime(timezone=True), nullable=True,
        comment="When the signature was applied",
    )
    trust_tier = Column(
        String(20), nullable=True, default=TRUST_TIER_UNSIGNED,
        server_default=TRUST_TIER_UNSIGNED,
        comment="unsigned / tenant_signed / partner_signed / revoked",
    )

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Indexes
    __table_args__ = (
        # Most common query: list enabled entries for a tenant
        Index("ix_mcp_catalog_tenant_enabled", "tenant_id", "enabled"),

        # Name search within tenant
        Index("ix_mcp_catalog_tenant_name", "tenant_id", "name"),

        # Filter by runtime type
        Index("ix_mcp_catalog_tenant_runtime", "tenant_id", "runtime", "enabled"),

        # Unique slug per tenant (prevents duplicate custom entries).
        # NOTE: In Postgres NULLs are distinct, so this constraint does NOT
        # enforce uniqueness across global rows (tenant_id IS NULL) — see the
        # partial unique index below for that.
        UniqueConstraint("tenant_id", "slug", name="uq_mcp_catalog_tenant_slug"),

        # One GLOBAL row per slug. The (tenant_id, slug) unique above can't
        # enforce this because Postgres treats NULL tenant_ids as distinct.
        Index(
            "ix_mcp_catalog_global_slug",
            "slug",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
        ),

        # Filter by source (system vs custom)
        Index("ix_mcp_catalog_tenant_source", "tenant_id", "source"),

        # Audit queries
        Index("ix_mcp_catalog_created_at", "tenant_id", "created_at"),
    )

    def to_dict(self, brief: bool = False) -> Dict[str, Any]:
        """Convert to dictionary for API responses.

        Args:
            brief: If True, return only summary fields (for list views).
        """
        result = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "slug": self.slug,
            "short_description": self.short_description,
            "icon_url": self.icon_url,
            "has_icon": self.icon_data is not None,
            "repo_url": self.repo_url,
            "runtime": self.runtime,
            "categories": self.categories or [],
            "auth_pattern": self.auth_pattern,
            "source": self.source,
            "enabled": self.enabled,
            # Trust + signature surface.
            # `signature` itself is intentionally NOT exposed in
            # responses; clients verify by calling the dedicated
            # verify endpoint or reading the column server-side.
            "trust_tier": self.trust_tier or TRUST_TIER_UNSIGNED,
            "signed_by": self.signed_by,
            "signed_at": self.signed_at.isoformat() if self.signed_at else None,
            "signature_algorithm": self.signature_algorithm,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        if not brief:
            result.update({
                "description": self.description,
                "remote_config": self.remote_config,
                "containerized_config": self.containerized_config,
                "env_vars": self.env_vars or [],
                "tool_preview": self.tool_preview or [],
                "metadata_extra": self.metadata_extra,
            })

        return result
