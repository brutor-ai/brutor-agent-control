# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skill Pydantic Models for API Requests/Responses

Covers:
- Skill CRUD (create, update, detail, list)
- Lifecycle transitions (publish, deprecate, archive, validate)
- Group assignment (via resource_group_agent_skills association)
- Execution responses (status, detail)
- Search & filtering

Follows the agentskills.io specification for field naming and constraints.

Access control:
- Skills are assigned to Resource Groups via the resource_group_agent_skills
  association table (same pattern as LLM models and server configs).
- Governance (limits, approval, execution mode) lives on the ResourceGroup.
- There is no separate binding concept.
"""

import re
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator, ConfigDict

from app.services import release_scope


# =============================================================================
# Shared Validators
# =============================================================================

SKILL_NAME_PATTERN = re.compile(r'^[a-z][a-z0-9-]{0,62}[a-z0-9]$')

ALLOWED_SKILL_STATUSES = ['draft', 'validated', 'published', 'deprecated', 'archived']

ALLOWED_EXECUTION_STATUSES = [
    'pending', 'running', 'completed', 'failed', 'cancelled', 'timed_out'
]

ALLOWED_APPROVAL_STATUSES = [
    'not_required', 'pending', 'approved', 'rejected', 'expired'
]


def _validate_skill_name(v: str) -> str:
    """
    Validate skill name follows agentskills.io spec:
    - 1-64 chars
    - lowercase a-z, digits, hyphens
    - Must start with letter, end with letter or digit
    """
    if not SKILL_NAME_PATTERN.match(v):
        raise ValueError(
            "Skill name must be 2-64 chars, lowercase letters/digits/hyphens, "
            "start with a letter, end with a letter or digit. "
            "Example: 'query-sales-data', 'generate-report'"
        )
    return v


# =============================================================================
# Nested Response Models
# =============================================================================

class AgentSkillGroupInfo(BaseModel):
    """Nested group information in skill responses"""
    id: str
    name: str
    display_name: Optional[str] = None
    group_type: Optional[str] = None
    is_active: bool
    parent_group_id: Optional[str] = None

    class Config:
        from_attributes = True


class SkillMetadataInfo(BaseModel):
    """
    Gateway extensions stored in the spec's metadata JSON field.
    All fields optional — progressive enhancement.
    """
    required_mcp_servers: Optional[List[str]] = Field(
        None, description="ServerConfig IDs the skill needs"
    )
    required_llm_models: Optional[List[str]] = Field(
        None, description="LLModel or RoutingGroup IDs the skill needs"
    )
    oauth_scopes_required: Optional[List[str]] = Field(
        None, description="OAuth scopes needed for execution"
    )
    rate_limit_tier: Optional[str] = Field(
        None, description="Rate limit tier: 'standard', 'premium', 'enterprise'"
    )
    approval_required: Optional[bool] = Field(
        None, description="Default approval requirement"
    )
    cost_estimate_usd: Optional[float] = Field(
        None, ge=0, description="Estimated cost per execution in USD"
    )
    tags: Optional[List[str]] = Field(
        None, description="Categorization tags for catalog filtering"
    )
    icon_name: Optional[str] = Field(
        None, max_length=50, description="Lucide icon name for UI display"
    )
    category: Optional[str] = Field(
        None, max_length=100, description="Skill category for catalog grouping"
    )


class ExecutionSummary(BaseModel):
    """Nested execution summary in responses"""
    id: str
    skill_name: Optional[str] = None
    skill_version: Optional[str] = None
    execution_status: str
    approval_status: str
    duration_ms: Optional[int] = None
    total_tokens: Optional[int] = None
    estimated_cost_usd: Optional[float] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    started_at: str
    completed_at: Optional[str] = None

    class Config:
        from_attributes = True


# =============================================================================
# Skill Create / Update
# =============================================================================

class AgentSkillCreate(BaseModel):
    """
    Request model for creating an Agent Skill

    Follows agentskills.io spec for field naming:
    - name: directory name convention (lowercase, hyphens)
    - description: human-readable, 1-1024 chars
    - skill_md_content: full SKILL.md body (below frontmatter)
    - metadata: gateway extensions (see SkillMetadataInfo)
    """
    # Spec fields
    name: str = Field(
        ..., min_length=2, max_length=64,
        description="Skill name: lowercase a-z, digits, hyphens. "
                    "Must start with letter. Example: 'query-sales-data'"
    )
    description: str = Field(
        ..., min_length=1, max_length=1024,
        description="Human-readable description of what this skill does"
    )
    skill_md_content: str = Field(
        ..., min_length=1,
        description="Full SKILL.md body below the YAML frontmatter (spec recommends <5000 tokens)"
    )

    # Spec optional fields
    license: Optional[str] = Field(
        None, max_length=100,
        description="License identifier: 'Apache-2.0', 'MIT', 'proprietary'"
    )
    compatibility: Optional[List[str]] = Field(
        None,
        description="Compatible agent identifiers"
    )
    allowed_tools: Optional[List[str]] = Field(
        None,
        description="Pre-approved tool names the skill may invoke"
    )
    tenant_limits: Optional[Dict[str, Any]] = Field(
        None, description="Tenant-wide default skill limits (min-wins with RG)."
    )
    tenant_governance: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "A policy on the skill itself. Not available in this release: only an empty value "
            "is accepted. A group's skill governance, the skill's metadata and its assignment to "
            "a group are what the gateway applies."
        ),
    )

    # Versioning
    version: str = Field(
        default="1.0.0", max_length=50,
        description="Semantic version (major.minor.patch)"
    )

    # Gateway extensions (stored as metadata JSON)
    metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="Gateway metadata extensions (see SkillMetadataInfo for schema)"
    )

    # Group assignments (via resource_group_agent_skills association table)
    group_ids: Optional[List[str]] = Field(
        None,
        description="Resource Group IDs to assign this skill to. "
                    "Empty/null = assign to Default group automatically."
    )

    # How this skill is consumed over MCP
    disclosure_mode: str = Field(
        default="progressive",
        description="progressive (L1→L3 model orchestration, the supported path), "
                    "execute (legacy one-shot, deprecated), or both (transition)."
    )

    # Allow a group member to download this skill for local install
    allow_local_export: bool = Field(
        default=False,
        description="When true, group members may export this skill's bundle for "
                    "install into a local agent (default off)."
    )

    # Validators
    @field_validator('tenant_governance')
    @classmethod
    def _no_skill_policy(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if value:
            raise ValueError(release_scope.SKILL_POLICY)
        return value

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _validate_skill_name(v)

    @field_validator('disclosure_mode')
    @classmethod
    def validate_disclosure_mode(cls, v: str) -> str:
        allowed = {"progressive", "execute", "both"}
        if v not in allowed:
            raise ValueError(f"disclosure_mode must be one of {sorted(allowed)}")
        return v

    @field_validator('version')
    @classmethod
    def validate_version(cls, v: str) -> str:
        parts = v.split('.')
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError("Version must be semantic: 'major.minor.patch' (e.g. '1.0.0')")
        return v

    @field_validator('skill_md_content')
    @classmethod
    def validate_skill_md(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("SKILL.md content cannot be empty")
        if len(v) > 500_000:
            raise ValueError(
                f"SKILL.md content too large ({len(v):,} chars). "
                f"Spec recommends <5000 tokens. Maximum: 500,000 chars."
            )
        return v


class AgentSkillUpdate(BaseModel):
    """
    Request model for updating an Agent Skill (partial update)

    Only fields included in the request body are applied.
    Published skills cannot be updated (create a new version instead).
    """

    model_config = ConfigDict(extra="forbid")
    # `name` and `version` are editable on a DRAFT skill. Both are
    # rejected outright on a published one by the immutability guard in
    # the endpoint, so the pair stays stable for anything a client could
    # already be depending on. They were previously absent here while the
    # admin form sent them, so an edit reported success and changed
    # nothing — a silent no-op is worse than a refusal.
    name: Optional[str] = Field(
        None, min_length=2, max_length=64,
        description="Skill name: lowercase a-z, digits, hyphens."
    )
    version: Optional[str] = Field(
        None, max_length=50,
        description="Semantic version (major.minor.patch)"
    )
    description: Optional[str] = Field(
        None, min_length=1, max_length=1024,
        description="Updated description"
    )
    skill_md_content: Optional[str] = Field(
        None, min_length=1,
        description="Updated SKILL.md body"
    )
    license: Optional[str] = Field(None, max_length=100)
    compatibility: Optional[List[str]] = None
    allowed_tools: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    allow_local_export: Optional[bool] = None
    tenant_limits: Optional[Dict[str, Any]] = None
    tenant_governance: Optional[Dict[str, Any]] = Field(
        None, description="A policy on the skill itself. Not available in this release: only an empty value is accepted."
    )
    disclosure_mode: Optional[str] = Field(
        None,
        description="Consumption mode over MCP: progressive | execute | both."
    )

    @field_validator('tenant_governance')
    @classmethod
    def _no_skill_policy(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if value:
            raise ValueError(release_scope.SKILL_POLICY)
        return value

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _validate_skill_name(v)

    @field_validator('version')
    @classmethod
    def validate_version(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        parts = v.split('.')
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError("Version must be semantic: 'major.minor.patch' (e.g. '1.0.0')")
        return v

    @field_validator('disclosure_mode')
    @classmethod
    def validate_disclosure_mode(cls, v):
        if v is not None and v not in {"progressive", "execute", "both"}:
            raise ValueError("disclosure_mode must be one of ['both', 'execute', 'progressive']")
        return v

    # Group assignments (replaces existing)
    group_ids: Optional[List[str]] = Field(
        None,
        description="Updated group assignments (replaces existing). "
                    "Null/omitted = no change."
    )

    @field_validator('skill_md_content')
    @classmethod
    def validate_skill_md(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("SKILL.md content cannot be empty")
        if len(v) > 500_000:
            raise ValueError("SKILL.md content too large")
        return v


# =============================================================================
# Lifecycle Requests
# =============================================================================

class SkillPublishRequest(BaseModel):
    """Publish a skill (DRAFT/VALIDATED -> PUBLISHED)"""
    skill_id: str = Field(..., description="Skill ID to publish")


class SkillDeprecateRequest(BaseModel):
    """Deprecate a published skill"""
    skill_id: str = Field(..., description="Skill ID to deprecate")
    reason: str = Field(
        ..., min_length=1, max_length=1024,
        description="Deprecation reason (e.g. 'Superseded by v2.0.0')"
    )


class SkillValidateRequest(BaseModel):
    """Trigger validation on a skill"""
    skill_id: str = Field(..., description="Skill ID to validate")


class SkillBulkDeleteRequest(BaseModel):
    """Delete several skills in one call.

    Published skills are skipped (not an error) — they must be
    deprecated then archived before deletion, matching the single
    DELETE guard. The response reports which ids were deleted and
    which were skipped with a reason."""
    skill_ids: List[str] = Field(
        ..., min_length=1, max_length=500,
        description="IDs of the skills to delete (1-500 per call)"
    )


class SkillBulkDeleteSkipped(BaseModel):
    """One skill that the bulk delete could not remove."""
    skill_id: str = Field(..., description="Skill ID that was skipped")
    reason: str = Field(..., description="Why it was skipped")


class SkillBulkDeleteResponse(BaseModel):
    """Outcome of a bulk delete."""
    deleted: List[str] = Field(default_factory=list, description="IDs successfully deleted")
    skipped: List[SkillBulkDeleteSkipped] = Field(
        default_factory=list, description="Skills that were not deleted, with reasons"
    )
    deleted_count: int = Field(0, description="Number of skills deleted")
    skipped_count: int = Field(0, description="Number of skills skipped")


# =============================================================================
# Skill Response Models
# =============================================================================

class AgentSkillResponse(BaseModel):
    """
    Full skill response for admin API.
    Matches AgentSkill.to_dict() output.
    """
    # Identity
    id: str
    tenant_id: str
    name: str
    description: str
    version: str
    status: str  # SkillStatus value
    enabled: bool
    allow_local_export: bool = False  # local-install gate
    disclosure_mode: str = "progressive"  # consumption mode over MCP

    # Spec fields
    license: Optional[str] = None
    compatibility: Optional[List[str]] = None
    allowed_tools: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    tenant_limits: Optional[Dict[str, Any]] = None
    tenant_governance: Optional[Dict[str, Any]] = None

    # Content (optionally included)
    skill_md_content: Optional[str] = None
    skill_md_hash: str

    # Publishing audit
    published_at: Optional[str] = None
    published_by: Optional[str] = None
    deprecated_at: Optional[str] = None
    deprecation_reason: Optional[str] = None

    # Validation
    validation_errors: Optional[List[str]] = None
    last_validated_at: Optional[str] = None

    # Metrics
    execution_count: int = 0
    last_executed_at: Optional[str] = None

    # Access control — group assignment via resource_group_agent_skills
    group_ids: List[str] = []
    groups: Optional[List[AgentSkillGroupInfo]] = None

    # Audit
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class AgentSkillCatalogEntry(BaseModel):
    """
    Lightweight catalog entry for skill discovery (Progressive Disclosure Level 1).
    ~50-100 tokens per skill. Matches AgentSkill.to_catalog_dict().
    """
    id: str
    name: str
    description: str
    version: str
    status: str
    license: Optional[str] = None
    tags: Optional[List[str]] = None
    category: Optional[str] = None
    icon_name: Optional[str] = None
    cost_estimate_usd: Optional[float] = None
    approval_required: bool = False
    compatibility: Optional[List[str]] = None
    execution_count: int = 0

    class Config:
        from_attributes = True


# =============================================================================
# Execution Response Models
# =============================================================================

class AgentSkillExecutionResponse(BaseModel):
    """Full execution detail response"""
    id: str
    tenant_id: str
    skill_id: Optional[str] = None
    group_id: Optional[str] = None
    user_id: Optional[int] = None

    # Denormalized identifiers
    skill_name: Optional[str] = None
    skill_version: Optional[str] = None
    group_name: Optional[str] = None
    username: Optional[str] = None

    # Correlation
    request_id: str
    correlation_id: Optional[str] = None
    parent_execution_id: Optional[str] = None

    # Status
    execution_status: str
    started_at: str
    running_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None

    # Resources consumed
    mcp_servers_used: Optional[List[Dict[str, Any]]] = None
    llm_models_used: Optional[List[Dict[str, Any]]] = None
    total_tokens: Optional[int] = None
    total_mcp_calls: Optional[int] = None
    estimated_cost_usd: Optional[float] = None

    # Approval
    approval_status: str
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    approval_notes: Optional[str] = None

    # Error
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None

    # Context snapshot
    execution_context: Optional[Dict[str, Any]] = None

    created_at: str

    class Config:
        from_attributes = True


# =============================================================================
# List / Detail Response Wrappers
# =============================================================================

class AgentSkillLimits(BaseModel):
    """The skill size limits, one constant each (`app/constants/agent_skill.py`)."""
    skill_max_bytes: int = Field(
        ..., description="Most bytes one skill may unpack to: the sum of its files, uncompressed (30 MB)"
    )
    archive_max_bytes: int = Field(..., description="Most bytes one imported zip may unpack to (300 MB)")
    archive_max_skills: int = Field(..., description="Most skills one imported zip may hold (50)")


class AgentSkillListResponse(BaseModel):
    """Response for listing skills"""
    skills: Dict[str, AgentSkillResponse] = Field(
        ..., description="Dictionary of skills by ID"
    )
    total: int = Field(..., description="Total number of skills")
    limits: AgentSkillLimits = Field(..., description="The skill size limits an import is held to")


class AgentSkillDetailResponse(BaseModel):
    """Response for single skill detail"""
    skill: AgentSkillResponse = Field(..., description="Skill details")
    auto_bound_server_group_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Resource Group IDs that had the system Agent Skill MCP "
            "server auto-bound as a side effect of this call. Empty / "
            "null when no auto-binding happened (server was already "
            "bound, or the call didn't touch group assignments). The "
            "admin UI surfaces this as an informational toast so the "
            "side effect is visible. Absent on responses that pre-date "
            "the auto-binding feature."
        ),
    )


class AgentSkillExecutionListResponse(BaseModel):
    """Response for listing executions"""
    executions: List[ExecutionSummary] = Field(
        ..., description="List of execution summaries"
    )
    total: int = Field(..., description="Total number of executions")


class AgentSkillExecutionDetailResponse(BaseModel):
    """Response for single execution detail"""
    execution: AgentSkillExecutionResponse = Field(
        ..., description="Execution details"
    )


class AgentSkillCatalogResponse(BaseModel):
    """Response for skill catalog listing (lightweight)"""
    skills: List[AgentSkillCatalogEntry] = Field(
        ..., description="Catalog entries"
    )
    total: int = Field(..., description="Total skills available")


# =============================================================================
# Group Assignment Models
# =============================================================================

class SkillGroupAssignment(BaseModel):
    """Request for assigning skill to groups"""
    group_ids: List[str] = Field(
        ..., description="Group IDs to assign", min_length=1
    )


class SkillGroupUnassignment(BaseModel):
    """Request for removing skill from groups"""
    group_ids: List[str] = Field(
        ..., description="Group IDs to remove", min_length=1
    )


# =============================================================================
# Validation Response
# =============================================================================

class SkillValidationResponse(BaseModel):
    """Response from skill validation"""
    skill_id: str
    skill_name: str
    valid: bool
    errors: List[str] = Field(default_factory=list)
    validated_at: str


# =============================================================================
# Search Models
# =============================================================================

class AgentSkillSearchRequest(BaseModel):
    """Search/filter request for skills"""

    # Text search (partial, case-insensitive)
    name: Optional[str] = Field(None, max_length=64)
    description: Optional[str] = Field(None, max_length=1024)

    # Exact match filters
    status: Optional[str] = Field(None, description="Filter by status")
    category: Optional[str] = Field(None, max_length=100)
    enabled: Optional[bool] = None

    # Tag filtering
    tags: Optional[List[str]] = Field(
        None, description="Filter by tags (OR logic)"
    )

    # Pagination
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)

    # Sorting
    sort_by: str = Field(
        default="created_at",
        description="Sort field: 'name', 'created_at', 'updated_at', 'execution_count'"
    )
    sort_order: str = Field(default="desc", description="'asc' or 'desc'")

    @field_validator('status')
    @classmethod
    def validate_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v.lower() not in ALLOWED_SKILL_STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(ALLOWED_SKILL_STATUSES)}")
        return v.lower()

    @field_validator('sort_by')
    @classmethod
    def validate_sort_by(cls, v: str) -> str:
        allowed = ['name', 'created_at', 'updated_at', 'execution_count', 'status', 'version']
        if v not in allowed:
            raise ValueError(f"sort_by must be one of: {', '.join(allowed)}")
        return v

    @field_validator('sort_order')
    @classmethod
    def validate_sort_order(cls, v: str) -> str:
        if v.lower() not in ('asc', 'desc'):
            raise ValueError("sort_order must be 'asc' or 'desc'")
        return v.lower()


class AgentSkillSearchResponse(BaseModel):
    """Paginated search response"""
    skills: List[AgentSkillResponse] = Field(..., description="Matching skills")
    total: int = Field(..., description="Total matching results")
    offset: int
    limit: int


# =============================================================================
# Execution Request (for proxy endpoint)
# =============================================================================

class SkillExecutionRequest(BaseModel):
    """
    Request to execute a skill through the proxy.

    The agent (or user) sends this to trigger skill execution.
    The gateway resolves the group, validates access via
    resource_group_agent_skills, and orchestrates.
    """
    skill_id: Optional[str] = Field(
        None,
        description="Skill ID to execute (lookup by ID)"
    )
    skill_name: Optional[str] = Field(
        None, max_length=64,
        description="Skill name to execute (lookup by name — picks latest published version)"
    )
    input_params: Optional[Dict[str, Any]] = Field(
        None,
        description="Input parameters for the skill (passed to SKILL.md execution context)"
    )
    group_id: Optional[str] = Field(
        None,
        description="Target Resource Group (overrides auto-resolution from auth context)"
    )
    correlation_id: Optional[str] = Field(
        None,
        description="Correlation ID for linking chained skill executions"
    )
    parent_execution_id: Optional[str] = Field(
        None,
        description="Parent execution ID for skill composition tracking"
    )

    @field_validator('skill_name')
    @classmethod
    def validate_skill_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return _validate_skill_name(v)
        return v


class SkillExecutionStatusResponse(BaseModel):
    """Response after initiating a skill execution"""
    execution_id: str
    skill_id: str
    skill_name: str
    execution_status: str
    approval_status: str
    correlation_id: Optional[str] = None
    message: str = Field(
        ...,
        description="Human-readable status message "
                    "(e.g. 'Execution started', 'Pending approval')"
    )


# ============================================================================
# Resource Models (references/ directory)
# ============================================================================

def is_safe_skill_filename(name: str) -> bool:
    """A skill file's name is its path inside the skill's directory (the importer
    stores `scripts/run.py`), and the runner lays files out by it. It must stay
    inside that directory."""
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return False
    return ".." not in name.split("/")


class _SkillFileName(BaseModel):
    @field_validator("filename", check_fields=False)
    @classmethod
    def _filename_stays_in_the_skill(cls, v: str) -> str:
        if not is_safe_skill_filename(v):
            raise ValueError("filename must be a relative path inside the skill, with no '..' or backslash")
        return v


class AgentSkillResourceCreate(_SkillFileName):
    """Create a new resource file for a skill"""
    filename: str = Field(
        ..., min_length=1, max_length=255,
        description="Filename within references/ directory (e.g. 'REFERENCE.md')"
    )
    resource_type: str = Field(
        default="reference",
        description="Type: reference, form, schema, template, example, asset"
    )
    content: str = Field(
        ..., min_length=1, max_length=5_000_000,
        description="File content (text)"
    )
    mime_type: Optional[str] = Field(
        default=None,
        description="MIME type (e.g. 'text/markdown', 'application/json')"
    )

class AgentSkillResourceUpdate(BaseModel):
    """Update an existing resource file"""

    model_config = ConfigDict(extra="forbid")
    content: Optional[str] = Field(
        default=None, max_length=5_000_000,
        description="Updated file content"
    )
    resource_type: Optional[str] = Field(
        default=None,
        description="Updated type classification"
    )
    mime_type: Optional[str] = Field(
        default=None,
        description="Updated MIME type"
    )

class AgentSkillResourceResponse(BaseModel):
    """Single resource in API responses"""
    id: str
    skill_id: str
    filename: str
    resource_type: str
    content_hash: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    content: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class AgentSkillResourceListResponse(BaseModel):
    """List of resources for a skill"""
    resources: list
    total: int


# ============================================================================
# Script Models (scripts/ directory)
# ============================================================================

class AgentSkillScriptCreate(_SkillFileName):
    """Create a new script file for a skill"""
    filename: str = Field(
        ..., min_length=1, max_length=255,
        description="Filename within scripts/ directory (e.g. 'validate_input.py')"
    )
    description: Optional[str] = Field(
        default=None, max_length=1024,
        description="Human-readable description"
    )
    script_content: str = Field(
        ..., min_length=1, max_length=5_000_000,
        description="Full script source code"
    )
    language: str = Field(
        default="python",
        description="Script language: python, javascript, typescript, bash"
    )
    execution_mode: str = Field(
        default="sandbox",
        description="Security mode: sandbox, trusted, disabled"
    )
    args_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description="JSON Schema for skills__run_script args (L2 manifest)."
    )

class AgentSkillScriptUpdate(BaseModel):
    """Update an existing script file"""

    model_config = ConfigDict(extra="forbid")
    description: Optional[str] = Field(default=None, max_length=1024)
    script_content: Optional[str] = Field(default=None, max_length=5_000_000)
    language: Optional[str] = None
    execution_mode: Optional[str] = None
    args_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description="JSON Schema for skills__run_script args (L2 manifest)."
    )

class AgentSkillScriptResponse(BaseModel):
    """Single script in API responses"""
    id: str
    skill_id: str
    filename: str
    description: Optional[str] = None
    language: str
    execution_mode: str
    script_hash: str
    args_schema: Optional[Dict[str, Any]] = None
    script_content: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class AgentSkillScriptListResponse(BaseModel):
    """List of scripts for a skill"""
    scripts: list
    total: int


# ============================================================================
# Asset Models (assets/ directory)
# ============================================================================

class AgentSkillAssetCreate(_SkillFileName):
    """Create a new asset file for a skill"""
    filename: str = Field(
        ..., min_length=1, max_length=255,
        description="Filename within assets/ directory (e.g. 'invoice_template.docx')"
    )
    asset_type: str = Field(
        default="data",
        description="Type: template, image, data"
    )
    description: Optional[str] = Field(
        default=None, max_length=1024,
        description="Human-readable description"
    )
    content: str = Field(
        ..., min_length=1, max_length=10_000_000,
        description="File content (plain text or base64 for binary)"
    )
    mime_type: Optional[str] = Field(
        default=None,
        description="MIME type (e.g. 'text/csv', 'image/png', 'application/json')"
    )
    encoding: str = Field(
        default="utf-8",
        description="Content encoding: 'utf-8' for text, 'base64' for binary"
    )
    vars_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description="JSON Schema for skills__render_template vars (L2 manifest)."
    )

class AgentSkillAssetUpdate(BaseModel):
    """Update an existing asset file"""

    model_config = ConfigDict(extra="forbid")
    description: Optional[str] = Field(default=None, max_length=1024)
    content: Optional[str] = Field(
        default=None, max_length=10_000_000,
        description="Updated file content"
    )
    asset_type: Optional[str] = Field(
        default=None,
        description="Updated type classification"
    )
    mime_type: Optional[str] = Field(
        default=None,
        description="Updated MIME type"
    )
    encoding: Optional[str] = Field(
        default=None,
        description="Updated encoding"
    )
    vars_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description="JSON Schema for skills__render_template vars (L2 manifest)."
    )

class AgentSkillAssetResponse(BaseModel):
    """Single asset in API responses"""
    id: str
    skill_id: str
    filename: str
    description: Optional[str] = None
    asset_type: str
    content_hash: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    encoding: str = "utf-8"
    vars_schema: Optional[Dict[str, Any]] = None
    content: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

class AgentSkillAssetListResponse(BaseModel):
    """List of assets for a skill"""
    assets: list
    total: int
