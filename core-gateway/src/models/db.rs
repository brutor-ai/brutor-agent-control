// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
// SQLx row types matching the Python SQLAlchemy models.
// Only the columns the proxy layer actually reads/writes are included.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::FromRow;

// ---------------------------------------------------------------------------
// Tenant
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct Tenant {
    pub id: String,
    pub name: String,
    pub is_active: bool,
    pub suspended: bool,
    pub suspension_reason: Option<String>,
    // NB: the tenant quota columns are deliberately NOT mapped here — the
    // table no longer has the rate ceilings this struct once
    // carried (they were never queried), and resource quotas are enforced
    // by the control plane at create time, not the proxy. The one ceiling
    // the proxy enforces, media_storage_quota_bytes, is selected directly
    // in routes/files.rs.
    pub features: Option<serde_json::Value>,
    pub settings: Option<serde_json::Value>,
    /// Data residency profile.
    /// Shape: {"allowed_regions": ["eu-west-1",...], "block_global": bool}.
    /// NULL = no residency enforcement (existing tenants keep
    /// working). Consumed by `services::residency::check`.
    #[sqlx(default)]
    pub data_residency: Option<serde_json::Value>,
}

// ---------------------------------------------------------------------------
// API Key (read-only for proxy — validation + usage tracking)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct ApiKey {
    pub id: String,
    pub tenant_id: String,
    pub key_hash: String,
    pub key_prefix: String,
    pub name: String,
    pub scope: String,
    pub user_id: Option<i32>,
    pub user_email: Option<String>,
    pub group_id: Option<String>,
    // Agent Identity this key authenticates as (the actor), if any.
    pub agent_id: Option<String>,
    pub allowed_skill_ids: Option<serde_json::Value>,
    pub skill_rate_limit_daily: Option<i32>,
    pub expires_at: Option<DateTime<Utc>>,
    pub enabled: bool,
    pub revoked: bool,
    pub usage_count: i32,
    pub last_used_at: Option<DateTime<Utc>>,
    /// The platform service key; see `AuthContext::api_key_is_platform`.
    pub is_platform_service: bool,
    // Populated via LEFT JOIN when needed, not a direct column
    #[sqlx(default)]
    pub group_name: Option<String>,
    /// The bound group's `is_active`, via the same LEFT JOIN (None: no group,
    /// or the group row is gone).
    #[sqlx(default)]
    pub group_active: Option<bool>,
    /// Whether the agent identity the key is bound to stands (enabled,
    /// active, not revoked, not expired), read with the key by
    /// `auth::authenticate_request`. None: bound to no agent, or not read;
    /// an agent-bound key is accepted only on `Some(true)`.
    #[sqlx(default)]
    pub agent_stands: Option<bool>,
}

impl ApiKey {
    pub fn is_valid(&self) -> bool {
        self.enabled && !self.revoked && self.expires_at.map(|exp| exp > Utc::now()).unwrap_or(true)
    }

    pub fn is_global(&self) -> bool {
        self.scope == "global"
    }

    pub fn is_user_specific(&self) -> bool {
        self.scope == "user" && self.user_id.is_some()
    }

    pub fn is_group_specific(&self) -> bool {
        self.group_id.is_some()
    }
}

// ---------------------------------------------------------------------------
// End User
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct EndUser {
    pub id: i32,
    pub tenant_id: String,
    pub username: String,
    pub email: Option<String>,
    pub display_name: Option<String>,
    pub password_hash: String,
    pub is_active: bool,
    pub is_verified: bool,
    pub created_at: Option<DateTime<Utc>>,
    pub last_login: Option<DateTime<Utc>>,
    pub user_metadata: Option<serde_json::Value>,
    pub avatar_url: Option<String>,
    pub totp_enabled: Option<bool>,
    pub totp_secret: Option<String>,
    /// First time the user completed TOTP verification — kept for
    /// audit / "enabled on" display and nothing else.
    pub totp_verified_at: Option<DateTime<Utc>>,
    /// Hashed one-time backup codes. Stored as a JSON array of bcrypt
    /// hashes so a DB dump can't be used to bypass TOTP.
    pub backup_codes: Option<serde_json::Value>,
    /// Primary resource group — the user's default active runtime scope.
    /// NULL only for users with no group memberships.
    pub primary_group_id: Option<String>,
}

// ---------------------------------------------------------------------------
// MCP Server
// ---------------------------------------------------------------------------
/// The `mcp_servers.auth_type` of a server that is called with its stored
/// static token as the bearer.
pub const STATIC_TOKEN_AUTH_TYPE: &str = "bearer";

#[derive(Debug, Clone, FromRow)]
pub struct McpServer {
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub base_url: String,
    pub mcp_endpoint_path: Option<String>,
    pub auth_type: Option<String>,
    pub auth_token: Option<String>,
    pub timeout: Option<f64>,
    pub enabled: bool,
    /// MCP Apps governance flag — when False, the gateway suppresses
    /// `_meta.ui.resourceUri` for tools served by this server and
    /// refuses the `/v1/portal/mcp-apps/{server_id}/resource` proxy.
    /// `Option<bool>` so older DBs (column not yet present) are
    /// tolerated and treated as enabled by default. `None` ≡ enabled.
    pub mcp_apps_enabled: Option<bool>,
    pub capabilities: Option<serde_json::Value>,
    pub health_status: Option<String>,
    pub idp_id: Option<String>,
    /// Requests to this server in flight at once, per gateway instance (a
    /// semaphore of the process); NULL is
    /// `mcp_limits::DEFAULT_MAX_CONCURRENT_REQUESTS` (10). In an Enterprise
    /// cluster each instance allows this many; a cluster-wide ceiling is the
    /// group's `execution.max_concurrency_per_group`.
    pub max_concurrent_requests: Option<i32>,
    // Resilience config (per-server overrides for circuit breaker + retry)
    pub retry_attempts: Option<i32>,
    pub retry_after_ms: Option<i32>,
    pub failure_threshold: Option<i32>,
    pub recovery_timeout_secs: Option<i32>,
    /// Streamable-HTTP listener affinity — see
    /// `wants_listener` column comment in the `mcp_servers`
    /// table. `None` means "column doesn't exist yet" (mid-
    /// migration / cold deploy) and is treated as `true` so we
    /// don't silently change behaviour for servers we haven't
    /// probed yet. Inspected by the early gate in
    /// `routes::mcp::proxy_request` to short-circuit listener
    /// GETs at 405 instead of round-tripping upstream.
    pub wants_listener: Option<bool>,
    /// Region this MCP server serves from
    /// (e.g. `us-east-1`, `eu-west-1`, `global`). NULL means
    /// unspecified; the tenant's `data_residency.block_global`
    /// flag decides whether unspecified is allowed.
    #[sqlx(default)]
    pub region: Option<String>,
    /// MCP protocol version negotiated at capability
    /// discovery (e.g. `2025-06-18`, `2026-07-28`). NULL = not yet
    /// discovered.
    #[sqlx(default)]
    pub protocol_version: Option<String>,
    /// Wire dialect the upstream speaks: `handshake`
    /// (legacy initialize flow) or `stateless` (2026-07-28
    /// server/discover). NULL is treated as handshake.
    #[sqlx(default)]
    pub dialect: Option<String>,
}

impl McpServer {
    /// True when this server may serve MCP-Apps `_meta.ui.*` fields to
    /// portal clients. Treats `None` (column absent on a legacy DB)
    /// as enabled by default — flipping the default would silently
    /// break existing tenants on rolling deploys.
    pub fn mcp_apps_allowed(&self) -> bool {
        self.mcp_apps_enabled.unwrap_or(true)
    }

    /// True when discovery recorded this upstream as speaking the
    /// stateless 2026-07-28 dialect — the proxy must then bridge legacy
    /// clients (synthesize `initialize`, inject `_meta` + the
    /// MCP-Protocol-Version header). NULL/`handshake` → legacy flow.
    pub fn is_stateless_upstream(&self) -> bool {
        self.dialect.as_deref() == Some(crate::constants::mcp_protocol::dialect::STATELESS)
    }

    /// True when this server has declared (via its `initialize`
    /// response capabilities) that it wants the optional SSE
    /// listener channel opened. Treats `None` as true for the
    /// same reason `mcp_apps_allowed` does — back-compat on a
    /// rolling deploy.
    pub fn wants_listener_stream(&self) -> bool {
        self.wants_listener.unwrap_or(true)
    }

    /// The static bearer token this server is called with, opened: the ONE
    /// way to read `auth_token` (`tests/mcp_static_token_guard.rs`).
    ///
    /// * `Ok(None)`: the server is not called with a static token. Its
    ///   `auth_type` is not `bearer` (`none`, `oauth`, absent, or a value the
    ///   gateway does not know), or nothing is stored. The stored value of a
    ///   server that is not `bearer` is never read, opened or sent: a token
    ///   left on the row from an earlier configuration is not a credential
    ///   of the server as it is configured now.
    /// * `Ok(Some(token))`: the token in clear, decrypted from the stored
    ///   ciphertext.
    /// * `Err`: the stored value is not a ciphertext the gateway can open:
    ///   written under another `ENCRYPTION_KEY`, an envelope the gateway
    ///   cannot unwrap, or a value that is not ciphertext at all. The
    ///   column holds ciphertext only (every writer of the Control Plane
    ///   encrypts it, and the Control Plane refuses the same values), so
    ///   the stored value is never the token: the caller refuses the call
    ///   and sends nothing.
    pub fn static_token(
        &self,
        encryption: &crate::services::encryption::EncryptionService,
    ) -> Result<Option<String>, crate::services::encryption::EncryptionError> {
        if self.auth_type.as_deref() != Some(STATIC_TOKEN_AUTH_TYPE) {
            return Ok(None);
        }
        match self.auth_token.as_deref().filter(|v| !v.is_empty()) {
            None => Ok(None),
            Some(stored) => encryption.decrypt(stored).map(Some),
        }
    }
}

// ---------------------------------------------------------------------------
// Server Config (MCP server + OAuth client binding)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct ServerConfig {
    pub id: String,
    pub tenant_id: String,
    // NULL for a Virtual MCP server's config (`virtual_server_id` set).
    pub mcp_server_id: Option<String>,
    pub oauth_client_id: Option<String>,
    pub name: String,
    pub priority: Option<i32>,
    pub is_default: bool,
    pub enabled: bool,
}

// ---------------------------------------------------------------------------
// LLM Model
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, Default, FromRow)]
pub struct LlmModel {
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub description: Option<String>,
    pub usage: Option<String>,
    // `provider_id` is the FK source of truth. The
    // `provider` slug is no longer a column on `llm_model` — it
    // gets stamped onto the struct post-load by `ProviderCache`
    // (see `services/cache/provider_cache.rs::stamp_models`). The
    // `#[sqlx(default)]` attribute tells `query_as` not to look
    // for a `provider` column in the SELECT result; it'll be `""`
    // until the cache stamp runs.
    pub provider_id: String,
    #[sqlx(default)]
    pub provider: String,
    /// Provider-wide `additional_drop_params` stamped post-load by
    /// `ProviderCache::stamp_models`. Unioned with the
    /// model row's own `additional_drop_params` in
    /// `services::providers::drop_unsupported_params`. The
    /// `#[sqlx(default)]` attribute keeps existing `SELECT *`-style
    /// queries against `llm_model` working — the column doesn't live
    /// on `llm_model`; it's inherited from the joined `llm_providers`
    /// row at stamp time.
    #[sqlx(default)]
    pub provider_additional_drop_params: Option<serde_json::Value>,
    pub model_name: String,
    pub mode: Option<String>,
    pub parameters: Option<f64>,
    pub tags: Option<serde_json::Value>,
    // Connection
    pub api_url_auto_discovered: Option<bool>,
    pub api_url: Option<String>,
    // Auth
    pub auth_type: Option<String>,
    pub api_key: Option<String>,
    pub provider_config: Option<String>, // Fernet-encrypted JSON (cloud credentials)
    /// Not a column. Set when the row is loaded and its `api_key` or
    /// `provider_config` is a ciphertext the gateway could not decrypt:
    /// the field is then `None` and the row is
    /// refused at dispatch (`provider_credential_missing`).
    #[sqlx(default)]
    pub credential_unreadable: bool,

    /// Synthetic, never a column. Set once the row's stored credentials
    /// have been opened (`llm_router::open_model_credentials`): from then
    /// on `api_key` and `provider_config` hold plaintext (or nothing), and
    /// a loader that is handed the row again leaves them alone. A row as
    /// it is stored has this unset, and what it holds is ciphertext.
    #[sqlx(default)]
    pub credentials_opened: bool,
    // Model config defaults
    pub default_temperature: Option<f64>,
    pub default_max_tokens: Option<i32>,
    pub system_prompt: Option<String>,
    // Core generation params
    pub top_p: Option<f64>,
    pub n: Option<i32>,
    pub stop_sequences: Option<serde_json::Value>,
    pub seed: Option<i32>,
    // Penalties
    pub presence_penalty: Option<f64>,
    pub frequency_penalty: Option<f64>,
    pub logit_bias: Option<serde_json::Value>,
    // Response format
    pub response_format: Option<serde_json::Value>,
    pub logprobs: Option<bool>,
    pub top_logprobs: Option<i32>,
    // Streaming
    pub stream_options: Option<serde_json::Value>,
    // Capabilities
    pub supports_streaming: Option<bool>,
    pub supports_function_calling: Option<bool>,
    pub supports_vision: Option<bool>,
    pub supports_batch: Option<bool>,
    // Tools
    pub tools: Option<serde_json::Value>,
    pub tool_choice: Option<String>,
    pub parallel_tool_calls: Option<bool>,
    // Safety
    pub user_identifier: Option<String>,
    // Provider-specific
    pub api_version: Option<String>,
    pub extra_headers: Option<serde_json::Value>,
    // Advanced
    pub model_metadata: Option<serde_json::Value>,
    // Note: `context_window_fallback` is no longer a column of `llm_model`
    // (that concern moved to the routing group). Model-level `fallback_models`
    // was re-introduced: an ordered list of model ids/names to try, in
    // order, when THIS model is unavailable — the industry-standard model→model
    // failover chain. See `routing` block below.
    pub drop_params: Option<bool>,
    pub additional_drop_params: Option<serde_json::Value>,
    /// Generic switch for extended-thinking / reasoning mode. Translated
    /// per-provider by the LLM adapters at request time:
    ///   - Anthropic: inject `thinking: {type: "enabled", budget_tokens: N}`
    ///     and force `temperature` to 1.0
    ///   - OpenAI o-series: inject `reasoning_effort: "..."` and strip
    ///     temperature / top_p
    ///   - Google Gemini 2.5 Thinking: inject `thinking_config: {...}`
    ///   - Other providers: no-op (the underlying API has no equivalent)
    pub thinking_enabled: Option<bool>,
    // Routing
    pub routing_group_id: Option<String>,
    pub routing_weight: Option<i32>,
    /// Model-level failover chain: ordered JSON array of model ids or
    /// model_names to try, in order, when this model's own call fails with a
    /// retryable error. Resolved + decrypted via the same path as any model.
    pub fallback_models: Option<serde_json::Value>,
    pub rate_limit_rpm: Option<i32>,
    pub rate_limit_tpm: Option<i32>,
    // Cost
    pub cost_per_input_token: Option<f64>,
    pub cost_per_output_token: Option<f64>,
    /// Per-generated-image cost (USD). Set on image-mode models.
    /// A column of its own, not a key of the `capabilities` JSONB blob,
    /// so cost dashboards roll up per-
    /// image and per-token spend cleanly.
    pub cost_per_image: Option<f64>,
    /// Whitelist of accepted image sizes for image-mode models
    /// (e.g. `["1024x1024","1024x1792"]`). Enforced at the proxy
    /// edge before forwarding upstream — a request with an
    /// unlisted size is rejected with 400 before any provider
    /// call is made.
    pub supported_image_sizes: Option<serde_json::Value>,
    /// Whitelist of accepted quality tiers for image-mode models
    /// (e.g. `["standard","hd"]`). Same enforcement point as
    /// `supported_image_sizes`.
    pub supported_image_qualities: Option<serde_json::Value>,
    /// Maximum images per single request (`n` parameter). NOT
    /// NULL in the DB; modelled `Option<i32>` because sqlx maps
    /// nullable-or-not the same way without `#[sqlx(default)]`.
    pub max_images_per_request: Option<i32>,
    /// Per-character cost (USD) for TTS models. tts-1 ~ 0.000015,
    /// tts-1-hd ~ 0.00003.
    pub cost_per_audio_char: Option<f64>,
    /// Whitelist of voice ids accepted by this model.
    pub supported_voices: Option<serde_json::Value>,
    /// Whitelist of output audio formats accepted by this model.
    pub supported_audio_formats: Option<serde_json::Value>,
    /// Per-minute STT cost (USD) for transcription models.
    pub cost_per_audio_minute: Option<f64>,
    /// Hard cap on input clip duration. Mirrors provider limits.
    pub max_audio_duration_seconds: Option<i32>,
    /// Whitelist of accepted input file formats / MIME types.
    pub supported_input_formats: Option<serde_json::Value>,
    /// JSON array of response_format strings the upstream
    /// accepts for this model (STT today). NULL = whitelist not
    /// declared, the route falls back to a model-name heuristic for
    /// one release cycle.
    #[sqlx(default)]
    pub supported_response_formats: Option<serde_json::Value>,
    /// USD per second of generated video.
    pub cost_per_video_second: Option<f64>,
    /// Resolution whitelist for video models.
    pub supported_video_resolutions: Option<serde_json::Value>,
    /// Clip-duration whitelist (seconds) for video models.
    pub supported_video_durations: Option<serde_json::Value>,
    /// Hard cap on a single clip in seconds.
    pub max_video_duration_seconds: Option<i32>,
    // Connection settings
    pub timeout: Option<f64>,
    /// Max simultaneous in-flight requests to this model. Enforced by a
    /// per-model semaphore at dispatch (`AppState.llm_concurrency`).
    /// `None` → unbounded (fall back to rpm/tpm rate-limiters).
    pub max_concurrent_requests: Option<i32>,
    // Circuit breaker / cooldown (per-model, overrides routing group if set).
    // `allowed_fails` + `cooldown_time` drive `llm_resilience::gate_dispatch`.
    // (`cooldown_window` is no longer a column of `llm_model`.)
    pub allowed_fails: Option<i32>,
    pub cooldown_time: Option<i32>,
    // Status
    pub enabled: bool,
    pub capabilities: Option<serde_json::Value>,
    // Health status
    pub health_status: Option<String>,
    pub last_health_check: Option<DateTime<Utc>>,
    pub last_health_success: Option<DateTime<Utc>>,
    pub consecutive_health_failures: Option<i32>,
    pub health_check_error: Option<String>,
    // Performance metrics
    pub total_requests: Option<i32>,
    pub failed_requests: Option<i32>,
    pub total_tokens_consumed: Option<i32>,
    pub avg_latency_ms: Option<f64>,
    // Timestamps
    pub created_at: Option<DateTime<Utc>>,
    pub updated_at: Option<DateTime<Utc>>,
    /// Runtime-only annotation (not a `llm_model` column): when this model was
    /// selected as a member of a routing group, the router stamps the routing
    /// group's id here so the shared dispatch path can also charge the
    /// GROUP-level RPM/TPM counters (keyed by group id) — the same counters
    /// `select_from_routing_group` checks. Without this the group-level
    /// rate limits never trip because only per-model counters were incremented.
    #[sqlx(default)]
    pub routed_via_group_id: Option<String>,

    /// Synthetic, never a column. The `stream_timeout` (seconds) of the
    /// routing group this model was selected through, when the group states
    /// one: the longest a streamed call waits for this member's first
    /// chunk before it is given up on and the next model is tried.
    #[sqlx(default)]
    pub routed_stream_timeout: Option<f64>,

    /// Synthetic, never a column. Set when `complexity-based-routing` chose
    /// this model, so the dispatch path can carry the tier and the signals
    /// that produced it onto the audit trail. A routing decision is a governed
    /// decision: recording *which* tier and *why* is what separates this from
    /// an opaque optimisation. `None` for every other strategy.
    #[sqlx(default)]
    pub routed_via_tier: Option<String>,

    /// Signals that fired during complexity scoring, comma-joined, e.g.
    /// `"code,technical,long_prompt"`. Explanatory only — never an input to
    /// any decision.
    #[sqlx(default)]
    pub routed_tier_signals: Option<String>,
}

// ---------------------------------------------------------------------------
// LLM Routing Group
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow, Default)]
pub struct LlmRoutingGroup {
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub description: Option<String>,
    // Routing strategy
    pub routing_strategy: Option<String>,
    pub routing_strategy_args: Option<serde_json::Value>,
    /// Complexity-based routing. Both nullable and only read by the
    /// `complexity-based-routing` strategy, so every existing group is
    /// unaffected. `#[sqlx(default)]` keeps older fixtures loading.
    #[sqlx(default)]
    pub complexity_config: Option<serde_json::Value>,
    #[sqlx(default)]
    pub complexity_floor_tier: Option<String>,
    // Failure handling & cooldown. (The group's `allowed_fails`,
    // `cooldown_window`, `enable_pre_call_checks`, `region_filter`,
    // `min_context_window` and `timeout` columns were never read and are
    // gone from the table.)
    pub cooldown_time: Option<i32>,
    pub disable_cooldowns: Option<bool>,
    // Fallback configuration
    pub fallback_models: Option<serde_json::Value>,
    pub context_window_fallbacks: Option<serde_json::Value>,
    pub content_policy_fallbacks: Option<serde_json::Value>,
    pub default_fallbacks: Option<serde_json::Value>,
    pub max_fallbacks: Option<i32>,
    // Capabilities a member must have
    pub required_capabilities: Option<serde_json::Value>,
    // Retry configuration
    pub num_retries: Option<i32>,
    pub retry_after: Option<i32>,
    pub stream_timeout: Option<f64>,
    pub retry_policy: Option<serde_json::Value>,
    pub allowed_fails_policy: Option<serde_json::Value>,
    // Load balancing tuning
    pub routing_max_retries: Option<i32>,
    pub routing_retry_delay: Option<f64>,
    pub ttl_for_blocked_models: Option<i32>,
    // Rate limiting (group-level)
    pub max_parallel_requests: Option<i32>,
    pub rpm_limit: Option<i32>,
    pub tpm_limit: Option<i32>,
    // Alerting
    pub alerting_enabled: Option<bool>,
    pub alerting_threshold: Option<i32>,
    pub alerting_webhook_url: Option<String>,
    pub alerting_config: Option<serde_json::Value>,
    // Advanced
    pub router_settings: Option<serde_json::Value>,
    pub custom_metadata: Option<serde_json::Value>,
    // Status
    pub enabled: bool,
}

// ---------------------------------------------------------------------------
// Resource Group (read-only for proxy — quota enforcement)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct ResourceGroup {
    pub id: String,
    pub tenant_id: String,
    pub parent_group_id: Option<String>,
    pub hierarchy_level: Option<i32>,
    pub hierarchy_path: Option<String>,
    #[sqlx(rename = "inherit_from_parent")]
    pub inherit_resources: bool,
    pub name: String,
    pub display_name: Option<String>,
    pub description: Option<String>,
    pub icon: Option<String>,
    pub color: Option<String>,
    pub group_type: Option<String>,
    /// AI System kind: agent | assistant | application |
    /// integration | workflow | service. Only populated when
    /// `group_type` is the AI-System member; NULL for every other group.
    ///
    /// Note the case trap on `group_type`: the Postgres `grouptype` enum
    /// stores SQLAlchemy member *names*, so raw SQL sees `'AI_SYSTEM'`
    /// while the control-plane JSON API emits the lowercase *value*
    /// `'ai_system'`. Compare case-insensitively in SQL.
    pub system_kind: Option<String>,
    pub is_active: bool,
    pub is_system: bool,
    pub llm_global_limits: Option<serde_json::Value>,
    pub mcp_global_limits: Option<serde_json::Value>,
    pub llm_global_governance: Option<serde_json::Value>,
    pub mcp_global_governance: Option<serde_json::Value>,
    pub skill_global_governance: Option<serde_json::Value>,
    pub skill_global_limits: Option<serde_json::Value>,
    // A2A (Agent-to-Agent) per-group limits + governance. Both merge
    // restrictively along the ancestor chain; null on a given row
    // means "inherit from parent".
    pub a2a_global_limits: Option<serde_json::Value>,
    pub a2a_global_governance: Option<serde_json::Value>,
    // Multimodal (audio_speech / audio_transcription /
    // image_generation / video_generation / embedding) limits +
    // governance. Single JSONB column per category, keyed by
    // mode. Read by the multimodal route handlers to gate
    // request shape (whitelisted voices, sizes, durations,
    // etc.) before the upstream call dispatches.
    pub media_global_limits: Option<serde_json::Value>,
    pub media_global_governance: Option<serde_json::Value>,
    /// AI System ids that may call this system as a
    /// service. A JSON list of strings; NULL = none declared.
    #[sqlx(default)]
    pub intended_client_systems: Option<serde_json::Value>,
    /// When true, an A2A call from a caller system
    /// not in `intended_client_systems` is refused before the service is
    /// contacted (`services::accepted_callers`). `default` so a row read
    /// without the column reads "not enforced" rather than failing to decode.
    #[sqlx(default)]
    pub enforce_accepted_callers: bool,
    pub created_by: Option<String>,
    pub updated_by: Option<String>,
    pub created_at: Option<DateTime<Utc>>,
    pub updated_at: Option<DateTime<Utc>>,
}

// ---------------------------------------------------------------------------
// Resource Group LLM Governance (per-model-per-group)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow, Deserialize)]
pub struct ResourceGroupLlmGovernance {
    pub id: String,
    pub resource_group_id: String,
    pub llm_model_id: String,
    pub governance: Option<serde_json::Value>,
}

// ---------------------------------------------------------------------------
// Resource Group Membership
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct ResourceGroupMembership {
    pub id: i32,
    pub tenant_id: String,
    pub user_id: i32,
    pub group_id: String,
}

// ---------------------------------------------------------------------------
// Guardrail Config (read for enforcement)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, Default, FromRow, Serialize)]
pub struct GuardrailConfig {
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub enabled: bool,
    pub is_default: bool,
    pub priority: Option<i32>,
    pub description: Option<String>,
    /// Per-mode LLM surface gates. Each one matches a single
    /// operational route in the proxy.
    #[sqlx(default)]
    pub chat_input_enabled: Option<bool>,
    #[sqlx(default)]
    pub chat_output_enabled: Option<bool>,
    // Streaming output guardrail posture: 'auto' | 'sync' |
    // 'async'. None/auto = sync when a redact output check is active,
    // else async-interrupt. Consumed by stream_guard::needs_sync.
    #[sqlx(default)]
    pub streaming_output_mode: Option<String>,
    #[sqlx(default)]
    pub embeddings_input_enabled: Option<bool>,
    #[sqlx(default)]
    pub image_gen_input_enabled: Option<bool>,
    #[sqlx(default)]
    pub image_gen_output_enabled: Option<bool>,
    #[sqlx(default)]
    pub audio_tts_input_enabled: Option<bool>,
    #[sqlx(default)]
    pub audio_tts_output_enabled: Option<bool>,
    #[sqlx(default)]
    pub audio_stt_input_enabled: Option<bool>,
    #[sqlx(default)]
    pub audio_stt_output_enabled: Option<bool>,
    #[sqlx(default)]
    pub video_gen_input_enabled: Option<bool>,
    #[sqlx(default)]
    pub video_gen_output_enabled: Option<bool>,
    #[sqlx(default)]
    pub batch_input_enabled: Option<bool>,
    #[sqlx(default)]
    pub batch_output_enabled: Option<bool>,
    /// Moderation gate. Even when ON, the orchestrator hard-bypasses
    /// `jailbreak`, `prompt_injection`, and `toxic_content` whenever
    /// surface == `moderation_input` — running safety classifiers on a
    /// safety-classifier-bound request body is recursive nonsense.
    /// PII + secrets still run if enabled (a moderation call carrying
    /// an SSN is still a real policy concern).
    #[sqlx(default)]
    pub moderation_input_enabled: Option<bool>,
    #[sqlx(default)]
    pub mcp_input_enabled: Option<bool>,
    #[sqlx(default)]
    pub mcp_output_enabled: Option<bool>,
    /// Scan tool descriptions from MCP servers' `tools/list`
    /// responses for prompt-injection payloads at server registration
    /// and on every catalog refresh. Enforced by the Python control
    /// plane in `mcp_server_manager.discover_capabilities`, NOT by
    /// the runtime proxy — this field is present here purely so that
    /// proxy `SELECT *` style reads of the guardrail row round-trip
    /// without sqlx complaining about a missing column. `sqlx(default)`
    /// keeps reads against a not-yet-migrated DB working (the bool
    /// defaults to false in that case, which is harmless since the
    /// proxy doesn't act on this field).
    #[sqlx(default)]
    pub mcp_registration_enabled: Option<bool>,
    #[sqlx(default)]
    pub skill_input_enabled: Option<bool>,
    #[sqlx(default)]
    pub skill_output_enabled: Option<bool>,
    /// Apply input guardrails to incoming A2A v1.0 messages (the
    /// `tasks.send` / `message.send` payload an external agent sends
    /// to a Brutor-hosted Agent Card). Default false until the
    /// operator opts the policy in via the admin UI.
    #[sqlx(default)]
    pub a2a_inbound_enabled: Option<bool>,
    /// Apply input guardrails to outbound A2A delegations (the
    /// payload Brutor sends when one of its hosted agents calls a
    /// peer agent at another vendor). Default false. The block UX
    /// returns a v1.0 structured `policy_blocked` error to the
    /// caller in the local task chain.
    #[sqlx(default)]
    pub a2a_outbound_enabled: Option<bool>,
    pub pii_detection_enabled: bool,
    pub pii_action: Option<String>,
    /// Per-check provider selector. `built_in` (default
    /// when null) runs the in-process detector. Other values map to
    /// the matching provider config in `providers`.
    #[sqlx(default)]
    pub pii_provider: Option<String>,
    /// Optional per-tenant subset of PII categories. Null = run all
    /// known categories.
    #[sqlx(default)]
    pub pii_categories: Option<serde_json::Value>,
    /// Confidence threshold forwarded to Presidio's
    /// `/analyze.score_threshold`. Built-in regex detector ignores
    /// this (no continuous score). Null = use Presidio's default.
    #[sqlx(default)]
    pub pii_threshold: Option<f64>,
    pub prompt_injection_enabled: bool,
    pub prompt_injection_action: Option<String>,
    pub prompt_injection_sensitivity: Option<f64>,
    #[sqlx(default)]
    pub prompt_injection_provider: Option<String>,
    pub jailbreak_detection_enabled: bool,
    pub jailbreak_action: Option<String>,
    #[sqlx(default)]
    pub jailbreak_provider: Option<String>,
    pub toxic_content_enabled: bool,
    pub toxic_content_action: Option<String>,
    pub toxic_content_threshold: Option<f64>,
    #[sqlx(default)]
    pub toxic_content_provider: Option<String>,
    /// Per-category thresholds for OpenAI Moderation. JSON object
    /// `{ "hate": 0.7, "harassment": 0.8, … }`. Empty / null falls
    /// back to `toxic_content_threshold` (single global threshold)
    /// for every category.
    #[sqlx(default)]
    pub toxic_content_category_thresholds: Option<serde_json::Value>,
    pub banned_words: Option<serde_json::Value>,
    pub banned_patterns: Option<serde_json::Value>,
    /// Per-policy match mode for `banned_words`. `substring` (default,
    /// for back-compat) or `word_boundary` (fixes the Scunthorpe
    /// problem). See `services::guardrails::banned::MatchMode`.
    #[sqlx(default)]
    pub banned_words_match_mode: Option<String>,
    // -----------------------------------------------------------------------
    // Per-check surface granularity.
    //
    // Each *_surfaces column lists the subset of surfaces on which
    // the matching check fires. NULL means "no per-check restriction
    // set — fall back to the default surface set baked into the
    // orchestrator helper" (see `check_surfaces` in
    // services::guardrails::mod). The outer gate (the config-level
    // *_enabled flags above) is intersected with each per-check set
    // so an operator cannot fire a check on a surface the config
    // doesn't even cover.
    #[sqlx(default)]
    pub pii_surfaces: Option<serde_json::Value>,
    #[sqlx(default)]
    pub prompt_injection_surfaces: Option<serde_json::Value>,
    #[sqlx(default)]
    pub jailbreak_surfaces: Option<serde_json::Value>,
    #[sqlx(default)]
    pub toxic_content_surfaces: Option<serde_json::Value>,
    #[sqlx(default)]
    pub banned_words_surfaces: Option<serde_json::Value>,
    #[sqlx(default)]
    pub banned_patterns_surfaces: Option<serde_json::Value>,
    // -----------------------------------------------------------------------
    // Secrets detection.
    //
    // First-class check alongside PII. Categories opt-in via
    // `secrets_categories` (NULL = default 7-category set as
    // defined by SecretsDetector::default_categories()).
    // `secrets_threshold` is reserved for the Presidio extension
    // path; the built-in detector ignores it.
    pub secrets_detection_enabled: bool,
    #[sqlx(default)]
    pub secrets_action: Option<String>,
    #[sqlx(default)]
    pub secrets_categories: Option<serde_json::Value>,
    #[sqlx(default)]
    pub secrets_surfaces: Option<serde_json::Value>,
    #[sqlx(default)]
    pub secrets_threshold: Option<f64>,
    // -----------------------------------------------------------------------
    // Per-surface overrides.
    //
    // Each `<check>_per_surface` JSONB column carries optional overrides
    // keyed by surface token. Shape:
    //   {"chat_input":  {"action": "block", "provider": "presidio", ...},
    //    "chat_output": {"action": "redact"}}
    //
    // Resolution: per-surface override → scalar column → system default.
    // The orchestrator dispatch consults these via
    // `services::guardrails::per_surface::resolve_*`.
    #[sqlx(default)]
    pub pii_per_surface: Option<serde_json::Value>,
    #[sqlx(default)]
    pub prompt_injection_per_surface: Option<serde_json::Value>,
    #[sqlx(default)]
    pub jailbreak_per_surface: Option<serde_json::Value>,
    #[sqlx(default)]
    pub toxic_content_per_surface: Option<serde_json::Value>,
    #[sqlx(default)]
    pub secrets_per_surface: Option<serde_json::Value>,
    #[sqlx(default)]
    pub banned_words_per_surface: Option<serde_json::Value>,
    #[sqlx(default)]
    pub banned_patterns_per_surface: Option<serde_json::Value>,
    pub providers: Option<serde_json::Value>,
    /// Per-check fail-open overrides keyed by check name (`pii`,
    /// `prompt_injection`, …). Lets a tenant be fail-closed on
    /// PII even when the global `fail_open` is true.
    #[sqlx(default)]
    pub fail_open_overrides: Option<serde_json::Value>,
    pub max_check_timeout_ms: Option<f64>,
    pub fail_open: bool,
    pub log_all_checks: bool,
    /// Per-call scope filters. When `scope_mcp_server_id`
    /// is set, this config only fires for `tools/call`s on that
    /// upstream. When `scope_tool_name` is set, only for calls whose
    /// `params.name` matches. Both NULL = applies to all MCP calls
    /// (the default).
    #[sqlx(default)]
    pub scope_mcp_server_id: Option<String>,
    #[sqlx(default)]
    pub scope_tool_name: Option<String>,
    pub created_at: Option<chrono::DateTime<chrono::Utc>>,
    pub updated_at: Option<chrono::DateTime<chrono::Utc>>,
}

// ---------------------------------------------------------------------------
// Virtual Server
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct VirtualServer {
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub capabilities: Option<serde_json::Value>,
    pub enabled: bool,
}

// ---------------------------------------------------------------------------
// OAuth Token (read/write for OAuth proxy)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct OAuthToken {
    pub id: i32,
    pub tenant_id: String,
    pub gateway_user_id: Option<String>,
    pub idp_user_id: String,
    pub mcp_server_id: String,
    pub idp_id: String,
    pub client_id: Option<String>,
    pub encrypted_client_secret: Option<String>,
    pub encrypted_access_token: String,
    pub encrypted_refresh_token: Option<String>,
    pub token_type: Option<String>,
    pub expires_at: chrono::NaiveDateTime,
    pub scopes: Option<String>,
    /// SEP-2352: the IdP `issuer` this token was
    /// minted under. Refresh against a *different* issuer is refused
    /// (credentials are issuer-bound). `#[sqlx(default)]` tolerates
    /// pre-a125 schemas.
    #[sqlx(default)]
    pub issuer: Option<String>,
    pub revoked: bool,
    pub revoked_at: Option<chrono::NaiveDateTime>,
    pub created_at: chrono::NaiveDateTime,
    pub last_refreshed_at: Option<chrono::NaiveDateTime>,
    pub refresh_count: i32,
    pub updated_at: chrono::NaiveDateTime,
}

impl OAuthToken {
    pub fn is_expired(&self) -> bool {
        self.expires_at < Utc::now().naive_utc()
    }

    pub fn is_near_expiry(&self, minutes: i64) -> bool {
        self.expires_at < (Utc::now() + chrono::Duration::minutes(minutes)).naive_utc()
    }

    pub fn is_valid(&self) -> bool {
        !self.revoked && !self.is_expired()
    }
}

// ---------------------------------------------------------------------------
// OAuth App Client
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct OAuthAppClient {
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub app_type: Option<String>,
    pub idp_id: String,
    pub client_id: String,
    pub encrypted_client_secret: Option<String>,
    pub redirect_uri: Option<String>,
    pub scopes: Option<serde_json::Value>,
    pub client_type: Option<String>,
    pub supports_pkce: Option<bool>,
    pub allowed_grant_types: Option<serde_json::Value>,
    pub is_active: bool,
}

impl OAuthAppClient {
    pub fn is_confidential(&self) -> bool {
        self.client_type.as_deref() == Some("confidential")
    }

    pub fn is_public_with_secret(&self) -> bool {
        self.client_type.as_deref() == Some("public_with_secret")
    }

    pub fn needs_secret_for_pkce(&self) -> bool {
        self.is_public_with_secret()
    }
}

// ---------------------------------------------------------------------------
// IdP Server
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct IdpServer {
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub issuer: String,
    #[sqlx(rename = "type")]
    pub idp_type: Option<String>,
    pub well_known_url: Option<String>,
    pub authorization_endpoint: Option<String>,
    pub token_endpoint: Option<String>,
    pub revocation_endpoint: Option<String>,
    pub introspection_endpoint: Option<String>,
    pub jwks_uri: Option<String>,
    pub userinfo_endpoint: Option<String>,
    pub registration_endpoint: Option<String>,
    pub supports_pkce: Option<bool>,
    pub supports_refresh_tokens: Option<bool>,
    pub supports_client_credentials: Option<bool>,
    pub token_endpoint_auth_methods: Option<serde_json::Value>,
    pub scopes_supported: Option<serde_json::Value>,
    pub response_types_supported: Option<serde_json::Value>,
    pub grant_types_supported: Option<serde_json::Value>,
    pub cached_config: Option<serde_json::Value>,
    pub last_discovered: Option<chrono::DateTime<chrono::Utc>>,
    pub discovery_cache_ttl: Option<i32>,
    pub enabled: bool,
    pub verified: Option<bool>,
    pub description: Option<String>,
    pub logo_url: Option<String>,
    pub created_at: Option<chrono::DateTime<chrono::Utc>>,
    pub updated_at: Option<chrono::DateTime<chrono::Utc>>,
}

// ---------------------------------------------------------------------------
// Agent Skill (read-only for proxy — resolution + catalog)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct AgentSkill {
    pub id: String,
    pub tenant_id: String,
    pub name: String,
    pub description: String,
    pub license: Option<String>,
    pub compatibility: Option<serde_json::Value>,
    pub allowed_tools: Option<serde_json::Value>,
    pub version: String,
    pub status: String, // SkillStatus enum cast to text
    pub enabled: bool,
    pub metadata: Option<serde_json::Value>,
    /// SKILL.md body content — parsed by `skill_executor` at tools/call time
    /// to extract file references, run scripts, and render templates.
    pub skill_md_content: Option<String>,
    pub execution_count: i32,
    pub last_executed_at: Option<DateTime<Utc>>,
    pub published_at: Option<DateTime<Utc>>,
    pub created_at: Option<DateTime<Utc>>,
    /// Provenance for git-sourced skills. NULL for
    /// manually authored skills; otherwise the id of the SkillSource
    /// this skill was synced from.
    pub source_repo_id: Option<String>,
}

// ---------------------------------------------------------------------------
// Agent Skill Script (executable code from scripts/ directory)
// ---------------------------------------------------------------------------
// Port of Python `app/database/models/agent_skill_script.py`.
// Each row represents one executable file from the skill's scripts/ directory.
// Security modes: sandbox (restricted env, 30s timeout), trusted (full env,
// 120s timeout, requires admin approval), disabled (never runs).
// Used by `skill_executor::execute_script()` during tools/call execution.
#[derive(Debug, Clone, FromRow)]
pub struct AgentSkillScript {
    pub id: String,
    pub tenant_id: String,
    pub skill_id: String,
    pub filename: String,
    pub description: Option<String>,
    pub script_content: String,
    pub script_hash: String,
    pub language: String,       // python, javascript, typescript, bash
    pub execution_mode: String, // sandbox, trusted, disabled
    pub approved_by: Option<String>,
    pub approved_at: Option<DateTime<Utc>>,
    pub created_at: Option<DateTime<Utc>>,
    pub updated_at: Option<DateTime<Utc>>,
}

impl AgentSkillScript {
    pub fn is_executable(&self) -> bool {
        self.execution_mode != "DISABLED"
    }

    pub fn is_sandboxed(&self) -> bool {
        self.execution_mode == "SANDBOX"
    }
}

// ---------------------------------------------------------------------------
// Agent Skill Asset (static files from assets/ directory)
// ---------------------------------------------------------------------------
// Port of Python `app/database/models/agent_skill_asset.py`.
// Each row represents one file from the skill's assets/ directory.
// Asset types: template, image, data. Content is TEXT (plain text or
// base64 for binary files). Used by `skill_executor` for template
// rendering and data file loading during tools/call execution.
#[derive(Debug, Clone, FromRow)]
pub struct AgentSkillAsset {
    pub id: String,
    pub tenant_id: String,
    pub skill_id: String,
    pub asset_type: String, // template, image, data
    pub filename: String,
    pub description: Option<String>,
    pub content: String,
    pub content_hash: String,
    pub mime_type: Option<String>,
    pub size_bytes: Option<i32>,
    pub encoding: String, // utf-8, base64
    pub created_at: Option<DateTime<Utc>>,
    pub updated_at: Option<DateTime<Utc>>,
}

// ---------------------------------------------------------------------------
// Agent Skill Resource (reference docs from references/ directory)
// ---------------------------------------------------------------------------
// Port of Python `app/database/models/agent_skill_resource.py`.
// Progressive Disclosure Level 3 — reference documents loaded individually
// on demand during tools/call execution, never included in catalog responses.
// Resource types: reference, form, schema, template, example, asset.
// Used by `skill_executor` when SKILL.md references files from references/.
#[derive(Debug, Clone, FromRow)]
pub struct AgentSkillResource {
    pub id: String,
    pub tenant_id: String,
    pub skill_id: String,
    pub resource_type: String, // reference, form, schema, template, example, asset
    pub filename: String,
    pub content: String,
    pub content_hash: String,
    pub mime_type: Option<String>,
    pub size_bytes: Option<i32>,
    pub created_at: Option<DateTime<Utc>>,
    pub updated_at: Option<DateTime<Utc>>,
}

impl AgentSkill {
    /// Get a value from the metadata JSON.
    pub fn get_metadata_value(&self, key: &str) -> Option<&serde_json::Value> {
        self.metadata.as_ref()?.get(key)
    }

    /// Check if this skill requires approval by default.
    pub fn is_approval_required(&self) -> bool {
        self.get_metadata_value("approval_required")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
    }

    /// Get categorization tags.
    pub fn get_tags(&self) -> Vec<String> {
        self.get_metadata_value("tags")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Progressive disclosure Level 1: catalog entry (~50-100 tokens).
    pub fn to_catalog_json(&self) -> serde_json::Value {
        serde_json::json!({
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "status": self.status,
            "license": self.license,
            "tags": self.get_tags(),
            "category": self.get_metadata_value("category"),
            "icon_name": self.get_metadata_value("icon_name"),
            "cost_estimate_usd": self.get_metadata_value("cost_estimate_usd"),
            "approval_required": self.is_approval_required(),
            "compatibility": self.compatibility,
            "execution_count": self.execution_count,
        })
    }
}

// ---------------------------------------------------------------------------
// Agent Skill Execution (read/write for proxy — audit trail)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow)]
pub struct AgentSkillExecution {
    pub id: String,
    pub tenant_id: String,
    pub skill_id: Option<String>,
    pub group_id: Option<String>,
    pub user_id: Option<i32>,
    pub skill_name: Option<String>,
    pub skill_version: Option<String>,
    pub group_name: Option<String>,
    pub username: Option<String>,
    pub request_id: String,
    pub correlation_id: Option<String>,
    pub parent_execution_id: Option<String>,
    pub execution_status: String,
    pub started_at: Option<DateTime<Utc>>,
    pub running_at: Option<DateTime<Utc>>,
    pub completed_at: Option<DateTime<Utc>>,
    pub duration_ms: Option<i32>,
    pub error_type: Option<String>,
    pub error_message: Option<String>,
    pub execution_context: Option<serde_json::Value>,
}

// ---------------------------------------------------------------------------
// Batch Job (read/write for batch proxy)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow, Serialize, Deserialize)]
pub struct BatchJob {
    pub id: String,
    pub tenant_id: String,
    pub resource_group_id: Option<String>,
    pub resource_group_name: Option<String>,
    pub user_id: Option<i32>,
    pub username: Option<String>,
    pub api_key_id: Option<String>,
    /// The agent identity whose own workload token submitted the job.
    /// None for a key's job and
    /// for a user's.
    pub agent_id: Option<String>,
    pub llm_model_id: Option<String>,
    pub llm_model_name: Option<String>,
    pub provider: String,
    pub model: String,
    pub endpoint: String,
    pub completion_window: String,
    pub provider_batch_id: Option<String>,
    pub provider_input_file_id: Option<String>,
    pub provider_output_file_id: Option<String>,
    pub provider_error_file_id: Option<String>,
    pub description: Option<String>,
    pub custom_metadata: Option<serde_json::Value>,
    pub status: String,
    pub total_requests: i32,
    pub completed_requests: i32,
    pub failed_requests: i32,
    pub total_input_tokens: Option<i32>,
    pub total_output_tokens: Option<i32>,
    pub estimated_cost_usd: Option<f64>,
    pub error_type: Option<String>,
    pub error_message: Option<String>,
    pub error_details: Option<serde_json::Value>,
    pub created_at: Option<DateTime<Utc>>,
    pub submitted_at: Option<DateTime<Utc>>,
    pub validating_at: Option<DateTime<Utc>>,
    pub in_progress_at: Option<DateTime<Utc>>,
    pub completed_at: Option<DateTime<Utc>>,
    pub expires_at: Option<DateTime<Utc>>,
    pub cancelled_at: Option<DateTime<Utc>>,
    pub cancelled_by: Option<String>,
    /// The run the submitting request was a step of, its step and
    /// depth. The result fetcher's block rows land on this run. NULL for
    /// a job that carries none.
    pub root_task_id: Option<String>,
    pub submit_task_id: Option<String>,
    pub delegation_depth: Option<i32>,
}

// ---------------------------------------------------------------------------
// Batch Request (read/write for batch proxy)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, FromRow, Serialize, Deserialize)]
pub struct BatchRequest {
    pub id: String,
    pub tenant_id: String,
    pub batch_job_id: String,
    pub custom_id: String,
    pub request_index: Option<i32>,
    pub status: String,
    pub request_body: Option<serde_json::Value>,
    pub model_requested: Option<String>,
    pub response_status_code: Option<i32>,
    pub response_body: Option<serde_json::Value>,
    pub input_tokens: Option<i32>,
    pub output_tokens: Option<i32>,
    pub estimated_cost_usd: Option<f64>,
    pub error_type: Option<String>,
    pub error_message: Option<String>,
    pub created_at: Option<DateTime<Utc>>,
    pub completed_at: Option<DateTime<Utc>>,
}

// ---------------------------------------------------------------------------
// Proxy Log (write-only from proxy)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProxyLogInsert {
    pub id: String,
    pub tenant_id: String,
    pub timestamp: DateTime<Utc>,
    pub proxy_type: String,
    pub proxy_subtype: Option<String>,
    pub skill_id: Option<String>,
    pub skill_execution_id: Option<String>,
    pub correlation_id: Option<String>,
    pub parent_request_id: Option<String>,
    pub request_id: Option<String>,
    pub end_user_session_id: String,
    pub gateway_user_id: Option<String>,
    pub gateway_user_username: Option<String>,
    pub idp_user_id: Option<String>,
    pub idp_user_username: Option<String>,
    pub idp_user_email: Option<String>,
    pub client_ip: Option<String>,
    pub user_agent: Option<String>,
    pub server_id: Option<String>,
    pub idp_server_id: Option<String>,
    pub resource_group_id: Option<String>,
    // Per-key attribution. NULL on JWT-authed requests.
    pub api_key_id: Option<String>,
    pub api_key_name: Option<String>,
    pub http_method: String,
    pub endpoint: String,
    pub target_url: Option<String>,
    pub request_size_bytes: Option<i32>,
    pub request_headers: Option<String>,
    pub request_body_preview: Option<String>,
    pub http_status_code: Option<i32>,
    pub response_size_bytes: Option<i32>,
    pub response_headers: Option<String>,
    pub response_body_preview: Option<String>,
    pub success: bool,
    pub error_message: Option<String>,
    pub error_type: Option<String>,
    pub duration_ms: Option<f64>,
    // MCP-specific
    pub mcp_method: Option<String>,
    pub mcp_request_id: Option<String>,
    pub mcp_capability_name: Option<String>,
    // LLM-specific
    pub llm_model_requested: Option<String>,
    pub llm_model_used: Option<String>,
    pub llm_provider: Option<String>,
    pub llm_prompt_tokens: Option<i32>,
    pub llm_completion_tokens: Option<i32>,
    pub llm_total_tokens: Option<i32>,
    pub llm_streaming: Option<bool>,
    pub llm_temperature: Option<f64>,
    pub llm_max_tokens: Option<i32>,
    pub llm_function_calling: Option<bool>,
    pub llm_used_fallback: Option<bool>,
    pub llm_fallback_reason: Option<String>,
    pub llm_estimated_cost_usd: Option<f64>,
    // Media unit counters — populated by the corresponding media
    // route (image / TTS / STT / video). The token columns above
    // mean nothing for these modes; lumping them together would
    // either skew per-token analytics or hide the work entirely.
    // Each row carries the counter that matches its mode; the
    // others stay NULL so SUM() ignores them at the dashboard
    // aggregation step.
    pub media_images_generated: Option<i32>,
    pub media_audio_chars: Option<i64>,
    pub media_audio_seconds_input: Option<f64>,
    pub media_video_seconds_output: Option<f64>,
    // Response Cache (gateway-side: Redis exact + Qdrant semantic)
    pub llm_cache_hit: Option<bool>,
    pub llm_cache_layer: Option<String>,
    pub llm_tokens_saved: Option<i32>,
    pub llm_cache_similarity_score: Option<f64>,
    // Prompt Cache (provider-side: Anthropic cache_control / OpenAI
    // automatic prefix / Gemini cachedContents). Orthogonal to the
    // response cache — these fields are populated when the gateway
    // proxied a call to the provider AND the provider reported
    // prompt-cache telemetry in its response. Nullable on non-Anthropic
    // / non-OpenAI providers that don't expose this data.
    pub prompt_cache_write_tokens: Option<i32>,
    pub prompt_cache_read_tokens: Option<i32>,
    pub prompt_cache_savings_usd: Option<f64>,
    // RAG Knowledge Base
    pub rag_enabled: Option<bool>,
    pub rag_chunks_retrieved: Option<i32>,
    pub rag_context_tokens: Option<i32>,
    pub rag_collection_ids: Option<String>,
    /// SHA-256 of the instruction text sent (system messages / `system` / systemInstruction).
    /// A fingerprint, never the prompt: it says *whether* the instructions changed.
    pub prompt_fingerprint: Option<String>,
    /// The outcome reported on this step: the client's
    /// `X-Brutor-Outcome-Id` (verbatim, ≤ 128 chars, validated only at
    /// finalize), or — on an outbound A2A row — the DELEGATE's. A column
    /// (`proxy_logs.outcome_id`), so a run the sweeper backstop finalizes is
    /// still judged on the outcome its client reported.
    pub outcome_id: Option<String>,
    // Where this step was witnessed: `gateway` (proxied) or `otel`
    // (ingested telemetry). Never NULL: an absent value would read as
    // "unknown", and the whole point is that provenance is always known.
    pub source: String,
    pub rag_top_similarity_score: Option<f64>,
    pub rag_retrieval_duration_ms: Option<f64>,
    // OAuth
    pub oauth_flow_type: Option<String>,
    pub oauth_scope: Option<String>,
    // Call chain, on every surface:
    // LLM, MCP, A2A and
    // Skill. `root_task_id` groups steps into a single agent
    // flow; `current_task_id` uniquely identifies this node;
    // `parent_task_id` is the previous step's current_task_id (null at
    // the root). Mission Control joins on these columns to reconstruct
    // the full tree across all steps.
    pub root_task_id: Option<String>,
    pub delegation_depth: Option<i32>,
    pub current_task_id: Option<String>,
    pub parent_task_id: Option<String>,
    // What initiated this chain: llm_user | agent_invocation |
    // skill_execution | scheduled_job | external_api. Populated on
    // root rows only.
    pub trace_root_kind: Option<String>,
    // How this row joined the chain: minted | verified |
    // client_dropped | sig_failed. Populated on every row that runs
    // through the trace parse-or-mint helper.
    pub trace_continuity: Option<String>,
    // Explicit finalization. The terminal state the
    // client asked for via `X-Brutor-Run-End` on this step, already validated
    // against `run_ledger::terminal::ALL` by the run-end middleware. NULL on
    // every step that carried no marker, which is nearly all of them. Lives on
    // the row (not on the telemetry channel) so it cannot be reordered ahead
    // of the step it closes — see `middleware::run_end`.
    pub run_end_state: Option<String>,
    // Data residency.
    // Region the request actually reached. NULL on blocked rows
    // and rows where the upstream had no region declared.
    pub region_routed_to: Option<String>,
    // Outcome of the residency check: allowed | blocked |
    // not_evaluated. NULL on older rows OR on rows
    // where no residency path was exercised.
    pub residency_class: Option<String>,
    // Compliance mapping.
    // Interned framework tags fired by the tagging engine.
    // NULL when the tenant has no compliance profile; a JSONB
    // array (even empty) when the engine ran. See
    // `services::compliance::derive_tags`.
    pub compliance_tags: Option<serde_json::Value>,
    // Orchestrator outcome flags.
    // Boolean flags surfaced from the orchestrator layer so the
    // compliance engine can tag SOC 2 CC6.1 / CC6.6 / CC7.2 +
    // GDPR + HIPAA without re-querying. All default to false
    // (NOT NULL on the DB side); helpers in `proxy_logger.rs`
    // flip them at the canonical orchestrator outcome sites.
    pub guardrail_blocked: bool,
    pub argument_policy_denied: bool,
    pub pii_detected: bool,
    pub is_auth_event: bool,
    // Agent Identity (subject/actor) + the
    // per-agent runtime-authz decision. All NULL unless the request's actor
    // is an agent principal.
    pub actor_agent_id: Option<String>,
    pub subject_id: Option<String>,
    pub subject_kind: Option<String>,
    pub agent_authz_decision: Option<String>,
    pub agent_grant_id: Option<String>,
    /// Semantic governance policy that fired (block or shadow), when SGP decided.
    pub semantic_policy_id: Option<String>,
    // --- Decision record -----------------------------------------
    // Which policy decided, under what authority, via which exception path,
    // and against which upstream call. Every one of these was already in hand
    // at the decision and was discarded before the row was written, which left
    // the trail able to say *something* denied a call but not which thing.
    // --- Tamper evidence ---------------------------------------
    // Stamped by `audit_chain::stamp_batch` immediately before the INSERT,
    // so a row shed under backpressure never consumes a sequence number —
    // shedding must not manufacture a gap that reads as deletion.
    pub chain_id: Option<String>,
    pub chain_seq: Option<i64>,
    pub prev_hash: Option<String>,
    pub row_hash: Option<String>,
    /// The canonical format `row_hash` was sealed under (`v3`), set
    /// by `stamp_batch` with the hash. The verifier dispatches on it; the
    /// tag is also inside the hashed bytes, so editing it breaks the row.
    #[serde(default)]
    pub chain_version: Option<String>,
    /// Client-asserted turn id — steps sharing one belong to the
    /// same pass of the agent loop. NULL when the client asserted nothing.
    /// Client-asserted logical phase. NULL means the client
    /// asserted none — never inferred.
    pub step_id: Option<String>,
    /// Optional display label for the step.
    pub step_name: Option<String>,
    pub turn_id: Option<String>,
    /// Optional client-supplied turn ordinal within the run.
    pub turn_seq: Option<i32>,
    /// Argument policy that produced the decision (the id behind the existing
    /// `argument_policy_denied` boolean).
    pub argument_policy_id: Option<String>,
    /// Guardrail config whose check fired.
    pub guardrail_config_id: Option<String>,
    /// Which check fired: `pii` | `prompt_injection` | `jailbreak` |
    /// `toxic_content` | `banned_words` | `banned_pattern` | `secrets`.
    pub guardrail_check: Option<String>,
    /// What it did: `block` | `redact` | `warn`. Distinct from
    /// `guardrail_blocked` because a redaction changed what went upstream
    /// without blocking, and a warn changed nothing.
    pub guardrail_action: Option<String>,
    /// Checks that were CONFIGURED but did not run on this call,
    /// because the detector failed and the config opted into log-and-allow.
    /// `[{config, check, reason}]`; `None` when every configured check ran.
    ///
    /// The row's other guardrail fields describe a check that *fired*. This
    /// one is the only thing distinguishing "checked and clean" from "never
    /// checked" — without it the assurance report cannot tell the difference,
    /// and would report coverage the call never had.
    pub guardrails_skipped: Option<serde_json::Value>,
    /// Checks that ran on a degraded rung: [{config, check, tier}].
    pub guardrails_degraded: Option<serde_json::Value>,
    /// Autonomy level in force; `None` when the call was denied before the
    /// autonomy gate ran, so no level was ever resolved.
    pub autonomy_level: Option<String>,
    /// How `autonomy_level` was obtained: `live` | `last_known_good` | `floor`.
    /// Anything but `live` means enforcement ran on degraded information.
    pub autonomy_provenance: Option<String>,
    /// The approval whose one-time token authorised this call — set on the
    /// *retried* call, which is what makes approval → action walkable forwards.
    pub consumed_approval_request_id: Option<String>,
    /// Upstream provider's own request id; the only join between our audit
    /// trail and the vendor's.
    pub upstream_request_id: Option<String>,
    /// State the quota/budget decision was taken against. The limits
    /// it was judged against are not duplicated here — they are pinned by
    /// the AI System's `contract_hash`. `None` when no quota decision was
    /// taken on this call, which is most calls.
    pub pre_action_state: Option<serde_json::Value>,
    /// SHA-256 (hex) over the request / response content exactly as
    /// the gateway captured it, sealed before truncation, redaction or a
    /// `store_bodies: none` policy touches the stored preview. Covered by
    /// the row hash, so nothing retention does to the body can touch them.
    pub request_content_hash: Option<String>,
    pub response_content_hash: Option<String>,
    /// W3C trace context the caller asserted (`traceparent`), or the
    /// span's own ids for OTel-inlet rows. Asserted, not witnessed, so
    /// outside the hash chain — like `correlation_id`.
    pub trace_id: Option<String>,
    pub span_id: Option<String>,
    /// The version the MCP server announced at `initialize`, and the
    /// SHA-256 of the tool definition in force when this call was made.
    /// Witnessed by the gateway; covered by the row hash.
    pub server_version: Option<String>,
    pub tool_definition_hash: Option<String>,
    /// The model whose completion decided this action: the most
    /// recent LLM call in the same run. Derived at write time from sibling
    /// rows, never asserted. Resolved before the batch is sealed, so inside
    /// the hash chain from format v3: the sibling it was derived from
    /// may sit on another writer's chain or be pruned, so the row itself must
    /// prove what it said.
    pub deciding_model: Option<String>,
    /// `action_records.record_id` of the sealed evidence record
    /// minted for this row, stamped by the writer just before the INSERT.
    /// Derived (the record cites the row hash, not the other way round), so
    /// outside the hash chain. NULL = no record (non-consequential row, or
    /// evidence signing not configured).
    pub record_id: Option<String>,
    /// The A2A task state the caller received,
    /// normalised by [`crate::services::a2a_task_state`]. Written on OUTBOUND
    /// A2A rows whose reply named one; NULL everywhere else. Derived from the
    /// response. Set before sealing and an outcome the run-segment ledger
    /// reads, so inside the hash chain from format v3. The single
    /// source for run-segment outcomes and the external boundary metrics.
    #[serde(default)]
    pub a2a_task_state: Option<String>,
    /// The caller's implementation release, resolved from
    /// the highest-trust carrier present, and which carrier it came from
    /// (`token` | `mcp_client_info` | `baggage` | `header`). Written ONLY by
    /// `agent_impl::stamp` via `build_proxy_log`. Describes, never authorises.
    /// Inside the hash chain from format v3: the trust level
    /// (`agent_impl_source`) says how much the value is worth, the chain
    /// proves the record of it was not altered afterwards.
    #[serde(default)]
    pub agent_impl_name: Option<String>,
    #[serde(default)]
    pub agent_impl_version: Option<String>,
    #[serde(default)]
    pub agent_impl_build: Option<String>,
    #[serde(default)]
    pub agent_impl_source: Option<String>,
    /// The normalised client label. The raw
    /// `user_agent` is kept alongside.
    #[serde(default)]
    pub client_label: Option<String>,
    // --- The credential that made the call -----------------------------
    // Stated by authentication and the identity judge
    // (`services::identity_policy`); sealed. NULL on a row written before
    // authentication succeeded.
    /// `api_key` | `workload_token` | `x509_svid` | `brutor_token` |
    /// `human_session` | `platform_service` | `anonymous`.
    #[serde(default)]
    pub credential_kind: Option<String>,
    /// The credential's level on the assurance ladder: `key` |
    /// `bound_key` | `workload` | `workload_pop`. NULL outside the ladder.
    #[serde(default)]
    pub credential_assurance: Option<String>,
    /// A stable, non-secret reference to the credential (a key id, an
    /// identity provider and subject, a session).
    #[serde(default)]
    pub credential_ref: Option<String>,
    /// The identity judge's verdict: `met` | `below_policy` | `refused` |
    /// `exempt`. NULL when no surface judged the call.
    #[serde(default)]
    pub credential_policy_outcome: Option<String>,
    // Custom
    pub custom_metadata: Option<serde_json::Value>,
    pub tags: Option<serde_json::Value>,
}

// ---------------------------------------------------------------------------
// Proxy Metric (write-only from proxy)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProxyMetricInsert {
    pub id: String,
    pub tenant_id: String,
    pub timestamp: DateTime<Utc>,
    pub proxy_type: String,
    pub request_id: String,
    pub correlation_id: Option<String>,
    pub server_id: Option<String>,
    pub http_method: String,
    pub endpoint: String,
    pub endpoint_category: String,
    pub total_duration_ms: Option<f64>,
    pub http_status_code: Option<i32>,
    pub success: bool,
    pub error_type: Option<String>,
    pub request_size_bytes: Option<i32>,
    pub response_size_bytes: Option<i32>,
    pub request_headers_count: Option<i32>,
    pub response_headers_count: Option<i32>,
    pub gateway_user_id: Option<String>,
    pub gateway_username: Option<String>,
    pub end_user_type: Option<String>,
    pub client_ip: Option<String>,
    pub user_agent_hash: Option<String>,
    pub mcp_data: Option<serde_json::Value>,
    pub llm_data: Option<serde_json::Value>,
    pub request_hour: i32,
    pub request_day_of_week: i32,
}

// ---------------------------------------------------------------------------
// Usage Log (write from proxy, read for quota checks)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UsageLogInsert {
    pub tenant_id: String,
    pub user_id: Option<i32>,
    pub group_id: Option<String>,
    pub ancestry_path: Option<String>,
    pub group_name: Option<String>,
    pub resource_type: String,
    pub resource_id: Option<String>,
    pub resource_name: Option<String>,
    pub prompt_tokens: Option<i32>,
    pub completion_tokens: Option<i32>,
    pub total_tokens: Option<i32>,
    pub model_name: Option<String>,
    pub provider: Option<String>,
    pub mcp_tool_name: Option<String>,
    pub mcp_call_count: Option<i32>,
    pub mcp_execution_time_ms: Option<i32>,
    pub mcp_data_size_bytes: Option<i32>,
    pub estimated_cost: Option<f64>,
    pub currency: Option<String>,
    pub prompt_cost: Option<f64>,
    pub completion_cost: Option<f64>,
    pub request_id: String,
    pub status_code: Option<i32>,
    pub response_time_ms: Option<i32>,
    pub http_method: Option<String>,
    pub endpoint: Option<String>,
    pub client_ip: Option<String>,
    pub user_agent: Option<String>,
    pub is_inherited: bool,
    pub inherited_from_group_id: Option<String>,
    pub created_at: DateTime<Utc>,
}

// ---------------------------------------------------------------------------
// Usage Alert (persistent alert records for admin dashboard)
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct UsageAlert {
    pub id: String,
    pub tenant_id: String,
    pub resource_type: String, // "llm", "mcp", "skill"
    pub group_id: String,
    pub group_name: Option<String>,
    pub alert_type: String, // "warning", "hard_stop", "resumed"
    pub metric: String,     // "daily_tokens", "monthly_budget", etc.
    pub threshold_pct: i32, // 80, 90, 100
    pub current_value: Option<i64>,
    pub current_value_float: Option<f64>,
    pub limit_value: Option<i64>,
    pub limit_value_float: Option<f64>,
    pub utilization_pct: f64,
    pub period: String, // "daily" or "monthly"
    pub period_start: DateTime<Utc>,
    pub status: String, // "active", "acknowledged", "resolved"
    pub notified_at: Option<DateTime<Utc>>,
    pub acknowledged_by: Option<String>,
    pub acknowledged_at: Option<DateTime<Utc>>,
    pub resolved_at: Option<DateTime<Utc>>,
    pub resolution_note: Option<String>,
    pub triggered_by_request_id: Option<String>,
    pub message: Option<String>,
    pub created_at: Option<DateTime<Utc>>,
}

// ---------------------------------------------------------------------------
// System Alert (persistent alerts for downstream failures, resource health
// transitions, infrastructure thresholds, and internal errors). Distinct
// from `UsageAlert`, which is quota-scoped.
// ---------------------------------------------------------------------------
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct SystemAlert {
    pub id: String,
    pub tenant_id: Option<String>,
    pub severity: String, // "critical" | "high" | "medium" | "low"
    pub category: String, // see services::system_alerts::AlertCategory
    pub source: String,
    pub resource_type: Option<String>,
    pub resource_id: Option<String>,
    pub resource_name: Option<String>,
    pub error_code: Option<String>,
    pub title: String,
    pub message: String,
    pub context: Option<serde_json::Value>,
    pub status: String, // "active" | "acknowledged" | "resolved"
    pub first_seen_at: DateTime<Utc>,
    pub last_seen_at: DateTime<Utc>,
    pub occurrence_count: i32,
    pub acknowledged_at: Option<DateTime<Utc>>,
    pub acknowledged_by: Option<String>,
    pub resolved_at: Option<DateTime<Utc>>,
    pub resolved_by: Option<String>,
    pub resolution_note: Option<String>,
    pub dedup_key: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Duration, Utc};

    fn make_api_key() -> ApiKey {
        ApiKey {
            id: "key-1".to_string(),
            tenant_id: "tenant-1".to_string(),
            key_hash: "hash".to_string(),
            key_prefix: "brut_".to_string(),
            name: "Test Key".to_string(),
            scope: "global".to_string(),
            user_id: None,
            user_email: None,
            group_id: None,
            agent_id: None,
            allowed_skill_ids: None,
            skill_rate_limit_daily: None,
            expires_at: None,
            enabled: true,
            revoked: false,
            usage_count: 0,
            last_used_at: None,
            is_platform_service: false,
            group_name: None,
            group_active: None,
            agent_stands: None,
        }
    }

    #[test]
    fn test_api_key_is_valid_happy_path() {
        let key = make_api_key();
        assert!(key.is_valid());
    }

    #[test]
    fn test_api_key_is_valid_disabled() {
        let mut key = make_api_key();
        key.enabled = false;
        assert!(!key.is_valid());
    }

    #[test]
    fn test_api_key_is_valid_revoked() {
        let mut key = make_api_key();
        key.revoked = true;
        assert!(!key.is_valid());
    }

    #[test]
    fn test_api_key_is_valid_expired() {
        let mut key = make_api_key();
        key.expires_at = Some(Utc::now() - Duration::hours(1));
        assert!(!key.is_valid());
    }

    #[test]
    fn test_api_key_is_valid_no_expiry() {
        let mut key = make_api_key();
        key.expires_at = None;
        assert!(key.is_valid());
    }

    #[test]
    fn test_api_key_is_global() {
        let key = make_api_key();
        assert!(key.is_global());
    }

    #[test]
    fn test_api_key_is_user_specific() {
        let mut key = make_api_key();
        key.scope = "user".to_string();
        key.user_id = Some(42);
        assert!(key.is_user_specific());
    }

    #[test]
    fn test_api_key_is_group_specific() {
        let mut key = make_api_key();
        key.group_id = Some("group-1".to_string());
        assert!(key.is_group_specific());
    }

    // -----------------------------------------------------------------------
    // OAuthAppClient helpers
    // -----------------------------------------------------------------------

    fn make_oauth_client() -> OAuthAppClient {
        OAuthAppClient {
            id: "oauth-1".to_string(),
            tenant_id: "tenant-1".to_string(),
            name: "Test OAuth Client".to_string(),
            app_type: None,
            idp_id: "idp-1".to_string(),
            client_id: "client-id-1".to_string(),
            encrypted_client_secret: None,
            redirect_uri: None,
            scopes: None,
            client_type: None,
            supports_pkce: None,
            allowed_grant_types: None,
            is_active: true,
        }
    }

    #[test]
    fn test_oauth_client_is_confidential_true() {
        let mut client = make_oauth_client();
        client.client_type = Some("confidential".to_string());
        assert!(client.is_confidential());
    }

    #[test]
    fn test_oauth_client_is_confidential_false() {
        let mut client = make_oauth_client();
        client.client_type = Some("public".to_string());
        assert!(!client.is_confidential());
    }

    #[test]
    fn test_oauth_client_is_public_with_secret_true() {
        let mut client = make_oauth_client();
        client.client_type = Some("public_with_secret".to_string());
        assert!(client.is_public_with_secret());
    }

    #[test]
    fn test_oauth_client_is_public_with_secret_false() {
        let mut client = make_oauth_client();
        client.client_type = Some("public".to_string());
        assert!(!client.is_public_with_secret());
    }

    #[test]
    fn test_oauth_client_needs_secret_for_pkce_delegates() {
        let mut client = make_oauth_client();
        client.client_type = Some("public_with_secret".to_string());
        assert!(client.needs_secret_for_pkce());

        client.client_type = Some("public".to_string());
        assert!(!client.needs_secret_for_pkce());
    }

    // -----------------------------------------------------------------------
    // AgentSkill helpers
    // -----------------------------------------------------------------------

    fn make_agent_skill() -> AgentSkill {
        AgentSkill {
            id: "skill-1".to_string(),
            tenant_id: "tenant-1".to_string(),
            name: "query-sales-data".to_string(),
            description: "Query sales data from the warehouse".to_string(),
            license: Some("MIT".to_string()),
            compatibility: None,
            allowed_tools: None,
            version: "1.0.0".to_string(),
            status: "active".to_string(),
            enabled: true,
            metadata: None,
            skill_md_content: None,
            execution_count: 42,
            last_executed_at: None,
            published_at: None,
            created_at: None,
            source_repo_id: None,
        }
    }

    #[test]
    fn test_agent_skill_is_approval_required_with_metadata() {
        let mut skill = make_agent_skill();
        skill.metadata = Some(serde_json::json!({"approval_required": true}));
        assert!(skill.is_approval_required());
    }

    #[test]
    fn test_agent_skill_is_approval_required_without_metadata() {
        let skill = make_agent_skill();
        assert!(!skill.is_approval_required());
    }

    #[test]
    fn test_agent_skill_get_tags_with_tags() {
        let mut skill = make_agent_skill();
        skill.metadata = Some(serde_json::json!({"tags": ["analytics", "sales"]}));
        assert_eq!(skill.get_tags(), vec!["analytics", "sales"]);
    }

    #[test]
    fn test_agent_skill_get_tags_without_tags() {
        let skill = make_agent_skill();
        let tags: Vec<String> = skill.get_tags();
        assert!(tags.is_empty());
    }

    #[test]
    fn test_agent_skill_to_catalog_json() {
        let mut skill = make_agent_skill();
        skill.metadata = Some(serde_json::json!({
            "tags": ["analytics"],
            "category": "data",
            "icon_name": "chart",
            "cost_estimate_usd": 0.05,
            "approval_required": true
        }));

        let json = skill.to_catalog_json();
        assert_eq!(json["id"], "skill-1");
        assert_eq!(json["name"], "query-sales-data");
        assert_eq!(json["description"], "Query sales data from the warehouse");
        assert_eq!(json["version"], "1.0.0");
        assert_eq!(json["status"], "active");
        assert_eq!(json["license"], "MIT");
        assert_eq!(json["tags"], serde_json::json!(["analytics"]));
        assert_eq!(json["category"], "data");
        assert_eq!(json["icon_name"], "chart");
        assert_eq!(json["approval_required"], true);
        assert_eq!(json["execution_count"], 42);
    }
}
