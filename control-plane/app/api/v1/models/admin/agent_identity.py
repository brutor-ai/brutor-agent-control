# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Pydantic models for the Agent Identity admin API.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict
from app.api.v1.models.admin.resource_fields import ResourceAuthorshipFields


# Declare the vocabularies the database already enforces.
#
# `AgentIdentity._v_source` / `_v_enforcement_mode` reject anything outside
# these sets on write, and the response model documented the real union in a
# COMMENT (`# enforce | dry_run`) while typing the field `str`. A comment is
# not a contract: the OpenAPI schema said "string", the generated TypeScript
# said `string`, and the Admin Console re-declared the union by hand.
#
# Third occurrence of this shape (after semantic-policy `action`/`mode`), so
# it is worth stating as a rule: if a validator constrains a column, the
# response model should say so — otherwise the constraint stops at the
# database and every consumer re-invents it without verification.
#
# Mirrors app/database/models/agent_identity.py::AGENT_SOURCES /
# ENFORCEMENT_MODES / AGENT_STATUSES.
AgentSource = Literal["manual", "idp", "discovered"]
AgentEnforcementMode = Literal["enforce", "dry_run"]
AgentStatus = Literal["active", "suspended"]
#: Mirrors app.constants.agent_implementation.AgentImplTrust.
AgentReleaseTrust = Literal["verified", "declared"]

_IMPL_NAME_DESC = (
    "The implementation name this identity's software must declare "
    "(lower-cased, [a-z0-9._-]{1,64}); null = not declared. Describes; never "
    "decides access."
)
_APPROVED_DESC = (
    "The release versions an approver accepts: exact versions or a "
    "prefix ending in '.*' (e.g. '1.4.*'). Stored sorted and de-duplicated. "
    "Empty = observe only. Part of the contract closure of every AI System "
    "the identity is a member of."
)
_REQUIRE_DESC = "A run that carries no implementation name/version is a finding."


# ── Agent identities ────────────────────────────────────────────────────
class AgentIdentityCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    owner_user_id: Optional[int] = None
    owner_label: Optional[str] = None
    idp_server_id: Optional[str] = None
    idp_subject: Optional[str] = None
    source: AgentSource = "manual"
    expires_at: Optional[str] = None  # ISO-8601
    default_deny: bool = True
    enforcement_mode: AgentEnforcementMode = "enforce"
    implementation_name: Optional[str] = Field(None, description=_IMPL_NAME_DESC)
    approved_versions: List[str] = Field(default_factory=list, description=_APPROVED_DESC)
    require_release: bool = Field(False, description=_REQUIRE_DESC)


class AgentIdentityUpdateRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    description: Optional[str] = None
    owner_user_id: Optional[int] = None
    owner_label: Optional[str] = None
    idp_server_id: Optional[str] = None
    idp_subject: Optional[str] = None
    enabled: Optional[bool] = None
    status: Optional[AgentStatus] = None
    expires_at: Optional[str] = None
    default_deny: Optional[bool] = None
    enforcement_mode: Optional[AgentEnforcementMode] = None
    implementation_name: Optional[str] = Field(None, description=_IMPL_NAME_DESC)
    approved_versions: Optional[List[str]] = Field(None, description=_APPROVED_DESC)
    require_release: Optional[bool] = Field(None, description=_REQUIRE_DESC)


class AgentIdentityResponse(ResourceAuthorshipFields):
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    owner_user_id: Optional[int] = None
    owner_label: Optional[str] = None
    idp_server_id: Optional[str] = None
    idp_subject: Optional[str] = None
    source: AgentSource
    enabled: bool
    revoked_at: Optional[str] = None
    expires_at: Optional[str] = None
    status: AgentStatus
    default_deny: bool
    enforcement_mode: AgentEnforcementMode = "enforce"
    implementation_name: Optional[str] = Field(None, description=_IMPL_NAME_DESC)
    approved_versions: List[str] = Field(default_factory=list, description=_APPROVED_DESC)
    require_release: bool = Field(False, description=_REQUIRE_DESC)
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AgentIdentityListResponse(BaseModel):
    agents: List[AgentIdentityResponse]
    total: int


# ── Releases ───────────────────────────────────────────────────────────
class AgentReleaseResponse(BaseModel):
    """One observed release of the identity's software. Written by the core's
    run finaliser; `approved` and `name_matches` are derived at read time."""

    id: str
    agent_id: str
    name: str
    version: str
    build: Optional[str] = Field(None, description="git SHA or image digest; null = none declared")
    label: str = Field(..., description="name@version, plus #build when there is one")
    trust: AgentReleaseTrust
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    run_count: int
    fingerprints: List[str] = Field(
        default_factory=list,
        description="Distinct implementation fingerprints seen for this release (at most 16)",
    )
    fingerprint_count: int
    approved: Optional[bool] = Field(
        None,
        description="Derived from approved_versions; null when the identity approves nothing (observe only)",
    )
    name_matches: Optional[bool] = Field(
        None, description="Whether the release's name is implementation_name; null when none is declared",
    )
    current: bool = Field(False, description="The release seen last")


class AgentReleaseListResponse(BaseModel):
    agent_id: str
    implementation_name: Optional[str] = None
    approved_versions: List[str] = Field(default_factory=list)
    require_release: bool = False
    current_release_id: Optional[str] = None
    releases: List[AgentReleaseResponse]
    total: int


# ── Grants ──────────────────────────────────────────────────────────────
class AgentGrantCreateRequest(BaseModel):
    action_type: str = Field(..., description="llm_call | mcp_tool | a2a_call | skill_exec")
    target: str = "*"
    effect: str = Field(..., description="allow | deny | approval_required")
    constraints: Optional[Dict[str, Any]] = None
    expires_at: Optional[str] = None


class AgentGrantResponse(BaseModel):
    id: str
    tenant_id: str
    agent_id: str
    action_type: str
    target: str
    effect: str
    constraints: Optional[Dict[str, Any]] = None
    enabled: bool
    expires_at: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AgentGrantListResponse(BaseModel):
    grants: List[AgentGrantResponse]
    total: int


# ── Memberships ─────────────────────────────────────────────────────────
class AgentMembershipCreateRequest(BaseModel):
    group_id: str
    role: Optional[str] = "member"


class AgentMembershipResponse(BaseModel):
    id: int
    tenant_id: str
    agent_id: str
    group_id: str
    role: Optional[str] = None
    added_by: Optional[str] = None
    joined_at: Optional[str] = None


class AgentMembershipListResponse(BaseModel):
    memberships: List[AgentMembershipResponse]
    total: int
