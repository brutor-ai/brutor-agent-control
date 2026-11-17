# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# EndUserGroup — first-class user-collection entity

Lets operators bind a *group of users* to a Resource Group (and to
guardrails, cache configs, etc. via the same RG path), instead of
binding every user individually. Administrators create the groups
(`source = 'manual'`, the only source in this release; SAML single
sign-on and SCIM provisioning, which created groups of their own, are
not in it). Resource Groups have a M2M to EndUserGroup, so binding a
resource group to a user-group extends to every user *currently* in
that group.

The columns `source_external_id` and `idp_server_id` were written by
those two; nothing writes them now, and they stay in the schema until
the migrations are squashed.

## Why not just reuse `ResourceGroupMembership`

`ResourceGroupMembership` is a user↔RG row; it has no name, no
provenance, no aggregate semantics. It can't represent "the Okta
engineering group" as a thing, only "user U is in RG R". The
operator-facing concept is the group, not each row of the M2M, so
the group needs to be a real entity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, TYPE_CHECKING

from sqlalchemy import (
    func,
    text,
    Column, Integer, String, Boolean, Text, DateTime, ForeignKey, Index,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship, validates, Mapped
from sqlalchemy.sql import func

from app.database import Base


# ----------------------------------------------------------------------
# M2M association tables
# ----------------------------------------------------------------------

# EndUserGroup ↔ EndUser. A user can be in any number
# of groups; a group can hold any number of users. ON DELETE CASCADE
# on both sides so deleting either end cleans up dangling rows.
from sqlalchemy import Table

if TYPE_CHECKING:
    # Relationship annotations only (`Mapped[List["X"]]`): SQLAlchemy resolves
    # the names through its registry, and importing them here at run time would
    # be a cycle.
    from app.database.models.end_user import EndUser
    from app.database.models.resource_group import ResourceGroup

end_user_group_members = Table(
    'end_user_group_members',
    Base.metadata,
    Column(
        'end_user_group_id',
        String(64),
        ForeignKey('end_user_groups.id', ondelete='CASCADE'),
        primary_key=True,
    ),
    Column(
        # `EndUser.id` is Integer-autoincrement, not a ULID — see
        # end_user.py:41. Match its type so the FK lines up.
        'end_user_id',
        Integer,
        ForeignKey('end_users.id', ondelete='CASCADE'),
        primary_key=True,
    ),
    Column(
        'created_at',
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    ),
    Column(
        'source',
        String(16),
        nullable=False,
        default='manual',
        comment=(
            "'manual' (admin added), 'saml' (JIT reconciler), or "
            "'scim' (SCIM PATCH op). Lets the reconciler tell apart "
            "rows it should manage from rows it must leave alone."
        ),
        server_default="manual",
    ),
    Index('ix_end_user_group_members_user', 'end_user_id'),
    Index('ix_end_user_group_members_group', 'end_user_group_id'),
)


# Resource Group ↔ EndUserGroup. Binds a *group* of users to a
# Resource Group; every current member of the EndUserGroup inherits
# the binding. Independent of `resource_group_memberships` (the
# direct user↔RG M2M), which stays as the escape hatch for manual
# one-off assignments.
resource_group_end_user_groups = Table(
    'resource_group_end_user_groups',
    Base.metadata,
    Column(
        'resource_group_id',
        String(255),
        ForeignKey('resource_groups.id', ondelete='CASCADE'),
        primary_key=True,
    ),
    Column(
        'end_user_group_id',
        String(64),
        ForeignKey('end_user_groups.id', ondelete='CASCADE'),
        primary_key=True,
    ),
    Column(
        'created_at',
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    ),
    Column(
        'created_by',
        String(255),
        nullable=True,
        comment="admin_id who created the binding (audit attribution).",
    ),
    Index('ix_rg_end_user_groups_group', 'end_user_group_id'),
)


# ----------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------


VALID_SOURCES = frozenset({'manual'})


class EndUserGroup(Base):
    """A named collection of `EndUser` rows.

    Group membership is M2M via `end_user_group_members`. A group can
    in turn be bound to Resource Groups via
    `resource_group_end_user_groups` — granting every current member
    the RG's downstream resources.
    """

    __tablename__ = "end_user_groups"

    id = Column(
        String(64),
        primary_key=True,
        comment="ULID, prefixed 'eug-'",
    )
    tenant_id = Column(
        String(255),
        ForeignKey('tenants.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )

    name = Column(
        String(255),
        nullable=False,
        comment=(
            "Machine-friendly name (slug). Used by the SAML/SCIM "
            "reconciler as the lookup key after source_external_id "
            "fails. UNIQUE per tenant."
        ),
    )
    display_name = Column(
        String(255),
        nullable=True,
        comment="Operator-facing label shown in the UI.",
    )
    description = Column(Text, nullable=True)

    source = Column(
        String(16),
        nullable=False,
        default='manual',
        comment="'manual' | 'saml' | 'scim' — how the group was created.",
        server_default="manual",
    )
    source_external_id = Column(
        String(255),
        nullable=True,
        comment=(
            "The IdP-side group name (for SAML) or SCIM externalId "
            "(for SCIM). NULL for manual groups. Combined with "
            "idp_server_id, forms the stable re-find key for the "
            "reconciler so re-pushes don't duplicate rows."
        ),
    )
    idp_server_id = Column(
        String(255),
        ForeignKey('idp_servers.id', ondelete='SET NULL'),
        nullable=True,
        comment=(
            "Origin IdP for SAML/SCIM-sourced groups. NULL for manual. "
            "ON DELETE SET NULL — deleting an IdP doesn't kill the "
            "downstream groups (they convert to orphan manual groups, "
            "which the operator can re-attach or delete)."
        ),
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # ---------------------- relationships ----------------------

    members: Mapped[List["EndUser"]] = relationship(
        "EndUser",
        secondary=end_user_group_members,
        back_populates="user_groups",
        lazy="selectin",
    )

    resource_groups: Mapped[List["ResourceGroup"]] = relationship(
        "ResourceGroup",
        secondary=resource_group_end_user_groups,
        back_populates="end_user_groups",
        lazy="selectin",
    )

    # No explicit relationship to IdPServer — the FK is enough.

    # ---------------------- constraints ----------------------

    __table_args__ = (
        # Group names are unique within a tenant.
        UniqueConstraint(
            'tenant_id', 'name',
            name='uq_end_user_groups_tenant_name',
        ),
        # The external key of a group an identity provider created
        # (none in this release). The partial nature (WHERE
        # source_external_id IS NOT NULL) is in the migration.
        UniqueConstraint(
            'tenant_id', 'idp_server_id', 'source_external_id',
            name='uq_end_user_groups_tenant_idp_external',
        ),
        Index('ix_end_user_groups_tenant', 'tenant_id'),
        Index('ix_end_user_groups_idp', 'idp_server_id'),
        Index('ix_end_user_groups_source', 'source'),
    )

    # ---------------------- validators ----------------------

    @validates('source')
    def _validate_source(self, _key: str, value: str) -> str:
        if value not in VALID_SOURCES:
            raise ValueError(
                f"EndUserGroup.source must be one of {sorted(VALID_SOURCES)}; "
                f"got {value!r}"
            )
        return value

    # ---------------------- helpers ----------------------

    @property
    def member_count(self) -> int:
        """Cheap when `members` is selectin-loaded (default)."""
        return len(self.members) if self.members is not None else 0

    def to_dict(
        self,
        *,
        include_members: bool = False,
        include_resource_groups: bool = False,
    ) -> Dict[str, Any]:
        """Serialise for API responses.

        `include_members` / `include_resource_groups` flags control
        whether we expand the relationships — listing endpoints leave
        them off to keep the payload light, detail endpoints turn
        them on.
        """
        d: Dict[str, Any] = {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "source": self.source,
            "source_external_id": self.source_external_id,
            "idp_server_id": self.idp_server_id,
            "member_count": self.member_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_members:
            d["members"] = [
                {
                    "id": u.id,
                    "username": u.username,
                    "email": u.email,
                }
                for u in (self.members or [])
            ]
        if include_resource_groups:
            d["resource_groups"] = [
                {
                    "id": rg.id,
                    "name": rg.name,
                    "display_name": rg.display_name,
                }
                for rg in (self.resource_groups or [])
            ]
        return d

    def __repr__(self) -> str:
        return (
            f"<EndUserGroup(id={self.id!r}, tenant={self.tenant_id!r}, "
            f"name={self.name!r}, source={self.source!r}, "
            f"members={self.member_count})>"
        )


__all__ = [
    "EndUserGroup",
    "end_user_group_members",
    "resource_group_end_user_groups",
    "VALID_SOURCES",
]
