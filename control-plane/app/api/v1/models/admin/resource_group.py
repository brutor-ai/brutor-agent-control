# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Resource Group API Models 
Pydantic models for resource group management with flexible hierarchy
"""

from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator, validator, ConfigDict

from app.api.v1.models.admin.mcp_limits_document import McpLimits
from app.api.v1.models.admin.governance_documents import (
    A2AGovernance,
    LlmGovernance,
    McpGovernance,
    SkillGovernance,
)
from typing import Optional, List, Dict, Any, Literal

# EU AI Act risk tiers. Closed enum at the API
# boundary: the tagging engine emits `eu_ai_act.<tier>` and the
# dashboard groups by these exact tier names, so an unknown value
# would land a tag the dashboard couldn't account for.
EuAiActRiskTier = Literal["minimal", "limited", "high", "unacceptable"]
# Governance lifecycle (AI-System attributes on the RG). Mirrors
# LIFECYCLE_STAGES in app/database/models/ai_asset.py.
LifecycleStage = Literal["proposed", "approved", "active", "deprecated", "retired"]
# Autonomy levels, ordered most- to least-permissive, matching AUTONOMY_RANK
# in app/database/models/ai_system_contract.py and `Autonomy` in
# brutor-gateway-core/src/services/autonomy.rs.
AutonomyLevel = Literal["autonomous", "approval_required", "restricted", "suspended"]
from enum import Enum

from app.api.v1.models.admin.llm_model import LLMModelHealthStatusResponse
from app.api.v1.models.admin.resource_fields import ResourceVersionField
from app.api.v1.models.admin.server_config import MCPServerInfo
from app.services import release_scope

# ============================================================================
# ENUM DEFINITIONS
# ============================================================================


# Estate rollup exclusion. Validated here so the reasons stay a closed
# vocabulary the UI, the report and the digest can render.
ROLLUP_EXCLUSION_REASONS = (
    "retired",
    "deprecated",
    "no_evidence_acknowledged",
    "scheduled_offline",
)


# How the allow-lists of several levels apply to one outbound A2A call.
A2A_ALLOW_LISTS_RULE = (
    "An address must match every level: an outbound A2A call is allowed only when its "
    "address matches the allow-list of each level that states one (the agent card's own "
    "list included). The lists are not merged."
)

A2A_GOVERNANCE_DESCRIPTION = (
    "A2A policy of the group: `capability.default_access` (`enabled`, `approval_required` "
    "or `disabled`), `capability.approval_timeout_seconds`, `data.deny_classifications`, "
    "`remote.allowed_globs`. It applies to the group and everything below it, beside the "
    "policy of every ancestor and of the agent card: the strictest verdict of any level is "
    "the call's. `approval_required` holds a call that does work (a message, an invocation) "
    "for a person in the approval queue; a call that only reads is not held. `disabled` "
    "refuses every A2A call. `remote.allowed_globs` judges outbound calls only (the address "
    "the call leaves for): an inbound call has no remote address and is not judged by it. "
    "A document the gateway cannot read refuses every call, so one is refused here."
)


class RollupExclusion(BaseModel):
    reason: Literal["retired", "deprecated", "no_evidence_acknowledged", "scheduled_offline"]
    note: str = Field(..., min_length=3, max_length=500, description="Why, for the reader of the report")
    expires_at: Optional[datetime] = Field(
        None,
        description=(
            "When the exclusion lapses and the system re-enters the rollup. "
            "Required for no_evidence_acknowledged and scheduled_offline — an "
            "unmeasured system may be excused for a while, never forever."
        ),
    )

    @model_validator(mode="after")
    def _temporary_reasons_expire(self) -> "RollupExclusion":
        if self.reason in ("no_evidence_acknowledged", "scheduled_offline") and self.expires_at is None:
            raise ValueError(f"rollup_exclusion.expires_at is required for reason '{self.reason}'")
        return self

class ResourceGroupType(str, Enum):
    """Group type enumeration"""
    # Organizational units
    ORGANIZATION = "organization"
    DIVISION = "division"
    DEPARTMENT = "department"
    BUSINESS_UNIT = "business_unit"
    TEAM = "team"

    # Work structures
    PROJECT = "project"
    PROGRAM = "program"
    INITIATIVE = "initiative"

    # AI/Technical units
    # An AI System is just a Resource Group of this type — it groups
    # its resources, carries limits/policies, and inherits like any RG.
    #
    # There is no APPLICATION / AGENT / ASSISTANT / SERVICE group type: the
    # kind of an AI System is `system_kind` below, not a group type.
    AI_SYSTEM = "ai_system"

    # Special
    CUSTOM = "custom"


class SystemKind(str, Enum):
    """The shape of an AI System.

    Only meaningful when `group_type == GroupType.AI_SYSTEM`. Selects the
    lifecycle signal profile: trajectory drift is the headline signal for
    `agent`/`workflow` and near-meaningless for `application`; cadence and
    silence detection are critical for `integration` and wrong for
    `assistant`.
    """
    AGENT = "agent"                  # Autonomous / semi-autonomous goal pursuit
    ASSISTANT = "assistant"          # Human-in-the-loop conversational
    APPLICATION = "application"      # Product feature calling models
    INTEGRATION = "integration"      # Scheduled / event-driven pipeline
    WORKFLOW = "workflow"            # Orchestrated, deterministic control flow
    SERVICE = "service"              # Internal shared AI capability


# ============================================================================
# NESTED MODELS
# ============================================================================

class CapabilityFilterSummary(BaseModel):
    """Summary of capability filters for a server config within a group"""
    has_filters: bool = False
    total: int = 0
    enabled: int = 0
    approval_required: int = 0
    disabled: int = 0

class ServerConfigInfo(BaseModel):
    """Nested server config information"""
    id: str
    name: str
    description: Optional[str] = None
    enabled: bool
    icon_url: Optional[str]
    mcp_server: Optional[MCPServerInfo] = None
    is_inherited: bool = False
    capability_filters: Optional[CapabilityFilterSummary] = None
    has_limits: bool = False

class LLModelInfo(BaseModel):
    """Nested LLM model information"""
    id: str
    name: str
    description: Optional[str] = None
    provider: str
    model_name: str
    health_status: LLMModelHealthStatusResponse
    is_inherited: bool = False
    has_governance: bool = False
    has_limits: bool = False
    # Whether this DIRECT binding shows the model in the end-user portal
    # picker. None for inherited rows (the flag lives on the ancestor
    # binding). Does not affect access.
    portal_visible: Optional[bool] = None

class GroupHierarchyInfo(BaseModel):
    """Compact group information for hierarchy views"""
    id: str
    name: str
    display_name: Optional[str]
    group_type: Optional[ResourceGroupType]
    hierarchy_level: int
    is_active: bool


class GroupAncestorInfo(BaseModel):
    """Ancestor group information"""
    id: str
    name: str
    group_type: Optional[ResourceGroupType]
    hierarchy_level: int


# ============================================================================
# RESOURCE GROUP CRUD MODELS
# ============================================================================

class ResourceGroupCreate(ResourceVersionField):
    """Schema for creating a new resuorce group"""
    name: str = Field(..., description="Unique group name", min_length=1, max_length=255)
    display_name: Optional[str] = Field(None, description="Display name for UI", max_length=255)
    description: Optional[str] = Field(None, description="Description of the group's purpose")
    group_type: Optional[ResourceGroupType] = Field(ResourceGroupType.CUSTOM, description="Group type")
    system_kind: Optional[SystemKind] = Field(
        None,
        description=(
            "AI System kind. Required when group_type is "
            "'ai_system' — defaults to 'agent' if omitted. Must be null for "
            "every other group type."
        ),
    )
    parent_group_id: Optional[str] = Field(None, description="Parent group ID for hierarchical organization")
    inherit_resources: bool = Field(True, description="Whether this group inherits resources and limits from parent groups")

    # Visual
    icon: Optional[str] = Field(None, description="Lucide-react icon name")
    color: Optional[str] = Field(None, description="Color for UI display")

    # Governance / ownership (AI-System attributes on the RG)
    owner: Optional[str] = Field(None, description="Accountable owner (person or team)", max_length=255)
    intended_use: Optional[str] = Field(None, description="ISO 42001 intended-use / purpose documentation")
    intended_client_labels: Optional[List[str]] = Field(None, description="client_label strings of non-system callers this group is intended to be accessed through")
    intended_client_systems: Optional[List[str]] = Field(None, description="AI System ids that may call this system. Each must be an AI System in the tenant, not this one.")
    enforce_accepted_callers: bool = Field(
        False,
        description=(
            "AI Systems only: the gateway refuses an A2A call from a caller "
            "system not in intended_client_systems (proxy subtype unaccepted_caller) instead of "
            "reporting it as an advisory finding."
        ),
    )
    lifecycle_stage: Optional[LifecycleStage] = Field(None, description="Governance lifecycle stage")
    autonomy_level: Optional[AutonomyLevel] = Field(
        None,
        description=(
            "How much this group may do without a human. "
            "Resolved min-wins across the hierarchy and the acting agent, so "
            "this can only tighten what a parent already set."
        ),
    )

    # Limits
    llm_global_limits: Optional[Dict[str, Any]] = Field(None, description="LLM global usage limits")
    mcp_global_limits: Optional[McpLimits] = Field(None, description="MCP limits of the group: an MCP limits document.")

    # Per-RG compliance posture. Read by the
    # tagging engine to emit eu_ai_act.<tier> and hipaa.rg_marked.
    eu_ai_act_risk_tier: Optional[EuAiActRiskTier] = Field(
        None,
        description=(
            "EU AI Act risk classification for this resource group. "
            "Drives `eu_ai_act.<tier>` audit tagging when the tenant "
            "has 'eu_ai_act' in its compliance profile."
        ),
    )
    eu_ai_act_role: Optional[str] = Field(
        None,
        max_length=40,
        description=(
            "Regulated role this tenant holds for the AI System: "
            "provider | deployer | provider_and_deployer | importer | "
            "distributor | product_manufacturer | authorised_representative. "
            "Obligations attach to the (system, role) pair."
        ),
    )
    phi_resource: Optional[bool] = Field(
        None,
        description=(
            "Operator-declared HIPAA PHI scope. True tags every call "
            "through this RG as `hipaa.rg_marked` (independent of "
            "what the PII detector saw on the wire)."
        ),
    )
    sensitive_data: Optional[bool] = Field(
        None,
        description=(
            "Operator-declared sensitive-data scope (the privileged-read "
            "rule). True seals read-only tool calls through this RG as "
            "evidence records."
        ),
    )

    is_active: bool = Field(True, description="Whether this group is active")

    @validator('name')
    def validate_name(cls, v):
        """Validate group name format"""
        if not v or not v.strip():
            raise ValueError("Group name cannot be empty")
        # Allow alphanumeric, hyphens, underscores, spaces
        import re
        if not re.match(r'^[a-zA-Z0-9\s\-_]+$', v):
            raise ValueError("Group name can only contain letters, numbers, spaces, hyphens, and underscores")
        return v.strip()

    @model_validator(mode="after")
    def _couple_system_kind(self):
        """Enforce the group_type/system_kind coupling.

        Defaulting rather than rejecting the missing case mirrors the
        model's flush hook: an AI System with no stated kind is an agent.
        The reverse case is rejected loudly —
        supplying a kind for a non-AI-System means the caller believes the
        group is something it is not, and silently dropping the field would
        surface later as an AI System missing from the fleet board.
        """
        if self.group_type == ResourceGroupType.AI_SYSTEM:
            if self.system_kind is None:
                self.system_kind = SystemKind.AGENT
        elif self.system_kind is not None:
            raise ValueError(
                "system_kind is only valid when group_type is 'ai_system' "
                f"(got group_type={self.group_type.value if self.group_type else None!r})"
            )
        if self.enforce_accepted_callers and self.group_type != ResourceGroupType.AI_SYSTEM:
            raise ValueError(
                "enforce_accepted_callers is only valid when group_type is 'ai_system' "
                "(only an AI System is called as a shared service)"
            )
        return self


class ResourceGroupUpdate(ResourceVersionField):
    """Schema for updating a resource group"""

    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    display_name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    group_type: Optional[ResourceGroupType] = None
    system_kind: Optional[SystemKind] = Field(
        None,
        description=(
            "AI System kind. Only valid when the group is — "
            "or is being changed to — group_type 'ai_system'. Omit to leave "
            "unchanged."
        ),
    )
    run_idle_timeout_seconds: Optional[int] = Field(
        None,
        ge=5,
        le=86400,
        description=(
            "Seconds a run may sit idle before it is "
            "closed as abandoned. Pass null to fall back to the platform "
            "default (120s)."
        ),
    )
    parent_group_id: Optional[str] = None
    inherit_resources: Optional[bool] = None

    # Visual
    icon: Optional[str] = None
    color: Optional[str] = None

    # Governance / ownership (AI-System attributes on the RG)
    owner: Optional[str] = Field(None, max_length=255, description="Accountable owner. Pass null to clear.")
    intended_use: Optional[str] = Field(None, description="ISO 42001 intended-use / purpose. Pass null to clear.")
    intended_client_labels: Optional[List[str]] = Field(None, description="Intended client_label strings (non-system callers). Pass null to clear.")
    intended_client_systems: Optional[List[str]] = Field(None, description="AI System ids that may call this system. Pass null to clear.")
    enforce_accepted_callers: Optional[bool] = Field(
        None,
        description=(
            "AI Systems only (422 otherwise): refuse A2A callers not in "
            "intended_client_systems. Omit to leave unchanged."
        ),
    )
    lifecycle_stage: Optional[LifecycleStage] = Field(None, description="Governance lifecycle stage. Pass null to clear.")
    autonomy_level: Optional[AutonomyLevel] = Field(
        None, description="Autonomy level. Pass null to clear."
    )
    rollup_exclusion: Optional[RollupExclusion] = Field(
        None,
        description=(
            "Exclude this AI System from the estate health/verdict "
            "rollup, with a reason and (for temporary reasons) an expiry. "
            "Pass null to include it again. Disclosed everywhere the rollup is."
        ),
    )
    replay_suite_id: Optional[str] = Field(
        None,
        description=(
            "The replay suite the promotion gate consults. "
            "Pass null to unbind. A suite can be bound here after its "
            "coverage report has been reviewed, without rebuilding it."
        ),
    )

    # Limits
    llm_global_limits: Optional[Dict[str, Any]] = None
    mcp_global_limits: Optional[McpLimits] = Field(None, description="MCP limits of the group: an MCP limits document.")
    a2a_global_limits: Optional[Dict[str, Any]] = Field(
        None,
        description="A2A invocation limits: invocations.{daily_limit, monthly_limit, hard_stop, ...}, chain.max_delegation_depth",
    )

    # Governance
    skill_global_governance: Optional[SkillGovernance] = Field(None, description="Skill governance: execution_mode, discovery_enabled, approval_timeout_seconds")
    a2a_global_governance: Optional[A2AGovernance] = Field(
        None,
        description=A2A_GOVERNANCE_DESCRIPTION,
    )

    # Per-RG compliance posture (see Create
    # model for semantics). NULL clears the field on the row.
    eu_ai_act_risk_tier: Optional[EuAiActRiskTier] = Field(
        None,
        description="EU AI Act risk classification. Pass null to clear.",
    )
    eu_ai_act_role: Optional[str] = Field(
        None,
        max_length=40,
        description="EU AI Act regulated role. Pass null to clear.",
    )
    phi_resource: Optional[bool] = Field(
        None,
        description="HIPAA PHI scope flag. True tags every call as hipaa.rg_marked.",
    )
    sensitive_data: Optional[bool] = Field(
        None,
        description="Sensitive-data flag. True seals read-only tool calls as evidence records.",
    )

    is_active: Optional[bool] = None

    @model_validator(mode="after")
    def _reject_kind_on_non_ai_system(self):
        """Reject the combination this model *can* see as wrong.

        PATCH semantics make full coupling validation impossible here: an
        omitted field means "leave unchanged", so a payload carrying only
        `system_kind` is valid iff the stored group is already an AI System,
        which this model cannot know. The endpoint re-checks the merged
        state against the row (`ResourceGroup.validate_system_kind`). What
        *is* unambiguously wrong — and worth a 422 rather than a round trip
        to the database — is a single payload that sets a non-AI-System
        group type and a kind at the same time.
        """
        if (
            self.group_type is not None
            and self.group_type != ResourceGroupType.AI_SYSTEM
            and self.system_kind is not None
        ):
            raise ValueError(
                "system_kind is only valid when group_type is 'ai_system' "
                f"(got group_type={self.group_type.value!r})"
            )
        if (
            self.group_type is not None
            and self.group_type != ResourceGroupType.AI_SYSTEM
            and self.enforce_accepted_callers
        ):
            raise ValueError(
                "enforce_accepted_callers is only valid when group_type is 'ai_system' "
                f"(got group_type={self.group_type.value!r})"
            )
        if "enforce_accepted_callers" in self.model_fields_set and self.enforce_accepted_callers is None:
            raise ValueError("enforce_accepted_callers is a boolean; omit it to leave it unchanged")
        return self


class ResourceGroupResponse(BaseModel):
    """Schema for resource group response"""
    id: str
    name: str
    display_name: Optional[str]
    description: Optional[str]
    group_type: Optional[ResourceGroupType]
    # AI System kind. Declared so to_dict()'s value survives
    # the response — Pydantic strips undeclared fields.
    system_kind: Optional[SystemKind] = None
    # Per-system run idle window. Null = platform default.
    run_idle_timeout_seconds: Optional[int] = None
    # Default approval window stated on this group (null = inherited).
    approval_window_seconds: Optional[int] = None

    # Hierarchy
    parent_group_id: Optional[str] = None
    hierarchy_level: int
    hierarchy_path: Optional[str]
    inherit_resources: bool

    # Visual
    icon: Optional[str]
    color: Optional[str]

    # Governance / ownership (AI-System attributes on the RG).
    # Declared here so to_dict()'s values survive the response (Pydantic strips
    # undeclared fields).
    owner: Optional[str] = None
    intended_use: Optional[str] = None
    intended_client_labels: Optional[List[str]] = None
    intended_client_systems: Optional[List[str]] = None
    enforce_accepted_callers: bool = False
    lifecycle_stage: Optional[LifecycleStage] = None
    autonomy_level: Optional[AutonomyLevel] = None
    replay_suite_id: Optional[str] = None
    rollup_exclusion: Optional[Dict[str, Any]] = None
    # The live outcome catalogue (edited through
    # /admin/ai-systems/{id}/outcomes, which validates it).
    declared_outcomes: Optional[List[Dict[str, Any]]] = None

    # Status
    is_active: bool
    # System (undeletable Default) group flag — emitted by to_dict() but
    # previously NOT declared here, so Pydantic silently stripped it from
    # every RG response (the admin UI couldn't tell the Default group apart).
    is_system: Optional[bool] = None

    # Metadata. Declared here rather than inherited: this response is a
    # hand-built dict, and the Audit Info tab reads exactly these four plus
    # `version`.
    version: Optional[str] = None
    created_by: Optional[str]
    created_at: str
    updated_at: str
    updated_by: Optional[str] = None

    # Optional detailed data (controlled by query params)
    stats: Optional[Dict[str, int]] = None
    server_configs: Optional[List[ServerConfigInfo]] = None
    llm_models: Optional[List[LLModelInfo]] = None

    # Hierarchy data
    parent_group_name: Optional[str] = None
    parent_group_type: Optional[ResourceGroupType] = None
    children_count: Optional[int] = None
    children: Optional[List[GroupHierarchyInfo]] = None
    ancestors: Optional[List[GroupAncestorInfo]] = None

    # Limits — declare ALL that to_dict(include_limits=True) emits, otherwise
    # Pydantic strips the undeclared ones and the GET/PATCH response shows the
    # default (operator edits appear to "vanish" on form reload).
    llm_global_limits: Optional[Dict[str, Any]] = None
    mcp_global_limits: Optional[Dict[str, Any]] = None
    skill_global_limits: Optional[Dict[str, Any]] = None
    a2a_global_limits: Optional[Dict[str, Any]] = None
    media_global_limits: Optional[Dict[str, Any]] = None

    # Governance
    llm_global_governance: Optional[LlmGovernance] = None
    mcp_global_governance: Optional[McpGovernance] = None
    skill_global_governance: Optional[SkillGovernance] = None
    skill_discovery_enabled: Optional[bool] = None
    skill_execution_mode: Optional[str] = None
    a2a_global_governance: Optional[A2AGovernance] = None
    media_global_governance: Optional[Dict[str, Any]] = None

    # Per-RG compliance posture.
    eu_ai_act_risk_tier: Optional[EuAiActRiskTier] = None
    eu_ai_act_role: Optional[str] = None
    phi_resource: Optional[bool] = None
    sensitive_data: Optional[bool] = None

    # Provenance ('manual' | 'import'); set when a workspace is
    # auto-discovered from a vendor usage import.
    source: Optional[str] = None
    source_external_id: Optional[str] = None

    class Config:
        from_attributes = True


class ResourceGroupListResponse(BaseModel):
    """Schema for list of resource groups"""
    groups: List[ResourceGroupResponse]
    total: int


class ResourceGroupStatsResponse(BaseModel):
    """Schema for user resource statistics"""
    total_groups: int
    active_groups: int
    inactive_groups: int
    groups_by_type: Dict[str, int]
    total_memberships: int
    root_groups_count: int


# ============================================================================
# HIERARCHY NAVIGATION MODELS
# ============================================================================

class GroupHierarchyTree(BaseModel):
    """Recursive tree structure for group hierarchy"""
    id: str
    name: str
    display_name: Optional[str]
    group_type: Optional[ResourceGroupType]
    # Carried through the tree so callers that flatten it
    # looking for AI Systems (Mission Control's scope filter, the fleet
    # board) get the kind without a second round trip per node.
    system_kind: Optional[SystemKind] = None
    hierarchy_level: int
    is_active: bool
    parent_group_id: Optional[str]
    inherit_resources: bool

    # Statistics
    member_count: int
    server_config_count: int
    llm_model_count: int
    access_key_count: int

    # Governance at a glance. The tree is where an operator forms their
    # mental model of an estate, and it used to say nothing about where
    # limits or policy actually live — so a department that governs nothing
    # looked exactly like one that caps every model beneath it.
    #
    # `sets_own_*` is true when this group carries a document of its own on
    # any surface (LLM, MCP, skill, media). `inherits_*` is true when some
    # ancestor does, whether or not this group adds to it. Both are cheap:
    # the builder already holds every row.
    sets_own_limits: bool = Field(
        False, description="This group sets limits of its own on at least one surface"
    )
    sets_own_policy: bool = Field(
        False, description="This group sets governance of its own on at least one surface"
    )
    inherits_limits: bool = Field(
        False, description="An ancestor sets limits that bind this group"
    )
    inherits_policy: bool = Field(
        False, description="An ancestor sets governance that binds this group"
    )

    # Children (recursive)
    children: List['GroupHierarchyTree'] = []


class GroupAncestorPathResponse(BaseModel):
    """Response for ancestor path queries"""
    group_id: str
    group_name: str
    ancestors: List[GroupAncestorInfo]
    full_path: str  # e.g., "Engineering / Frontend / React Team"


class GroupDescendantsResponse(BaseModel):
    """Response for descendants queries"""
    group_id: str
    group_name: str
    descendants: List[GroupHierarchyInfo]
    total_descendants: int


class GroupSiblingsResponse(BaseModel):
    """Response for siblings queries"""
    group_id: str
    group_name: str
    parent_group_id: Optional[str]
    siblings: List[GroupHierarchyInfo]
    total_siblings: int


# ============================================================================
# EFFECTIVE RESOURCES & LIMITS MODELS
# ============================================================================

class EffectiveLLModelsResponse(BaseModel):
    """Response for effective LLM models (with inheritance)"""
    group_id: str
    group_name: str

    # Direct models
    direct_models: List[LLModelInfo]
    direct_count: int

    # Inherited models
    inherited_models: List[LLModelInfo]
    inherited_count: int

    # Combined (deduplicated)
    effective_models: List[LLModelInfo]
    effective_count: int


class EffectiveServerConfigsResponse(BaseModel):
    """Response for effective server configs (with inheritance)"""
    group_id: str
    group_name: str

    # Direct configs
    direct_configs: List[ServerConfigInfo]
    direct_count: int

    # Inherited configs
    inherited_configs: List[ServerConfigInfo]
    inherited_count: int

    # Combined (deduplicated)
    effective_configs: List[ServerConfigInfo]
    effective_count: int

class AddLLMModelToGroupRequest(BaseModel):
    llm_model_id: str = Field(..., description="ID of the LLM model to add")
    portal_visible: Optional[bool] = Field(
        None,
        description=(
            "Show this model in the end-user portal model picker. If omitted, "
            "defaults to false for routing members (bound only so the routing "
            "group resolves) and true otherwise. Does not affect access."
        ),
    )


class PortalVisibilityUpdate(BaseModel):
    """Body for the per-binding portal-visibility PATCH."""

    model_config = ConfigDict(extra="forbid")
    portal_visible: bool = Field(..., description="Show this bound model in the end-user portal model picker.")

class AddServerConfigToGroupRequest(BaseModel):
    server_config_id: str = Field(..., description="ID of the server config to add")


class AgentSkillInfo(BaseModel):
    """Nested agent skill information for group views"""
    id: str
    name: str
    description: Optional[str] = None
    version: str
    status: str
    enabled: bool
    category: Optional[str] = None
    icon_name: Optional[str] = None
    execution_count: int = 0
    is_inherited: bool = False
    has_limits: bool = False
    has_governance: bool = False


class EffectiveAgentSkillsResponse(BaseModel):
    """Response for effective agent skills (with inheritance)"""
    group_id: str
    group_name: str

    # Direct skills
    direct_skills: List[AgentSkillInfo]
    direct_count: int

    # Inherited skills
    inherited_skills: List[AgentSkillInfo]
    inherited_count: int

    # Combined (deduplicated)
    effective_skills: List[AgentSkillInfo]
    effective_count: int


class GuardrailConfigInfo(BaseModel):
    """Compact guardrail config info for group views"""
    id: str
    name: str
    description: Optional[str] = None
    enabled: bool
    is_default: bool
    priority: int = 0
    surfaces: Dict[str, bool] = {}
    pii_enabled: bool = False
    prompt_injection_enabled: bool = False
    jailbreak_enabled: bool = False
    toxic_content_enabled: bool = False
    secrets_enabled: bool = False
    banned_word_count: int = 0
    banned_pattern_count: int = 0
    provider_count: int = 0
    # Number of surfaces with an explicit override per check.
    # 0 means "scalar action applies everywhere"; ≥1 means the row's
    # Active Checks column should advertise that surfaces deviate
    # from the scalar (otherwise an operator scanning the list can't
    # tell that a config is doing per-surface work).
    per_surface_counts: Dict[str, int] = {}
    fail_open: bool = False
    max_check_timeout_ms: float = 2000.0
    is_inherited: bool = False
    source_group_id: Optional[str] = None
    source_group_name: Optional[str] = None
    group_ids: List[str] = []


class EffectiveGuardrailsResponse(BaseModel):
    """Response for effective guardrail configs (with inheritance)"""
    group_id: str
    group_name: str
    direct_configs: List[GuardrailConfigInfo]
    direct_count: int
    inherited_configs: List[GuardrailConfigInfo]
    inherited_count: int
    effective_configs: List[GuardrailConfigInfo]
    effective_count: int


class GroupLimitsResponse(BaseModel):
    """Response for group limits with governance inheritance"""
    group_id: str
    group_name: str

    # Direct limits set on this group
    llm_global_limits: Optional[Dict[str, Any]]
    mcp_global_limits: Optional[Dict[str, Any]]
    skill_global_limits: Optional[Dict[str, Any]] = None

    # Effective limits after restrictive inheritance (min of all ancestors)
    effective_llm_limits: Optional[Dict[str, Any]] = None
    effective_mcp_limits: Optional[Dict[str, Any]] = None
    effective_skill_limits: Optional[Dict[str, Any]] = None

    # Inherited floor — the most restrictive ancestor limits (what you can't go above)
    inherited_llm_limits: Optional[Dict[str, Any]] = None
    inherited_mcp_limits: Optional[Dict[str, Any]] = None
    inherited_skill_limits: Optional[Dict[str, Any]] = None


class LLMGlobalLimitsUpdate(BaseModel):
    """Schema for updating LLM global limits"""

    model_config = ConfigDict(extra="forbid")
    llm_global_limits: Dict[str, Any] = Field(..., description="LLM global usage limits to set")


class SiblingLimitInfo(BaseModel):
    """Sibling group limit allocation for capacity planning"""
    group_id: str
    group_name: str
    limits: Optional[Dict[str, Any]] = None


class LimitCapacityInfo(BaseModel):
    """
    Capacity breakdown against the budget this group draws on: the nearest
    ancestor that actually sets limits, what the other groups drawing on it
    have already claimed, and what is left.

    The holder is not always the immediate parent. Estates commonly set a
    budget once at the organisation and leave the departments between it
    and an AI system unset; computing this against the immediate parent
    alone reported nothing at exactly the depth where operators configure.
    """
    parent_limit: Optional[Dict[str, Any]] = Field(None, description="Limits of the nearest ancestor that sets any")
    parent_name: Optional[str] = None
    parent_is_immediate: bool = Field(
        True,
        description="False when the budget comes from a grandparent or higher, so the console can name the holder rather than say 'parent'",
    )
    siblings: List[SiblingLimitInfo] = Field(default_factory=list, description="Other groups drawing on the same budget, with their limits")
    sibling_total: Optional[Dict[str, Any]] = Field(None, description="Sum of those groups' numeric limit values")
    available: Optional[Dict[str, Any]] = Field(None, description="Holder limit minus the total already claimed = what this group can still take")


class LLMGlobalLimitsResponse(BaseModel):
    """Response for LLM global limits with governance inheritance"""
    group_id: str
    group_name: str
    llm_global_limits: Optional[Dict[str, Any]]
    effective_llm_limits: Optional[Dict[str, Any]] = Field(
        None, description="Effective limits after restrictive merge with all ancestors"
    )
    inherited_llm_limits: Optional[Dict[str, Any]] = Field(
        None, description="Inherited floor from ancestors (cannot set less restrictive)"
    )
    capacity: Optional[LimitCapacityInfo] = None


class MCPGlobalLimitsUpdate(BaseModel):
    """Schema for updating MCP global limits"""

    model_config = ConfigDict(extra="forbid")
    mcp_global_limits: McpLimits = Field(..., description="MCP limits of the group to set: an MCP limits document.")


class MCPGlobalLimitsResponse(BaseModel):
    """Response for MCP global limits with governance inheritance"""
    group_id: str
    group_name: str
    mcp_global_limits: Optional[Dict[str, Any]]
    effective_mcp_limits: Optional[Dict[str, Any]] = Field(
        None, description="Effective limits after restrictive merge with all ancestors"
    )
    inherited_mcp_limits: Optional[Dict[str, Any]] = Field(
        None, description="Inherited floor from ancestors (cannot set less restrictive)"
    )
    capacity: Optional[LimitCapacityInfo] = None


class SkillGlobalLimitsUpdate(BaseModel):
    """Schema for updating skill global limits"""

    model_config = ConfigDict(extra="forbid")
    skill_global_limits: Dict[str, Any] = Field(..., description="Skill global usage limits to set")


class SkillGlobalLimitsResponse(BaseModel):
    """Response for skill global limits with governance inheritance"""
    group_id: str
    group_name: str
    skill_global_limits: Optional[Dict[str, Any]]
    effective_skill_limits: Optional[Dict[str, Any]] = Field(
        None, description="Effective limits after restrictive merge with all ancestors"
    )
    inherited_skill_limits: Optional[Dict[str, Any]] = Field(
        None, description="Inherited floor from ancestors (cannot set less restrictive)"
    )
    capacity: Optional[LimitCapacityInfo] = None


# ----- Media (multimodal) limits + governance ---------------------------------
#
# Schema is `Dict[str, Any]` keyed by mode (audio_speech,
# audio_transcription, image_generation, video_generation,
# embedding). Same shape pattern as the existing skill/llm/mcp
# response models so the admin UI's PATCH/GET roundtrip stays
# consistent. Inheritance is computed the same way as the other
# surfaces: limits through `_compute_inherited_limits`, governance
# through `_compute_inherited_media_governance`, which mirrors the
# proxy's per-field rules (whitelists intersect, ceilings take the
# min, `speed_min` takes the max, requirements OR).

class MediaGlobalLimitsUpdate(BaseModel):

    model_config = ConfigDict(extra="forbid")
    media_global_limits: Dict[str, Any] = Field(
        ...,
        description=(
            "Per-mode media usage limits keyed by LLModelMode value. Not available in this release, "
            "and refused: every `monthly_*` key, `daily_warning_percent`, `monthly_warning_percent`, "
            "`hard_stop: false` (every media limit blocks when reached) and every key of the "
            "`embedding` mode (embeddings are counted against the group's LLM limits)."
        ),
    )

    @field_validator("media_global_limits")
    @classmethod
    def _in_this_release(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        problem = release_scope.media_limits_problem(value)
        if problem:
            raise ValueError(problem)
        return value


class MediaGlobalLimitsResponse(BaseModel):
    group_id: str
    group_name: str
    media_global_limits: Optional[Dict[str, Any]]
    effective_media_limits: Optional[Dict[str, Any]] = Field(
        None, description="Effective limits after restrictive merge with all ancestors"
    )
    inherited_media_limits: Optional[Dict[str, Any]] = Field(
        None, description="Inherited floor from ancestors (cannot set less restrictive)"
    )
    capacity: Optional[LimitCapacityInfo] = None


class MediaGlobalGovernanceUpdate(BaseModel):

    model_config = ConfigDict(extra="forbid")
    media_global_governance: Dict[str, Any] = Field(
        ...,
        description=(
            "Per-mode behavioral governance keyed by LLModelMode value. Not available in this "
            "release, and refused: `embedding.max_input_tokens_per_item`, and "
            "`audio_transcription.require_prompt_guardrail: false` (the guardrails always check "
            "the prompt of a transcription)."
        ),
    )

    @field_validator("media_global_governance")
    @classmethod
    def _in_this_release(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        problem = release_scope.media_governance_problem(value)
        if problem:
            raise ValueError(problem)
        return value


class MediaGlobalGovernanceResponse(BaseModel):
    group_id: str
    group_name: str
    media_global_governance: Optional[Dict[str, Any]]
    effective_media_governance: Optional[Dict[str, Any]] = Field(
        None, description="Effective policy after restrictive merge with all ancestors"
    )
    inherited_media_governance: Optional[Dict[str, Any]] = Field(
        None, description="Inherited from ancestors (cannot set less restrictive)"
    )


class ModelLimitInfo(BaseModel):
    """Sibling model limit allocation for capacity planning"""
    llm_model_id: str
    llm_model_name: str
    limits: Optional[Dict[str, Any]] = None


class PerModelCapacityInfo(BaseModel):
    """Capacity breakdown for per-model limits within a group"""
    group_global_limit: Optional[Dict[str, Any]] = Field(None, description="Group's effective global LLM limits")
    group_name: Optional[str] = None
    other_models: List[ModelLimitInfo] = Field(default_factory=list, description="Other models' per-model limits in this group")
    other_models_total: Optional[Dict[str, Any]] = Field(None, description="Sum of other models' numeric limit values")
    available: Optional[Dict[str, Any]] = Field(None, description="Group global limit minus other models' total")


class LLMModelLimitsResponse(BaseModel):
    """Response for per-model limits with effective computation"""
    group_id: str
    group_name: str
    llm_model_id: str
    llm_model_name: str
    limits: Optional[Dict[str, Any]] = Field(
        None, description="Per-model limits set directly on this group+model pair"
    )
    effective_global_limits: Optional[Dict[str, Any]] = Field(
        None, description="Effective global LLM limits (after ancestor merge)"
    )
    effective_limits: Optional[Dict[str, Any]] = Field(
        None, description="Final effective limits: min(global, per_item)"
    )
    portal_visible: Optional[bool] = Field(
        None, description="Whether this bound model appears in the end-user portal model picker"
    )
    capacity: Optional[PerModelCapacityInfo] = None


class ServerLimitInfo(BaseModel):
    """Sibling server limit allocation"""
    server_config_id: str
    server_name: str
    limits: Optional[Dict[str, Any]] = None


class PerServerCapacityInfo(BaseModel):
    """Capacity breakdown for per-server limits within a group"""
    group_global_limit: Optional[Dict[str, Any]] = Field(None, description="Group's effective global MCP limits")
    group_name: Optional[str] = None
    other_servers: List[ServerLimitInfo] = Field(default_factory=list)
    other_servers_total: Optional[Dict[str, Any]] = None
    available: Optional[Dict[str, Any]] = None


class MCPServerLimitsResponse(BaseModel):
    """Response for per-server limits with effective computation"""
    group_id: str
    group_name: str
    server_config_id: str
    server_config_name: str
    limits: Optional[Dict[str, Any]] = Field(
        None, description="Per-server limits set directly on this group+server pair"
    )
    effective_global_limits: Optional[Dict[str, Any]] = Field(
        None, description="Effective global MCP limits (after ancestor merge)"
    )
    effective_limits: Optional[Dict[str, Any]] = Field(
        None, description="Final effective limits: min(global, per_item)"
    )
    capacity: Optional[PerServerCapacityInfo] = None


class SkillLimitInfo(BaseModel):
    """Sibling skill limit allocation"""
    agent_skill_id: str
    skill_name: str
    limits: Optional[Dict[str, Any]] = None


class PerSkillCapacityInfo(BaseModel):
    """Capacity breakdown for per-skill limits within a group"""
    group_global_limit: Optional[Dict[str, Any]] = Field(None, description="Group's effective global skill limits")
    group_name: Optional[str] = None
    other_skills: List[SkillLimitInfo] = Field(default_factory=list)
    other_skills_total: Optional[Dict[str, Any]] = None
    available: Optional[Dict[str, Any]] = None


class SkillItemLimitsResponse(BaseModel):
    """Response for per-skill limits with effective computation"""
    group_id: str
    group_name: str
    agent_skill_id: str
    agent_skill_name: str
    limits: Optional[Dict[str, Any]] = Field(
        None, description="Per-skill limits set directly on this group+skill pair"
    )
    effective_global_limits: Optional[Dict[str, Any]] = Field(
        None, description="Effective global skill limits (after ancestor merge)"
    )
    effective_limits: Optional[Dict[str, Any]] = Field(
        None, description="Final effective limits: min(global, per_item)"
    )
    capacity: Optional[PerSkillCapacityInfo] = None


class ItemLimitsUpdate(BaseModel):
    """Schema for updating per-item limits"""

    model_config = ConfigDict(extra="forbid")
    limits: Dict[str, Any] = Field(..., description="Per-item limits to set")


class LLMGlobalGovernanceUpdate(BaseModel):
    """The LLM governance to set on a resource group, for every model."""

    model_config = ConfigDict(extra="forbid")
    governance: LlmGovernance = Field(..., description="The LLM governance document to set")


class MCPGlobalGovernanceUpdate(BaseModel):
    """The MCP governance to set on a resource group."""

    model_config = ConfigDict(extra="forbid")
    governance: McpGovernance = Field(..., description="The MCP governance document to set")


class SkillGovernanceUpdate(BaseModel):
    """The skill governance to set: on a resource group for every skill, or
    for one skill of the group."""

    model_config = ConfigDict(extra="forbid")
    governance: SkillGovernance = Field(..., description="The skill governance document to set")


class LLMGlobalGovernanceResponse(BaseModel):
    """Response for LLM global governance with inheritance"""
    group_id: str
    group_name: str
    llm_global_governance: Optional[LlmGovernance] = Field(None, description="Direct governance set on this group")
    effective_llm_governance: Optional[LlmGovernance] = Field(None, description="Effective after ancestor merge")
    inherited_llm_governance: Optional[LlmGovernance] = Field(None, description="Inherited from ancestors")


class MCPGlobalGovernanceResponse(BaseModel):
    """Response for MCP global governance with inheritance"""
    group_id: str
    group_name: str
    mcp_global_governance: Optional[McpGovernance] = Field(None, description="Direct governance set on this group")
    effective_mcp_governance: Optional[McpGovernance] = Field(None, description="Effective after ancestor merge")
    inherited_mcp_governance: Optional[McpGovernance] = Field(None, description="Inherited from ancestors")


class SkillGlobalGovernanceResponse(BaseModel):
    """Response for skill global governance with inheritance"""
    group_id: str
    group_name: str
    skill_global_governance: Optional[SkillGovernance] = Field(None, description="Direct governance set on this group")
    effective_skill_governance: Optional[SkillGovernance] = Field(None, description="Effective after ancestor merge")
    inherited_skill_governance: Optional[SkillGovernance] = Field(None, description="Inherited from ancestors")


class ApprovalWindowUpdate(BaseModel):
    """Set (or clear with null) this group's default approval window."""

    model_config = ConfigDict(extra="forbid")
    approval_window_seconds: Optional[int] = Field(
        None,
        ge=60,
        le=604800,
        description=(
            "Seconds a human-approval hold raised for this group stays open when "
            "the policy that raised it names no window. Null inherits. "
            "60 .. 604800 (7 days); may not exceed the inherited window."
        ),
    )


class ApprovalWindowResponse(BaseModel):
    """This group's approval window, what it inherits, and the value a
    hold that names no policy window gets (the core's `approval_window`
    resolver: group chain min-wins, then tenant, then platform)."""
    group_id: str
    group_name: str
    approval_window_seconds: Optional[int] = Field(None, description="Stated on this group")
    inherited_window_seconds: Optional[int] = Field(
        None, description="Smallest window stated by an ancestor (a ceiling for this group)"
    )
    inherited_from_group_id: Optional[str] = None
    inherited_from_group_name: Optional[str] = None
    tenant_default_seconds: Optional[int] = None
    platform_default_seconds: int
    effective_seconds: int
    effective_source: str = Field(..., description="resource_group | tenant | platform")
    min_seconds: int
    max_seconds: int


# ─── A2A (Agent-to-Agent) governance + limits ──────────────────────────────


class A2AGlobalLimitsResponse(BaseModel):
    """Response for A2A global limits with governance inheritance."""
    group_id: str
    group_name: str
    a2a_global_limits: Optional[Dict[str, Any]] = None
    effective_a2a_limits: Optional[Dict[str, Any]] = Field(
        None, description="Effective limits after restrictive merge with all ancestors",
    )
    inherited_a2a_limits: Optional[Dict[str, Any]] = Field(
        None, description="Inherited floor from ancestors (cannot set less restrictive)",
    )


class A2AGlobalLimitsUpdate(BaseModel):
    """Schema for updating A2A global limits."""
    a2a_global_limits: Dict[str, Any] = Field(
        ..., description="A2A global usage limits to set",
    )


class GroupAgentCardsRequest(BaseModel):
    """The whole set of agent cards a resource group is bound to."""

    model_config = ConfigDict(extra="forbid")

    agent_card_ids: List[str] = Field(..., description="Every agent card the group is bound to after the request")


class A2ARemoteAllowListLevel(BaseModel):
    """The outbound allow-list one level of the group's chain states."""

    group_id: str = Field(..., description="The group that states the list")
    group_name: str
    allowed_globs: List[str] = Field(
        ..., description="The addresses an outbound A2A call may go to, as this level's patterns"
    )


class A2AGlobalGovernanceResponse(BaseModel):
    """Response for A2A global governance with inheritance."""
    group_id: str
    group_name: str
    a2a_global_governance: Optional[A2AGovernance] = Field(
        None, description="Direct governance set on this group",
    )
    effective_a2a_governance: Optional[A2AGovernance] = Field(
        None,
        description=(
            "What applies to the group from itself and its ancestors: the strictest "
            "`capability.default_access`, the shortest approval window and every denied "
            "classification. It carries no `remote` section: see `remote_allow_lists`."
        ),
    )
    inherited_a2a_governance: Optional[A2AGovernance] = Field(
        None, description="The same, from the ancestors alone",
    )
    remote_allow_lists: List[A2ARemoteAllowListLevel] = Field(
        default_factory=list,
        description=(
            "The outbound allow-list of every level that states one, from the root to this "
            "group. " + A2A_ALLOW_LISTS_RULE
        ),
    )
    remote_allow_lists_rule: str = Field(A2A_ALLOW_LISTS_RULE, description="How the lists apply")


class AgentCardGroupBinding(BaseModel):
    """Schema for binding an agent card to a set of resource groups."""
    group_ids: List[str] = Field(
        ...,
        description=(
            "Full list of group IDs the card should be bound to. Replaces any "
            "existing bindings (PUT semantics)."
        ),
    )


class EffectiveAgentCardItem(BaseModel):
    """One agent-card row in the effective-view response."""
    id: str
    name: str
    version: str
    description: Optional[str] = None
    trust_tier: str
    is_trusted: bool
    enabled: bool
    capability_count: int = 0
    is_inherited: bool = False
    source_group_id: Optional[str] = None
    source_group_name: Optional[str] = None
    group_ids: List[str] = Field(default_factory=list)


class EffectiveAgentCardsResponse(BaseModel):
    """Effective-agent-cards response for a resource group."""
    group_id: str
    group_name: str
    direct_cards: List[EffectiveAgentCardItem] = Field(default_factory=list)
    direct_count: int = 0
    inherited_cards: List[EffectiveAgentCardItem] = Field(default_factory=list)
    inherited_count: int = 0
    effective_cards: List[EffectiveAgentCardItem] = Field(default_factory=list)
    effective_count: int = 0


class SkillGovernanceResponse(BaseModel):
    """Response for per-skill governance"""
    group_id: str
    group_name: str
    agent_skill_id: str
    agent_skill_name: str
    governance: Optional[SkillGovernance] = Field(None, description="Direct per-skill governance")
    effective_global_governance: Optional[SkillGovernance] = Field(None, description="Effective global skill governance")
    effective_governance: Optional[SkillGovernance] = Field(None, description="Final effective governance")
    has_governance: bool = Field(False, description="Whether any governance is configured")


class LLMModelGovernanceUpdate(BaseModel):
    """Schema for updating LLM governance for a specific model in a group"""

    model_config = ConfigDict(extra="forbid")
    governance: LlmGovernance = Field(..., description="LLM behavioral governance to set for this model")


class LLMModelGovernanceResponse(BaseModel):
    """Response for per-model LLM governance with inheritance"""
    group_id: str
    group_name: str
    llm_model_id: str
    llm_model_name: str
    governance: Optional[LlmGovernance] = Field(
        None, description="Direct governance set on this group for this model"
    )
    effective_governance: Optional[LlmGovernance] = Field(
        None, description="Effective governance after restrictive merge with all ancestors"
    )
    inherited_governance: Optional[LlmGovernance] = Field(
        None, description="Inherited governance from ancestors (cannot relax)"
    )
    has_governance: bool = Field(
        False, description="Whether any governance is configured (direct or inherited)"
    )


# ============================================================================
# MCP CAPABILITY ACCESS CONTROL MODELS
# ============================================================================

class MCPCapabilityInfo(BaseModel):
    """Individual capability entry with three-state access control"""
    capability_type: str = Field(..., description="tool, resource, prompt, or resource_template")
    capability_name: str = Field(..., description="Tool name, resource URI, prompt name, or template URI")
    access: str = Field('enabled', description="Access state: 'enabled', 'approval_required', or 'disabled'")
    approval_timeout_seconds: Optional[int] = Field(
        None, ge=1, le=604800,
        description="Timeout in seconds for approval_required capabilities: 1 .. 604800 (7 days)",
    )
    inherited_access: Optional[str] = Field(None, description="Access state inherited from ancestor (most restrictive)")
    source_group_id: Optional[str] = Field(None, description="ID of the ancestor group enforcing the inherited access level")
    source_group_name: Optional[str] = Field(None, description="Name of the ancestor group enforcing the inherited access level")

class MCPDiscoveredCapability(BaseModel):
    """A single discovered capability from the MCP server"""
    capability_type: str = Field(..., description="tool, resource, prompt, or resource_template")
    name: str = Field(..., description="Capability name or URI")
    description: Optional[str] = None

class MCPCapabilityFilterResponse(BaseModel):
    """Response for capability filters on a group+server"""
    group_id: str
    group_name: str
    server_config_id: str
    server_config_name: str
    capabilities: List[MCPCapabilityInfo]
    total: int
    has_filters: bool = Field(description="True if any capability filters are configured")
    discovered_capabilities: Optional[List[MCPDiscoveredCapability]] = Field(
        None, description="Capabilities discovered from the MCP server (None if not yet discovered)"
    )
    discovered_at: Optional[str] = Field(None, description="ISO timestamp of when capabilities were discovered")
    global_governance_defaults: Optional[dict] = Field(
        None,
        description="Effective MCP global governance defaults from group hierarchy. "
                    "Provides default_tool_access, default_resource_access, etc. for capabilities without explicit filters."
    )

class MCPCapabilityFilterUpdate(BaseModel):
    """Bulk update capability filters for a group+server"""

    model_config = ConfigDict(extra="forbid")
    capabilities: List[MCPCapabilityInfo] = Field(
        ..., description="Full set of capability filters. Replaces all existing filters for this group+server."
    )


# ============================================================================
# RESOURCE GROUP MEMBERSHIP MODELS
# ============================================================================

class GroupMembershipAdd(BaseModel):
    """Schema for adding users to a group"""
    user_ids: List[str] = Field(..., description="List of user IDs to add", min_items=1)
    role: Optional[str] = Field(None, description="Optional role within the resource group")
    make_primary: bool = Field(
        False,
        description=(
            "If True, set this group as each user's primary group (active runtime "
            "scope). If False and the user has no primary group yet, it is still "
            "set automatically — first joined group is always primary."
        ),
    )


class SetPrimaryGroupRequest(BaseModel):
    """Explicit change of a user's primary group."""

    model_config = ConfigDict(extra="forbid")
    group_id: str = Field(..., description="Target resource group ID (user must be a member)")


class GroupMembershipRemove(BaseModel):
    """Schema for removing users from a group"""
    user_ids: List[str] = Field(..., description="List of user IDs to remove", min_items=1)


class GroupMemberInfo(BaseModel):
    """Schema for group member information"""
    user_id: str
    username: str
    email: Optional[str]
    display_name: Optional[str]
    avatar_url: Optional[str] = None
    role: Optional[str]
    joined_at: str
    # Hierarchy support
    is_inherited: bool = Field(default=False, description="Whether member is inherited from parent group")
    source_group_id: Optional[str] = Field(None, description="ID of the group this member belongs to (if inherited)")
    source_group_name: Optional[str] = Field(None, description="Name of the group this member belongs to (if inherited)")
    # Primary-group indicator — true when this resource group is the user's
    # primary_group_id (active runtime scope).
    is_primary: bool = Field(default=False, description="Whether this group is the user's primary (active) group")


class GroupMemberListResponse(BaseModel):
    """Schema for list of group members"""
    group_id: str
    group_name: str
    members: List[GroupMemberInfo]
    total: int


class ResourceGroupMembershipResponse(BaseModel):
    """Schema for user's group memberships"""
    user_id: str
    username: str
    groups: List[Dict[str, Any]]
    total_groups: int

# ============================================================================
# API KEY MODELS (for Resource Group Access)
# ============================================================================

class APIKeyCreate(BaseModel):
    """Request model for creating a new API key for a resource group"""

    name: str = Field(..., min_length=1, max_length=200, description="Human-readable name")
    description: Optional[str] = Field(None, max_length=1000, description="Optional description")

    # Access mode (UI uses 'shared' or 'user', we map to scope internally)
    access_mode: str = Field('shared', description="'shared' (global) or 'user' (user-specific)")
    user_id: Optional[int] = Field(None, description="User ID if access_mode='user'")

    # Expiration
    expires_at: Optional[datetime] = Field(None, description="When key expires")
    enabled: Optional[bool] = Field(True, description="Whether key is enabled")

    # Optional agent-identity binding: calls made with this key are attributed
    # to the agent principal and pass through its agent-authz grants.
    agent_id: Optional[str] = Field(
        None, description="Agent identity to bind this key to (actor attribution)")

    @validator('access_mode')
    def validate_access_mode(cls, v):
        if v not in ['shared', 'user']:
            raise ValueError("access_mode must be 'shared' or 'user'")
        return v

    @validator('user_id')
    def validate_user_id(cls, v, values):
        if values.get('access_mode') == 'user' and not v:
            raise ValueError("user_id is required when access_mode='user'")
        return v


class APIKeyUpdate(BaseModel):
    """Request model for updating an existing API key"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)

    # Expiration. Omit to leave unchanged; null clears it (refused where the
    # group's identity policy sets a key lifetime ceiling, and for a
    # personal key).
    expires_at: Optional[datetime] = None

    # Status
    enabled: Optional[bool] = None

    # Agent-identity binding. Omit to leave unchanged; pass "" to clear.
    agent_id: Optional[str] = None


class APIKeyRevokeRequest(BaseModel):
    """Request model for revoking a key"""

    reason: Optional[str] = Field(None, max_length=500, description="Reason for revocation")


class APIKeyResponse(BaseModel):
    """Response model for a single API key (detailed)"""

    id: str
    tenant_id: str
    key_prefix: str
    name: str
    description: Optional[str]

    # Scope (return both scope and access_mode for compatibility)
    scope: str
    access_mode: Optional[str] = None  # Derived from scope
    user_id: Optional[int]
    user_email: Optional[str]
    agent_id: Optional[str] = None
    group_id: Optional[str]
    group_name: Optional[str]
    group_type: Optional[str]

    # Expiration
    expires_at: Optional[datetime]
    is_expired: bool

    # Status
    enabled: bool
    revoked: bool
    revoked_at: Optional[datetime]
    revoked_by: Optional[str]
    revoked_reason: Optional[str]
    is_valid: bool

    # Usage
    last_used_at: Optional[datetime]
    last_used_by_user_id: Optional[int]
    usage_count: int

    # Metadata
    created_at: datetime
    created_by: Optional[str]
    updated_at: datetime
    updated_by: Optional[str]

    class Config:
        from_attributes = True


class APIKeySummary(BaseModel):
    """Response model for API key (summary for lists)"""

    id: str
    key_prefix: str
    name: str
    description: Optional[str] = None
    scope: str
    access_mode: Optional[str] = None  # Derived from scope
    user_id: Optional[int] = None
    user_email: Optional[str] = None
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    agent_id: Optional[str] = None
    enabled: bool
    revoked: bool
    is_valid: bool
    is_expired: bool
    last_used_at: Optional[datetime]
    usage_count: int
    expires_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class APIKeyCreatedResponse(BaseModel):
    """Response model for newly created key (includes the actual key)"""

    id: str
    key_value: str  # Full API key - ONLY shown once!
    full_key: Optional[str] = None  # Alias for key_value for compatibility
    key: Optional[str] = None  # Another alias
    key_prefix: str
    name: str
    description: Optional[str]
    scope: str
    access_mode: Optional[str] = None  # Derived from scope
    user_id: Optional[int] = None
    user_email: Optional[str] = None
    group_id: Optional[str]
    group_name: Optional[str]
    agent_id: Optional[str] = None
    enabled: bool
    expires_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class APIKeyListResponse(BaseModel):
    """Response model for list of keys"""

    keys: List[APIKeySummary]
    total: int
    page: Optional[int] = 1
    page_size: Optional[int] = 100

    class Config:
        from_attributes = True


# ============================================================================
# GROUP-SPECIFIC API KEY MODELS
# ============================================================================

class GroupAPIKeysResponse(BaseModel):
    """Response for keys scoped to a specific group"""
    group_id: str
    group_name: str
    group_type: Optional[str]
    keys: List[APIKeySummary]
    total: int


class GroupAPIKeyUsageResponse(BaseModel):
    """Response for usage stats of group-scoped keys"""
    group_id: str
    group_name: str
    total_keys: int
    active_keys: int
    total_usage: int
    total_tokens_consumed: int
    keys_usage: List[Dict[str, Any]]  # Per-key usage details

# ============================================================================
# USAGE ANALYTICS MODELS
# ============================================================================

class UsageSummary(BaseModel):
    """Top-level usage summary"""
    total_requests: int = 0
    total_errors: int = 0
    error_rate: float = 0.0
    total_cost: float = 0.0
    unique_users: int = 0
    descendant_groups: int = 0  # Only populated when include_hierarchy=true


class ModelUsageInfo(BaseModel):
    """Per-model usage breakdown"""
    model_name: str
    provider: Optional[str] = None
    request_count: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0


class ToolUsageInfo(BaseModel):
    """Per-tool usage breakdown"""
    server_name: str
    tool_name: str
    call_count: int = 0
    total_execution_time_ms: int = 0
    error_count: int = 0


class LLMUsageMetrics(BaseModel):
    """Aggregated LLM usage metrics"""
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_cost: float = 0.0
    prompt_cost: float = 0.0
    completion_cost: float = 0.0
    avg_tokens_per_request: float = 0.0
    avg_response_time_ms: float = 0.0
    top_models: List[ModelUsageInfo] = []


class MCPUsageMetrics(BaseModel):
    """Aggregated MCP usage metrics"""
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    total_tool_calls: int = 0
    total_cost: float = 0.0
    total_data_bytes: int = 0
    avg_execution_time_ms: float = 0.0
    top_tools: List[ToolUsageInfo] = []


class SkillUsageInfo(BaseModel):
    """Per-skill usage rollup for the Skills panel."""
    skill_name: str
    skill_id: Optional[str] = None
    call_count: int = 0
    total_execution_time_ms: int = 0
    error_count: int = 0


class SkillUsageMetrics(BaseModel):
    """Aggregated Agent-Skill execution metrics (usage_logs resource_type='skill')."""
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    total_cost: float = 0.0
    avg_execution_time_ms: float = 0.0
    top_skills: List[SkillUsageInfo] = []


class AgentCardUsageInfo(BaseModel):
    """Per-card usage rollup for the A2A panel."""
    card_id: str
    card_name: str
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    total_duration_ms: float = 0.0


class AgentCapabilityUsageInfo(BaseModel):
    """Per-capability usage rollup. ``capability_name`` is the
    dotted A2A capability the caller invoked (e.g. ``hello``)."""
    card_id: Optional[str] = None
    card_name: Optional[str] = None
    capability_name: str
    call_count: int = 0
    error_count: int = 0


class A2AUsageMetrics(BaseModel):
    """Aggregated A2A (Agent-to-Agent) usage metrics.

    Unlike MCP/LLM, A2A traffic is currently only written to
    ``proxy_logs`` (not the dedicated ``usage_logs`` aggregation
    table), so this view is computed directly from proxy_logs rows
    where ``proxy_type='a2a'``. Mirror of ``MCPUsageMetrics`` so the
    admin UI's Agent Usage panel can reuse the MCP panel layout.
    """
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    avg_duration_ms: float = 0.0
    total_data_bytes: int = 0
    top_cards: List[AgentCardUsageInfo] = []
    top_capabilities: List[AgentCapabilityUsageInfo] = []


class MediaModeMetrics(BaseModel):
    """Per-mode roll-up for the LLM Media usage panel.

    Each media mode bills in its own unit (images, characters,
    seconds), so a single token-based row would either skew analytics
    or hide the work entirely. We carry the typed counter for the
    mode plus the standard request_count/cost so the admin UI can
    show ``$X across N images / N chars / N seconds`` per mode.
    """
    request_count: int = 0
    cost_usd: float = 0.0
    # Typed billing-unit counter — only one of these is meaningful
    # for any given mode; the others stay at 0 (the response trims
    # absent counters via `model_dump(exclude_unset=...)` is not
    # used here, so we keep all four declared and let the UI render
    # the relevant one).
    images_generated: int = 0
    audio_chars: int = 0
    audio_seconds_input: float = 0.0
    video_seconds_output: float = 0.0


class MediaUsageMetrics(BaseModel):
    """Aggregated LLM Media (image / TTS / STT / video) usage.

    Like A2A, media traffic only lives in ``proxy_logs`` — the
    typed counter columns (``media_*``) are populated by the four
    media routes via ``record_multimodal_outcome``. The token
    columns are NULL for these rows, so ``llm_economics`` (which
    sums tokens) does not double-count them.
    """
    request_count: int = 0
    estimated_cost_usd: float = 0.0
    modes: Dict[str, MediaModeMetrics] = {}


class DailyUsageBreakdown(BaseModel):
    """Single day usage breakdown for charting"""
    date: str
    llm_requests: int = 0
    mcp_requests: int = 0
    a2a_requests: int = 0
    skill_requests: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    error_count: int = 0


class GroupUsageResponse(BaseModel):
    """Complete usage analytics response for a resource group"""
    group_id: str
    group_name: str
    period: str
    include_hierarchy: bool = False
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    # Aggregated summary
    summary: UsageSummary

    # Resource-type breakdowns
    llm: LLMUsageMetrics
    mcp: MCPUsageMetrics
    skill: SkillUsageMetrics = SkillUsageMetrics()
    a2a: A2AUsageMetrics = A2AUsageMetrics()
    media: MediaUsageMetrics = MediaUsageMetrics()

    # Daily data for charts
    daily_breakdown: List[DailyUsageBreakdown] = []

    class Config:
        from_attributes = True



# ============================================================================
# USAGE ALERT MODELS
# ============================================================================

class UsageAlertInfo(BaseModel):
    """Individual usage alert information"""
    id: str
    resource_type: str = Field(..., description="'llm' or 'mcp'")
    group_id: str
    group_name: Optional[str] = None
    alert_type: str = Field(..., description="'warning', 'hard_stop', or 'resumed'")
    metric: str = Field(..., description="e.g. 'daily_tokens', 'monthly_budget', 'daily_mcp_calls'")
    threshold_pct: int = Field(..., description="Threshold percentage crossed (e.g. 80, 90, 100)")
    current_value: int = Field(0, description="Current usage value (tokens, requests, etc.)")
    current_value_float: Optional[float] = Field(None, description="Current value for cost metrics (USD)")
    limit_value: int = Field(0, description="Configured limit value")
    limit_value_float: Optional[float] = Field(None, description="Configured limit for cost metrics (USD)")
    utilization_pct: float = Field(..., description="Actual utilization percentage")
    period: str = Field(..., description="'daily' or 'monthly'")
    period_start: Optional[str] = None
    status: str = Field(..., description="'active', 'acknowledged', or 'resolved'")
    message: Optional[str] = None
    triggered_by_request_id: Optional[str] = None
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[str] = None
    resolved_at: Optional[str] = None
    resolution_note: Optional[str] = None
    created_at: Optional[str] = None

    class Config:
        from_attributes = True


class GroupAlertResponse(BaseModel):
    """Response for usage alerts listing — scoped to a single group"""
    group_id: str
    group_name: str
    alerts: List[UsageAlertInfo]
    total_count: int
    active_count: int = 0
    hard_stop_count: int = 0

    class Config:
        from_attributes = True


class TenantAlertsResponse(BaseModel):
    """Response for tenant-wide usage alerts listing"""
    tenant_id: str
    alerts: List[UsageAlertInfo]
    total_count: int
    active_count: int = 0
    hard_stop_count: int = 0
    groups_affected: int = 0

    class Config:
        from_attributes = True


class AlertAcknowledgeResponse(BaseModel):
    """Response for alert acknowledgement"""
    id: str
    status: str
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[str] = None
    message: Optional[str] = None

    class Config:
        from_attributes = True


# Allow forward references for recursive model
GroupHierarchyTree.model_rebuild()