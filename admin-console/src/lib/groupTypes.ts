/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import type { ApiSendableBody } from './api/controlPlaneTypes';
import type { components } from './generated/control-plane-api';
import {HealthStatus} from "./llmTypes";
import {MCPServerConfig} from "./types";

/**
 * TypeScript types for Resource Group Management
 */


// ============================================================================
// ENUMS
// ============================================================================

export enum GroupType {
    // Organizational units
    ORGANIZATION = 'organization',
    DIVISION = 'division',
    DEPARTMENT = 'department',
    BUSINESS_UNIT = 'business_unit',
    TEAM = 'team',

    // Work structures
    PROJECT = 'project',
    PROGRAM = 'program',
    INITIATIVE = 'initiative',

    // AI/Technical units
    //
    // There is no APPLICATION / AGENT / ASSISTANT / SERVICE type: they
    // would express as a *group type* what is really a *kind of AI System*,
    // and give the platform two ways to say "this is an agent". That
    // meaning is carried by SystemKind below.
    AI_SYSTEM = 'ai_system',

    // Special
    CUSTOM = 'custom'
}

/**
 * The shape of an AI System.
 *
 * Only meaningful when `group_type === GroupType.AI_SYSTEM`. Selects the
 * lifecycle signal profile — trajectory drift is the headline signal for
 * `agent`/`workflow` and near-meaningless for `application`; cadence and
 * silence detection matter for `integration` and not for `assistant`.
 */
export enum SystemKind {
    AGENT = 'agent',
    ASSISTANT = 'assistant',
    APPLICATION = 'application',
    INTEGRATION = 'integration',
    WORKFLOW = 'workflow',
    SERVICE = 'service'
}

// ============================================================================
// BASIC TYPES
// ============================================================================

export type CapabilityAccess = 'enabled' | 'approval_required' | 'disabled';

export interface CapabilityFilterSummary {
    has_filters: boolean;
    total: number;
    enabled: number;
    disabled: number;
    approval_required: number;
}

export interface ServerConfigInfo {
    id: string;
    name: string;
    description: string;
    enabled: boolean;
    icon_url: string;
    mcp_server: MCPServerConfig;
    is_inherited: boolean;
    capability_filters?: CapabilityFilterSummary;
    has_limits?: boolean;
}

export interface LLModelInfo {
    id: string;
    name: string;
    description?: string;
    provider: string;
    model_name: string;
    is_inherited: boolean;
    health_status: HealthStatus;
    has_governance?: boolean;
    has_limits?: boolean;
    /**
     * Whether this binding is visible in the end-user portal model
     * picker. Defaults to true for normal models; routing *member*
     * models bound for access are typically hidden (false) so the
     * portal only offers the virtual routing model. Only meaningful on
     * direct bindings — inherited rows report the binding from the
     * owning ancestor group.
     */
    portal_visible?: boolean;
}

export interface AgentSkillInfo {
    id: string;
    name: string;
    description?: string;
    version: string;
    status: string;
    enabled: boolean;
    category?: string;
    icon_name?: string;
    execution_count: number;
    is_inherited: boolean;
    has_limits?: boolean;
    has_governance?: boolean;
}

export interface GroupHierarchyInfo {
    id: string;
    name: string;
    display_name?: string;
    group_type?: GroupType;
    hierarchy_level: number;
    is_active: boolean;
}

export interface GroupAncestorInfo {
    id: string;
    name: string;
    group_type?: GroupType;
    hierarchy_level: number;
}

export interface GroupStats {
    member_count: number;
    member_count_with_descendants: number;
    server_config_count: number;
    server_config_count_with_inherited: number;
    llm_model_count: number;
    llm_model_count_with_inherited: number;
    access_key_count: number;
    direct_children_count: number;
    total_descendants_count: number;
}

// ============================================================================
// LIMITS TYPES (GOVERNANCE INHERITANCE - ALWAYS ON, RESTRICTIVE MERGE)
// ============================================================================

/**
 * LLM short-window rate limits — token bucket + cooldown.
 *
 * Mirrors the primitives every major LLM provider exposes:
 *   - RPM + request_burst → token bucket on request count.
 *   - ITPM + tokens_burst → token bucket on prompt tokens (Anthropic /
 *     Azure OpenAI naming). Charged POST-response from billed tokens.
 *   - OTPM → token bucket on completion tokens.
 *   - USD/min + usd_burst → spend-rate ceiling, charged post-response.
 *   - cooldown_seconds → penalty lockout after any bucket trip.
 *
 * Rust enforcement: `usage.rs::enforce_llm_rate_limits` +
 * `book_llm_post_response`.
 */
export interface LLMThroughputLimits {
    max_requests_per_minute?: number;
    max_input_tokens_per_minute?: number;
    max_output_tokens_per_minute?: number;
    max_usd_per_minute?: number;
    request_burst?: number;
    tokens_burst?: number;
    usd_burst?: number;
    cooldown_seconds?: number;
}

/**
 * LLM concurrency cap — semaphore on simultaneous in-flight requests.
 * Independent of rate. Rust enforcement: `usage.rs::acquire_llm_concurrency`
 * (RAII guard releases on Drop, including 5xx / guardrail BLOCK / cancel).
 */
export interface LLMConcurrencyLimits {
    max_concurrent?: number;
}

export interface LLMTokenLimits {
    /** Total prompt + completion tokens per day, counted together. */
    daily_limit?: number;
    monthly_limit?: number;
    daily_warning_percent?: number;
    monthly_warning_percent?: number;
    /** Input (prompt) tokens per day — independent cap. */
    daily_input_limit?: number;
    monthly_input_limit?: number;
    /** Output (completion) tokens per day — independent cap. */
    daily_output_limit?: number;
    monthly_output_limit?: number;
}

export interface LLMGlobalLimits {
    budget?: {
        daily_limit_usd?: number;
        monthly_limit_usd?: number;
        daily_warning_percent?: number;
        monthly_warning_percent?: number;
        // NOTE: there is no `hard_stop` — a configured dollar budget always
        // blocks at 100%. A stored config may carry the key; it
        // is ignored by the proxy.
    };
    tokens?: LLMTokenLimits;
    requests?: {
        daily_limit?: number;
        monthly_limit?: number;
        daily_warning_percent?: number;
        monthly_warning_percent?: number;
    };
    /** Short-window rate + burst + cooldown — see `LLMThroughputLimits`. */
    throughput?: LLMThroughputLimits;
    /** Concurrent in-flight cap — see `LLMConcurrencyLimits`. */
    concurrency?: LLMConcurrencyLimits;
}

/**
 * MCP rate limits — token-bucket + quota + penalty model.
 *
 * `max_calls_per_minute` + `burst_limit` form a token bucket (sustained
 * refill rate + burst capacity); `max_calls_per_hour` / `max_calls_per_day`
 * are fixed-window quotas; `cooldown_seconds` is an optional hard penalty
 * box. Used both server-wide (`MCPGlobalLimits.frequency`) and per-tool
 * (`MCPGlobalLimits.per_tool[toolName]`).
 */
// A type, not an interface: an MCP limits document's `frequency` and
// `per_tool` entries are open records, and only a type is assignable to one.
export type MCPFrequencyLimits = {
    max_calls_per_minute?: number;
    max_calls_per_hour?: number;
    max_calls_per_day?: number;
    burst_limit?: number;
    cooldown_seconds?: number;
};

/**
 * An MCP limits document, as the Control Plane takes it (its
 * `McpLimitsDocument`): a group's `mcp_global_limits`, a group's binding of
 * a server (`limits`) and a server's `tenant_limits`. `execution` and `data`
 * hold the four limits the gateway enforces on a call, `frequency` and
 * `per_tool` its rate limits (`MCPFrequencyLimits`). No other key is taken.
 */
export type MCPGlobalLimits = components['schemas']['McpLimitsDocument'];

export interface SkillGlobalLimits {
    execution?: {
        daily_limit?: number;
        monthly_limit?: number;
        daily_warning_percent?: number;
        monthly_warning_percent?: number;
    };
}

// ============================================================================
// CAPACITY TYPES (SIBLING ALLOCATION BREAKDOWN)
// ============================================================================

export interface SiblingLimitInfo {
    group_id: string;
    group_name: string;
    limits: Record<string, any> | null;
}

export interface LimitCapacityInfo {
    /** Limits of the nearest ancestor that sets any — not always the parent. */
    parent_limit: Record<string, any> | null;
    parent_name: string | null;
    /** False when the budget comes from a grandparent or higher, so the
     *  console can name the holder instead of saying "parent". */
    parent_is_immediate: boolean;
    /** The other groups drawing on that budget, excluding this group's own branch. */
    siblings: SiblingLimitInfo[];
    sibling_total: Record<string, any> | null;
    available: Record<string, any> | null;
}

export interface ModelLimitInfo {
    llm_model_id: string;
    llm_model_name: string;
    limits: Record<string, any> | null;
}

export interface PerModelCapacityInfo {
    group_global_limit: Record<string, any> | null;
    group_name: string | null;
    other_models: ModelLimitInfo[];
    other_models_total: Record<string, any> | null;
    available: Record<string, any> | null;
}

export interface ServerLimitInfo {
    server_config_id: string;
    server_name: string;
    limits: Record<string, any> | null;
}

export interface PerServerCapacityInfo {
    group_global_limit: Record<string, any> | null;
    group_name: string | null;
    other_servers: ServerLimitInfo[];
    other_servers_total: Record<string, any> | null;
    available: Record<string, any> | null;
}

export interface SkillLimitInfo {
    agent_skill_id: string;
    skill_name: string;
    limits: Record<string, any> | null;
}

export interface PerSkillCapacityInfo {
    group_global_limit: Record<string, any> | null;
    group_name: string | null;
    other_skills: SkillLimitInfo[];
    other_skills_total: Record<string, any> | null;
    available: Record<string, any> | null;
}

// ============================================================================
// LIMITS API TYPES (SEPARATE ENDPOINTS)
// ============================================================================

export interface LLMGlobalLimitsResponse {
    group_id: string;
    group_name: string;
    llm_global_limits: LLMGlobalLimits | null;
    effective_llm_limits: LLMGlobalLimits | null;
    inherited_llm_limits: LLMGlobalLimits | null;
    capacity?: LimitCapacityInfo | null;
}

export interface MCPGlobalLimitsResponse {
    group_id: string;
    group_name: string;
    mcp_global_limits: MCPGlobalLimits | null;
    effective_mcp_limits: MCPGlobalLimits | null;
    inherited_mcp_limits: MCPGlobalLimits | null;
    capacity?: LimitCapacityInfo | null;
}

export interface SkillGlobalLimitsResponse {
    group_id: string;
    group_name: string;
    skill_global_limits: SkillGlobalLimits | null;
    effective_skill_limits: SkillGlobalLimits | null;
    inherited_skill_limits: SkillGlobalLimits | null;
    capacity?: LimitCapacityInfo | null;
}

export interface LLMModelLimitsResponse {
    group_id: string;
    group_name: string;
    llm_model_id: string;
    llm_model_name: string;
    limits: Record<string, any> | null;
    effective_global_limits: Record<string, any> | null;
    effective_limits: Record<string, any> | null;
    /** Per-binding end-user portal visibility (null only on inherited rows). */
    portal_visible?: boolean | null;
    capacity?: PerModelCapacityInfo | null;
}

export interface MCPServerLimitsResponse {
    group_id: string;
    group_name: string;
    server_config_id: string;
    server_config_name: string;
    limits: Record<string, any> | null;
    effective_global_limits: Record<string, any> | null;
    effective_limits: Record<string, any> | null;
    capacity?: PerServerCapacityInfo | null;
}

export interface SkillItemLimitsResponse {
    group_id: string;
    group_name: string;
    agent_skill_id: string;
    agent_skill_name: string;
    limits: Record<string, any> | null;
    effective_global_limits: Record<string, any> | null;
    effective_limits: Record<string, any> | null;
    capacity?: PerSkillCapacityInfo | null;
}

// ============================================================================
// LLM GOVERNANCE
// ============================================================================

// The governance documents are the Control Plane's: a key it does not name
// is refused (422), so the console writes them with its types.
type Schemas = components['schemas'];

export type LLMGovernance = Schemas['LlmGovernanceDocument'];
export type GovernanceEnforceMode = NonNullable<Schemas['LlmTemperatureGovernance']['enforce']>;
/** The access a group gives an MCP capability or an A2A call: one of them can be held for a person. */
export type ToolAccessMode = NonNullable<Schemas['McpGovernanceDocument']['default_tool_access']>;
/** A model's tool use: allowed or refused. The gateway holds nothing for a person on a model's tool calls. */
export type ModelToolAccess = NonNullable<Schemas['LlmToolUseGovernance']['access']>;
export type FragmentPosition = Schemas['LlmMandatoryFragment']['position'];
export type LLMModelGovernanceResponse = Schemas['LLMModelGovernanceResponse'];
export type MCPGlobalGovernance = Schemas['McpGovernanceDocument'];
/** Skill governance of a group, for every skill or for one. */
export type SkillGlobalGovernance = Schemas['SkillGovernanceDocument'];
export type SkillGovernance = Schemas['SkillGovernanceDocument'];
export type LLMGlobalGovernanceResponse = Schemas['LLMGlobalGovernanceResponse'];
export type MCPGlobalGovernanceResponse = Schemas['MCPGlobalGovernanceResponse'];
export type SkillGlobalGovernanceResponse = Schemas['SkillGlobalGovernanceResponse'];
export type SkillGovernanceResponse = Schemas['SkillGovernanceResponse'];
export type A2AGovernance = Schemas['A2AGovernanceDocument'];
/** The outbound allow-list one level of a group's chain states. */
export type A2ARemoteAllowListLevel = Schemas['A2ARemoteAllowListLevel'];

// ============================================================================
// RESOURCE GROUP
// ============================================================================

export interface GroupHierarchyResponse {
    tree: GroupHierarchyTree[];
}

export interface GroupMemberListResponse {
    members: GroupMember[];
    total: number;
    include_descendants: boolean;
}

export interface CreateResourceGroupRequest {
    name: string;
    group_type: string;
    /**
     * AI System kind. Send only when group_type is
     * 'ai_system' — the control plane returns 422 for a kind on any other
     * type. Omitted for an AI System, it defaults to 'agent'.
     */
    system_kind?: SystemKind;
    display_name?: string;
    description?: string;
    parent_group_id?: string;
    is_active?: boolean;
    inherit_resources?: boolean;
    icon?: string;
    color?: string;
    // Governance / ownership (AI-System attributes on the RG)
    owner?: string | null;
    intended_use?: string | null;
    /** Non-system callers (client_label strings). */
    intended_client_labels?: string[] | null;
    /** AI System ids this system accepts as callers. */
    intended_client_systems?: string[] | null;
    /** AI Systems only: the gateway refuses an A2A call
     *  from a caller system not in intended_client_systems (proxy subtype
     *  `unaccepted_caller`) instead of reporting an advisory finding. */
    enforce_accepted_callers?: boolean;
    lifecycle_stage?: LifecycleStage | null;
    rollup_exclusion?: RollupExclusion | null;
}

/**
 * Attested exclusion from Mission Control's worst-of health/verdict
 * rollup. Not a mute: reason + note are required, temporary reasons carry an
 * expiry, the server stamps `by`/`at`, and every rollup surface counts and
 * discloses exclusions. Deprecated/retired systems are excluded by stage
 * without one of these.
 */
export type RollupExclusionReason =
    | 'retired'
    | 'deprecated'
    | 'no_evidence_acknowledged'
    | 'scheduled_offline';

export const ROLLUP_EXCLUSION_REASON_LABELS: Record<RollupExclusionReason, string> = {
    retired: 'Retired',
    deprecated: 'Deprecated',
    no_evidence_acknowledged: 'No evidence yet — acknowledged',
    scheduled_offline: 'Scheduled offline',
};

/** Reasons that must carry an expiry: an unmeasured system may be excused for a while, never forever. */
export const ROLLUP_EXCLUSION_TEMPORARY: RollupExclusionReason[] = [
    'no_evidence_acknowledged',
    'scheduled_offline',
];

export interface RollupExclusion {
    reason: RollupExclusionReason;
    note: string;
    expires_at?: string | null;
    /** Server-stamped. */
    by?: string | null;
    at?: string | null;
}

/**
 * What an update of a resource group may send: the Control Plane's request
 * type. A field left out is left as it is; `null` clears one that can be
 * cleared. `system_kind` goes as null when a group stops being an AI System.
 * `enforce_accepted_callers` is for an AI System only: the gateway then
 * refuses an A2A call from a caller system that is not in
 * `intended_client_systems`.
 */
export type UpdateResourceGroupRequest = ApiSendableBody<'/v1/admin/resource-groups/{group_id}', 'patch'>;

export interface ResourceGroup {
    id: string;
    name: string;
    display_name?: string;
    description?: string;
    group_type?: GroupType;
    // AI System kind — set iff group_type is 'ai_system'.
    system_kind?: SystemKind | null;

    // Hierarchy
    parent_group_id?: string;
    parent_group_name?: string;
    parent_group_type?: GroupType;
    hierarchy_level: number;
    hierarchy_path?: string;
    children_count?: number;
    children?: GroupHierarchyInfo[];
    ancestors?: GroupAncestorInfo[];

    // Visual
    icon?: string;
    color?: string;

    // Governance / ownership (AI-System attributes on the RG)
    owner?: string | null;
    intended_use?: string | null;
    /** Non-system callers (client_label strings). */
    intended_client_labels?: string[] | null;
    /** AI System ids this system accepts as callers. */
    intended_client_systems?: string[] | null;
    /** AI Systems only: the gateway refuses an A2A call
     *  from a caller system not in intended_client_systems (proxy subtype
     *  `unaccepted_caller`) instead of reporting an advisory finding. */
    enforce_accepted_callers?: boolean;
    lifecycle_stage?: LifecycleStage | null;
    rollup_exclusion?: RollupExclusion | null;

    // Status
    is_active: boolean;
    is_system: boolean;  // System groups (e.g. Default) cannot be deleted
    inherit_resources: boolean;

    // Metadata
    /** Operator-authored label, editable on the Audit Info tab. */
    version?: string;
    created_by?: string;
    created_at: string;
    updated_at: string;
    updated_by?: string;

    // Optional data (loaded on demand)
    stats?: GroupStats;
    server_configs?: ServerConfigInfo[];
    llm_models?: LLModelInfo[];

    // Limits (governance inheritance - always on, restrictive merge)
    llm_global_limits?: LLMGlobalLimits;
    mcp_global_limits?: MCPGlobalLimits;

    // Governance
    llm_global_governance?: LLMGovernance;
    mcp_global_governance?: MCPGlobalGovernance;
    skill_global_governance?: SkillGlobalGovernance;

    // Skills
    skill_discovery_enabled?: boolean;

    // Per-RG compliance posture. Read+written
    // via the Governance → Compliance sub-tab on the RG details
    // panel. Surfaced on every GET; the tagging engine in the
    // gateway-core proxy reads them at audit-write time to emit
    // `eu_ai_act.<tier>` and `hipaa.rg_marked` tags.
    eu_ai_act_risk_tier?: 'minimal' | 'limited' | 'high' | 'unacceptable' | null;
    eu_ai_act_role?: string | null;
    phi_resource?: boolean | null;
    // Privileged-read rule: read-only tool calls through a sensitive
    // group are sealed as evidence records. Absent on a CP that does
    // not have the field, so read as `?? false`.
    sensitive_data?: boolean | null;
}

/** EU AI Act risk tier — closed enum. Use for dropdown options. */
export const EU_AI_ACT_RISK_TIERS = [
    'minimal',
    'limited',
    'high',
    'unacceptable',
] as const;
export type EuAiActRiskTier = (typeof EU_AI_ACT_RISK_TIERS)[number];

/** Governance lifecycle stage — closed enum. Dropdown options. */
export const LIFECYCLE_STAGES = [
    'proposed',
    'approved',
    'active',
    'deprecated',
    'retired',
] as const;
export type LifecycleStage = (typeof LIFECYCLE_STAGES)[number];
export const LIFECYCLE_STAGE_LABELS: Record<LifecycleStage, string> = {
    proposed: 'Proposed',
    approved: 'Approved',
    active: 'Active',
    deprecated: 'Deprecated',
    retired: 'Retired',
};

// ============================================================================
// HIERARCHY RESPONSES
// ============================================================================

export interface GroupHierarchyTree {
    id: string;
    name: string;
    display_name?: string;
    group_type?: GroupType;
    // Carried through the tree so callers that flatten it looking for AI
    // Systems get the kind without a round trip per node.
    system_kind?: SystemKind | null;
    hierarchy_level: number;
    is_active: boolean;
    is_system?: boolean;  // System groups (e.g. Default) cannot be deleted
    parent_group_id?: string;
    inherit_resources: boolean;
    icon?: string;  // Custom lucide icon name override
    color?: string; // Custom icon color override

    // Statistics
    member_count: number;
    server_config_count: number;
    llm_model_count: number;
    api_key_count: number;

    /** This group carries limits of its own on at least one surface. */
    sets_own_limits?: boolean;
    /** This group carries governance of its own on at least one surface. */
    sets_own_policy?: boolean;
    /** An ancestor's limits bind this group. */
    inherits_limits?: boolean;
    /** An ancestor's governance binds this group. */
    inherits_policy?: boolean;

    // Children (recursive)
    children: GroupHierarchyTree[];
}

// ============================================================================
// EFFECTIVE RESOURCES RESPONSES
// ============================================================================

export interface EffectiveLLModelsResponse {
    group_id: string;
    group_name: string;
    direct_models: LLModelInfo[];
    direct_count: number;
    inherited_models: LLModelInfo[];
    inherited_count: number;
    effective_models: LLModelInfo[];
    effective_count: number;
}

export interface EffectiveServerConfigsResponse {
    group_id: string;
    group_name: string;
    direct_configs: ServerConfigInfo[];
    direct_count: number;
    inherited_configs: ServerConfigInfo[];
    inherited_count: number;
    effective_configs: ServerConfigInfo[];
    effective_count: number;
}

// MCP Capability Filters
export interface MCPCapabilityInfo {
    capability_type: 'tool' | 'resource' | 'prompt' | 'resource_template';
    capability_name: string;
    access: CapabilityAccess;
    approval_timeout_seconds?: number;
    inherited_access?: CapabilityAccess;
    source_group_id?: string;
    source_group_name?: string;
}

export interface MCPDiscoveredCapability {
    capability_type: 'tool' | 'resource' | 'prompt' | 'resource_template';
    name: string;
    description?: string;
}

export interface MCPCapabilityFilterResponse {
    group_id: string;
    group_name: string;
    server_config_id: string;
    server_config_name: string;
    capabilities: MCPCapabilityInfo[];
    total: number;
    has_filters: boolean;
    discovered_capabilities?: MCPDiscoveredCapability[];
    discovered_at?: string;
}

export interface EffectiveAgentSkillsResponse {
    group_id: string;
    group_name: string;
    direct_skills: AgentSkillInfo[];
    direct_count: number;
    inherited_skills: AgentSkillInfo[];
    inherited_count: number;
    effective_skills: AgentSkillInfo[];
    effective_count: number;
}

export interface GuardrailConfigInfo {
    id: string;
    name: string;
    description?: string;
    enabled: boolean;
    is_default: boolean;
    priority: number;
    surfaces: Record<string, boolean>;
    pii_enabled: boolean;
    prompt_injection_enabled: boolean;
    jailbreak_enabled: boolean;
    toxic_content_enabled: boolean;
    secrets_enabled?: boolean;
    banned_word_count: number;
    banned_pattern_count: number;
    provider_count: number;
    /**
     * Number of surfaces that override the scalar action per
     * check. Keyed by check token (pii, prompt_injection, jailbreak,
     * toxic_content, secrets, banned_words, banned_patterns). Absent
     * key or 0 means "no overrides on this check".
     */
    per_surface_counts?: Record<string, number>;
    fail_open: boolean;
    max_check_timeout_ms: number;
    is_inherited: boolean;
    source_group_id?: string;
    source_group_name?: string;
    group_ids: string[];
}

export interface EffectiveGuardrailsResponse {
    group_id: string;
    group_name: string;
    direct_configs: GuardrailConfigInfo[];
    direct_count: number;
    inherited_configs: GuardrailConfigInfo[];
    inherited_count: number;
    effective_configs: GuardrailConfigInfo[];
    effective_count: number;
}

// ============================================================================
// GROUP MEMBERS
// ============================================================================

export interface GroupMember {
    user_id: string;
    username: string;
    email?: string;
    display_name?: string;
    avatar_url?: string;
    role?: string;
    joined_at: string;
    is_inherited?: boolean;  // indicates if member is inherited from parent group
    source_group_id?: string;  // ID of the group this member belongs to (if inherited)
    source_group_name?: string;  // Name of the group this member belongs to (if inherited)
    is_primary?: boolean;  // true when this resource group is the user's primary (active) group
}

export interface GroupMemberListResponse {
    group_id: string;
    group_name: string;
    members: GroupMember[];
    total: number;
}

// ============================================================================
// GROUP API KEYS
// ============================================================================

export interface GroupAPIKey {
    id: string;
    key_value?: string;
    key_prefix: string;
    name: string;
    description?: string;
    scope: 'shared' | 'user';
    user_id?: number;  // Integer from server
    user_email?: string;
    group_id?: string;
    group_name?: string;
    enabled: boolean;
    revoked: boolean;
    is_valid: boolean;
    is_expired: boolean;
    last_used_at?: string;
    usage_count: number;
    expires_at?: string;
    created_at: string;
}

export interface GroupAPIKeyListResponse {
    keys: GroupAPIKey[];
    total: number;
}

export interface GroupAPIKeyUsageResponse {
    group_id: string;
    group_name: string;
    total_keys: number;
    active_keys: number;
    total_usage: number;
    total_tokens_consumed: number;
    keys_usage: Array<{
        key_id: string;
        tokens_consumed: number;
        last_used_by_user_id?: string;
    }>;
}
export interface CreateGroupAPIKeyRequest {
    name: string;
    description?: string;
    access_mode: string; // 'shared' or 'user'
    user_id?: number;    // Integer, optional - required only for access_mode='user'
    expires_at?: string;
    enabled?: boolean;
    agent_id?: string;   // Optional agent-identity binding (actor attribution)
}

export const GROUP_TYPE_LABELS: Record<GroupType, string> = {
    [GroupType.ORGANIZATION]: 'Organization',
    [GroupType.DIVISION]: 'Division',
    [GroupType.DEPARTMENT]: 'Department',
    [GroupType.BUSINESS_UNIT]: 'Business Unit',
    [GroupType.TEAM]: 'Team',
    [GroupType.PROJECT]: 'Project',
    [GroupType.PROGRAM]: 'Program',
    [GroupType.INITIATIVE]: 'Initiative',
    [GroupType.AI_SYSTEM]: 'AI System',
    [GroupType.CUSTOM]: 'Custom'
};

/** Human labels for AI System kinds. */
export const SYSTEM_KIND_LABELS: Record<SystemKind, string> = {
    [SystemKind.AGENT]: 'Agent',
    [SystemKind.ASSISTANT]: 'Assistant',
    [SystemKind.APPLICATION]: 'Application',
    [SystemKind.INTEGRATION]: 'Integration',
    [SystemKind.WORKFLOW]: 'Workflow',
    [SystemKind.SERVICE]: 'Service'
};

/**
 * One-line descriptions shown next to the kind picker. Operators pick a
 * kind once and it decides which detectors run for the life of the system,
 * so the choice deserves more than a bare label.
 */
export const SYSTEM_KIND_DESCRIPTIONS: Record<SystemKind, string> = {
    [SystemKind.AGENT]: 'Autonomous or semi-autonomous goal pursuit — multi-step, uses tools',
    [SystemKind.ASSISTANT]: 'Human-in-the-loop conversational; a person sets the pace',
    [SystemKind.APPLICATION]: 'A product feature calling models — often single-shot, high volume',
    [SystemKind.INTEGRATION]: 'Scheduled or event-driven pipeline running unattended',
    [SystemKind.WORKFLOW]: 'Orchestrated multi-step work with deterministic control flow',
    [SystemKind.SERVICE]: 'Internal shared AI capability called by other systems'
};

/** Human label for an AI System kind, falling back to the raw value. */
export function systemKindLabel(kind?: string | null): string {
    if (!kind) return '';
    return SYSTEM_KIND_LABELS[kind as SystemKind] ?? kind;
}

/**
 * Human label for a Resource Group's type (e.g. "AI System"). Falls back to the
 * raw value for any unknown/absent type.
 */
export function groupTypeLabel(groupType?: string | null): string | null {
    if (!groupType) return null;
    return GROUP_TYPE_LABELS[groupType as GroupType] ?? groupType;
}

/**
 * Canonical display label for a Resource Group everywhere in the admin UI:
 * `"<display_name || name> (<TypeLabel>)"`. When the group's `group_type` is
 * absent (some list/usage payloads don't carry it), the type suffix is omitted.
 * Accepts any object exposing name/display_name/group_type.
 */
export function formatGroupLabel(group: {
    name?: string | null;
    display_name?: string | null;
    group_type?: string | null;
} | null | undefined): string {
    if (!group) return '';
    const base = group.display_name || group.name || '';
    const type = groupTypeLabel(group.group_type);
    return type ? `${base} (${type})` : base;
}

// ============================================================================
// USAGE ANALYTICS TYPES
// ============================================================================

export interface UsageSummary {
    total_requests: number;
    total_errors: number;
    error_rate: number;
    total_cost: number;
    unique_users: number;
    descendant_groups: number;
}

export interface ModelUsageInfo {
    model_name: string;
    provider?: string;
    request_count: number;
    total_tokens: number;
    total_cost: number;
}

export interface ToolUsageInfo {
    server_name: string;
    tool_name: string;
    call_count: number;
    total_execution_time_ms: number;
    error_count: number;
}

export interface LLMUsageMetrics {
    request_count: number;
    success_count: number;
    error_count: number;
    total_tokens: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_cost: number;
    prompt_cost: number;
    completion_cost: number;
    avg_tokens_per_request: number;
    avg_response_time_ms: number;
    top_models: ModelUsageInfo[];
}

export interface MCPUsageMetrics {
    request_count: number;
    success_count: number;
    error_count: number;
    total_tool_calls: number;
    total_cost: number;
    total_data_bytes: number;
    avg_execution_time_ms: number;
    top_tools: ToolUsageInfo[];
}

export interface SkillUsageInfo {
    skill_name: string;
    skill_id?: string | null;
    call_count: number;
    total_execution_time_ms: number;
    error_count: number;
}

export interface SkillUsageMetrics {
    request_count: number;
    success_count: number;
    error_count: number;
    total_cost: number;
    avg_execution_time_ms: number;
    top_skills: SkillUsageInfo[];
}

export interface AgentCardUsageInfo {
    card_id: string;
    card_name: string;
    request_count: number;
    success_count: number;
    error_count: number;
    total_duration_ms: number;
}

export interface AgentCapabilityUsageInfo {
    card_id?: string | null;
    card_name?: string | null;
    capability_name: string;
    call_count: number;
    error_count: number;
}

export interface A2AUsageMetrics {
    request_count: number;
    success_count: number;
    error_count: number;
    avg_duration_ms: number;
    total_data_bytes: number;
    top_cards: AgentCardUsageInfo[];
    top_capabilities: AgentCapabilityUsageInfo[];
}

// Per-mode roll-up for the LLM Media usage panel. Each media mode
// bills in its own unit (images, characters, seconds), so a single
// token-based row would either skew analytics or hide the work
// entirely. The typed counters below mirror the four `media_*`
// columns on `proxy_log` — only one is meaningful for any given
// mode; the rest stay 0.
export interface MediaModeMetrics {
    request_count: number;
    cost_usd: number;
    images_generated: number;
    audio_chars: number;
    audio_seconds_input: number;
    video_seconds_output: number;
}

export type MediaMode =
    | 'image_generation'
    | 'audio_speech'
    | 'audio_transcription'
    | 'video_generation';

export interface MediaUsageMetrics {
    request_count: number;
    estimated_cost_usd: number;
    modes: Partial<Record<MediaMode, MediaModeMetrics>>;
}

export interface DailyUsageBreakdown {
    date: string;
    llm_requests: number;
    mcp_requests: number;
    a2a_requests: number;
    skill_requests: number;
    total_tokens: number;
    total_cost: number;
    error_count: number;
}

export type UsagePeriod = '24h' | '7d' | '30d' | '90d' | 'all';

export interface GroupUsageResponse {
    group_id: string;
    group_name: string;
    period: string;
    include_hierarchy: boolean;
    start_date?: string;
    end_date?: string;
    summary: UsageSummary;
    llm: LLMUsageMetrics;
    mcp: MCPUsageMetrics;
    skill?: SkillUsageMetrics;
    a2a?: A2AUsageMetrics;
    media?: MediaUsageMetrics;
    daily_breakdown: DailyUsageBreakdown[];
}

export interface GroupUsageQueryParams {
    period?: UsagePeriod;
    include_hierarchy?: boolean;
    resource_type?: 'llm' | 'mcp';
    /**
     * Restrict the rollup to a single end-user (the gateway
     * `end_users.id`, an integer). Drives the per-row Usage
     * action on the Group Members tab — the response shape is
     * unchanged, every metric becomes that user's slice.
     */
    user_id?: number;
}

// ============================================================================
// USAGE ALERT TYPES
// ============================================================================

export type AlertStatus = 'active' | 'acknowledged' | 'resolved';
export type AlertType = 'warning' | 'hard_stop';

export interface UsageAlertInfo {
    id: string;
    resource_type: 'llm' | 'mcp';
    group_id: string;
    group_name?: string;
    alert_type: AlertType;
    metric: string;
    threshold_pct: number;
    current_value: number;
    current_value_float?: number;
    limit_value: number;
    limit_value_float?: number;
    utilization_pct: number;
    period: 'daily' | 'monthly';
    period_start?: string;
    status: AlertStatus;
    message?: string;
    triggered_by_request_id?: string;
    acknowledged_by?: string;
    acknowledged_at?: string;
    resolved_at?: string;
    resolution_note?: string;
    created_at?: string;
}

export interface GroupAlertResponse {
    group_id: string;
    group_name: string;
    alerts: UsageAlertInfo[];
    total_count: number;
    active_count: number;
    hard_stop_count: number;
}

export interface TenantAlertsResponse {
    tenant_id: string;
    alerts: UsageAlertInfo[];
    total_count: number;
    active_count: number;
    hard_stop_count: number;
    groups_affected: number;
}

export interface AlertAcknowledgeResponse {
    id: string;
    status: AlertStatus;
    acknowledged_by?: string;
    acknowledged_at?: string;
    message?: string;
}

export interface UsageAlertQueryParams {
    status?: AlertStatus;
    resource_type?: 'llm' | 'mcp';
    alert_type?: AlertType;
    group_id?: string;
    limit?: number;
}

// ============================================================================
// System alerts (downstream failures, health transitions, infra thresholds,
// internal errors). Distinct from UsageAlertInfo which is quota-scoped.
// Mirror of `brutor-gateway-control/app/database/models/system_alert.py`.
// ============================================================================

export type SystemAlertSeverity = 'critical' | 'high' | 'medium' | 'low';

export type SystemAlertCategory =
    | 'downstream_provider'
    | 'downstream_idp'
    | 'downstream_cache'
    | 'downstream_vector_db'
    | 'resource_health'
    | 'infrastructure_threshold'
    | 'internal_error'
    // Liveness misses — mirrored into the assurance inbox as findings.
    | 'ai_system_liveness';

export type SystemAlertStatus = 'active' | 'acknowledged' | 'resolved';

export interface SystemAlert {
    id: string;
    tenant_id: string | null;
    severity: SystemAlertSeverity;
    category: SystemAlertCategory;
    source: string;
    resource_type: string | null;
    resource_id: string | null;
    resource_name: string | null;
    error_code: string | null;
    title: string;
    message: string;
    context: Record<string, unknown> | null;
    status: SystemAlertStatus;
    first_seen_at: string | null;
    last_seen_at: string | null;
    occurrence_count: number;
    acknowledged_at: string | null;
    acknowledged_by: string | null;
    resolved_at: string | null;
    resolved_by: string | null;
    resolution_note: string | null;
    dedup_key: string;
}

/**
 * Counts used by Mission Control to render the badges without rescanning
 * every row client-side. `by_category` lets the "downstream vs internal"
 * ratio be rendered without another round-trip.
 */
export interface SystemAlertsSummary {
    total: number;
    active: number;
    acknowledged: number;
    critical: number;
    high: number;
    medium: number;
    low: number;
    by_category: Partial<Record<SystemAlertCategory, number>>;
}

export interface SystemAlertsListResponse {
    alerts: SystemAlert[];
    summary: SystemAlertsSummary;
    timestamp: string;
    tenant_scope: 'self' | 'all';
}

export interface SystemAlertAcknowledgeResponse {
    success: boolean;
    alert_id: string;
    status: SystemAlertStatus;
    acknowledged_at: string;
    acknowledged_by: string;
}

export interface SystemAlertResolveResponse {
    success: boolean;
    alert_id: string;
    status: SystemAlertStatus;
    resolved_at: string;
    resolved_by: string;
    resolution_note: string | null;
}

// ============================================================================
// OAuth Tokens (admin read-only diagnostic view). Mirror of
// `brutor-gateway-control/app/api/v1/endpoints/admin/oauth_tokens.py`.
// No encrypted material is ever included — `has_refresh_token` replaces
// the raw refresh token.
// ============================================================================

export type OAuthTokenStatus =
    | 'active'
    | 'expired_with_refresh'
    | 'expired_no_refresh'
    | 'revoked'
    | 'idp_unavailable';

export interface OAuthTokenInfo {
    id: number;
    tenant_id: string;
    gateway_user_id: string | null;
    gateway_username: string | null;
    idp_user_id: string;
    idp_id: string;
    idp_name: string | null;
    mcp_server_id: string;
    mcp_server_name: string | null;
    scopes: string | null;
    token_type: string | null;
    expires_at: string | null;
    revoked: boolean;
    revoked_at: string | null;
    has_refresh_token: boolean;
    refresh_count: number;
    last_refreshed_at: string | null;
    created_at: string | null;
    updated_at: string | null;
    status: OAuthTokenStatus;
    status_reason: string;
}

export interface OAuthTokensSummary {
    total: number;
    active: number;
    expired_with_refresh: number;
    expired_no_refresh: number;
    revoked: number;
    idp_unavailable: number;
}

export interface OAuthTokensListResponse {
    tokens: OAuthTokenInfo[];
    summary: OAuthTokensSummary;
    timestamp: string;
}

/**
 * Per-token outcome of a revoke call. `idp_revoked` is `true` only if
 * the IdP's RFC 7009 revocation endpoint returned 2xx — the DB
 * revocation is implicit (a row only shows up in `results` if the DB
 * write succeeded; IdP failures never block that).
 */
export interface OAuthTokenRevokeResult {
    token_id: number;
    mcp_server_id: string;
    idp_id: string;
    idp_revoked: boolean;
    idp_error: string | null;
}

export interface OAuthTokensRevokeResponse {
    revoked_count: number;
    results: OAuthTokenRevokeResult[];
    timestamp: string;
}

export interface OAuthTokensBulkRevokeRequest {
    mcp_server_id?: string;
    idp_id?: string;
    gateway_user_id?: string;
    /**
     * Limit to tokens currently in this diagnostic status. Same values
     * as `OAuthTokenStatus`. Useful for "revoke every token that's
     * currently active" vs "revoke only the expired-no-refresh ones".
     */
    status_filter?: OAuthTokenStatus;
}