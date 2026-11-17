/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLM Model Types with Group-Based Access Control
 *
 * - Models scoped to user groups (Default group = all users)
 * - The gateway's router handles routing strategy (load balancing, fallbacks)
 * - Full completion parameter support
 */

import type { ApiRequestBody, ApiSendableBody } from './api/controlPlaneTypes';
import type { components } from './generated/control-plane-api';

/** The control plane's schemas (generated from its OpenAPI document): the fields taken from them below cannot drift. */
type ControlPlaneSchemas = components['schemas'];

// ============================================================================
// Group Information
// ============================================================================

export interface GroupInfo {
    id: string;
    name: string;
    display_name?: string;
    group_type?: string;
    is_active: boolean;
    parent_group_id?: string;
}

// ============================================================================
// Health Status
// ============================================================================

// ``requires_tier`` is set by the control plane when a restricted-tier
// model (Codex preview, Pro, o1-pro, …) fails the health probe because
// the configured API key isn't eligible. The UI shows it as a distinct
// "Requires <Tier>" badge — not red, since the catalogue is correct.
export type ModelStatus =
    | 'healthy'
    | 'unhealthy'
    | 'degraded'
    | 'requires_tier'
    | 'unknown';

/** Probe taxonomy that produced the most recent health-status
 *  update — drives the secondary badge next to Healthy/Unhealthy.
 *
 *  - `catalog`   verified via the provider's /models endpoint
 *                (free, confirms key + reachability + that our
 *                model_name is in the upstream catalog)
 *  - `endpoint`  verified via a real (cheap) inference call to the
 *                mode endpoint at the provider
 *  - `synthetic` Brutor-side validation 400 only — DOES NOT verify
 *                the upstream. The badge for this type is amber to
 *                flag that the green Healthy state is tentative.
 *
 *  `null` when never probed. */
export type ProbeType = 'catalog' | 'endpoint' | 'synthetic';

export interface HealthStatus {
    status: ModelStatus;
    last_check?: string;
    last_success?: string;
    consecutive_failures: number;
    error_message?: string;
    avg_latency_ms?: number;
    /** Tier name for ``requires_tier`` status (e.g. "Pro", "Codex preview"). */
    required_tier?: string;
    /** Last probe type that produced this health status. See `ProbeType`. */
    probe_type?: ProbeType | null;
    /** Probe taxonomy of the most recent health-status update. See `ProbeType`. */
    last_probe_type?: ProbeType | string | null;
}

// ============================================================================
// Performance Metrics
// ============================================================================

export interface PerformanceMetrics {
    total_requests: number;
    failed_requests: number;
    success_rate: number;
    avg_latency_ms?: number;
    total_tokens_consumed: number;
}

// ============================================================================
// LLM Model Configuration (Response Type)
// ============================================================================

export interface LLModelConfig {
    // Basic Information
    id: string;
    name: string;
    description?: string;
    usage?: string;  // Usage description and use cases
    tags?: string[];  // Tags for filtering (cost, speed, capabilities, use cases)
    tenant_id: string;

    // Provider Information
    provider: string;  // openai, anthropic, gemini, etc.
    model_name: string;  // gpt-5.2, claude-sonnet-5-5, etc.
    mode: string; // chat, completion, embedding, routing

    // Connection Details
    api_url_auto_discovered: boolean;  // True if the provider's standard URL is used
    api_url?: string;  // Only needed for custom deployments (Bedrock, Ollama, etc.)

    // Authentication
    auth_type: string;  // api_key, oauth, none
    // Note: api_key NOT included in responses (security)
    /**
     * Whether a provider credential (`provider_config`) is stored for the
     * model. The credential itself is never returned.
     */
    has_provider_config?: ControlPlaneSchemas['LLModelResponse']['has_provider_config'];
    /** The AWS region stored in the credential of an Amazon Bedrock model; null when none is. No secret. */
    aws_region?: ControlPlaneSchemas['LLModelResponse']['aws_region'];
    /** The Google Cloud project stored in the credential of a Vertex AI model; null when none is. No secret. */
    gcp_project_id?: ControlPlaneSchemas['LLModelResponse']['gcp_project_id'];
    /** The Google Cloud location stored in the credential of a Vertex AI model; null when none is. No secret. */
    gcp_location?: ControlPlaneSchemas['LLModelResponse']['gcp_location'];
    /**
     * Per secret key of the credential (`aws_access_key_id`,
     * `aws_secret_access_key`, `aws_session_token`,
     * `gcp_credentials_json`): whether a value is stored. The values are
     * never returned.
     */
    provider_config_keys?: ControlPlaneSchemas['LLModelResponse']['provider_config_keys'];

    // Model Configuration - Defaults
    default_temperature: number;
    default_max_tokens: number;

    // System Prompt
    system_prompt?: string;  // Default system prompt for all completions

    // ========================================================================
    // COMPLETION PARAMETERS
    // ========================================================================

    // Core Generation Parameters
    top_p?: number;  // Nucleus sampling threshold (0-1)
    n?: number;  // Number of completions (1-10)
    stop_sequences?: string[];  // Stop generation at these strings
    seed?: number;  // Reproducible outputs

    // Penalties
    presence_penalty?: number;  // Penalize tokens based on presence (-2 to 2)
    frequency_penalty?: number;  // Penalize tokens based on frequency (-2 to 2)
    logit_bias?: Record<string, number>;  // Token probability modification

    // Response Format & Output
    response_format?: Record<string, any>;  // Force JSON mode: {type: "json_object"}
    logprobs?: boolean;  // Return log probabilities
    top_logprobs?: number;  // Number of top tokens with probabilities (0-5)

    // Streaming
    stream_options?: Record<string, any>;  // {include_usage: true}

    // Tools/Functions
    tools?: any[];  // Predefined tools/functions
    tool_choice?: string;  // 'none', 'auto', 'required', or specific function
    parallel_tool_calls?: boolean;  // Enable parallel function calling

    // Safety & Monitoring
    user_identifier?: string;  // End-user tracking for abuse monitoring

    // Provider-Specific
    api_version?: string;  // API version (Azure, Bedrock, etc.)
    extra_headers?: Record<string, string>;  // Additional HTTP headers

    // Advanced
    model_metadata?: Record<string, any>;  // Custom metadata for logging
    drop_params?: boolean;  // Whether the provider's dropped-parameters list applies (unset: it does)
    additional_drop_params?: string[];  // Specific parameter names to drop
    thinking_enabled?: boolean;  // Generic thinking/reasoning toggle (per-provider translated by proxy)

    // Model Capabilities
    supports_streaming: boolean;
    supports_function_calling: boolean;
    supports_vision: boolean;
    supports_batch: boolean;

    // Routing configuration
    /**
     * `null` is a meaningful value here: the admin UI sends it
     * to detach a model from its current routing group. With
     * `undefined` the field is stripped from the JSON payload
     * and the server treats it as "no change" — making detach
     * impossible on an already-attached model.
     */
    routing_group_id?: string | null; // Participate in load-balancing by assigning routing group
    routing_weight: number;  // Weight for load balancing
    fallback_models?: string[] | null;  // Model-level failover chain: ordered model ids to try when this model fails
    rate_limit_rpm?: number;  // Requests per minute
    rate_limit_tpm?: number;  // Tokens per minute

    // Per-model circuit breaker. Read by the proxy on every
    // dispatch — direct and routed. Empty/null = inherit routing
    // group / proxy default.

    // Probe taxonomy of the most recent health-status update (mirrors
    // `health_status.last_probe_type` for callers that read it off the row).
    last_probe_type?: ProbeType | string | null;

    // Model Size
    parameters?: number;  // Model size in billions of parameters (e.g. 8, 70, 405)

    // Cost Management
    cost_per_input_token?: number;  // USD per 1M tokens
    cost_per_output_token?: number;  // USD per 1M tokens
    // Image generation — only meaningful when mode='image_generation'.
    cost_per_image?: number;
    supported_image_sizes?: string[];      // e.g. ["1024x1024","1024x1792"]
    supported_image_qualities?: string[];  // e.g. ["standard","hd"]
    max_images_per_request?: number;       // Default 1; DALL-E 2 supports up to 10
    // Audio TTS — only meaningful when mode='audio_speech'.
    cost_per_audio_char?: number;
    supported_voices?: string[];           // e.g. ["alloy","echo","fable","onyx","nova","shimmer"]
    supported_audio_formats?: string[];    // e.g. ["mp3","opus","aac","flac","wav","pcm"]
    // Audio STT — only meaningful when mode='audio_transcription'.
    cost_per_audio_minute?: number;
    max_audio_duration_seconds?: number;
    supported_input_formats?: string[];    // e.g. ["mp3","mp4","wav","m4a","webm"]
    // null = clear the whitelist, fall back to proxy's per-model default;
    // undefined = field not sent (PATCH leaves existing value untouched).
    supported_response_formats?: string[] | null; // e.g. ["json","text","verbose_json","srt","vtt"]
    // Video generation — only meaningful when mode='video_generation'.
    cost_per_video_second?: number;
    supported_video_resolutions?: string[]; // e.g. ["720p","1080p"]
    supported_video_durations?: number[];   // e.g. [5, 10]
    max_video_duration_seconds?: number;

    // Connection Settings
    timeout: number;  // Seconds
    max_concurrent_requests?: number | null;  // Cap on in-flight requests (1-1024), null = unbounded
    /** Circuit breaker: consecutive failed calls after which the gateway stops sending to the model (1-100); set with `cooldown_time`. */
    allowed_fails?: number | null;
    /** Seconds the circuit breaker stays open, and the Retry-After of a call refused meanwhile (1-3600). */
    cooldown_time?: number | null;

    // Status
    enabled: boolean;

    // Provenance: 'manual' (operator-added) vs 'import' (discovered via
    // Traffic Data Import; observability-only until promoted).
    source?: string;

    // Retirement metadata. All fields nullable — most models aren't
    // on a published retirement schedule. Date columns serialise as
    // ISO `YYYY-MM-DD` strings from the backend. The
    // `llm_model_retirement_warner` job emits a system_alert when
    // `retirement_reminder_date <= today AND retire_date > today`
    // (medium severity), and a louder one when `retire_date <= today`
    // (high severity).
    retire_date?: string | null;
    retirement_announced_at?: string | null;
    retirement_reminder_date?: string | null;
    retirement_replacement_model_name?: string | null;
    retirement_replacement_provider_key?: string | null;
    retirement_notes?: string | null;

    // Additional Capabilities
    capabilities?: Record<string, any>;

    // ========================================================================
    // GROUP-BASED ACCESS CONTROL
    // All models belong to at least the Default resource group.
    // Models in Default = accessible to all users.
    // Remove from Default = restrict to specific groups only.
    // ========================================================================

    groups?: GroupInfo[];  // Groups that can access this model

    // Health & Performance
    health_status: HealthStatus;
    performance_metrics: PerformanceMetrics;

    // Metadata — the AI Resource identity block. `version` is
    // operator-authored; `created_by` / `updated_by` are system-stamped
    // and read-only.
    version?: string | null;
    created_by?: string | null;
    updated_by?: string | null;
    created_at: string;
    updated_at: string;
}

// ============================================================================
// Request Types (include sensitive data like api_key)
// ============================================================================

/**
 * Request to create a new LLM model
 * Includes api_key which is NOT returned in responses
 */
export interface LLModelCreateRequest {
    // Basic Information
    name: string;
    description?: string;
    usage?: string;
    tags?: string[];
    tenant_id: string;

    // Provider Information
    provider: string;
    model_name: string;
    mode: string; // chat, completion, embedding, routing

    // Connection Details
    api_url_auto_discovered?: boolean;  // Default: true for standard providers
    api_url?: string;  // Required if api_url_auto_discovered=false

    // Authentication
    auth_type: string;
    api_key?: string;  // Required for api_key auth_type

    // Model Configuration
    default_temperature?: number;
    default_max_tokens?: number;

    // System Prompt
    system_prompt?: string;  // Default system prompt for all completions (max 100k chars)

    // Completion parameters
    top_p?: number;
    n?: number;
    stop_sequences?: string[];
    seed?: number;
    presence_penalty?: number;
    frequency_penalty?: number;
    logit_bias?: Record<string, number>;
    response_format?: Record<string, any>;
    logprobs?: boolean;
    top_logprobs?: number;
    stream_options?: Record<string, any>;
    tools?: any[];
    tool_choice?: string;
    parallel_tool_calls?: boolean;
    user_identifier?: string;
    api_version?: string;
    extra_headers?: Record<string, string>;
    model_metadata?: Record<string, any>;
    drop_params?: boolean;
    thinking_enabled?: boolean;  // Generic thinking/reasoning toggle (per-provider translated by proxy)
    additional_drop_params?: string[];

    // Model Capabilities
    supports_streaming?: boolean;
    supports_function_calling?: boolean;
    supports_vision?: boolean;
    supports_batch?: boolean;

    // Model Size
    parameters?: number;  // Model size in billions (e.g. 8, 70, 405)

    // Routing
    /**
     * Allow `null` so a model creation/update payload can
     * carry an explicit detach signal to the backend; with
     * `undefined` the field would be stripped from the JSON
     * and the server would treat it as "no change".
     */
    routing_group_id?: string | null;
    routing_weight?: number;
    fallback_models?: string[] | null;
    rate_limit_rpm?: number;
    rate_limit_tpm?: number;

    // Per-model circuit breaker. Empty/null = inherit default.

    // Cost Management
    cost_per_input_token?: number;
    cost_per_output_token?: number;

    // Connection Settings
    timeout?: number;
    max_concurrent_requests?: number | null;
    allowed_fails?: number | null;
    cooldown_time?: number | null;

    // Status
    enabled?: boolean;

    // Group Access
    group_ids?: string[];  // Group IDs (empty/null = Default group only = all users)

    // Additional
    capabilities?: Record<string, any>;
}

/**
 * What an update of a model may send: the Control Plane's request type. A
 * field left out is left as it is. `api_key` is named only to change it.
 */
export type LLModelUpdateRequest = ApiSendableBody<'/v1/admin/llms/{model_id}', 'patch'>;

// ============================================================================
// List/Response Types
// ============================================================================

export interface LLModelListResponse {
    models: Record<string, LLModelConfig>;
    total: number;
}

// ============================================================================
// Global LLM Catalog
// ============================================================================
//
// The browseable catalog of LLM models available for import. This is a
// GLOBAL table (`llm_catalog`), distinct from the per-tenant configured
// `llm_model` instances surfaced by `LLModelConfig`/`listLLMs`. A catalog
// entry has no `enabled` flag (that's an instance concern); importing one
// materialises a configured instance the operator can then key + assign.
//
// Field names mirror the backend `LlmCatalogEntryResponse` (built from
// `LlmCatalog.to_dict()`).

export interface LLMCatalogEntry {
    id: string;
    provider_key: string;
    model_name: string;
    display_name: string;
    description?: string | null;
    mode: string;
    capabilities?: Record<string, unknown> | null;
    context_window?: number | null;
    max_output_tokens?: number | null;
    default_cost_per_input_token?: number | null;
    default_cost_per_output_token?: number | null;
    icon_url?: string | null;
    recommended: boolean;
    deprecated: boolean;
    source: string;
    /**
     * True when a model of this provider and mode has no destination without
     * its own `api_url`: the gateway knows no base URL for it. Importing such
     * an entry needs `api_url`. The control plane computes it from
     * `provider_key` and `mode`.
     */
    requires_api_url: ControlPlaneSchemas['LlmCatalogEntryResponse']['requires_api_url'];
    // Catalog-default media/capability metadata. Curated on the
    // global catalog entry; materialised onto the configured llm_model on import.
    cost_per_image?: number;
    supported_image_sizes?: string[];
    supported_image_qualities?: string[];
    max_images_per_request?: number;
    cost_per_audio_char?: number;
    cost_per_audio_minute?: number;
    supported_voices?: string[];
    supported_audio_formats?: string[];
    supported_input_formats?: string[];
    supported_response_formats?: string[];
    max_audio_duration_seconds?: number;
    cost_per_video_second?: number;
    supported_video_resolutions?: string[];
    supported_video_durations?: number[];
    max_video_duration_seconds?: number;
    thinking_enabled?: boolean;
    additional_drop_params?: string[];
    parameters?: number;
    tags?: string[];
    retire_date?: string;
    created_at?: string | null;
    updated_at?: string | null;
}

export interface LLMCatalogListResponse {
    catalog: LLMCatalogEntry[];
    total: number;
}

/**
 * Optional overrides applied when importing a catalog entry into the
 * tenant as a configured `llm_model`. An empty body imports the entry
 * with the catalog's descriptive defaults, enabled=true, no api_key.
 */
export interface LLMCatalogImportRequest {
    name?: string;
    enabled?: boolean;
    api_key?: string;
    /** The model's own endpoint. Required for an entry whose `requires_api_url` is true: the import is refused (422) without it. */
    api_url?: NonNullable<ControlPlaneSchemas['LlmCatalogImportRequest']['api_url']>;
    group_ids?: string[];
}

/**
 * What the creation of a custom global catalog entry sends (a system
 * administrator's): the Control Plane's request type. The provider key and
 * the model name cannot be changed by an update.
 */
export type LLMCatalogCreateRequest = ApiRequestBody<'/v1/admin/llm-catalog', 'post'>;

// ============================================================================
// Health Check
// ============================================================================

export interface HealthCheckResponse {
    model_id: string;
    model_name: string;
    status: 'healthy' | 'unhealthy' | 'timeout';
    response_time_ms?: number;
    error_message?: string;
    timestamp: string;
    /** Which probe produced the verdict: 'catalog' | 'endpoint' | 'synthetic'; null when none ran. */
    probe_type?: string | null;
    /** Background sweep skipped a persistently misconfigured model; status is the last recorded one. */
    backed_off?: boolean;
}

// ============================================================================
// Router management types
// ============================================================================

// Enums
export type RetryPolicy =
    | 'none'
    | 'fixed_delay'
    | 'exponential_backoff'
    | 'exponential_backoff_jitter';

export type RouterHealthStatus =
    | 'healthy'
    | 'unhealthy'
    | 'unknown';

// ─── Enhanced Router Status Types ─────────────────────────────────────────────

export interface RoutingGroupModelSummary {
    model_id: string;
    model_name: string;
    provider: string;
    health_status: string;
    routing_weight: number;
    avg_latency_ms: number | null;
    total_requests: number;
    failed_requests: number;
    error_rate: number;
}

export interface RoutingGroupStatusSummary {
    id: string;
    name: string;
    routing_strategy: string;

    // Model health (enabled models only)
    total_models: number;
    healthy_models: number;
    unhealthy_models: number;

    // Performance
    total_requests: number;
    total_failed: number;
    error_rate: number;
    avg_latency_ms: number | null;

    has_fallbacks: boolean;

    // Models
    models: RoutingGroupModelSummary[];
}

export interface RouterCacheStatus {
    cached_routers: number;
    max_cached_routers: number;
    cache_utilization: number;
    cache_hits: number;
    cache_misses: number;
    hit_rate: number;
    total_rebuilds: number;
    router_ttl_seconds: number;
}

export interface RouterStatusResponse {
    // Router State
    enabled: boolean;
    routing_strategy: RoutingStrategy;
    /** Complexity routing (opt-in). Null/absent for every other strategy. */
    complexity_floor_tier?: ComplexityTier | null;
    complexity_config?: Record<string, unknown> | null;

    // Deployment Statistics (enabled only)
    total_deployments: number;
    healthy_deployments: number;
    unhealthy_deployments: number;
    unassigned_deployments: number;

    // Routing Groups (enabled only)
    total_routing_groups: number;
    routing_groups: RoutingGroupStatusSummary[];

    // Cache Configuration (Redis)
    cache_enabled: boolean;
    cache_status: 'connected' | 'disconnected' | 'error';
    redis_url?: string;

    // Router Instance Cache
    router_cache: RouterCacheStatus;

    // Reliability Features
    fallback_enabled: boolean;
    health_check_enabled: boolean;

    // Aggregate Performance (enabled models only)
    total_requests: number;
    total_errors: number;
    error_rate: number;
    avg_latency_ms: number;

    // Runtime
    uptime_seconds: number;
    timestamp: string;
}

// Router Health Check Response
export interface RouterHealthCheckResponse {
    status: RouterHealthStatus;
    enabled: boolean;
    deployments_available: number;
    cache_available: boolean;
    timestamp: string;
}

// Router Configuration Update Request
export interface RouterConfigUpdateRequest {
    routing_strategy?: RoutingStrategy;
    cache_enabled?: boolean;
    redis_url?: string;
    fallback_enabled?: boolean;
    health_check_enabled?: boolean;
    health_check_interval?: number;  // 10-600 seconds
    retry_policy?: RetryPolicy;
    max_retries?: number;  // 0-10
    timeout_seconds?: number;  // 5-300
}

// Router Configuration Response
export interface RouterConfigResponse {
    success: boolean;
    message: string;
    config: Record<string, any>;
    restart_required: boolean;
}

// Router Configuration (GET response)
export interface RouterConfig {
    routing_strategy: RoutingStrategy;
    cache_enabled: boolean;
    redis_url?: string;
    fallback_enabled: boolean;
    health_check_enabled: boolean;
    health_check_interval: number;
    retry_policy: RetryPolicy;
    max_retries: number;
    timeout_seconds: number;
}

/**
 * Router test request — POST /v1/admin/llm-router/test.
 *
 * Sends a real chat-completion through the proxy against the
 * specified routing group. There is no `model` field to target
 * a specific catalogue entry — for direct-model testing the
 * LLM Models page is the right surface.
 */
export interface RouterTestRequest {
    routing_group_id: string;
    message?: string;
}

/**
 * One result entry in the in-memory test history. Used for
 * both the immediate POST response and the items in the
 * GET /v1/admin/llm-router/test-history list payload.
 */
export interface RouterTestResponse {
    timestamp: string;
    routing_group_id: string;
    routing_group_name: string;
    routing_strategy: string;
    message: string;
    /** Member model the router actually picked. `null` on
     *  upstream error before dispatch. */
    selected_model?: string | null;
    selected_provider?: string | null;
    /** First completion message content. `null` on error. */
    response_text?: string | null;
    /** End-to-end ms across the control-plane → proxy →
     *  upstream → control-plane round-trip. */
    latency_ms?: number | null;
    status_code?: number | null;
    error?: string | null;
}

export interface RouterTestHistoryResponse {
    items: RouterTestResponse[];
    total: number;
}

// Model Deployment
export interface ModelDeployment {
    model_id: string;
    model_name: string;
    provider_model_name: string;
    provider: string;
    enabled: boolean;
    health_status: RouterHealthStatus;
    routing_group_id?: string;
    routing_group_name?: string;
    routing_weight: number;
    avg_latency_ms?: number;
    total_requests: number;
    error_rate: number;
    cost_per_1k_input?: number;
    cost_per_1k_output?: number;
}

export interface ModelDeploymentListResponse {
    deployments: ModelDeployment[];
    total: number;
    healthy: number;
    unhealthy: number;
}

// Router Statistics Response
export interface RouterStatistics {
    total_requests: number;
    successful_requests: number;
    failed_requests: number;
    error_rate: number;
    avg_latency_ms: number;
    p50_latency_ms?: number;
    p95_latency_ms?: number;
    p99_latency_ms?: number;
    requests_by_model: Record<string, number>;
    requests_by_routing_group: Record<string, number>;
    cache_hits: number;
    cache_misses: number;
    cache_hit_rate: number;
    period_start: string;
    period_end: string;
}

// Router Control Response (restart, enable, disable)
export interface RouterControlResponse {
    success: boolean;
    message: string;
    enabled?: boolean;
    estimated_downtime_seconds?: number;
}

// Cache Clear Response
export interface CacheClearResponse {
    success: boolean;
    message: string;
    entries_cleared: number;
}

// Cache Stats Response
export interface CacheStatsResponse {
    total_entries: number;
    cache_size_mb: number;
    hit_rate: number;
    total_hits: number;
    total_misses: number;
    avg_response_time_ms: number;
}

// Deployment List Query Params
export interface DeploymentListQueryParams {
    filter_status?: RouterHealthStatus;
    enabled_only?: boolean;
    sort_by?: 'name' | 'latency' | 'requests' | 'error_rate';
}

// Router Statistics Query Params
export interface RouterStatisticsQueryParams {
    start_time?: string;  // ISO 8601
    end_time?: string;    // ISO 8601
    group_by_model?: boolean;
    group_by_routing_group?: boolean;
}

// ============================================================================
// Routing Group Types
// ============================================================================

export type RoutingStrategy =
    | 'simple-shuffle'
    | 'least-busy'
    | 'usage-based-routing'
    | 'usage-based-routing-v2'
    | 'latency-based-routing'
    | 'cost-based-routing'
    | 'complexity-based-routing';

/** Complexity tiers, cheapest -> most capable. Wire contract with the proxy. */
export type ComplexityTier = 'simple' | 'medium' | 'complex' | 'reasoning';

export const COMPLEXITY_TIERS: ComplexityTier[] = [
    'simple',
    'medium',
    'complex',
    'reasoning',
];

export interface RoutingGroupTierAssignment {
    tier: ComplexityTier;
    llm_model_id: string;
    position: number;
}

export interface RoutingGroupTierItem extends RoutingGroupTierAssignment {
    id: string;
    routing_group_id: string;
}

export interface RoutingGroupTiersResponse {
    routing_group_id: string;
    tiers: RoutingGroupTierItem[];
    total: number;
}

/**
 * Inbound surface a routing group serves. Members must share
 * this mode — `update_llm` / `create_llm` reject mismatches
 * on the server. Mirrors `LLModelMode` minus the synthetic
 * `routing` value (reserved for the virtual model the control
 * plane creates per group).
 */
export type RoutingGroupMode =
    | 'chat'
    | 'completion'
    | 'responses'
    | 'embedding'
    | 'image_generation'
    | 'audio_speech'
    | 'audio_transcription'
    | 'video_generation'
    | 'rerank'
    | 'moderation';

/**
 * What the creation of a routing group sends: the Control Plane's request
 * type. `mode` is the inbound surface the group serves; the models assigned
 * to it share it.
 */
export type RoutingGroupCreate = ApiRequestBody<'/v1/admin/llm-router/routing-groups', 'post'>;

/**
 * What an update of a routing group may send: the Control Plane's request
 * type. A key it does not name is refused (422). A change of `mode` is
 * refused (400) while models are assigned to the group.
 */
export type RoutingGroupUpdate = ApiSendableBody<'/v1/admin/llm-router/routing-groups/{group_id}', 'patch'>;

export interface RoutingGroup {
    // Basic Information
    id: string;
    tenant_id: string;
    name: string;
    description?: string;
    /**
     * Inbound surface this group serves. Surfaced for the
     * admin UI's group dropdown filters and the Test Router's
     * endpoint dispatch.
     */
    mode: RoutingGroupMode;


    // Routing Strategy
    routing_strategy: RoutingStrategy;

    // Failure Handling
    cooldown_time: number;

    // Fallback Configuration
    fallback_models?: string[];
    context_window_fallbacks?: string[];
    content_policy_fallbacks?: string[];
    default_fallbacks?: string[];
    max_fallbacks?: number;

    // Retry & Timeout Configuration
    num_retries: number;
    retry_after: number;
    /** Seconds a streamed call waits for the first chunk of its model before failing over; null: not set. */
    stream_timeout?: number | null;

    // Rate Limiting
    max_parallel_requests?: number;
    rpm_limit?: number;
    tpm_limit?: number;

    // Status
    enabled: boolean;
    custom_metadata?: Record<string, any>;

    // Statistics
    num_models: number;

    // Audit info
    /** Operator-authored version label; defaults to "1.0". */
    version?: string;
    /** System-set. Seeded rows read "system". */
    created_by?: string | null;
    /** System-set. */
    updated_by?: string | null;

    // Timestamps
    created_at: string;
    updated_at: string;
}

export interface RoutingGroupListItem {
    id: string;
    name: string;
    description?: string;
    /**
     * Inbound surface — same value as `RoutingGroup.mode`.
     * Surfaced on the listing so admins see the group's
     * intended traffic at a glance and the LLM Models page's
     * group-picker can filter by it.
     */
    mode: RoutingGroupMode;
    routing_strategy: RoutingStrategy;
    enabled: boolean;
    num_models: number;
    // Configuration indicators
    num_retries: number;
    cooldown_time: number;
    // Fallback details
    has_fallbacks: boolean;
    fallback_count: number;
    fallback_models?: string[];
    context_window_fallbacks?: string[];
    content_policy_fallbacks?: string[];
    default_fallbacks?: string[];
    // Rate limits
    rpm_limit?: number;
    tpm_limit?: number;
    max_parallel_requests?: number;
    // Timestamps
    created_at: string;
    updated_at: string;
}

export interface RoutingGroupListResponse {
    routing_groups: RoutingGroupListItem[];
    total: number;
}

export interface RoutingGroupDeleteResponse {
    success: boolean;
    message: string;
    group_id: string;
    models_disconnected: number;
}

// ============================================================================
// LLM Model Search Types
// ============================================================================

/**
 * Search request for finding LLM models
 * All filters are optional - combine to narrow results
 */
export interface LLModelSearchRequest {
    // Text search (partial, case-insensitive)
    name?: string;
    description?: string;
    model_name?: string;

    // Provider filters
    provider?: string;
    providers?: string[];

    // Capability filters
    supports_streaming?: boolean;
    supports_function_calling?: boolean;
    supports_vision?: boolean;
    supports_batch?: boolean;

    // Mode filter — exact match on the model's `mode` column.
    // Backed by the LLModelMode enum on the gateway side; values
    // are 'chat', 'responses', 'completion', 'embedding',
    // 'image_generation', 'audio_transcription', 'audio_speech',
    // 'video_generation', 'rerank', 'moderation', 'routing'.
    mode?: string;

    // Context window filters (in tokens). The catalog stores
    // `context_window` inside the `capabilities` JSONB column,
    // not as a top-level field; the search backend extracts and
    // casts it to int for these comparisons. Models that omit the
    // field are excluded from the result rather than treated as
    // matching an arbitrarily-large window.
    min_context_window?: number;
    max_context_window?: number;

    // Cost filters (USD per 1M tokens)
    min_cost_per_input_token?: number;
    max_cost_per_input_token?: number;
    min_cost_per_output_token?: number;
    max_cost_per_output_token?: number;

    // Tag search
    tags?: string[];

    // Status filters
    enabled_only?: boolean;
    healthy_only?: boolean;

    // Sorting
    sort_by?: 'name' | 'created_at' | 'updated_at' | 'cost_per_input_token' | 'cost_per_output_token' | 'avg_latency_ms' | 'total_requests';
    sort_order?: 'asc' | 'desc';

    // Pagination
    offset?: number;
    limit?: number;
}

/**
 * Search response with pagination metadata
 */
export interface LLModelSearchResponse {
    models: LLModelConfig[];
    total: number;
    offset: number;
    limit: number;
    has_more: boolean;
    filters_applied: Record<string, any>;
}

/**
 * Search summary statistics
 */
export interface LLModelSearchSummary {
    total_models: number;
    providers: Record<string, number>;  // Provider -> count
    streaming_capable: number;
    function_calling_capable: number;
    vision_capable: number;
    batch_capable: number;
    cost_stats: {
        input: {
            min: number | null;
            max: number | null;
            avg: number | null;
        };
        output: {
            min: number | null;
            max: number | null;
            avg: number | null;
        };
    };
    enabled_count: number;
    disabled_count: number;
    healthy_count: number;
    unhealthy_count: number;
}

/**
 * Search with summary response
 */
export interface LLModelSearchWithSummaryResponse {
    search_results: LLModelSearchResponse;
    summary: LLModelSearchSummary;
}

/**
 * Autocomplete suggestion
 */
export interface ModelSearchSuggestion {
    field: string;
    suggestions: string[];
    total: number;
}

/**
 * LLM Runner Types
 *
 * Types for managing remote machines with LLM deployment backends
 * (Docker Model Runner, Ollama, vLLM, KServe, etc.).
 * Backend-agnostic — the adapter_type field determines which backend is used.
 */

// ============================================================================
// Adapter Types
// ============================================================================

export type AdapterType = 'dmr' | 'kserve' | 'llmd';

export const ADAPTER_LABELS: Record<AdapterType, string> = {
    dmr: 'Docker Model Runner',
    kserve: 'KServe (Kubernetes + vLLM)',
    llmd: 'llm-d (distributed vLLM)',
};

export const ADAPTER_DEFAULT_PORTS: Record<AdapterType, number> = {
    dmr: 12434,
    kserve: 0,  // KServe uses K8s API, not a port
    llmd: 0,   // llm-d uses K8s API, not a port
};

// ============================================================================
// Remote Machine Configuration
// ============================================================================

export type MachineStatus = 'online' | 'offline' | 'unreachable' | 'error';

/**
 * A remote machine configured for local LLM inference.
 * Backend-agnostic — adapter_type determines how the gateway communicates with it.
 */
export interface ModelRunnerMachine {
    id: string;
    name: string;
    description?: string;

    // Connection
    host: string;               // IP or hostname (e.g., "192.168.1.50")
    port: number;               // Backend REST API port
    adapter_type: AdapterType;  // Which deployment backend

    // Status
    status: MachineStatus;
    last_seen?: string;         // ISO timestamp of last successful connection test
    last_error?: string;

    // Cached info from last connection test (adapter-dependent structure)
    system_info?: Record<string, any>;

    // KServe / llm-d shared
    kube_context?: string;
    kube_namespace?: string;
    hf_token_secret?: string;

    // llm-d cluster-wide defaults (null for non-llmd machines)
    default_prefill_accelerator_type?: string;
    default_decode_accelerator_type?: string;
    prefill_nodepool_labels?: Record<string, string>;
    decode_nodepool_labels?: Record<string, string>;
    kv_cache_pool_config?: LLMDKVCacheConfig;
    default_tensor_parallel?: number;

    // Metadata
    tenant_id: string;
    created_at: string;
    updated_at: string;
    created_by?: string;
}

// ============================================================================
// Machine CRUD Requests
// ============================================================================

export interface CreateMachineRequest {
    name: string;
    description?: string;
    host?: string;              // DMR only
    port?: number;              // DMR only
    adapter_type?: AdapterType; // Default: 'dmr'
    // KServe / llm-d shared
    kube_context?: string;
    kube_namespace?: string;
    hf_token_secret?: string;
    // llm-d cluster-wide defaults
    default_prefill_accelerator_type?: string;
    default_decode_accelerator_type?: string;
    prefill_nodepool_labels?: Record<string, string>;
    decode_nodepool_labels?: Record<string, string>;
    kv_cache_pool_config?: LLMDKVCacheConfig;
    default_tensor_parallel?: number;
}

export interface MachineListResponse {
    machines: ModelRunnerMachine[];
    total: number;
}

// ============================================================================
// Connection Test
// ============================================================================

export interface ConnectionTestResult {
    success: boolean;
    model_runner_reachable: boolean;    // Backend API reachable
    openai_api_reachable: boolean;      // Inference endpoint reachable
    system_info?: Record<string, any>;  // Adapter-dependent info
    latency_ms: number;
    error?: string;
    warnings: string[];
}

// ============================================================================
// Cluster node survey (Kubernetes-backed adapters only)
//
// Returned by GET /v1/admin/llm-runner/machines/{id}/cluster-nodes. Feeds:
//   - The accelerator dropdown in the Deploy-Model form
//   - A GPU capacity banner next to the machine card
// DMR machines return an empty survey; Kubernetes adapters (KServe, llm-d)
// populate it via CoreV1Api.list_node.
// ============================================================================

export interface NodeTaint {
    key: string;
    value: string;
    effect: string;  // NoSchedule / PreferNoSchedule / NoExecute
}

export interface ClusterNodeSummary {
    name: string;
    accelerator_label?: string | null;
    gpu_capacity: number;
    gpu_allocatable: number;
    ready: boolean;
    schedulable: boolean;
    taints: NodeTaint[];
    labels: Record<string, string>;
}

export interface ClusterNodeSurveyResponse {
    accelerators: string[];
    total_gpu_capacity: number;
    total_gpu_allocatable: number;
    gpu_node_count: number;
    has_gpu_nodes: boolean;
    nodes: ClusterNodeSummary[];
    // Non-empty when the survey failed. UI shows an error banner
    // instead of the dropdown when this is set.
    error: string;
}

// ============================================================================
// K8s deployment readiness (Gap 4 — startup preflight snapshot)
//
// Returned by GET /v1/admin/llm-runner/preflight. Mirrors the structure
// `check_k8s_deployment_readiness()` logs at startup so the admin UI
// can show a readiness panel without tailing container logs.
// ============================================================================

export interface CloudCliStatus {
    provider: string;               // "aws" / "gcp" / "azure"
    cli_path?: string | null;
    plugin_path?: string | null;    // exec-credential plugin (GKE/AKS)
    credentials_present: boolean;
    ready: boolean;
}

export interface K8sDeploymentReadinessResponse {
    ready: boolean;
    reasons: string[];
    warnings: string[];
    kubeconfig_path?: string | null;
    in_cluster: boolean;
    aws_cli_path?: string | null;
    cloud_clis: Record<string, CloudCliStatus>;
}

// ============================================================================
// Model Catalog (curated models available for deployment)
// ============================================================================

export interface CatalogModel {
    name: string;               // e.g., "ai/meta-llama/Llama-3.3-70B-Instruct"
    display_name: string;       // e.g., "Llama 3.3 70B Instruct"
    family: string;             // e.g., "llama", "qwen", "mistral", "gemma"
    parameter_count?: string;   // e.g., "70B", "8B", "3B"
    quantization?: string;      // e.g., "Q4_K_M", "Q8_0", "F16"
    size_gb?: number;           // Download size
    context_length?: number;    // e.g., 131072
    description?: string;
    tags: string[];             // e.g., ["instruction-tuned", "coding", "multilingual"]
    requires_gpu_memory_mb?: number;
}

export interface ModelCatalogResponse {
    models: CatalogModel[];
    total: number;
    source: string;             // e.g., "docker-model-runner", "manual"
}

// ============================================================================
// Deployed Models (running on a machine)
// ============================================================================

export type DeployedModelStatus = 'pulling' | 'starting' | 'running' | 'stopped' | 'error' | 'removing';

export interface DeployedModel {
    model_name: string;         // e.g., "ai/meta-llama/Llama-3.3-8B-Instruct"
    display_name: string;
    machine_id: string;
    status: DeployedModelStatus;

    // Endpoint info (once running)
    inference_url?: string;

    // Model details
    parameter_count?: string;
    quantization?: string;
    context_length?: number;
    size_gb?: number;

    // Registration with gateway
    registered_as_llm?: boolean;
    llm_model_id?: string;      // ID in gateway's LLM models table

    // Metadata
    deployed_at?: string;
    started_at?: string;
    stopped_at?: string;
    error_message?: string;
}

export interface DeployedModelListResponse {
    models: DeployedModel[];
    total: number;
    machine_id: string;
    machine_name: string;
}

// ============================================================================
// Model Operations
// ============================================================================

export interface PullModelRequest {
    model_name: string;         // e.g., "ai/meta-llama/Llama-3.3-8B-Instruct"
}

export interface PullModelResponse {
    success: boolean;
    model_name: string;
    message: string;
    status: DeployedModelStatus;
    error?: string;
}

export interface ModelActionResponse {
    success: boolean;
    model_name: string;
    machine_id: string;
    action: 'start' | 'stop' | 'remove';
    status: DeployedModelStatus;
    message: string;
    error?: string;
}

// ============================================================================
// Gateway Registration
// ============================================================================

/**
 * Register a deployed model as an LLM in the gateway's model table,
 * making it available for proxying through the gateway.
 */
export interface RegisterModelRequest {
    model_display_name?: string;    // Override display name in gateway
    provider_name?: string;         // Default: "openai"
    api_key?: string;               // If backend requires auth
}

export interface RegisterModelResponse {
    success: boolean;
    llm_model_id: string;           // ID in gateway's LLM models table
    model_name: string;
    inference_url: string;
    message: string;
}

// ============================================================================
// KServe Deployment
// ============================================================================

export interface KServeDeployRequest {
    model_name: string;
    gpu_count?: number;
    min_replicas?: number;
    max_replicas?: number;
    tensor_parallel?: number;
    auto_register?: boolean;
}

export interface KServeDeploymentStatus {
    model_name: string;
    service_name: string;
    namespace: string;
    ready: boolean;
    replicas_ready: number;
    replicas_desired: number;
    inference_url?: string;
    conditions: Array<{
        type?: string;
        status?: string;
        reason?: string;
        message?: string;
    }>;
    last_transition_time?: string;
    error?: string;
}

export interface LicenseCheckResult {
    model_id: string;
    license?: string;
    allowed: boolean;
    reason?: string;
}

// ============================================================================
// HuggingFace Catalog Search
// ============================================================================

export interface HFModel {
    model_id: string;               // e.g. "meta-llama/Llama-3.3-8B-Instruct"
    display_name: string;
    author?: string;
    downloads?: number;
    likes?: number;
    trending_score?: number;
    pipeline_tag?: string;
    tags: string[];
    last_modified?: string;
    library_name?: string;
    gated?: boolean;
}

export interface HFCatalogSearchResponse {
    models: HFModel[];
    total: number;
    source: string;
    query?: string;
}

export interface HFModelDetail {
    model_id: string;
    display_name: string;
    author?: string;
    downloads?: number;
    likes?: number;
    pipeline_tag?: string;
    library_name?: string;
    gated?: string;
    license?: string;
    architecture?: string;
    parameter_count?: number;
    parameter_count_label?: string;
    context_length?: number;
    model_size_bytes?: number;
    model_size_label?: string;
    languages?: string[];
    tags: string[];
    inference_url?: string;
}

// ============================================================================
// Helper Functions
// ============================================================================

export function getMachineStatusColor(status: MachineStatus): string {
    switch (status) {
        case 'online':
            return 'text-green-700 bg-green-100';
        case 'offline':
            return 'text-secondary bg-surface-raised';
        case 'unreachable':
            return 'text-amber-700 bg-amber-100';
        case 'error':
            return 'text-red-700 bg-red-100';
        default:
            return 'text-secondary bg-surface-raised';
    }
}

export function getModelStatusColor(status: DeployedModelStatus): string {
    switch (status) {
        case 'running':
            return 'text-green-700 bg-green-100';
        case 'pulling':
        case 'starting':
            return 'text-blue-700 bg-blue-100';
        case 'stopped':
            return 'text-secondary bg-surface-raised';
        case 'error':
            return 'text-red-700 bg-red-100';
        case 'removing':
            return 'text-amber-700 bg-amber-100';
        default:
            return 'text-secondary bg-surface-raised';
    }
}

export function getAdapterLabel(adapterType: AdapterType): string {
    return ADAPTER_LABELS[adapterType] || adapterType;
}

/**
 * Format model parameters (billions) for display.
 * Examples: 0.5 → "500M", 1.3 → "1.3B", 8 → "8B", 70 → "70B", 405 → "405B"
 */
export function formatModelParameters(params: number | undefined | null): string | null {
    if (params == null) return null;
    if (params < 1) {
        const millions = Math.round(params * 1000);
        return `${millions}M`;
    }
    // Show one decimal if not a whole number
    const label = Number.isInteger(params) ? params.toString() : params.toFixed(1);
    return `${label}B`;
}

/**
 * Check if a model is a virtual routing-group model.
 * Routing models represent a load-balanced routing group — they are not real LLM deployments.
 */
export function isRoutingModel(model: LLModelConfig): boolean {
    return model.mode === 'routing';
}

/**
 * Get the routing group ID from a virtual routing model.
 * The model_name field holds the routing group ID (set during creation).
 */
export function getRoutingGroupId(model: LLModelConfig): string | null {
    if (model.mode !== 'routing') return null;
    return model.model_name || null;
}

/**
 * The system Default resource group ID.
 * Models in Default group are accessible to all users.
 */
export const DEFAULT_GROUP_ID = 'default';

/**
 * Check if a model is in the Default resource group (i.e. accessible to all users).
 */
export function isInDefaultGroup(model: LLModelConfig): boolean {
    return model.groups?.some(g => g.id === DEFAULT_GROUP_ID) ?? false;
}

/**
 * Check if a model is restricted (NOT in the Default group).
 */
export function isGroupRestricted(model: LLModelConfig): boolean {
    if (!model.groups || model.groups.length === 0) return false;
    return !model.groups.some(g => g.id === DEFAULT_GROUP_ID);
}

// ============================================================================
// llm-d (LLMInferenceService) Deployment
// ============================================================================
//
// Distinct from KServeDeployRequest because the llm-d CRD splits the
// deployment into two roles (prefill + decode) with independent
// replica counts, accelerators, and node-selector labels. Sharing a
// request schema would force every field to be optional on both
// sides and lose type clarity.

/** Shared KV cache pool for an llm-d LLMInferenceService. */
export interface LLMDKVCacheConfig {
    backend: 'local' | 'redis' | 'p2p';
    size_gb?: number;
    connection_uri?: string;
}

/** Per-role (prefill or decode) deployment settings. */
export interface LLMDRoleConfig {
    replicas: number;
    accelerator_type: string;
    gpu_count: number;
    tensor_parallel: number;
    nodepool_labels?: Record<string, string>;
    extra_args?: string[];
    extra_env?: Record<string, string>;
}

export interface LLMDDeployRequest {
    machine_id: string;
    model_name: string;
    prefill: LLMDRoleConfig;
    decode: LLMDRoleConfig;
    kv_cache_pool?: LLMDKVCacheConfig;
    scheduler_hints?: Record<string, unknown>;
    auto_register?: boolean;
}

export interface LLMDDeploymentStatus {
    model_name: string;
    service_name: string;
    namespace: string;
    ready: boolean;
    replicas_ready: number;
    replicas_desired: number;
    inference_url?: string;
    conditions: Array<{
        type?: string;
        status?: string;
        reason?: string;
        message?: string;
    }>;
    last_transition_time?: string;
    error?: string;
}

export interface LLMDAcceleratorOption {
    id: string;
    label: string;
    category: 'gpu' | 'cpu';
}

export interface LLMDAcceleratorListResponse {
    accelerators: LLMDAcceleratorOption[];
}

// =============================================================================
// Routing-group in-process micro-cache config (per tenant)
// =============================================================================
//
// Backs the small exact-match cache inside the Rust proxy's
// `dispatch_routing_group_request`. Distinct from the platform-
// wide semantic cache configured under Semantic Cache. Surfaced
// in the admin UI under AI Models → Routing → Routing Group
// Cache.

export interface RouterCacheConfig {
    /** When false, the proxy skips this cache entirely for the
     *  tenant — every routing-group dispatch hits upstream. */
    enabled: boolean;
    /** Seconds (0–86400) — cached-response freshness window. */
    ttl_secs: number;
    /** ISO timestamp of the last config change; null on the
     *  first read (when the row was auto-materialised). */
    updated_at?: string | null;
}
