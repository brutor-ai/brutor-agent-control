# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Search Provider Configuration model.
Stores per-tenant search provider settings (Brave, Google, SearXNG).
"""

from sqlalchemy import Column, String, Integer, Boolean, Text, DateTime, JSON, ForeignKey, Index
from sqlalchemy.sql import func

from app.database import Base


class SearchProviderConfig(Base):
    """
    Per-tenant search provider configuration.

    Used by the Web Search system MCP server to perform web searches
    via Brave Search, Google Custom Search, or SearXNG.
    """
    __tablename__ = "search_provider_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        comment="Tenant this config belongs to (one per tenant)"
    )

    # Provider selection
    provider = Column(
        String(50),
        nullable=False,
        default="brave",
        comment="Search provider: brave, google, searxng"
    )
    enabled = Column(Boolean, nullable=False, default=True)

    # Provider credentials
    api_key_encrypted = Column(Text, nullable=True, comment="Encrypted API key for the provider")
    google_cx_id = Column(String(255), nullable=True, comment="Google Custom Search Engine ID")
    searxng_base_url = Column(String(500), nullable=True, comment="SearXNG instance base URL")

    # Search settings
    max_results_per_query = Column(Integer, nullable=False, default=10)
    safe_search = Column(Boolean, nullable=False, default=True)
    daily_query_limit = Column(Integer, nullable=True, comment="Max queries per day (null = unlimited)")

    # Domain filtering
    allowed_domains = Column(JSON, nullable=True, comment="Whitelist of allowed domains")
    blocked_domains = Column(JSON, nullable=True, comment="Blacklist of blocked domains")

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index('ix_search_provider_configs_tenant', 'tenant_id'),
        {'comment': 'Per-tenant search provider configuration'}
    )

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "provider": self.provider,
            "enabled": self.enabled,
            "google_cx_id": self.google_cx_id,
            "searxng_base_url": self.searxng_base_url,
            "max_results_per_query": self.max_results_per_query,
            "safe_search": self.safe_search,
            "daily_query_limit": self.daily_query_limit,
            "allowed_domains": self.allowed_domains or [],
            "blocked_domains": self.blocked_domains or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
