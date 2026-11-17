/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import type { ApiResponse, ApiSendableBody } from './api/controlPlaneTypes';

/**
 * Agent Skill Types
 *
 * Matches the Pydantic models in app/api/v1/models/admin/agent_skill.py
 * and the database models in app/database/models/agent_skill.py
 *
 * Skills follow the agentskills.io specification:
 * - SKILL.md content (instructions for LLM agents)
 * - resources - fetch files from the skill execution
 * - scripts - run as part of the skill execution
 * - assets - fetch assets from the skill execution
 * - Lifecycle: draft → validated → published → deprecated → archived
 * - Execution tracking
 */

// ============================================================================
// Enums
// ============================================================================

export type SkillStatus = 'draft' | 'validated' | 'published' | 'deprecated' | 'archived';
export type ExecutionStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'timed_out';
/**
 * The approval status the Control Plane carries on an execution. The gateway
 * records every execution as `not_required`: a skill that needs approval is
 * held in Mission Control → Approvals and has no execution until it runs. The
 * console shows nothing of this field.
 */
export type ApprovalStatus = 'not_required' | 'pending' | 'approved' | 'rejected' | 'expired';

// ============================================================================
// Nested / Shared Types
// ============================================================================

export interface SkillGroupInfo {
    id: string;
    name: string;
    display_name?: string;
    group_type?: string;
    is_active: boolean;
    parent_group_id?: string;
}

export interface SkillMetadataInfo {
    skill_type?: 'standalone' | 'composite';
    required_mcp_servers?: string[];
    required_llm_models?: string[];
    oauth_scopes_required?: string[];
    rate_limit_tier?: 'standard' | 'premium' | 'enterprise';
    approval_required?: boolean;
    cost_estimate_usd?: number;
    tags?: string[];
    icon_name?: string;
    category?: string;
}

export interface ExecutionSummary {
    id: string;
    skill_name?: string;
    skill_version?: string;
    execution_status: ExecutionStatus;
    approval_status: ApprovalStatus;
    duration_ms?: number;
    total_tokens?: number;
    estimated_cost_usd?: number;
    error_type?: string;
    error_message?: string;
    started_at: string;
    completed_at?: string;
}

// ============================================================================
// Agent Skill (Response / Config)
// ============================================================================

/**
 * How a skill may be consumed over the Agent Skills MCP surface.
 * - progressive: L1→L3 model-orchestrated progressive disclosure, every layer governed
 * - execute: one call to the gateway's REST execute route runs the skill and returns its result
 * - both: progressive disclosure and the REST execute route
 */
export type SkillDisclosureMode = 'progressive' | 'execute' | 'both';

export const SKILL_DISCLOSURE_MODES: { value: SkillDisclosureMode; label: string; help: string }[] = [
    {
        value: 'progressive',
        label: 'Progressive disclosure',
        help: 'Model discovers, loads SKILL.md, then runs scripts/reads files through governed MCP tools.',
    },
    {
        value: 'execute',
        label: 'One-shot execute',
        help: 'One call to the REST execute route runs the skill and returns its result. The steps inside the skill are not governed one by one.',
    },
    {
        value: 'both',
        label: 'Both',
        help: 'Progressive disclosure and the one-shot REST execute route.',
    },
];

export interface AgentSkillConfig {
    // Identity
    id: string;
    tenant_id: string;
    name: string;
    description: string;
    version: string;
    status: SkillStatus;
    enabled: boolean;

    // How this skill is consumed over MCP.
    disclosure_mode?: SkillDisclosureMode;

    // Spec fields
    license?: string;
    compatibility?: string[];
    allowed_tools?: string[];
    metadata?: SkillMetadataInfo;

    // Tenant-wide default Limits / Policies (min-wins with each Resource Group).
    tenant_limits?: Record<string, any> | null;
    tenant_governance?: Record<string, any> | null;

    // Content
    skill_md_content?: string;   // Full SKILL.md body (optionally loaded)
    skill_md_hash: string;

    // Publishing audit
    published_at?: string;
    published_by?: string;
    deprecated_at?: string;
    deprecation_reason?: string;

    // Validation
    validation_errors?: string[];
    last_validated_at?: string;

    // Metrics
    execution_count: number;
    last_executed_at?: string;

    // Access control
    is_global: boolean;
    group_ids: string[];
    groups?: SkillGroupInfo[];

    // Audit
    created_by?: string;
    updated_by?: string;
    created_at?: string;
    updated_at?: string;
}

// ============================================================================
// Catalog Entry (lightweight, ~50-100 tokens)
// ============================================================================

export interface AgentSkillCatalogEntry {
    id: string;
    name: string;
    description: string;
    version: string;
    status: SkillStatus;
    license?: string;
    tags?: string[];
    category?: string;
    icon_name?: string;
    cost_estimate_usd?: number;
    approval_required: boolean;
    compatibility?: string[];
    execution_count: number;
}

// ============================================================================
// Request Types
// ============================================================================

export interface AgentSkillCreateRequest {
    // Required spec fields
    name: string;
    description: string;
    skill_md_content: string;

    // Optional spec fields
    license?: string;
    compatibility?: string[];
    allowed_tools?: string[];
    version?: string;  // defaults to "1.0.0"

    // Gateway extensions
    metadata?: Record<string, any>;
    enabled?: boolean;

    // Consumption mode (defaults to 'progressive').
    disclosure_mode?: SkillDisclosureMode;

    // Group assignments
    group_ids?: string[];
}

/** What this update may send: the Control Plane's request type. A key it does not name is refused. */
export type AgentSkillUpdateRequest = ApiSendableBody<'/v1/admin/agent-skills/{skill_id}', 'patch'>;

// ============================================================================
// Execution Types
// ============================================================================

export interface AgentSkillExecution {
    id: string;
    tenant_id: string;
    skill_id?: string;
    group_id?: string;
    user_id?: number;

    // Denormalized
    skill_name?: string;
    skill_version?: string;
    group_name?: string;
    username?: string;

    // Correlation
    request_id: string;
    correlation_id?: string;
    parent_execution_id?: string;

    // Status
    execution_status: ExecutionStatus;
    started_at: string;
    running_at?: string;
    completed_at?: string;
    duration_ms?: number;

    // Resources
    mcp_servers_used?: Record<string, any>[];
    llm_models_used?: Record<string, any>[];
    total_tokens?: number;
    total_mcp_calls?: number;
    estimated_cost_usd?: number;

    // Approval
    approval_status: ApprovalStatus;
    approved_by?: string;
    approved_at?: string;
    approval_notes?: string;

    // Error
    error_type?: string;
    error_message?: string;
    error_details?: Record<string, any>;

    // Context
    execution_context?: Record<string, any>;

    created_at: string;
}

// ============================================================================
// API Response Wrappers
// ============================================================================

export interface AgentSkillListResponse {
    skills: Record<string, AgentSkillConfig>;
    total: number;
    /** The size limits an import is held to (`AgentSkillLimits` of the contract). */
    limits?: AgentSkillLimits;
}

/** The skill size limits, in bytes and skills, as the list of skills states them. */
export type AgentSkillLimits = ApiResponse<'/v1/admin/agent-skills', 'get'>['limits'];

export interface AgentSkillDetailResponse {
    skill: AgentSkillConfig;
    /**
     * Resource Group IDs that had the system Agent Skill MCP server
     * auto-bound as a side effect of this call (create/update with
     * group_ids set). The system server is a runtime dependency of
     * every skill — when an admin assigns a skill to a group that
     * doesn't yet have the server bound, the backend binds it
     * automatically so portal users don't 403 on first call. The
     * admin UI reads this field after a successful create/update
     * and surfaces a toast explaining the side effect.
     *
     * Null / absent when no auto-binding happened (server was
     * already bound, or the call didn't touch group assignments).
     */
    auto_bound_server_group_ids?: string[] | null;
}

export interface AgentSkillExecutionListResponse {
    executions: ExecutionSummary[];
    total: number;
}

export interface AgentSkillCatalogResponse {
    skills: AgentSkillCatalogEntry[];
    total: number;
}

/**
 * What became of one skill of an import: `imported`, `refused` (`reason`
 * says why) or `skipped` (a skill of the name exists: `already_exists`).
 * Written by hand: the Control Plane answers the import without a response
 * model (`agent_skills.py`, `_skill_result`).
 */
export type AgentSkillImportStatus = 'imported' | 'refused' | 'skipped';

export interface AgentSkillImportResult {
    zip_filename: string;
    /** The skill's top-level folder in the zip: "" for a SKILL.md at the root, null when the whole zip was refused. */
    folder: string | null;
    status: AgentSkillImportStatus;
    success: boolean;
    skill_name: string;
    skill_id: string | null;
    error: string | null;
    /** For `refused` and `skipped`: not_a_zip, invalid_archive, archive_too_large, too_many_skills, no_skill, too_large, invalid_skill, quota_exceeded, already_exists. */
    reason: string | null;
    resources_count: number;
    scripts_count: number;
    skipped_files: string[];
}

/** One zip of an import: why it was refused as a whole, and the files in it that belong to no skill folder (never attached). */
export interface AgentSkillImportArchive {
    zip_filename: string;
    error: string | null;
    unassigned_files: string[];
}

export interface AgentSkillImportResponse {
    success: boolean;
    message: string;
    total_files: number;
    success_count: number;
    error_count: number;
    refused_count: number;
    skipped_count: number;
    results: AgentSkillImportResult[];
    archives: AgentSkillImportArchive[];
    limits?: AgentSkillLimits;
}

export interface SkillValidationResponse {
    skill_id: string;
    skill_name: string;
    valid: boolean;
    errors: string[];
    validated_at: string;
}

// ============================================================================
// Search Types
// ============================================================================

export interface AgentSkillSearchRequest {
    name?: string;
    description?: string;
    status?: SkillStatus;
    category?: string;
    enabled?: boolean;
    tags?: string[];
    offset?: number;
    limit?: number;
    sort_by?: 'name' | 'created_at' | 'updated_at' | 'execution_count' | 'status' | 'version';
    sort_order?: 'asc' | 'desc';
}

export interface AgentSkillSearchResponse {
    skills: AgentSkillConfig[];
    total: number;
    offset: number;
    limit: number;
}

// ============================================================================
// Resource Types (references/ directory)
// ============================================================================

export type SkillResourceType = 'reference' | 'form' | 'schema' | 'template' | 'example' | 'asset';

export interface AgentSkillResource {
    id: string;
    skill_id: string;
    filename: string;
    resource_type: SkillResourceType;
    content_hash: string;
    mime_type?: string;
    size_bytes?: number;
    content?: string;
    created_at?: string;
    updated_at?: string;
}

export interface AgentSkillResourceCreateRequest {
    filename: string;
    resource_type?: string;
    content: string;
    mime_type?: string;
}

/** What this update may send: the Control Plane's request type. A key it does not name is refused. */
export type AgentSkillResourceUpdateRequest = ApiSendableBody<'/v1/admin/agent-skills/{skill_id}/resources/{resource_id}', 'patch'>;

export interface AgentSkillResourceListResponse {
    resources: AgentSkillResource[];
    total: number;
}

// ============================================================================
// Script Types (scripts/ directory)
// ============================================================================

export type ScriptLanguage = 'python' | 'javascript' | 'typescript' | 'bash';
export type ScriptExecutionMode = 'sandbox' | 'trusted' | 'disabled';

export interface AgentSkillScript {
    id: string;
    skill_id: string;
    filename: string;
    description?: string;
    language: ScriptLanguage;
    execution_mode: ScriptExecutionMode;
    script_hash: string;
    script_content?: string;
    approved_by?: string;
    approved_at?: string;
    created_at?: string;
    updated_at?: string;
}

export interface AgentSkillScriptCreateRequest {
    filename: string;
    description?: string;
    script_content: string;
    language?: string;
    execution_mode?: string;
}

/** What this update may send: the Control Plane's request type. A key it does not name is refused. */
export type AgentSkillScriptUpdateRequest = ApiSendableBody<'/v1/admin/agent-skills/{skill_id}/scripts/{script_id}', 'patch'>;

export interface AgentSkillScriptListResponse {
    scripts: AgentSkillScript[];
    total: number;
}


// ============================================================================
// Asset Types (assets/ directory)
// ============================================================================

export type AssetType = 'template' | 'image' | 'data';

export interface AgentSkillAsset {
    id: string;
    skill_id: string;
    filename: string;
    description?: string;
    asset_type: AssetType;
    content_hash: string;
    mime_type?: string;
    size_bytes?: number;
    encoding: string;
    content?: string;
    created_at?: string;
    updated_at?: string;
}

export interface AgentSkillAssetCreateRequest {
    filename: string;
    asset_type?: string;
    description?: string;
    content: string;
    mime_type?: string;
    encoding?: string;
}

/** What this update may send: the Control Plane's request type. A key it does not name is refused. */
export type AgentSkillAssetUpdateRequest = ApiSendableBody<'/v1/admin/agent-skills/{skill_id}/assets/{asset_id}', 'patch'>;

export interface AgentSkillAssetListResponse {
    assets: AgentSkillAsset[];
    total: number;
}

// ============================================================================
// UI Helpers
// ============================================================================

export const SKILL_STATUS_CONFIG: Record<SkillStatus, {
    label: string;
    color: string;
    bgColor: string;
    borderColor: string;
    description: string;
}> = {
    draft: {
        label: 'Draft',
        color: 'text-secondary',
        bgColor: 'bg-surface-raised',
        borderColor: 'border-strong',
        description: 'Editable, not yet published'
    },
    validated: {
        label: 'Validated',
        color: 'text-blue-700',
        bgColor: 'bg-blue-100',
        borderColor: 'border-blue-300',
        description: 'Passed spec compliance checks'
    },
    published: {
        label: 'Published',
        color: 'text-green-700',
        bgColor: 'bg-green-100',
        borderColor: 'border-green-300',
        description: 'Immutable, available for execution'
    },
    deprecated: {
        label: 'Deprecated',
        color: 'text-amber-700',
        bgColor: 'bg-amber-100',
        borderColor: 'border-amber-300',
        description: 'Superseded, still executable'
    },
    archived: {
        label: 'Archived',
        color: 'text-red-700',
        bgColor: 'bg-red-100',
        borderColor: 'border-red-300',
        description: 'Fully disabled'
    }
};

export const EXECUTION_STATUS_CONFIG: Record<ExecutionStatus, {
    label: string;
    color: string;
    bgColor: string;
}> = {
    pending: { label: 'Pending', color: 'text-amber-700', bgColor: 'bg-amber-100' },
    running: { label: 'Running', color: 'text-blue-700', bgColor: 'bg-blue-100' },
    completed: { label: 'Completed', color: 'text-green-700', bgColor: 'bg-green-100' },
    failed: { label: 'Failed', color: 'text-red-700', bgColor: 'bg-red-100' },
    cancelled: { label: 'Cancelled', color: 'text-secondary', bgColor: 'bg-surface-raised' },
    timed_out: { label: 'Timed Out', color: 'text-red-700', bgColor: 'bg-red-100' }
};

/** Default categories for skill catalog grouping */
export const SKILL_CATEGORIES = [
    'Data & Analytics',
    'Content & Writing',
    'Code & Development',
    'Communication',
    'Customer Support',
    'Research & Knowledge',
    'Automation & Workflow',
    'Finance & Accounting',
    'Sales & Marketing',
    'HR & People',
    'Legal & Compliance',
    'Operations',
    'Other'
] as const;

/** Resource type options */
export const RESOURCE_TYPES: SkillResourceType[] = [
    'reference', 'form', 'schema', 'template', 'example', 'asset'
];

/** Script language options */
export const SCRIPT_LANGUAGES: ScriptLanguage[] = [
    'python', 'javascript', 'typescript', 'bash'
];

/** Script execution mode options */
export const SCRIPT_EXECUTION_MODES: { value: ScriptExecutionMode; label: string; description: string }[] = [
    { value: 'sandbox', label: 'Sandbox', description: 'Restricted environment — no network, limited filesystem' },
    { value: 'trusted', label: 'Trusted', description: 'Full access — requires admin approval' },
    { value: 'disabled', label: 'Disabled', description: 'Stored for reference but never executed' },
];

/** Asset type options */
export const ASSET_TYPES: { value: AssetType; label: string; description: string }[] = [
    { value: 'template', label: 'Template', description: 'Document or configuration template' },
    { value: 'image', label: 'Image', description: 'Diagram, screenshot, or example image' },
    { value: 'data', label: 'Data', description: 'Lookup table, schema, or data file' },
];

/** Asset encoding options */
export const ASSET_ENCODINGS: { value: string; label: string }[] = [
    { value: 'utf-8', label: 'UTF-8 (text)' },
    { value: 'base64', label: 'Base64 (binary)' },
];

/** Common license identifiers */
export const SKILL_LICENSES = [
    'Apache-2.0',
    'MIT',
    'BSD-3-Clause',
    'proprietary',
    'CC-BY-4.0'
] as const;

/**
 * Skill name validation (matches backend _validate_skill_name).
 * lowercase a-z, digits, hyphens. Start with letter, end with letter/digit.
 */
export const SKILL_NAME_PATTERN = /^[a-z][a-z0-9-]{0,62}[a-z0-9]$/;

export function isValidSkillName(name: string): boolean {
    return SKILL_NAME_PATTERN.test(name);
}

/**
 * Format a skill name for display (hyphen-case → Title Case).
 * "query-sales-data" → "Query Sales Data"
 */
export function formatSkillName(name: string): string {
    return name.split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

/**
 * Get the Lucide icon name from skill metadata, with fallback.
 */
export function getSkillIconName(skill: AgentSkillConfig | AgentSkillCatalogEntry): string {
    const meta = 'metadata' in skill ? skill.metadata : null;
    return meta?.icon_name || 'Sparkles';
}

/**
 * Get skill category from metadata.
 */
export function getSkillCategory(skill: AgentSkillConfig | AgentSkillCatalogEntry): string {
    if ('category' in skill && skill.category) return skill.category;
    const meta = 'metadata' in skill ? skill.metadata : null;
    return meta?.category || 'Other';
}

/**
 * Get skill tags from metadata.
 */
export function getSkillTags(skill: AgentSkillConfig | AgentSkillCatalogEntry): string[] {
    if ('tags' in skill && skill.tags) return skill.tags;
    const meta = 'metadata' in skill ? skill.metadata : null;
    return meta?.tags || [];
}

/**
 * Check if a skill is in the Default resource group.
 */
export function isInDefaultGroup(skill: AgentSkillConfig): boolean {
    return skill.is_global || skill.groups?.some(g => g.name === 'Default') || false;
}


// ============================================================================
// Skill Sources (git-sourced repositories)
// ============================================================================

export type SkillSyncStatus = 'idle' | 'syncing' | 'failed';

export interface SkillSourceResponse {
    id: string;
    tenant_id: string;
    name: string;
    repo_url: string;
    git_ref: string;
    subpath?: string | null;
    enabled: boolean;
    sync_interval_seconds: number;
    auto_publish: boolean;
    status: SkillSyncStatus;
    last_synced_at?: string | null;
    last_attempted_at?: string | null;
    last_commit_sha?: string | null;
    sync_error?: string | null;
    discovered_skill_count: number;
    github_token_secret_ref?: string | null;
    created_at?: string | null;
    updated_at?: string | null;
    created_by?: string | null;
}

export interface SkillSourceListResponse {
    sources: SkillSourceResponse[];
    total: number;
}

export interface SkillSourceCreateRequest {
    name: string;
    repo_url: string;
    git_ref?: string;
    subpath?: string | null;
    enabled?: boolean;
    sync_interval_seconds?: number;
    auto_publish?: boolean;
    github_token_secret_ref?: string | null;
}

/** What this update may send: the Control Plane's request type. A key it does not name is refused. */
export type SkillSourceUpdateRequest = ApiSendableBody<'/v1/admin/skill-sources/{source_id}', 'patch'>;

/**
 * Trigger a browser download of a Blob (ZIP from `exportAgentSkill`).
 * Uses the SkillBundleDownloadHelper convention — call from UI button
 * handlers.
 */
export function downloadBlobAsFile(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}
