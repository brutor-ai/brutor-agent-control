// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

pub mod a2a_delegation;
pub mod a2a_governance;
pub mod a2a_task_state;
pub mod a2a_v1;
pub mod accepted_callers;
pub mod agent_approval;
pub mod agent_authz;
pub mod agent_impl;
pub mod agent_releases;
pub mod api_key_usage;
pub mod approval_window;
// Which AI System does this call belong to?
pub mod ai_system;
pub mod anthropic_adapter;
pub mod argument_policy;
pub mod assurance_checks;
pub mod assurance_inbox;
pub mod audit_chain;
pub mod audit_verify;
pub mod auth;
pub mod bedrock_runtime;
// A run may touch only the records it is for.
pub mod data_scope;
// Distinct records per requester across runs.
pub mod autonomy;
pub mod background_writer;
pub mod cache;
pub mod call_chain;
pub mod catalog_probe;
pub mod cimd;
pub mod circuit_breaker;
pub mod cloud_auth;
pub mod cloud_hosts;
pub mod complexity;
pub mod compliance;
pub mod configured_duration;
pub mod context_enrichment;
pub mod credential_verifier;
pub mod data_retention;
pub mod db_errors;
pub mod diagnostics;
pub mod evidence;
pub mod mcp_limits;
pub mod mfa_policy;
pub mod record_reach;
pub mod refused_agent_credential;
// Is this AI System behaving differently, and why?
pub mod drift;
pub mod edition;
pub mod edition_guard;
pub mod encryption;
pub mod error_envelope;
pub mod extension_state;
pub mod guardrails;
pub mod icon_cache;
pub mod installation_limits;
pub mod instance_lease;
pub mod internal_call;
pub mod jcs;
pub mod jsonrpc_body;
// Is this AI System still doing anything at all?
pub mod limits;
pub mod liveness;
pub mod llm_adapter;
pub mod llm_governance;
pub mod llm_preflight;
pub mod llm_refusal;
pub mod llm_resilience;
pub mod llm_router;
pub mod mcp_health;
pub mod mcp_wire;
pub mod media_governance;
pub mod media_storage;
pub mod model_metrics_aggregator;
pub mod multimodal_pipeline;
pub mod oauth_token_manager;
pub mod outbound_policy;
pub mod pool_saturation_monitor;
pub mod prompt_cache_usage;
pub mod prompt_caching;
pub mod providers;
pub mod proxy_logger;
pub mod proxy_metrics;
pub mod residency;
pub mod response_cache;
pub mod retry;
pub mod router_state;
// Roll proxy_logs steps up into runs.
pub mod otel_ingest;
pub mod replay;
pub mod run_budget;
pub mod run_controls;
pub mod run_intervention;
pub mod run_ledger;
pub mod run_outcome;
pub mod run_segments;
pub mod search_providers;
pub mod search_server;
pub mod semantic_policy;
pub mod service_auth;
pub mod session_budget;
pub mod skill_limits;
pub mod skill_orchestrator;
pub mod skill_runner_client;
pub mod sse;
pub mod stream_guard;
// skill_source_sync removed: config-import work belongs in the
// control plane. See app/services/skill_source_sync/ in
// brutor-gateway-control for the Python replacement.
pub mod skill_types;
pub mod system_alerts;
pub mod system_alerts_resolver;
// Postgres LISTEN consumer for System Operations Console
// NOTIFY channels (counters reset, cache invalidate, scheduler trigger /
// pause / resume, session revoke, log level, maintenance).
pub mod system_notify;
// Per-tick beacon writer. Each gateway-core
// scheduler stamps `scheduler_runtime_state.last_beacon_at` at the
// top of every tick so the operator console can render state=running
// for healthy loops.
pub mod system_ops_beacon;
pub mod system_server;
pub mod telemetry;
pub mod tenant_cache;
pub mod tenant_features;
pub mod tenant_gate;
pub mod tenant_paths;
pub mod tool_provenance;
pub mod trusted_proxies;
pub mod usage;
pub mod usage_spool;
pub mod video_pipeline;
pub mod workload_identity;

use std::sync::Arc;

use sqlx::PgPool;

use tokio_util::sync::CancellationToken;

use crate::config::AppConfig;
use crate::services::argument_policy::ArgumentPolicyCache;
use crate::services::background_writer::BackgroundWriter;
use crate::services::cache::{
    AgentCardCache, AgentSkillCache, McpServerCache, ModelCache, ProviderCache, ResourceGroupCache,
    UsageState,
};
use crate::services::circuit_breaker::CircuitBreakerRegistry;
use crate::services::encryption::EncryptionService;
use crate::services::guardrails::GuardrailsCache;
use crate::services::icon_cache::IconCache;
use crate::services::mcp_health::McpHealthUpdater;
use crate::services::oauth_token_manager::OAuthTokenManager;
use crate::services::router_state::RouterState;
use crate::services::telemetry::TelemetryState;

/// Shared application state, available to all request handlers via axum State.
pub struct AppState {
    pub config: AppConfig,
    pub db: PgPool,
    pub redis: Option<redis::aio::ConnectionManager>,
    /// Where an MCP session's `initialize` clientInfo is kept for
    /// the session's later requests, on any replica (Redis when configured).
    pub mcp_sessions: crate::services::agent_impl::SessionStore,
    pub http_client: reqwest::Client,
    pub background_writer: BackgroundWriter,
    /// Refused agent credentials of this instance not yet written.
    pub refused_credentials: Arc<refused_agent_credential::Recorder>,
    /// The evidence surface of each mount an extension registered:
    /// what a refusal recorded by authentication on such a route is a row
    /// of. Empty in a Community composition.
    pub route_surfaces: refused_agent_credential::RouteSurfaces,
    pub guardrails_cache: GuardrailsCache,
    /// Semantic Governance Policy cache (TTL) + judge. Shares the resource
    /// cache TTL; the judge self-calls the gateway's own governed chat route.
    pub semantic_policy_cache: crate::services::semantic_policy::SemanticPolicyCache,
    pub semantic_policy_judge: std::sync::Arc<dyn crate::services::semantic_policy::Judge>,
    /// Cache for AST-aware argument policies (POLICY-10). Shares the
    /// pub/sub invalidation channel with the other resource caches —
    /// admin writes propagate to all pods without restart.
    pub argument_policy_cache: ArgumentPolicyCache,
    /// Process-local view of open run aborts. Fed by the
    /// `brutor_run_abort` NOTIFY listener, by Redis, and by the sweeper's
    /// reconcile; read on every governed action.
    pub run_abort_cache: crate::services::run_intervention::RunAbortCache,
    pub encryption: EncryptionService,
    pub oauth_token_manager: OAuthTokenManager,
    pub telemetry: TelemetryState,
    /// LLM router state: cooldowns, rate limits, latency tracking, etc.
    pub router_state: RouterState,
    /// In-memory cache for MCP server icons (avoids repeated DB queries).
    pub icon_cache: IconCache,
    /// In-memory cache for LLM models and routing groups (eliminates DB queries on hot path).
    pub model_cache: ModelCache,
    /// In-memory cache for `llm_providers` rows. The proxy resolves
    /// `LlmModel.provider_id` → slug via this cache so adapter
    /// dispatch can match on `"openai"` etc. without a JOIN on every
    /// model lookup. Wrapped in `Arc` because the model loaders
    /// borrow it from inside async closures.
    pub provider_cache: Arc<ProviderCache>,
    /// In-memory cache for resource groups with effective inheritance (eliminates access control queries).
    pub group_cache: ResourceGroupCache,
    /// In-memory cache for MCP server configurations.
    pub mcp_cache: McpServerCache,
    /// Per-tenant `tenants.maintenance_mode`, cached for 1s and read by
    /// `middleware::maintenance`. Held here (rather than owned by the layer)
    /// so the `brutor:maintenance` NOTIFY consumer can drop an entry the
    /// moment an operator toggles the flag, instead of the tenant waiting
    /// out the TTL.
    pub maintenance_cache: crate::middleware::maintenance::MaintenanceCache,
    /// In-memory cache for agent skill configurations.
    pub skill_cache: AgentSkillCache,
    /// In-memory cache for A2A agent cards + their capabilities.
    /// Eliminates 1-3 DB queries per A2A invocation (`routes/a2a.rs`,
    /// `routes/a2a_v1.rs`).
    pub agent_card_cache: AgentCardCache,
    /// Event-driven A2A task-stream fan-out. SSE streams subscribe here;
    /// `task_store::write` fires `pg_notify`, the per-pod listener relays it,
    /// and the hub wakes the local subscribers. Replaces the 500ms poll in
    /// `build_task_stream` (a long reconcile tick remains as a safety net).
    pub a2a_stream_hub: Arc<crate::services::a2a_v1::stream_hub::A2aStreamHub>,
    /// Redis-backed usage counters (RPM, TPM, cooldowns, active requests). Survives pod restarts.
    /// `Arc` so the RAII `LlmConcurrencyGuard` (whose async slot-release is spawned
    /// on drop) can hold an owned `'static` handle to it.
    pub usage_state: Arc<UsageState>,
    /// The response cache the LLM routes ask: one lookup
    /// before the model is called, one store plan committed after the output
    /// guardrail's verdict. `NoResponseCache` (every lookup misses, nothing
    /// is stored) unless an extension installed a cache behind it.
    pub response_cache: std::sync::Arc<dyn response_cache::ResponseCache>,
    /// The prompt-cache hook: what the request path asks
    /// before it sends a request to a provider. `NoPromptCaching` until an
    /// implementation is installed (the Enterprise service lives in the
    /// extension state of the extension that installs it).
    pub prompt_cache_policy: std::sync::Arc<dyn prompt_caching::PromptCachePolicy>,
    /// The context-enricher hook: what the chat route and
    /// the admission ask about grounding a request in retrieved documents.
    /// `NoContextEnrichment` until an implementation is installed.
    pub context_enricher: std::sync::Arc<dyn context_enrichment::ContextEnricher>,
    /// The credential-verifier hook: the token-type registry
    /// `auth::authenticate_request` asks about every session token. A state
    /// nothing was installed on verifies administrators' sessions and
    /// service tokens and refuses every other type (`RefuseUnregistered`);
    /// the portal registers its sessions (`portal_session`).
    pub credential_verifiers: credential_verifier::CredentialVerifiers,
    /// Which Enterprise pieces were installed on this state:
    /// none, as `AppState::new` leaves it, is the Community Edition. Each
    /// Enterprise piece's `install` adds its feature; the Enterprise-data
    /// check at start (`edition_guard`) reads the edition from it.
    pub composition: edition::Composition,
    /// What the tenant layer requires of each path: whether
    /// a tenant is resolved, required, and blocked when suspended. The
    /// Community Edition's paths until a piece or an extension registers
    /// its own; a path nobody registered gets the most restrictive rule.
    pub tenant_paths: tenant_paths::TenantPaths,
    /// A reason other than suspension to refuse a tenant's runtime requests
    /// (`tenant_gate`). The default, as
    /// `AppState::new` leaves it, gates nothing: in the Community Edition
    /// only suspension blocks a tenant. The Enterprise Edition installs its
    /// trial gate here.
    pub tenant_gate: std::sync::Arc<dyn tenant_gate::TenantGate>,
    /// Circuit breaker registry for MCP server resilience.
    pub circuit_breakers: CircuitBreakerRegistry,
    /// Per-server concurrency limiters (permits per server on this
    /// instance, remade when `max_concurrent_requests` changes:
    /// `mcp_limits::server_permits`).
    pub mcp_concurrency: dashmap::DashMap<String, mcp_limits::ServerPermits>,
    /// Live health status updater for MCP servers.
    pub mcp_health: McpHealthUpdater,
    /// Circuit breaker registry for LLM upstream resilience. Separate
    /// from `circuit_breakers` (which is MCP-only) so trip thresholds
    /// can be tuned independently — LLM providers have different
    /// failure profiles than MCP servers (longer tail latencies, more
    /// rate-limit 429s, etc.). Keyed by `llm_model.id`.
    pub llm_circuit_breakers: CircuitBreakerRegistry,
    /// Per-LLM-model concurrency limiters (semaphore per model). Mirrors
    /// `mcp_concurrency`. Permit is acquired at dispatch and held for
    /// the duration of the upstream call (streaming included). Capped by
    /// `llm_model.max_concurrent_requests` if set, otherwise unbounded.
    pub llm_concurrency: dashmap::DashMap<String, Arc<tokio::sync::Semaphore>>,
    /// Global retry budget — caps in-flight retries as a fraction of
    /// in-flight requests per upstream. Prevents thundering-herd
    /// amplification during partial outages.
    pub retry_budget: crate::services::retry::RetryBudget,
    /// Last time a recovery probe was allowed through the health gate
    /// for each LLM model that is currently `health_status='unhealthy'`
    /// in the DB. The gate uses this to let exactly one request slip
    /// through every `HEALTH_PROBE_INTERVAL` so a recovered upstream
    /// can flip its own flag back to healthy — without a periodic
    /// background probe. Keyed by `llm_model.id`.
    pub llm_health_probes: dashmap::DashMap<String, std::time::Instant>,
    /// Cancelled on shutdown to terminate long-lived SSE streams.
    pub shutdown_token: CancellationToken,
    /// Multimodal Files API backend. Always set —
    /// defaults to `InMemoryStorage` so dev/test paths work
    /// without provisioning a bucket. The builder swaps in
    /// `FilesystemStorage` or `S3Storage` post-construction when the
    /// configuration chooses one (`BRUTOR_MEDIA_STORAGE_BACKEND`).
    pub media_storage: std::sync::Arc<dyn crate::services::media_storage::Storage>,
    /// The filesystem media store, when it is the one in use: the store whose
    /// signed download links the gateway serves itself (`routes::media`).
    /// `None` for every other store, and `/v1/media/` then answers 404.
    pub media_downloads: Option<std::sync::Arc<crate::services::media_storage::FilesystemStorage>>,
    /// Tiny dedicated DB pool used exclusively for diagnostic
    /// snapshots (`pg_stat_activity`) by the slow-request middleware
    /// and the pool saturation monitor. Reachable even when the main
    /// pool is starved — the whole point. `None` when it could not be
    /// opened, or a test configuration turns it off. See
    /// [`crate::services::diagnostics`] for usage.
    pub diagnostic_pool: Option<std::sync::Arc<PgPool>>,
    /// In-process counter aggregator that batches the
    /// per-request `UPDATE llm_model SET total_requests=…` writes.
    /// Eliminates row-level lock contention that previously blocked
    /// the admin reset-resilience endpoint under load. Flushed by a
    /// background task spawned in `main.rs`. See
    /// [`crate::services::model_metrics_aggregator`].
    pub model_metrics: model_metrics_aggregator::ModelMetricsAggregator,
    /// TTL cache for the two DB lookups the tenant
    /// middleware does on every request (api_key→tenant,
    /// tenant→status). Drops ~30% of pool acquires under sustained
    /// chat load. See [`crate::services::tenant_cache`].
    pub tenant_cache: tenant_cache::TenantCache,
    /// Negative cache for MCP servers that don't speak the
    /// optional Streamable-HTTP SSE listener channel.
    ///
    /// Populated when an inbound `GET /v1/proxy/mcp/{id}` is
    /// forwarded upstream and the response carries a non
    /// `text/event-stream` content type (e.g. AWS Knowledge MCP
    /// returning the GitHub README HTML). On a cache hit we
    /// short-circuit subsequent SSE-listen GETs to a clean 405
    /// without round-tripping upstream — that's the documented
    /// signal for MCP SDK clients to stop reopening the listener
    /// stream, breaking the otherwise-tight reconnect loop.
    ///
    /// Keyed by `mcp_servers.id`. Value is the timestamp the row
    /// was inserted; the lookup honours `NON_SSE_CACHE_TTL` and
    /// re-probes after expiry so a server that gains SSE support
    /// recovers without an operator action. In-process only — a
    /// fresh proxy instance re-probes from cold; that's
    /// intentional, the cost of one cold probe per replica is
    /// trivial compared to the loop it prevents.
    pub non_sse_mcp_servers: dashmap::DashMap<String, std::time::Instant>,
    /// State an extension owns, one value per type, filled
    /// before the state is shared. Nothing built in lives here; see
    /// `extension_state` and `tests/extension_state_guard.rs`.
    pub extensions: extension_state::ExtensionState,
    /// The invalidation handlers the listener consults: the
    /// built-in ones and the extensions'. Set by the builder; a state
    /// built by hand holds the built-in ones.
    pub invalidation: cache::InvalidationRegistry,
    /// The cache layers of the System Operations
    /// console beyond the Community ones (`policy`, `group`, `tool`), which
    /// `system_notify` flushes and reports by name. None until an extension
    /// registers its own.
    pub cache_layers: cache::layers::CacheLayers,
    /// The instance lease this gateway holds:
    /// set by the builder when it took one. `None` for a composition that
    /// runs as several instances, and for a state built by hand.
    pub instance_lease: Option<Arc<instance_lease::InstanceLease>>,
    /// Ceilings of the installation above every tenant's own limits
    /// (`services::installation_limits`). None in a state as `new` leaves
    /// it; an extension that knows the installation's ceilings sets them
    /// while the state is composed.
    pub installation_limits: installation_limits::InstallationLimits,
    /// Who answers the control plane's cache-statistics
    /// request. Nobody is installed in a Community state: `system_notify`
    /// answers with one reply for the one instance. The clustering
    /// extension installs its own.
    pub cache_stats_responder: system_notify::CacheStatsResponder,
}

impl AppState {
    pub fn new(
        config: AppConfig,
        db: PgPool,
        redis: Option<redis::aio::ConnectionManager>,
        http_client: reqwest::Client,
        telemetry: TelemetryState,
    ) -> Self {
        let cache_ttl = config.guardrail_cache_ttl_secs;

        let encryption = EncryptionService::new(&config.encryption_key);
        let token_manager = OAuthTokenManager::new(
            encryption.clone(),
            config.jwt_secret.clone(),
            config.jwt_algorithm.clone(),
            config.jwt_expires_in,
            http_client.clone(),
        );

        let provider_cache = Arc::new(ProviderCache::new(db.clone()));
        let model_cache = ModelCache::new(db.clone(), encryption.clone(), provider_cache.clone());
        let group_cache = ResourceGroupCache::new(db.clone());
        let mcp_cache = McpServerCache::new(db.clone());
        let skill_cache = AgentSkillCache::new(db.clone());
        let agent_card_cache = AgentCardCache::new(db.clone());
        let usage_state = Arc::new(UsageState::new(redis.clone()));

        Self {
            config,
            db,
            redis: redis.clone(),
            mcp_sessions: crate::services::agent_impl::SessionStore::new(redis.clone()),
            http_client: http_client.clone(),
            background_writer: BackgroundWriter::new().with_shadow_redis(redis.clone()),
            refused_credentials: Arc::new(refused_agent_credential::Recorder::new()),
            route_surfaces: refused_agent_credential::RouteSurfaces::default(),
            guardrails_cache: GuardrailsCache::new(cache_ttl, encryption.clone()),
            semantic_policy_cache: crate::services::semantic_policy::SemanticPolicyCache::new(
                cache_ttl,
            ),
            semantic_policy_judge: std::sync::Arc::new(
                crate::services::semantic_policy::SelfCallJudge {
                    http_client: http_client.clone(),
                },
            ),
            run_abort_cache: crate::services::run_intervention::RunAbortCache::new(),
            argument_policy_cache: ArgumentPolicyCache::new(cache_ttl),
            encryption,
            oauth_token_manager: token_manager,
            telemetry,
            router_state: RouterState::new(redis),
            icon_cache: IconCache::new(),
            model_cache,
            provider_cache,
            group_cache,
            mcp_cache,
            maintenance_cache: crate::middleware::maintenance::MaintenanceCache::new(),
            skill_cache,
            agent_card_cache,
            a2a_stream_hub: Arc::new(crate::services::a2a_v1::stream_hub::A2aStreamHub::new()),
            usage_state,
            response_cache: Arc::new(response_cache::NoResponseCache),
            prompt_cache_policy: Arc::new(prompt_caching::NoPromptCaching),
            context_enricher: Arc::new(context_enrichment::NoContextEnrichment),
            credential_verifiers: credential_verifier::CredentialVerifiers::community(
                credential_verifier::RefuseUnregistered,
            ),
            composition: edition::Composition::community(),
            tenant_paths: tenant_paths::TenantPaths::community(),
            tenant_gate: Arc::new(tenant_gate::NoTenantGate),
            circuit_breakers: CircuitBreakerRegistry::new(),
            mcp_concurrency: dashmap::DashMap::new(),
            mcp_health: McpHealthUpdater::new(),
            llm_circuit_breakers: CircuitBreakerRegistry::new(),
            llm_concurrency: dashmap::DashMap::new(),
            retry_budget: crate::services::retry::RetryBudget::new(),
            llm_health_probes: dashmap::DashMap::new(),
            shutdown_token: CancellationToken::new(),
            media_storage: std::sync::Arc::new(
                crate::services::media_storage::InMemoryStorage::new(),
            ),
            media_downloads: None,
            // Set later in `main.rs` after the diagnostic pool is
            // built, OR left as None when it is turned off (tests).
            diagnostic_pool: None,
            model_metrics: model_metrics_aggregator::ModelMetricsAggregator::new(),
            tenant_cache: tenant_cache::TenantCache::new(),
            non_sse_mcp_servers: dashmap::DashMap::new(),
            extensions: extension_state::ExtensionState::new(),
            invalidation: cache::InvalidationRegistry::builtin(),
            cache_layers: cache::layers::CacheLayers::community(),
            instance_lease: None,
            installation_limits: installation_limits::InstallationLimits::default(),
            cache_stats_responder: system_notify::CacheStatsResponder::community(),
        }
    }
}

pub type SharedState = Arc<AppState>;
