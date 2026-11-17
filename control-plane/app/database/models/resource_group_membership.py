# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Resource Group Membership Model
Manages the many-to-many relationship between end users and resource groups
"""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from typing import Dict, Any

from app.database import Base


class ResourceGroupMembership(Base):
    """
    User Group Membership model.

    Represents the membership of an end user in a resource group.
    This is the association table for the many-to-many relationship
    between EndUser and ResourceGroup.
    """
    __tablename__ = "resource_group_memberships"

    # Primary Key
    id = Column(Integer, primary_key=True, autoincrement=True)
    # Multi-tenancy
    tenant_id = Column(
        String(255),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        comment="Tenant this record belongs to"
    )

    # Foreign Keys
    user_id = Column(
        Integer,
        ForeignKey("end_users.id", ondelete="CASCADE"),
        nullable=False,
        comment="End user ID"
    )

    group_id = Column(
        String(255),
        ForeignKey("resource_groups.id", ondelete="CASCADE"),
        nullable=False,
        comment="Resource group ID"
    )

    # Membership Role (optional)
    role = Column(
        String(50),
        nullable=True,
        comment="Optional role within the resource group: member, admin, etc."
    )

    # Audit Fields
    added_by = Column(
        String(50),
        nullable=True,
        comment="AdminUser.id who added this membership"
    )

    # Timestamps
    joined_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="When the user joined the group"
    )

    # Relationships
    user = relationship(
        "EndUser",
        back_populates="group_memberships"
    )

    group = relationship(
        "ResourceGroup",
        back_populates="members"
    )

    # Table Constraints and Indexes
    __table_args__ = (
        # Unique constraint must include tenant_id
        # A user can be in the same group across different tenants
        UniqueConstraint('tenant_id', 'user_id', 'group_id',
                        name='uix_resource_group_memberships_tenant_resouce_group'),

        # Forward lookup - "Get all groups for a user"
        Index('ix_resource_group_memberships_tenant_user', 'tenant_id', 'user_id'),

        # Reverse lookup - "Get all members of a group"
        Index('ix_resource_group_memberships_tenant_group', 'tenant_id', 'group_id'),

        # Membership by role - "Get all admins in a group"
        Index('ix_resource_group_memberships_tenant_group_role', 'tenant_id', 'group_id', 'role'),

        # Recent memberships - "Show recently added members"
        Index('ix_resource_group_memberships_tenant_joined', 'tenant_id', 'joined_at'),

        {
            'comment': 'Membership relationship between end users and resource groups'
        }
    )

    def __repr__(self) -> str:
        return f"<ResourceGroupMembership(user_id={self.user_id}, group_id='{self.group_id}')>"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            'id': self.id,
            'user_id': str(self.user_id),
            'group_id': self.group_id,
            'role': self.role,
            'added_by': self.added_by,
            'joined_at': self.joined_at.isoformat() if self.joined_at else None
        }