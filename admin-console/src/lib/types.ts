/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import type { components, paths } from './generated/control-plane-api';


import type { ServerHealthStatus } from './serverHealth';

// The health of an MCP server, as the Control Plane's contract types it. What
// each status reads as is in `serverHealth.ts`.
export type { ServerHealthStatus } from './serverHealth';

// MCP Server configuration interface
export interface MCPServerConfig {
    id: string;
    name: string;
    description: string;

    // AI Resource identity block. `version` is operator-authored;
    // `created_by` / `updated_by` are system-stamped and read-only.
    version?: string | null;
    created_by?: string | null;
    updated_by?: string | null;
    created_at?: string | null;
    updated_at?: string | null;

    // Separate base URL and MCP endpoint configuration
    base_url: string;                      // e.g., "http://localhost:3000" or "https://api.githubcopilot.com/mcp"
    mcp_endpoint_path: string;             // e.g., "/api/mcp" or "" if MCP is at root

    // Authentication
    auth_type: 'none' | 'bearer' | 'oauth';
    /**
     * The static token of a bearer server. Write-only: never returned. In
     * an update, left out keeps the stored token, an empty string or null
     * removes it, a value replaces it.
     */
    auth_token?: string | null;
    /** Whether a static token is stored. Read-only. */
    has_auth_token?: boolean;
    idp_id?: string;
    idp_name?: string;                     // IdP display name (fetched from backend)
    /**
     * For an OAuth-protected server, what the Control Plane states of it as
     * stored: whether an identity provider is paired with it (the gateway
     * exchanges an authorization code only for a paired one), which, and the
     * Control Plane's warning when none is. Null for any other server.
     * Read-only: the Control Plane ignores it in a save.
     */
    oauth_pairing?: { paired: boolean; idp_ids?: string[]; warning?: string | null } | null;

    // Connection settings
    timeout: number;
    enabled: boolean;

    /**
     * Requests to this server in flight at once on one gateway instance
     * (every MCP request counts). Each instance of a cluster allows this
     * many. A positive whole number; null is the Control Plane's default of 10.
     */
    max_concurrent_requests?: number | null;

    // Tenant-wide default Limits / Policies for this server (min-wins with each
    // Resource Group). The limits are an MCP limits document; the policies an
    // opaque blob edited by the classic MCP form.
    tenant_limits?: components['schemas']['McpLimitsDocument'] | null;
    tenant_governance?: Record<string, any> | null;

    // MCP Apps governance (https://modelcontextprotocol.io/extensions/apps).
    // When false, the gateway suppresses `_meta.ui.resourceUri` for tools
    // served by this server — the user portal won't render iframe-based
    // UI bundles. Default true. Set false for compliance-bound tenants
    // who need to block iframe-rendered tool output.
    mcp_apps_enabled?: boolean;

    /**
     * Streamable-HTTP SSE listener affinity.
     *
     * MCP's Streamable HTTP transport defines an OPTIONAL
     * server-to-client event channel — a long-lived `GET
     * text/event-stream` clients open to receive notifications
     * (`tools.listChanged`, resource subscriptions,
     * server-emitted log messages, etc.). The gateway derives
     * this flag from the upstream's `initialize` response
     * capabilities: `true` iff the server declares any of
     * `tools.listChanged`, `resources.listChanged`,
     * `resources.subscribe`, `prompts.listChanged`, or
     * `logging`. When `false`, the gateway short-circuits
     * listener GETs to 405 and the portal SDK skips opening
     * the listener entirely — preventing the SSE reconnect
     * loop that pure request/response servers (e.g. AWS
     * Knowledge MCP) otherwise produce.
     *
     * Operator can override the derived value from the form when
     * the server's `initialize` response misrepresents its real
     * capabilities.
     */
    wants_listener?: boolean;
    /** Negotiated MCP protocol version at capability discovery, e.g. "2026-07-28". */
    protocol_version?: string | null;
    /** MCP wire dialect: 'handshake' | 'stateless'. NULL until discovered (treated as handshake). */
    dialect?: string | null;

    /**
     * Data residency. Geographic region this
     * MCP server serves traffic from (e.g. `us-east-1`,
     * `eu-west-1`, `global`). Read by the Rust runtime's residency
     * check and intersected with the tenant's `data_residency`
     * profile at request time; mismatches return 403.
     *
     * Blank / undefined means unspecified — the tenant's
     * `block_global` flag decides whether unspecified is allowed.
     */
    region?: string | null;

    // Health check configuration
    enable_health_check?: boolean;         // Enable health check for this server
    health_check_path?: string;            // Custom health check path (default: "/health")

    // Capabilities and discovery
    capabilities?: {
        tools?: any[];
        resources?: any[];
        prompts?: any[];
        resource_templates?: any[];
    };
    discovered_at?: string;
    server_info?: {
        name: string;
        version: string;
        protocol_version?: string;
    };

    // Health monitoring
    health_status?: ServerHealthStatus;

    // Server icon
    icon?: {
        type: string;
        url: string;
        mime_type: string;
        size: number;
        uploaded_at: string;
        available_sizes?: string[];
    };
}

// Inline tool-poisoning finding returned alongside MCP server create /
// update / refresh-capabilities. Mirrors `PoisonedToolFinding`
// (app/api/v1/models/admin/mcp_server.py) on the wire.
export interface PoisonedToolFinding {
    tool_name: string;
    field_kind: string;             // 'description' | 'name' | 'title'
    description_excerpt: string;    // first 500 chars of the matched field
    description_truncated: boolean;
    confidence: number;             // 0.0–1.0
    patterns_matched: number;
    suspicious_indicator_count: number;
}

// Response from POST /v1/admin/mcp-servers/{id}/discover-capabilities.
// Mirrors `RefreshCapabilitiesResponse` on the wire — see the Pydantic
// model for field-level docs.
export interface RefreshCapabilitiesResponse {
    success: boolean;
    server_id: string;
    discovered: boolean;
    tools_count: number;
    resources_count: number;
    resource_templates_count: number;
    prompts_count: number;
    drift_detected: boolean;
    poisoned_tools: PoisonedToolFinding[];
    message?: string;
}

// Virtual Server configuration interface
export interface VirtualServerConfig {
    id?: string;
    name: string;
    description: string;
    owner: string; // User ID who owns this virtual server
    enabled: boolean;
    capabilities: {
        tools?: Array<{
            server_id: string;
            capability_name: string;
        }>;
        resources?: Array<{
            server_id: string;
            capability_name: string;
        }>;
        prompts?: Array<{
            server_id: string;
            capability_name: string;
        }>;
    };
    created_at: string;
}

// Gateway statistics interface
export interface GatewayStats {
    total_servers: number;
    enabled_servers: number;
    virtual_servers: number;
    active_connections: number;
    total_requests: number;
    error_rate: number;
    uptime: number;
}

/**
 * Identity Provider Configuration. `protocol` is `oidc`, the console's one
 * protocol, or, for a stored provider the console shows locked, another
 * (`saml`).
 */
/**
 * NOT aliasable: the Control Plane's `IdPDetailResponse.idp` is a
 * bare `Dict[str, Any]`, so the generated schema carries no field at all. The
 * field set is instead pinned to the generated WRITE model (`IdPConfigRequest`),
 * which is fully typed, so a Control Plane rename still breaks the build here;
 * only the server-assigned and runtime fields are declared by hand.
 *
 * Making the response itself typed needs a real `response_model` on the CP side.
 */
export interface IdPConfig extends Partial<components['schemas']['IdPConfigRequest']> {
    id: string;
    name: string;
    enabled: boolean;
    created_at?: string;
    updated_at?: string;
    verified?: boolean;
    is_default?: boolean;
}

// =============================================================================
// Audit Export Destinations — scheduled proxy_log export
// =============================================================================

/**
 * One configured export sink. The `config` shape varies by destination_type:
 *
 *   s3:         { bucket, prefix, region, access_key_id, endpoint_url, sse, sse_kms_key_id, has_secret_access_key }
 *   webhook:    { url, has_bearer_token, has_headers, header_names }
 *   splunk_hec: { url, index, sourcetype, source, host, has_token }
 *
 * A response never carries a secret: `has_secret_access_key`,
 * `has_bearer_token`, `has_token` and `has_headers` say whether one is
 * stored. A request names a secret under its own name
 * (`secret_access_key`, `bearer_token`, `token`, `headers`).
 */
export interface AuditExportDestination {
    id: string;
    tenant_id: string;
    name: string;
    destination_type: 's3' | 'webhook' | 'splunk_hec';
    enabled: boolean;
    config: Record<string, unknown>;
    /**
     * Optional list of proxy-log column names to ship. Null ships every
     * column. The Control Plane refuses a list that names a column that
     * does not exist, or none.
     */
    field_allowlist?: string[] | null;
    interval_seconds: number;
    last_exported_at?: string | null;
    last_run_at?: string | null;
    last_run_status?: 'success' | 'no_data' | 'failure' | 'running' | null;
    last_run_rows?: number | null;
    last_run_error?: string | null;
    created_at?: string;
    updated_at?: string;
    created_by?: string | null;
}

export interface AuditExportDestinationListResponse {
    destinations: AuditExportDestination[];
    total: number;
}

export interface AuditExportRunResult {
    destination_id: string;
    outcome: 'success' | 'no_data' | 'failure';
    rows_exported: number;
    window_start?: string | null;
    window_end?: string | null;
    error?: string | null;
}

export interface OAuthAppClient {
    id: string;
    name: string;
    app_type: 'webapp' | 'mobile' | 'desktop' | 'cli' | 'service';
    idp_id: string;
    client_id: string;
    redirect_uri: string;
    scopes: string[];
    client_type: 'confidential' | 'public' | 'public_with_secret';
    supports_pkce: boolean;
    allowed_grant_types: string[];
    description?: string;
    registered_at: string;
    registered_by?: string;
    last_used?: string;
    is_active: boolean;
    /**
     * True when this client was created via RFC 7591
     * inbound DCR (a self-registering MCP / OAuth client hit
     * `/v1/auth/dcr/{tenant}/{idp}/register` rather than being
     * created by an admin). Surfaced on the admin UI with a badge
     * so operators can spot + revoke unexpected entries.
     */
    auto_registered?: boolean;
}

export interface ServerConfig {
    id: string;
    // Exactly ONE of mcp_server_id / virtual_server_id
    // is non-null. `server_kind` is the discriminator the admin
    // UI uses for badges, dropdown defaults, and the Edit form.
    mcp_server_id?: string | null;
    virtual_server_id?: string | null;
    server_kind?: 'mcp' | 'vmcp';
    oauth_client_id?: string | null;
    name: string;
    description?: string | null;
    priority: number;
    is_default: boolean;
    enabled: boolean;
    effective_enabled?: boolean;
    display_name?: string | null;
    display_description?: string | null;
    icon_url?: string | null;
    usage_count: number;
    /** Whether the server takes OAuth, and whether it takes no credential at all. */
    requires_oauth?: boolean;
    is_open_server?: boolean;
    last_used?: string | null;
    // Audit info. No `version`: a server config takes that from the MCP
    // server it configures.
    created_by?: string | null;
    updated_by?: string | null;
    created_at?: string;
    updated_at?: string;
    group_ids?: string[] | null;
    groups?: Array<{
        id: string;
        name: string;
        display_name?: string;
    }> | null;
    mcp_server?: {
        name: string;
        enabled?: boolean;
        auth_type?: string;
        icon?: {
            url?: string;
            uploaded_at?: string;
        };
    } | null;
    // VMCP-backed configs carry this instead of mcp_server.
    virtual_server?: {
        id: string;
        name: string;
        description?: string;
        enabled?: boolean;
        tool_count?: number;
    } | null;
    oauth_client?: {
        name: string;
        app_type?: string;
    } | null;
}

// ============================================================================
// CORE LOG ENTRY TYPE (matches ProxyLog database model to_dict())
// ============================================================================

export interface UnifiedProxyLogEntry {
    // Core identification (from DB columns)
    id: string;
    tenant_id: string;
    timestamp: string; // ISO 8601

    // Proxy type classification
    proxy_type: 'mcp' | 'virtual-mcp' | 'llm' | 'skill' | 'oauth' | 'a2a' | 'batch' | 'portal';
    proxy_subtype?: string;

    // Request correlation & tracing
    correlation_id?: string;
    parent_request_id?: string;
    request_id?: string;

    // user fields
    gateway_user_id?: string;
    gateway_user_username?: string;
    idp_user_id?: string;
    idp_user_username?: string;
    idp_user_email?: string;

    session_id: string;
    client_ip?: string;
    user_agent?: string;

    // Target server/resource
    server_id?: string;
    idp_server_id?: string;

    // Request details
    http_method: string;
    endpoint: string;
    target_url?: string;
    request_size_bytes?: number;

    // Request object with body preview and headers
    request?: {
        method: string;
        endpoint: string;
        target_url?: string;
        size_bytes?: number;
        headers?: string; // JSON string
        body_preview?: string; // Present only for callers holding proxy:logs:read_bodies
        /** Why a body may be empty: available | withheld | pruned | erased | none */
        body_state?: 'available' | 'withheld' | 'pruned' | 'erased' | 'none';
    };

    // Response details
    http_status_code?: number;
    response_size_bytes?: number;

    // Response object with body preview and headers
    response?: {
        status_code?: number;
        size_bytes?: number;
        duration_ms?: number;
        headers?: string; // JSON string
        body_preview?: string; // Present only for callers holding proxy:logs:read_bodies
        /** Why a body may be empty: available | withheld | pruned | erased | none */
        body_state?: 'available' | 'withheld' | 'pruned' | 'erased' | 'none';
    };

    // Success/error
    success: boolean;
    error_message?: string;
    error_type?: string;

    // Performance
    duration_ms?: number;
    cache_hit?: boolean;

    // MCP-specific fields (nested in to_dict() output)
    mcp?: {
        method?: string;
        request_id?: string;
        capability_name?: string;
        protocol_version?: string;
        streaming?: boolean;
    };

    // LLM-specific fields (nested in to_dict() output)
    llm?: {
        provider?: string;
        model_requested?: string;
        model_used?: string;
        prompt_tokens?: number;
        completion_tokens?: number;
        total_tokens?: number;
        streaming?: boolean;
        temperature?: number;
        max_tokens?: number;
        function_calling?: boolean;
        used_fallback?: boolean;
        fallback_reason?: string;
        router_rule_id?: string;
        estimated_cost_usd?: number;
        cache_hit?: boolean;
        cache_layer?: string;
        tokens_saved?: number;
        cache_similarity_score?: number;
    };

    // OAuth-specific fields (nested in to_dict() output)
    oauth?: {
        idp_id?: string;
        flow_type?: string;
        grant_type?: string; // Keeping for compatibility
        scope?: string;
        code_exchange?: boolean; // Keeping for compatibility
        token_refresh?: boolean; // Keeping for compatibility
    };

    // Optional metadata
    custom_metadata?: Record<string, any>;
    tags?: string[];

    // Multi-step agent trace.
    // The root of a chain that may span LLM, MCP, A2A, and skill
    // calls. Set when the audit row carries a call-chain header
    // tree; NULL on standalone (pre-Feature-1 or unchained) rows.
    root_task_id?: string | null;
    parent_task_id?: string | null;
    current_task_id?: string | null;
    delegation_depth?: number | null;
    trace_root_kind?: string | null;
    trace_continuity?: string | null;

    // Agent Identity — the acting agent principal (actor), the
    // delegating principal (subject), and the per-agent runtime-authz decision.
    // All null unless the request's actor was an agent.
    actor_agent_id?: string | null;
    subject_id?: string | null;
    subject_kind?: string | null;
    agent_authz_decision?: string | null;
    agent_grant_id?: string | null;

    // Decision record — which policy decided, under what authority,
    // via which exception path, against which upstream call. Null means no
    // such decision was taken on this call, never "we didn't record it".
    argument_policy_id?: string | null;
    guardrail_config_id?: string | null;
    /** pii | prompt_injection | jailbreak | toxic_content | banned_words | banned_pattern | secrets */
    guardrail_check?: string | null;
    /** block | redact | warn — a redaction changed what went upstream; a warn changed nothing. */
    guardrail_action?: string | null;
    /** autonomous | approval_required | restricted | suspended */
    autonomy_level?: string | null;
    /** live | last_known_good | floor — anything but `live` means this was
     *  enforced while the governance database was unreachable. */
    autonomy_provenance?: string | null;
    consumed_approval_request_id?: string | null;
    upstream_request_id?: string | null;
    /** State the quota/budget decision was taken against. The limits
     *  it was judged against are pinned by the AI System's contract_hash. */
    pre_action_state?: Record<string, unknown> | null;

    // Content digests, trace context, tool provenance, deciding model.
    /** SHA-256 of the request content as captured, sealed before retention
     *  truncates, redacts or drops the preview. Inside the row hash. */
    request_content_hash?: string | null;
    response_content_hash?: string | null;
    /** W3C trace context the caller asserted (`traceparent`), or the OTel
     *  span's own ids. Asserted, so outside the row hash. */
    trace_id?: string | null;
    span_id?: string | null;
    /** What the MCP server announced at initialize, and the SHA-256 of the
     *  tool definition in force for this call. Inside the row hash. */
    server_version?: string | null;
    tool_definition_hash?: string | null;
    /** The model whose completion decided this action — the run's most
     *  recent model call. Derived at write time; outside the row hash. */
    deciding_model?: string | null;

    // Compliance tags (`["gdpr_art_30",
    // "soc2.cc7.2", "eu_ai_act.high", ...]`). Derived by the
    // tagging engine when the tenant has a compliance_profile.
    //   - undefined / null = tenant has no profile (engine never ran)
    //   - []                = profile declared, this call fired no tags
    //   - [...]             = the tags that fired
    compliance_tags?: string[] | null;

    // Data residency outcome.
    //   - "allowed" / "blocked" / "not_evaluated"
    region_routed_to?: string | null;
    residency_class?: string | null;
}

// ============================================================================
// QUERY PARAMETERS (matches LogQueryRequest Pydantic model)
// ============================================================================

export interface ProxyLogsQueryParams {
    // Time filtering
    start_time?: string; // ISO 8601
    end_time?: string; // ISO 8601
    hours_ago?: number;

    // Proxy type filtering
    proxy_types?: readonly ('mcp' | 'virtual-mcp' | 'llm' | 'skill' | 'oauth' | 'a2a' | 'batch' | 'portal')[];
    proxy_subtypes?: string[];

    // Server filtering. A filter that takes several values is sent as a
    // list; the search matches any of them.
    server_ids?: readonly string[];
    idp_server_id?: string;

    // User filter fields
    gateway_user_ids?: readonly string[];
    idp_user_ids?: string[];
    idp_user_usernames?: readonly string[];
    idp_user_email?: string;

    session_id?: string;

    // Status filtering
    success?: boolean;

    // Correlation tracking
    correlation_id?: string;
    parent_request_id?: string;
    request_id?: string;

    // MCP-specific filters
    mcp_method?: string;
    mcp_capability_names?: readonly string[];

    // LLM-specific filters
    llm_providers?: string[];
    llm_model_requested?: string;
    llm_models_used?: string[];
    min_tokens?: number;
    max_cost_usd?: number;
    llm_streaming?: boolean;
    llm_function_calling?: boolean;
    llm_used_fallback?: boolean;

    // OAuth-specific filters
    oauth_flow_type?: string;

    // Network + routing
    client_ip?: string;
    target_urls?: readonly string[];
    http_status_codes?: number[];
    region_routed_to?: readonly string[];
    residency_class?: string[];

    // Free-text search across body previews + target_url + error_message.
    // Backed server-side by a GIN tsvector index.
    q?: string;

    // Performance filtering
    min_duration_ms?: number;
    max_duration_ms?: number;

    // Rows whose compliance_tags hold any of these (e.g.
    // "eu_ai_act.high", "soc2.cc7.2", "gdpr_art_30").
    compliance_tags_any?: readonly string[];

    // Pagination
    limit?: number;
    offset?: number;

    // Sorting
    sort_by?: string;
    sort_order?: 'asc' | 'desc';
}

// ============================================================================
// EXPORT PARAMETERS (the query parameters of GET /v1/admin/users/audit-logs/export)
// ============================================================================

type AuditLogsExportQuery = NonNullable<paths['/v1/admin/users/audit-logs/export']['get']['parameters']['query']>;

/**
 * What the export of the admin audit log filters by: its query parameters in
 * the Control Plane's contract, without the tenant and the format. They are
 * the filters of the list: administrators, actions, resource types, IP
 * addresses and HTTP methods (several of each, matched exactly), the
 * outcome, a time window and free text.
 */
export type AuditLogsExportParams = {
    [K in Exclude<keyof AuditLogsExportQuery, 'tenant_id' | 'format'>]?: NonNullable<AuditLogsExportQuery[K]>;
};

// ============================================================================
// STATS QUERY PARAMETERS
// ============================================================================

export interface ProxyStatsQueryParams {
    // Time filtering
    start_time?: string;
    end_time?: string;
    hours_ago?: number;

    // Filters
    proxy_types?: Array<'mcp' | 'virtual-mcp' | 'llm' | 'skill' | 'oauth' | 'a2a' | 'batch' | 'portal'>;
    server_id?: string;
    user_type?: string;
    username?: string;
    user_id?: string;
}

// ============================================================================
// API RESPONSES (matches Pydantic response models)
// ============================================================================

export interface AdminAccessInfo {
    admin_id: string;
    admin_username: string;
    tenant_id: string;
}

export interface PaginationInfo {
    total: number;
    offset: number;
    limit: number;
    returned: number;
    has_more: boolean;
}

export interface ProxyLogsResponse {
    logs: UnifiedProxyLogEntry[];
    pagination: PaginationInfo;
    filters_applied: Record<string, any>;
    accessed_by: AdminAccessInfo;
    timestamp: string;
}

export interface RequestThreadResponse {
    correlation_id: string;
    thread_length: number;
    logs: UnifiedProxyLogEntry[];
    accessed_by: AdminAccessInfo;
    timestamp: string;
}

export interface UserActivityResponse {
    user_id: string;
    username?: string;
    period_start: string;
    period_end: string;
    total_requests: number;
    successful_requests: number;
    failed_requests: number;
    logs: UnifiedProxyLogEntry[];
    accessed_by: AdminAccessInfo;
    timestamp: string;
}

export interface MCPStats {
    total_requests: number;
    successful_requests: number;
    failed_requests: number;
    success_rate: number;
    avg_duration_ms: number;
    top_methods?: Array<{
        method: string;
        count: number;
    }>;
    top_tools?: Array<{
        tool_name: string;
        count: number;
    }>;
}

export interface LLMStats {
    total_requests: number;
    successful_requests: number;
    failed_requests: number;
    success_rate: number;
    avg_duration_ms: number;
    total_tokens: number;
    total_cost_usd: number;
    avg_tokens_per_request: number;
    avg_cost_per_request: number;
    by_provider?: Record<string, {
        requests: number;
        tokens: number;
        cost_usd: number;
    }>;
    by_model?: Record<string, {
        requests: number;
        tokens: number;
        cost_usd: number;
    }>;
}

/** The OAuth figures of the proxy statistics, as the Control Plane's contract states them. */
export type OAuthStats = components['schemas']['OAuthStats'];

export interface VirtualMCPStats {
    total_requests: number;
    successful_requests: number;
    failed_requests: number;
    success_rate: number;
    avg_duration_ms: number;
}

export interface StatsByType {
    mcp?: MCPStats;
    llm?: LLMStats;
    oauth?: OAuthStats;
    virtual_mcp?: VirtualMCPStats;
}

export interface ProxyStatsResponse {
    period_start: string;
    period_end: string;
    total_requests: number;
    successful_requests: number;
    failed_requests: number;
    success_rate: number;
    avg_duration_ms: number;
    avg_request_size_bytes: number;
    avg_response_size_bytes: number;
    stats_by_type: StatsByType;
    accessed_by: AdminAccessInfo;
    timestamp: string;
}

export interface ProxyLogsExportResponse {
    message: string;
    format: 'csv' | 'json';
    download_url?: string;
    data?: string;
    file_size_bytes?: number;
    record_count: number;
    exported_by: AdminAccessInfo;
    timestamp: string;
}

export interface ProxyLogsCleanupResponse {
    message: string;
    deleted_count: number;
    days_to_keep: number;
    tenant_id: string;
    deleted_by: AdminAccessInfo;
    timestamp: string;
}

export interface ProxySystemHealthResponse {
    status: 'healthy' | 'degraded' | 'unhealthy';
    database_connection: boolean;
    total_logs: number;
    oldest_log_timestamp?: string;
    newest_log_timestamp?: string;
    logs_last_24h: number;
    errors_last_24h: number;
    avg_write_latency_ms?: number;
    storage_usage_mb?: number;
    issues: string[];
    timestamp: string;
}

// ============================================================================
// MCP Connection Test Data
// ============================================================================

export interface ServerConnectionTestData {
    url: string;
    auth_type: string;
    timeout?: number;
}

export interface ServerConnectionTestResult {
    // Basic connection status
    status: 'success' | 'partial_success' | 'error' | 'warning';
    message?: string;
    error_type?: string;
    duration_ms: number;
    http_status?: number;
    content_type?: string;

    // Server information (from MCP initialize response)
    server_info?: {
        name: string;
        version: string;
        protocol_version?: string;
    };

    // MCP capabilities
    capabilities_summary?: {
        tools: number;
        resources: number;
        prompts: number;
        resource_templates: number;
    };

    // Session status
    session_established?: boolean;

    // Helpful suggestions for fixing errors
    suggestions?: string[];

    // ========================================
    // OAuth Discovery Fields
    // ========================================
    // These fields are populated when the server returns a 401
    // with OAuth discovery information

    /**
     * OAuth discovery information from WWW-Authenticate header
     * or .well-known/oauth-protected-resource response
     */
    oauth_discovery?: {
        auth_scheme: string;                // e.g., "Bearer"
        error?: string;                     // OAuth error code
        error_description?: string;         // Human-readable error
        resource_metadata?: string;         // URL to resource metadata
        realm?: string;                     // OAuth realm
        scope?: string;                     // Required scopes
        issuer?: string;                    // OAuth issuer URL
        authorization_endpoint?: string;    // Authorization URL
        token_endpoint?: string;            // Token exchange URL
        well_known_url?: string;           // .well-known URL used
        discovery_method?: string;          // How OAuth was discovered ("www_authenticate" | "body_analysis")
        oauth_docs_url?: string;           // Provider's OAuth documentation URL
    };

    /**
     * Suggested IdP configuration based on OAuth discovery
     * This is populated when OAuth is discovered and no matching IdP exists
     */
    suggested_idp_config?: {
        id: string;                         // Generated IdP ID
        name: string;                       // Display name for IdP
        issuer?: string;                    // OAuth issuer URL (null for skeleton IdPs)
        type: string;                       // IdP type (e.g., "oidc_discoverable", "skeleton")
        well_known_url?: string;           // OIDC discovery URL
        authorization_endpoint?: string;    // Authorization endpoint
        token_endpoint?: string;            // Token endpoint
        jwks_uri?: string;                 // JSON Web Key Set URI
        userinfo_endpoint?: string;         // User info endpoint
        revocation_endpoint?: string;       // Token revocation endpoint
        introspection_endpoint?: string;    // Token introspection endpoint
        scopes_supported?: string[];        // Supported OAuth scopes
        response_types_supported?: string[]; // Supported response types
        grant_types_supported?: string[];   // Supported grant types
        token_endpoint_auth_methods?: string[]; // Supported auth methods
        supports_pkce?: boolean;            // PKCE support
        supports_refresh_tokens?: boolean;  // Refresh token support
        description?: string;               // IdP description
        oidc_discovered?: boolean;          // Whether OIDC discovery succeeded
        enabled?: boolean;                  // Whether IdP should be enabled
        skeleton?: boolean;                 // Whether this is a skeleton config needing manual setup
        notes?: string;                     // Notes about the configuration
    };

    /**
     * IdP ID (existing match or suggested skeleton)
     */
    idp_id?: string;

    /**
     * Existing IdP configuration that matches the discovered OAuth issuer
     * This is populated when OAuth is discovered and a matching IdP already exists
     */
    existing_idp?: {
        id: string;                         // IdP database ID
        name: string;                       // IdP display name
        enabled: boolean;                   // Whether IdP is enabled
        issuer: string;                     // OAuth issuer URL
    };
}

// ============================================================================
// Saved Log Views — per-tenant named filter presets shared by the
// proxy log + admin audit log viewers. `filters` carries the request envelope the
// viewer would have POSTed to /search at the time the view was saved.
// ============================================================================

export type SavedLogViewScope = 'proxy_logs' | 'admin_audit_logs';

export interface SavedLogView {
    id: string;
    tenant_id: string;
    scope: SavedLogViewScope;
    owner_admin_id: string | null;
    name: string;
    description: string | null;
    filters: Record<string, any>;
    is_shared: boolean;
    created_at: string | null;
    updated_at: string | null;
}

// ============================================================================
// HELPER FUNCTIONS
// ============================================================================

/**
 * Get capability display name.
 *
 * MCP, virtual-MCP, and A2A all surface their dotted capability under
 * `mcp.capability_name` (the column is named that historically; A2A
 * reuses it for its own capability field so the `Capability` column
 * works for both proxy types without a schema rename).
 *
 * Preference order:
 *   1. `mcp.capability_name` — the actual tool/resource/prompt name
 *      a `tools/call` (or similar) request invoked. This is what
 *      operators care about most.
 *   2. `mcp.method` — the JSON-RPC method itself (e.g. `tools/list`,
 *      `initialize`, `prompts/get`). Falls back here so the
 *      Capability column carries the connect/discovery handshake
 *      methods too, keeping the Method column dedicated to the HTTP
 *      verb: `POST (tools/list)` there would conflate transport with
 *      payload.
 */
export function getCapabilityDisplay(log: UnifiedProxyLogEntry): string | null {
    if (log.mcp?.capability_name) {
        return log.mcp.capability_name;
    }
    if (log.mcp?.method) {
        return log.mcp.method;
    }
    return null;
}

/**
 * Format duration in milliseconds to human-readable string
 */
export function formatDuration(ms?: number): string {
    if (ms === undefined || ms === null) return 'N/A';
    if (ms < 1) return '<1ms';
    if (ms < 1000) return `${Math.round(ms)}ms`;
    if (ms < 10000) return `${(ms / 1000).toFixed(2)}s`;
    return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * Format bytes to human-readable string (B, KB, MB, GB, TB).
 *
 * The unit index is clamped: an implausibly large payload size would
 * otherwise index past the table and render "1.0 undefined" in a log cell.
 */
export function formatBytes(bytes?: number): string {
    if (bytes === undefined || bytes === null) return 'N/A';
    if (bytes === 0) return '0 B';

    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(
        Math.max(Math.floor(Math.log(bytes) / Math.log(k)), 0),
        sizes.length - 1,
    );

    if (i === 0) return `${bytes} B`;

    return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

/**
 * Result of verifying one audit hash chain.
 *
 * Five verdicts rather than a boolean, because "not verified" covers four
 * situations an operator acts on differently:
 *   - `verified`   hashes link AND a trusted checkpoint covers them — the only
 *                  verdict that asserts integrity
 *   - `unattested` hashes link, nothing signed them (no signing key configured)
 *   - `gap`        sequence numbers missing: rows were deleted
 *   - `broken`     a hash does not match — tampering or corruption
 *   - `forged`     a signature verifies against a key we never trusted. Ranked
 *                  worst: a hash break can be corruption, an unauthorised
 *                  attestation is deliberate.
 *   - `unchained`  no chain columns; rows predate chaining. Not a pass, not a fail.
 */
export interface AuditChainReport {
    chain_id: string;
    verdict: 'verified' | 'unattested' | 'gap' | 'broken' | 'forged' | 'unchained';
    rows_checked: number;
    seq_start: number | null;
    seq_end: number | null;
    first_bad_seq: number | null;
    /** `[after, before]` pairs bracketing each missing run of sequences. */
    gaps: Array<[number, number]>;
    checkpoints_checked: number;
    checkpoints_valid: number;
    checkpoints_untrusted: number;
    /** Scan stopped at the limit — the verdict describes the range examined,
     *  not the whole chain. */
    truncated: boolean;
}
