# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Virtual Server Model
Represents virtual MCP servers that aggregate capabilities from multiple real servers
"""

from typing import Dict, Any, List

from sqlalchemy import Column, String, Boolean, Text, JSON, DateTime, Index, ForeignKey
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.sql import func

from app.database import Base


class VirtualServer(Base):
    """
    Virtual MCP server configuration

    Combines capabilities from multiple MCP servers into a single virtual server.
    Useful for creating custom capability sets for different users or use cases.
    """
    __tablename__ = "virtual_servers"

    # Primary identification
    id = Column(String(255), primary_key=True)
    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this record belongs to"
    )

    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # Capabilities from various servers
    # Format: {
    #   "tools": [{"server_id": "...", "capability_name": "..."}, ...],
    #   "resources": [{"server_id": "...", "capability_name": "..."}, ...],
    #   "prompts": [{"server_id": "...", "capability_name": "..."}, ...]
    # }
    capabilities = Column(JSON, nullable=False, default=lambda: {})

    # Ownership (references AdminUser.id)
    owner = Column(String(50), nullable=False)

    # Status
    enabled = Column(Boolean, nullable=False, default=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        # Multi-tenant queries - most common access pattern
        Index('ix_virtual_servers_tenant_enabled', 'tenant_id', 'enabled'),

        # Virtual server lookup by name within tenant
        Index('ix_virtual_servers_tenant_name', 'tenant_id', 'name'),

        # Owner queries - list all virtual servers for a user within tenant
        Index('ix_virtual_servers_tenant_owner', 'tenant_id', 'owner'),

        # Owner + enabled queries - "Get my active virtual servers"
        Index('ix_virtual_servers_tenant_owner_enabled', 'tenant_id', 'owner', 'enabled'),

        # Audit and history queries
        Index('ix_virtual_servers_created_at', 'tenant_id', 'created_at'),

        {
            'comment': 'Virtual servers aggregating capabilities from multiple MCP servers'
        }
    )

    def __repr__(self) -> str:
        return f"<VirtualServer(id='{self.id}', name='{self.name}', owner='{self.owner}')>"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities or {},
            "owner": self.owner,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }

    def add_tool(self, server_id: str, capability_name: str) -> None:
        """Add a tool capability from a server"""
        if not self.capabilities:
            self.capabilities = {"tools": [], "resources": [], "prompts": []}

        if "tools" not in self.capabilities:
            self.capabilities["tools"] = []

        # Avoid duplicates
        tool_ref = {"server_id": server_id, "capability_name": capability_name}
        if tool_ref not in self.capabilities["tools"]:
            self.capabilities["tools"].append(tool_ref)

    def add_resource(self, server_id: str, capability_name: str) -> None:
        """Add a resource capability from a server"""
        if not self.capabilities:
            self.capabilities = {"tools": [], "resources": [], "prompts": []}

        if "resources" not in self.capabilities:
            self.capabilities["resources"] = []

        resource_ref = {"server_id": server_id, "capability_name": capability_name}
        if resource_ref not in self.capabilities["resources"]:
            self.capabilities["resources"].append(resource_ref)

    def add_prompt(self, server_id: str, capability_name: str) -> None:
        """Add a prompt capability from a server"""
        if not self.capabilities:
            self.capabilities = {"tools": [], "resources": [], "prompts": []}

        if "prompts" not in self.capabilities:
            self.capabilities["prompts"] = []

        prompt_ref = {"server_id": server_id, "capability_name": capability_name}
        if prompt_ref not in self.capabilities["prompts"]:
            self.capabilities["prompts"].append(prompt_ref)

    def get_referenced_servers(self) -> List[str]:
        """Get list of all server IDs referenced by this virtual server"""
        server_ids = set()

        if self.capabilities:
            for cap_type in ["tools", "resources", "prompts"]:
                for cap in self.capabilities.get(cap_type, []):
                    server_ids.add(cap["server_id"])

        return list(server_ids)

    def remove_server_references(self, server_id: str) -> int:
        """Remove all capabilities from a specific server. Returns count removed."""
        if not self.capabilities:
            return 0

        removed_count = 0

        for cap_type in ["tools", "resources", "prompts"]:
            if cap_type in self.capabilities:
                original_len = len(self.capabilities[cap_type])
                self.capabilities[cap_type] = [
                    cap for cap in self.capabilities[cap_type]
                    if cap["server_id"] != server_id
                ]
                removed_count += original_len - len(self.capabilities[cap_type])

        # The name mappings the core reads, then tell the ORM: plain JSON, so an
        # in-place edit alone is never written.
        mapping = self.capabilities.get("source_mapping") or {}
        for name in [n for n, source in mapping.items() if source == server_id]:
            mapping.pop(name, None)
            (self.capabilities.get("reverse_name_mapping") or {}).pop(name, None)
        flag_modified(self, "capabilities")

        return removed_count