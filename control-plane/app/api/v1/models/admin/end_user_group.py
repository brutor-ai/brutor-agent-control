# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# EndUserGroup admin API request/response models

Pydantic schemas that wrap the EndUserGroup DB model for the admin
console endpoints under `/api/v1/admin/end-user-groups/*` and the
Resource-Group → Members extension that lets operators attach groups
(not just individual users).

Design choices:
  * `source` and `source_external_id` are READ-ONLY in the API. Every
    group is created by an administrator (`source = 'manual'`, the only
    source in this release).
  * Membership mutation lives on its own request models
    (`AddMembersRequest`, `SetMembersRequest`) so the partial update
    path doesn't conflate "rename" with "rewrite the member roster".
  * Listing endpoints return `EndUserGroupSummary` (no member list)
    to keep payload size sane on tenants with thousands of groups.
    Detail endpoints return `EndUserGroupDetail`.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, ConfigDict


class EndUserGroupCreate(BaseModel):
    """Admin-console payload for creating a manual EndUserGroup.

    `source` is always 'manual' and is NOT accepted in the request
    body. Same for `source_external_id` and `idp_server_id`.
    """

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description=(
            "Machine-friendly slug. Unique per tenant. Used as the "
            "fallback lookup key when source_external_id is null."
        ),
    )
    display_name: Optional[str] = Field(
        None,
        max_length=255,
        description="Operator-facing label shown in the UI.",
    )
    description: Optional[str] = Field(
        None,
        description="Free-form description; surfaces in the group list.",
    )
    # Optional initial roster. The endpoint also accepts an empty list
    # so groups can be created first and populated later.
    initial_member_ids: List[int] = Field(
        default_factory=list,
        description=(
            "Optional list of EndUser.id values to add as members on "
            "create. EndUser.id is Integer-autoincrement, not a ULID."
        ),
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        # Reconciler relies on a stable lookup key; whitespace-only
        # names would collide ambiguously with manual-vs-IdP groups.
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty or whitespace-only")
        return v


class EndUserGroupUpdate(BaseModel):
    """Partial update — only the operator-controllable fields. Source
    provenance fields (source/source_external_id/idp_server_id) are
    intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    display_name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty or whitespace-only")
        return v


# ---------------------------------------------------------------------------
# Membership mutation
# ---------------------------------------------------------------------------


class AddMembersRequest(BaseModel):
    """Idempotent additive membership change.

    Members already in the group are silently skipped — the endpoint
    returns the resulting roster so the caller can diff."""

    end_user_ids: List[int] = Field(
        ...,
        min_length=1,
        description="EndUser ids to ensure are members of this group.",
    )


class RemoveMembersRequest(BaseModel):
    """Idempotent subtractive membership change."""

    end_user_ids: List[int] = Field(
        ...,
        min_length=1,
        description="EndUser ids to ensure are NOT members.",
    )


class SetMembersRequest(BaseModel):
    """Replaces the entire member roster. Use for the UI's
    multi-select-then-save flow where the client doesn't track a diff.

    Empty list is allowed — it clears the roster (e.g. resetting a
    group before re-seeding from a different IdP)."""

    model_config = ConfigDict(extra="forbid")

    end_user_ids: List[int] = Field(
        ...,
        description=(
            "Authoritative member roster after the call. Members not "
            "in this list are removed; members in this list are added."
        ),
    )


# ---------------------------------------------------------------------------
# Response payloads
# ---------------------------------------------------------------------------


class EndUserGroupMember(BaseModel):
    """Compact member projection — what the detail endpoint inlines."""

    id: int
    username: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    is_active: bool = True


class EndUserGroupResourceGroupRef(BaseModel):
    """Compact RG projection for the detail endpoint."""

    id: str
    name: str
    display_name: Optional[str] = None


class EndUserGroupSummary(BaseModel):
    """List-endpoint payload — no members, no resource groups. Keeps
    payload size bounded on tenants with thousands of groups."""

    id: str
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    source: str  # 'manual'
    source_external_id: Optional[str] = None
    idp_server_id: Optional[str] = None
    member_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class EndUserGroupDetail(EndUserGroupSummary):
    """Detail-endpoint payload — inlines members and bound RGs.

    The admin UI uses this on the group-detail page to render the
    members table and the "bound to Resource Groups" sidebar."""

    members: List[EndUserGroupMember] = Field(default_factory=list)
    resource_groups: List[EndUserGroupResourceGroupRef] = Field(
        default_factory=list,
    )


# ---------------------------------------------------------------------------
# Resource Group → EndUserGroup binding
# ---------------------------------------------------------------------------


class ResourceGroupBindEndUserGroupRequest(BaseModel):
    """RG-side: bind a list of EndUserGroups to a Resource Group.
    Used by `POST /admin/resource-groups/{rg_id}/end-user-groups`."""

    end_user_group_ids: List[str] = Field(
        ...,
        min_length=1,
        description="EndUserGroup ids to attach.",
    )


class ResourceGroupUnbindEndUserGroupRequest(BaseModel):
    """RG-side: detach a list of EndUserGroups from a Resource Group."""

    end_user_group_ids: List[str] = Field(
        ...,
        min_length=1,
        description="EndUserGroup ids to detach.",
    )


class EndUserGroupBindResourceGroupsRequest(BaseModel):
    """UG-side: bind THIS EndUserGroup to a list of Resource Groups.
    Used by `POST /admin/end-user-groups/{ug_id}/resource-groups`."""

    resource_group_ids: List[str] = Field(
        ...,
        min_length=1,
        description="Resource Group ids to attach this user group to.",
    )


class EndUserGroupUnbindResourceGroupsRequest(BaseModel):
    """UG-side: detach THIS EndUserGroup from a list of Resource Groups."""

    resource_group_ids: List[str] = Field(
        ...,
        min_length=1,
        description="Resource Group ids to detach.",
    )


__all__ = [
    "EndUserGroupCreate",
    "EndUserGroupUpdate",
    "AddMembersRequest",
    "RemoveMembersRequest",
    "SetMembersRequest",
    "EndUserGroupMember",
    "EndUserGroupResourceGroupRef",
    "EndUserGroupSummary",
    "EndUserGroupDetail",
    "ResourceGroupBindEndUserGroupRequest",
    "ResourceGroupUnbindEndUserGroupRequest",
    "EndUserGroupBindResourceGroupsRequest",
    "EndUserGroupUnbindResourceGroupsRequest",
]
