// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # The gateway builder and the extension trait
//!
//! The binary used to connect the pools, build the state, mount the routes,
//! start the tasks and serve, all in `main`. That assembly is now
//! [`GatewayBuilder`], in the library, so that it has one definition the
//! binary and a test both build, and so that a second distribution can add
//! to it: `main` is `GatewayBuilder::new(config).run().await`.
//!
//! An [`Extension`] contributes the five things a distribution can own:
//! state (one value per type in `AppState.extensions`), routes (merged
//! inside the middleware stack, after the built-in routes), background
//! tasks (started after the built-in ones, in the order given), cache
//! invalidation handlers (registered after the built-in ones), and an
//! OpenAPI fragment (its `paths` and `components.schemas` merged into the
//! document the gateway serves). One extension of a composition may also
//! state the licence the composition is published under, which the document
//! states in place of the Community gateway's Apache-2.0
//! ([`Extension::published_licence`]). Every contribution has a default of
//! nothing, so an extension implements only what it adds.
//!
//! With no extensions, the assembly is what `main` did, in the same order:
//! `tests/integration/app_assembly.rs` and the OpenAPI snapshot pin it.
//!
//! What is refused, before anything is served: an extension written for
//! another version of this interface ([`EXTENSION_INTERFACE_VERSION`]); two
//! extensions of one name; an extension whose own check before start fails
//! ([`Extension::preflight`]); a task or invalidation handler whose name a
//! built-in or an earlier extension already has; an OpenAPI path or schema
//! the document already has; a second extension that states a licence; a state value whose type is already present
//! (`ExtensionState` refuses it). An extension that is half installed would
//! be an assembly nobody described, so [`GatewayBuilder::assemble`] fails as
//! a whole.
//!
//! ## The interface, version 1
//!
//! What an Enterprise extension needs so that the builder constructs no
//! Enterprise service itself. In the order the builder calls them:
//!
//! 1. [`Extension::interface_version`] and [`Extension::name`]: checked
//!    first, before anything of the extension runs.
//! 2. [`Extension::preflight`]: with the configuration only, before a pool
//!    is connected. A refusal here stops the start before anything outside
//!    the process is touched (the licence).
//! 3. [`Extension::prepare`]: async, with the state before it is shared; the
//!    pools, Redis and the HTTP client are there. Where an extension
//!    connects to a service of its own (a vector store) and builds what it
//!    will install.
//! 4. [`Extension::install_state`], then the tasks, invalidation handlers,
//!    routes and OpenAPI fragment.
//! 5. The Enterprise-data check at start, which is the builder's.
//! 6. [`Extension::after_assembly`]: async, with the shared state, after the
//!    check and before a task starts or a request is served. Where an
//!    extension reads the installation (warming a cache).
//!
//! [`Extension::allows_multiple_instances`] is asked by the instance check
//! (`services::instance_lease`): a composition runs as one
//! instance unless an extension says otherwise. [`GatewayBuilder::build`]
//! makes the check once Redis is connected, before the state exists: a
//! second instance of a single-instance composition is refused there, with
//! the message that names the Enterprise Edition.
//!
//! `prepare` and `after_assembly` run on the checked path only
//! ([`GatewayBuilder::assemble_checked`], which [`GatewayBuilder::build`]
//! uses). [`GatewayBuilder::assemble`] is the pure composition: it connects
//! to nothing and awaits nothing, so it runs neither. `install_state` must
//! therefore leave a coherent state when `prepare` never ran: what the
//! extension installs when its outside service is not configured.

use std::collections::{BTreeMap, BTreeSet};
use std::net::SocketAddr;
use std::sync::Arc;

use axum::Router;
use sqlx::postgres::PgPoolOptions;
use tokio::net::TcpListener;

use crate::config::{self, AppConfig};
use crate::env_registry::{self, Env, EnvError, EnvVar, Registry};
use crate::env_vars;
use crate::services::cache::{InvalidationHandler, InvalidationRegistry};
use crate::services::instance_lease::{InstanceLease, LeaseSettings};
use crate::services::telemetry::{self, TelemetryState};
use crate::services::{self, AppState, SharedState};
use crate::tasks::{self, BackgroundTask, TaskContext};
use crate::{app, openapi};

/// The version of the extension interface this library implements. It
/// changes when the meaning of an existing method, or the order the builder
/// calls them in, changes: something an extension can compile against and
/// still be wrong about. An extension states the version it was written for
/// ([`Extension::interface_version`]) and the assembly refuses any other.
pub const EXTENSION_INTERFACE_VERSION: u32 = 1;

/// A licence as the OpenAPI document states it (`info.license`): its name
/// and its SPDX identifier.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ApiLicence {
    pub name: &'static str,
    pub identifier: &'static str,
}

/// The licence of the Community gateway, which its document states unless
/// an extension of the composition states another
/// ([`Extension::published_licence`]).
pub const COMMUNITY_API_LICENCE: ApiLicence = ApiLicence {
    name: "Apache License 2.0",
    identifier: "Apache-2.0",
};

/// What one extension contributes. Every contribution has a default of
/// nothing; the name and the interface version have none.
#[async_trait::async_trait]
pub trait Extension: Send + Sync + 'static {
    /// Unique, stable name; what the System console and the logs show.
    fn name(&self) -> &'static str;

    /// The version of this interface the extension was written for, as a
    /// literal of the extension's own (`1`), not
    /// [`EXTENSION_INTERFACE_VERSION`]: naming the constant would always
    /// agree with whatever library the extension happens to be built with,
    /// which is the mistake this check exists to catch. No default, so that
    /// every extension says it.
    fn interface_version(&self) -> u32;

    /// Whether a deployment composed with this extension may run more than
    /// one gateway instance at a time. `false` unless the extension is what
    /// makes that safe (clustering). A composition allows it
    /// when one of its extensions does ([`Gateway::allows_multiple_instances`]).
    fn allows_multiple_instances(&self) -> bool {
        false
    }

    /// The entitlement this extension needs to be composed, by name; `None`
    /// for one that needs none. In a composition where an extension grants
    /// entitlements ([`Extension::entitlements`]), an extension whose
    /// entitlement is not granted is left out whole: no state, no routes,
    /// no tasks, no invalidation handler, no OpenAPI fragment, and its
    /// async steps are not run. Its routes answer as they do on a gateway
    /// that was never composed with it.
    fn entitlement(&self) -> Option<&'static str> {
        None
    }

    /// The entitlements this extension grants the composition; `None` for
    /// one that grants none (every extension but the one that knows what
    /// the installation is entitled to). Asked after
    /// [`Extension::preflight`] has passed, possibly more than once.
    fn entitlements(&self) -> Option<Entitlements> {
        None
    }

    /// The environment variables this extension reads: its own definitions,
    /// which the gateway's registry (`env_vars::REGISTRY`) does not hold.
    /// The distribution's registry is the gateway's and these
    /// (`env_vars::assemble`): its binary refuses their malformed values,
    /// documents them and prints them with `--print-env`. The gateway
    /// itself neither reads nor lists them.
    fn env_vars(&self) -> &'static [&'static EnvVar] {
        &[]
    }

    /// What is wrong with the values of this extension's variables in `env`
    /// that the kind of a variable does not say (a number out of range).
    /// Asked once, when the builder has read the environment, with the
    /// gateway's own such check: a refusal ends the start there, naming the
    /// variable and never the value.
    fn check_env(&self, _env: &Env) -> Result<(), EnvError> {
        Ok(())
    }

    /// The extension's own check before start, with the configuration only:
    /// no pool is connected yet and nothing outside the process has been
    /// touched. An error refuses the start as a whole. Called on every
    /// assembly path and possibly more than once (before the pools are
    /// connected, and again when the state is composed), so it must be
    /// cheap and give the same answer each time.
    fn preflight(&self, _config: &AppConfig) -> anyhow::Result<()> {
        Ok(())
    }

    /// Async preparation, before [`Extension::install_state`] and before the
    /// state is shared: the pools (`state.db`), Redis and the HTTP client
    /// are connected, the built-in services initialised. Where an extension
    /// connects to a service of its own and builds what it installs; it may
    /// install it here. An error refuses the start as a whole. Runs on the
    /// checked path only (see the module documentation).
    async fn prepare(&self, _state: &mut AppState) -> anyhow::Result<()> {
        Ok(())
    }

    /// Put the extension's state into `state.extensions` (one value per
    /// type). Runs before the state is shared, after the built-in services
    /// are initialised, so the extension may read `state.db`, `state.redis`
    /// and the rest.
    fn install_state(&self, _state: &mut AppState) -> anyhow::Result<()> {
        Ok(())
    }

    /// Routes to merge into the application, inside the middleware stack
    /// (tenant resolution, CORS, tracing and the rest apply to them as to
    /// the built-in routes). Paths are absolute.
    ///
    /// What the tenant layer requires of a path is its rule in
    /// `state.tenant_paths` (`services::tenant_paths`). A route under a
    /// prefix nobody registered gets the most restrictive rule: a tenant is
    /// required and a suspended tenant is blocked. An extension that wants
    /// something else for a prefix of its own registers it in
    /// [`Extension::install_state`]; it cannot change a rule that is in
    /// place.
    fn routes(&self) -> Option<Router<SharedState>> {
        None
    }

    /// Background tasks, started after the built-in ones in this order.
    fn tasks(&self) -> &'static [BackgroundTask] {
        &[]
    }

    /// Cache invalidation handlers, consulted after the built-in ones.
    fn invalidation_handlers(&self) -> &'static [InvalidationHandler] {
        &[]
    }

    /// An OpenAPI document whose `paths` and `components.schemas` join the
    /// gateway's. A path or schema the gateway already documents is refused.
    fn openapi(&self) -> Option<serde_json::Value> {
        None
    }

    /// The licence a composition with this extension is published under,
    /// in place of the Community gateway's ([`COMMUNITY_API_LICENCE`]): what
    /// the OpenAPI document states as `info.license`. `None` for an
    /// extension that does not change it. At most one extension of a
    /// composition states one; two are refused.
    fn published_licence(&self) -> Option<ApiLicence> {
        None
    }

    /// Async step after the assembly and the Enterprise-data check at start,
    /// before a background task starts or a request is served. The state is
    /// shared and complete. Where an extension reads the installation (a
    /// warm-up). An error refuses the start: nothing is served on a state an
    /// extension could not finish. Runs on the checked path only.
    async fn after_assembly(&self, _state: &SharedState) -> anyhow::Result<()> {
        Ok(())
    }
}

/// What a composition is entitled to, as its grantor states it.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Entitlements {
    /// Each entitlement the grantor knows, and whether it is held.
    pub features: BTreeMap<String, bool>,
    /// Names the grantor was given and does not know. They entitle to
    /// nothing.
    pub unknown: Vec<String>,
}

/// Why an assembly was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AssemblyError {
    ExtensionNameTaken(String),
    ExtensionNameEmpty,
    /// The extension was written for another version of the interface.
    InterfaceVersion {
        extension: String,
        built_for: u32,
        supported: u32,
    },
    Preflight {
        extension: String,
        reason: String,
    },
    Prepare {
        extension: String,
        reason: String,
    },
    AfterAssembly {
        extension: String,
        reason: String,
    },
    TaskNameTaken {
        extension: String,
        task: String,
    },
    InvalidationHandler {
        extension: String,
        reason: String,
    },
    OpenApiPathTaken {
        extension: String,
        path: String,
    },
    OpenApiSchemaTaken {
        extension: String,
        schema: String,
    },
    OpenApiShape {
        extension: String,
        reason: String,
    },
    LicenceStatedTwice {
        first: String,
        second: String,
    },
    State {
        extension: String,
        reason: String,
    },
}

impl std::fmt::Display for AssemblyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ExtensionNameTaken(n) => write!(f, "two extensions are named {n}"),
            Self::ExtensionNameEmpty => write!(f, "an extension has an empty name"),
            Self::InterfaceVersion {
                extension,
                built_for,
                supported,
            } => write!(
                f,
                "extension {extension} was written for version {built_for} of the extension \
                 interface and this gateway implements version {supported}: refusing to \
                 assemble it. Build the extension against this gateway's interface."
            ),
            Self::Preflight { extension, reason } => {
                write!(f, "extension {extension}: refused before start: {reason}")
            }
            Self::Prepare { extension, reason } => {
                write!(f, "extension {extension}: preparation failed: {reason}")
            }
            Self::AfterAssembly { extension, reason } => {
                write!(
                    f,
                    "extension {extension}: the step after assembly failed: {reason}"
                )
            }
            Self::TaskNameTaken { extension, task } => {
                write!(
                    f,
                    "extension {extension}: a task named {task} is already registered"
                )
            }
            Self::InvalidationHandler { extension, reason } => {
                write!(
                    f,
                    "extension {extension}: invalidation handler refused: {reason}"
                )
            }
            Self::OpenApiPathTaken { extension, path } => {
                write!(
                    f,
                    "extension {extension}: OpenAPI path {path} is already documented"
                )
            }
            Self::OpenApiSchemaTaken { extension, schema } => {
                write!(
                    f,
                    "extension {extension}: OpenAPI schema {schema} is already documented"
                )
            }
            Self::OpenApiShape { extension, reason } => {
                write!(f, "extension {extension}: OpenAPI fragment: {reason}")
            }
            Self::LicenceStatedTwice { first, second } => write!(
                f,
                "extensions {first} and {second} each state the licence the composition is \
                 published under; one may"
            ),
            Self::State { extension, reason } => {
                write!(f, "extension {extension}: state: {reason}")
            }
        }
    }
}

impl std::error::Error for AssemblyError {}

/// The gateway, assembled and ready to serve or to be driven by a test.
pub struct Gateway {
    pub state: SharedState,
    pub app: Router,
    /// The extensions assembled, in order.
    pub extensions: Vec<&'static str>,
    /// Every background task, built-in then extension, in start order.
    pub tasks: Vec<BackgroundTask>,
    /// The extensions themselves, kept for the steps that follow the
    /// assembly ([`Extension::after_assembly`]).
    installed: Vec<Box<dyn Extension>>,
}

impl Gateway {
    /// Whether this composition may run as more than one instance at a time:
    /// one of its extensions says so. A gateway with no extensions is one
    /// instance.
    pub fn allows_multiple_instances(&self) -> bool {
        self.installed.iter().any(|e| e.allows_multiple_instances())
    }

    /// The names of every background task, in start order.
    pub fn task_names(&self) -> Vec<&'static str> {
        self.tasks.iter().map(|t| t.name).collect()
    }

    /// Start every background task, built-in then extension.
    pub fn start_tasks(&self) {
        tasks::start_all(
            &TaskContext {
                state: &self.state,
                config: &self.state.config,
            },
            &self.tasks,
        );
    }
}

/// Assembles the gateway from its configuration and the extensions given.
pub struct GatewayBuilder {
    config: config::Source,
    telemetry: telemetry::Source,
    extensions: Vec<Box<dyn Extension>>,
    lease: LeaseSettings,
}

impl GatewayBuilder {
    /// A builder over `config`: a configuration that is already made (a
    /// test's `AppConfig`), or the process environment
    /// (`config::Source::environment`), which a binary's `main` hands over
    /// and [`GatewayBuilder::run`] reads, once, before anything else.
    pub fn new(config: impl Into<config::Source>) -> Self {
        Self {
            config: config.into(),
            telemetry: telemetry::Source::Given(TelemetryState {
                prometheus_handle: None,
            }),
            extensions: Vec::new(),
            lease: LeaseSettings::community(),
        }
    }

    /// The environment variables of this composition: the gateway's own and
    /// those its extensions declare ([`Extension::env_vars`]). What the
    /// binary reads its environment through, refuses malformed values by,
    /// and prints with `--print-env`.
    pub fn env_registry(&self, title: &'static str) -> &'static Registry {
        let extensions: Vec<&dyn Extension> = self.extensions.iter().map(Box::as_ref).collect();
        env_vars::assemble(title, &extensions)
    }

    /// Read the environment where the configuration is the environment's
    /// (a binary): the first thing a start does, before a setting is used.
    ///
    /// `.env` in the working directory is loaded; then the environment is
    /// read through the composition's registry ([`Self::env_registry`]). A
    /// malformed value and a missing required
    /// variable refuse the start, one line per name and never a value
    /// (`config::load`). Then the telemetry is initialised, and the
    /// `BRUTOR_*` variables that are set and not read are named in a
    /// warning.
    fn read_environment(&mut self) -> anyhow::Result<()> {
        let config::Source::Environment(distribution) = &self.config else {
            return Ok(());
        };
        let distribution = *distribution;
        dotenvy::dotenv().ok();
        let registry = self.env_registry(distribution.registry_title);
        // What each extension cannot use of its own variables is refused
        // with what the gateway cannot use of its own.
        let extensions: Vec<&dyn Extension> = self.extensions.iter().map(Box::as_ref).collect();
        let (env, config) = config::load(registry, |env| {
            env_vars::all_of(extensions.iter().map(|extension| extension.check_env(env)))
        })?;
        if matches!(self.telemetry, telemetry::Source::Environment) {
            // Structured logs, distributed tracing (OTel) and Prometheus
            // metrics: see telemetry.rs.
            self.telemetry = telemetry::Source::Given(telemetry::init(&env));
        }
        env_vars::warn_of_unread_product_variables(registry, distribution.edition);
        tracing::info!(
            "Starting Brutor Agent Control Core Gateway ({}) on {}",
            distribution.edition,
            config.listen_addr
        );
        self.config = config.into();
        Ok(())
    }

    /// The instance lease to take, in place of the Community
    /// gateway's own (`services::instance_lease`). For a test, which takes a
    /// key of its own on the Redis it shares with other tests; the binary
    /// never calls it (`tests/clustering_guard.rs`).
    pub fn instance_lease(mut self, lease: LeaseSettings) -> Self {
        self.lease = lease;
        self
    }

    /// The telemetry state: the binary hands over
    /// `telemetry::Source::Environment` (`services::telemetry::init`, once
    /// the environment is read); a test leaves the default, or gives a
    /// state.
    pub fn telemetry(mut self, telemetry: impl Into<telemetry::Source>) -> Self {
        self.telemetry = telemetry.into();
        self
    }

    /// Add an extension. Order is the order of routes, tasks and handlers.
    pub fn extension(mut self, extension: impl Extension) -> Self {
        self.extensions.push(Box::new(extension));
        self
    }

    pub fn extension_names(&self) -> Vec<&'static str> {
        self.extensions.iter().map(|e| e.name()).collect()
    }

    /// Connect the pools, build the state, initialise the built-in
    /// services, assemble the extensions, and warm what `main` warmed.
    /// Nothing is served and no task is started.
    pub async fn build(mut self) -> anyhow::Result<Gateway> {
        // First, where the configuration is the environment's (a binary):
        // the environment is read, once, and a start it refuses ends here.
        self.read_environment()?;
        let config::Source::Given(config) = self.config else {
            anyhow::bail!("the environment was not read");
        };
        let config = *config;
        let telemetry = match self.telemetry {
            telemetry::Source::Given(state) => state,
            // Asked for without a configuration from the environment.
            telemetry::Source::Environment => telemetry::init(&env_vars::process()),
        };

        // Before anything is connected: every extension is one this gateway
        // can assemble, and none refuses the start (the licence).
        Self::admit(&config, &self.extensions)?;

        // Database connection pool: `BRUTOR_GATEWAY_DB_POOL_SIZE`
        // connections at most.
        let db_pool = PgPoolOptions::new()
            .max_connections(config.db_pool_size)
            .acquire_timeout(std::time::Duration::from_secs(5))
            .idle_timeout(std::time::Duration::from_secs(600))
            .connect(&config.database_url)
            .await?;

        tracing::info!(
            max_connections = config.db_pool_size,
            "Database pool established"
        );

        // Diagnostic pool — tiny dedicated pool for
        // the request diagnostics (`pg_stat_activity` snapshots fired by
        // the slow-request middleware, plus pool sampler gauges).
        // Reachable when the main pool is starved. See
        // `services::diagnostics`.
        let diagnostic_pool = if config.diagnostic_pool_enabled {
            match services::diagnostics::build_diagnostic_pool(
                &config.database_url,
                config.diagnostic_pool_size,
            )
            .await
            {
                Ok(pool) => {
                    tracing::info!(
                        "Diagnostic pool established ({} max connections)",
                        config.diagnostic_pool_size
                    );
                    Some(Arc::new(pool))
                }
                Err(err) => {
                    // Diagnostic pool failure is non-fatal — we lose
                    // slow-request captures but the proxy still serves
                    // traffic. Log loudly so the operator notices.
                    tracing::warn!(
                        error = %err,
                        "Failed to build diagnostic pool — slow-request \
                         captures and pool gauges disabled."
                    );
                    None
                }
            }
        } else {
            tracing::info!("Diagnostic pool disabled");
            None
        };

        // Redis is REQUIRED. Every usage limit — quotas, budgets, run and session
        // caps, rate and throughput limits, in-flight slots — is admitted
        // atomically on its shared counters (`usage_state::admission`). Without
        // it each replica would enforce its own copy of every limit, multiplying
        // each by the replica count, so the proxy refuses to start rather than
        // run with limits it cannot hold (fail closed).
        let redis_pool = connect_redis(config.redis_url.as_deref()).await;
        let redis_pool = match redis_pool {
            Ok(pool) => pool,
            Err(reason) => {
                tracing::error!(
                    "Redis is required: usage limits are enforced on it. {reason}. Set REDIS_URL \
                     to a reachable Redis; refusing to start."
                );
                anyhow::bail!("Redis is required: {reason}");
            }
        };

        // The instance check (`services::instance_lease`): a
        // composition runs as one instance unless one of its extensions is
        // what makes several safe. With Redis connected and before anything
        // else of the installation is touched, the gateway takes its lease;
        // a live lease held by another instance refuses the start, with the
        // message that names the Enterprise Edition. `main` exits non-zero
        // with it, as it does without Redis.
        let granted = Self::granted(&self.extensions);
        // (An extension the composition is not entitled to is not part of
        // it, and allows nothing.)
        let instance_lease = if self
            .extensions
            .iter()
            .filter(|e| Self::is_entitled(granted.as_ref(), e.as_ref()))
            .any(|e| e.allows_multiple_instances())
        {
            tracing::info!(
                "Instance check skipped: this composition runs as more than one instance"
            );
            None
        } else {
            match InstanceLease::take(redis_pool.clone(), self.lease).await {
                Ok(lease) => {
                    tracing::info!(instance = %lease.id(), "Instance lease taken");
                    Some(Arc::new(lease))
                }
                Err(refusal) => {
                    tracing::error!("{refusal}");
                    return Err(refusal.into());
                }
            }
        };

        // HTTP client for outbound proxy requests — shared connection pool.
        // `connect_timeout` bounds the TCP/TLS connect phase (so an
        // unreachable upstream LLM host fails in ~10s); `timeout` bounds the
        // whole request. Per-request `.timeout(..)` overrides exist on the
        // LLM call builders in `services::llm_router`, but `connect_timeout`
        // is client-level only and is enforced here for every outbound call.
        //
        // Force IPv4 for all outbound calls. Some upstreams (notably
        // `api.anthropic.com`) publish AAAA records, but many container/VPC
        // networks have no IPv6 egress. When the client picks the AAAA and
        // there is no v6 route, the connect silently blackholes until the 10s
        // `connect_timeout` fires — surfacing as an intermittent ~10s hang and
        // 503 (OpenAI, which is IPv4-only, was unaffected, which is why this
        // looked Anthropic-specific). Binding the source to the unspecified
        // IPv4 address pins the socket family to v4, so DNS only uses A records
        // and never attempts a dead v6 path. `connect_timeout` (10s) is kept.
        //
        // Built through the outbound policy: hostnames are resolved by a
        // resolver that drops link-local/metadata addresses and the private
        // ranges BRUTOR_OUTBOUND_ALLOW_HOSTS does not allow, because every
        // upstream this client reaches — MCP servers, LLM endpoints, A2A
        // agents, IdPs — is configured by a tenant. See outbound_policy.rs.
        let http_client = services::outbound_policy::client_builder()
            .local_address(std::net::IpAddr::V4(std::net::Ipv4Addr::UNSPECIFIED))
            .pool_max_idle_per_host(20)
            .pool_idle_timeout(std::time::Duration::from_secs(90))
            .timeout(std::time::Duration::from_secs(120))
            .connect_timeout(std::time::Duration::from_secs(10))
            // No `.redirect(…)` here: the builder's policy checks every hop
            //  and refuses cross-origin hops; setting one would
            // replace it.
            .build()?;
        // Built now so a TLS initialisation failure stops startup rather than
        // the first request that fetches a public URL.
        let _ = services::outbound_policy::public_fetch_client();

        // What a distribution adds to the Community gateway it adds as an
        // extension: nothing of an Enterprise feature is
        // constructed or installed here.
        let mut app_state = AppState::new(
            config.clone(),
            db_pool,
            Some(redis_pool),
            http_client,
            telemetry,
        );
        app_state.instance_lease = instance_lease;

        // Multimodal Files API backend. The default
        // `InMemoryStorage` is already wired by AppState::new — we
        // swap in the filesystem or the S3 store when the configuration
        // chooses one (`config::media_storage_backend`).
        // Misconfiguration fails the boot loud (the directory probe in
        // `FilesystemStorage::open`, head_bucket inside `S3Storage::new`).
        if config.media_storage_backend == "filesystem" {
            let Some(dir) = config.media_filesystem_dir.clone() else {
                return Err(anyhow::anyhow!(
                    "BRUTOR_MEDIA_FILESYSTEM_DIR is required when BRUTOR_MEDIA_STORAGE_BACKEND=filesystem"
                ));
            };
            let store = services::media_storage::FilesystemStorage::open(
                services::media_storage::FilesystemConfig {
                    dir,
                    max_bytes: config.media_filesystem_max_bytes,
                    public_url: config.public_url.clone(),
                },
                services::media_storage::LinkSigner::from_jwt_secret(&config.jwt_secret),
            )
            .await
            .map_err(|err| {
                tracing::error!("filesystem media store failed to open: {err}");
                anyhow::anyhow!("filesystem media store init failed: {err}")
            })?;
            tracing::info!(
                dir = %store.root().display(),
                used_bytes = store.used_bytes(),
                max_bytes = store.max_bytes(),
                links = %format!("{}/v1/media/", config.public_url.trim_end_matches('/')),
                "Media storage backend: filesystem (one gateway instance)"
            );
            if app_state.instance_lease.is_none() {
                // A composition that runs as several instances (no lease).
                tracing::warn!(
                    "The filesystem media store serves ONE gateway instance, and this \
                     composition can run several: with more than one, set \
                     BRUTOR_MEDIA_STORAGE_BACKEND=s3. Each instance that uses the \
                     filesystem store reports itself in Redis; a second one is logged as an \
                     error."
                );
            }
            let store = Arc::new(store);
            app_state.media_storage = store.clone();
            app_state.media_downloads = Some(store);
        } else if config.media_storage_backend == "s3" {
            let bucket = match config.media_bucket.clone() {
                Some(b) => b,
                None => {
                    tracing::error!(
                        "BRUTOR_MEDIA_STORAGE_BACKEND=s3 but BRUTOR_MEDIA_BUCKET unset — refusing to start"
                    );
                    return Err(anyhow::anyhow!(
                        "BRUTOR_MEDIA_BUCKET is required when BRUTOR_MEDIA_STORAGE_BACKEND=s3"
                    ));
                }
            };
            let creds = match (
                config.media_s3_access_key_id.clone(),
                config.media_s3_secret_access_key.clone(),
            ) {
                (Some(k), Some(s)) => Some(services::media_storage::S3Credentials {
                    access_key_id: k,
                    secret_access_key: s,
                    session_token: None,
                }),
                // Either AWS-IAM (instance role / ECS task role) or
                // the standard credential chain — either way no
                // explicit key needed.
                (None, None) => None,
                _ => {
                    tracing::error!(
                        "BRUTOR_MEDIA_S3_ACCESS_KEY_ID and BRUTOR_MEDIA_S3_SECRET_ACCESS_KEY must be set together"
                    );
                    return Err(anyhow::anyhow!("partial S3 credentials"));
                }
            };
            let s3_cfg = services::media_storage::S3Config {
                region: config.media_s3_region.clone(),
                bucket: bucket.clone(),
                endpoint_url: config.media_s3_endpoint.clone(),
                signing_endpoint_url: config.media_s3_public_endpoint.clone(),
                force_path_style: config.media_s3_force_path_style,
                static_credentials: creds,
            };
            match services::media_storage::S3Storage::new(s3_cfg).await {
                Ok(s3) => {
                    tracing::info!(
                        "Media storage backend: S3 (bucket={}, endpoint={:?}, signing_endpoint={:?})",
                        bucket,
                        config.media_s3_endpoint,
                        config.media_s3_public_endpoint
                    );
                    app_state.media_storage = Arc::new(s3);
                }
                Err(err) => {
                    tracing::error!("S3 media storage failed boot probe: {err}");
                    return Err(anyhow::anyhow!("S3 media storage init failed: {err}"));
                }
            }
        } else {
            tracing::warn!(
                "Media storage backend: in-memory, lost at restart (development). Set \
                 BRUTOR_MEDIA_FILESYSTEM_DIR (one instance) or BRUTOR_MEDIA_STORAGE_BACKEND=s3 \
                 to keep files."
            );
        }

        // Wire the diagnostic pool through AppState BEFORE we wrap in
        // Arc — diagnostic_pool is `Option<Arc<PgPool>>` so cloning the
        // outer state still shares the same pool handle.
        app_state.diagnostic_pool = diagnostic_pool.clone();

        // Assembled, and refused here if this is a Community
        // Edition on a database the Enterprise Edition has used. Before the
        // reads below, the background tasks and the listener.
        let gateway = Self::assemble_checked(app_state, self.extensions).await?;
        let state = &gateway.state;

        // Secret-consistency canary — non-fatal. Each trial download mints a
        // unique ENCRYPTION_KEY; reusing a Postgres volume from a previous
        // install means stored model api_key ciphertext was encrypted under
        // the OLD key and fails HMAC under the current one — surfacing only
        // as per-request "decrypt failed" errors + upstream 401s. Decrypt one
        // stored key at boot and, on failure, log a single loud, actionable
        // line so the cause is obvious in the first page of logs. Mirrors the
        // control plane's `secret_preflight`.
        match sqlx::query_as::<_, (String, String)>(
            "SELECT id, api_key FROM llm_model \
             WHERE api_key IS NOT NULL AND api_key <> '' LIMIT 1",
        )
        .fetch_optional(&state.db)
        .await
        {
            Ok(Some((model_id, ciphertext))) => match state.encryption.decrypt(&ciphertext) {
                Ok(_) => tracing::info!("Secret preflight: ENCRYPTION_KEY matches the database"),
                Err(_) => tracing::error!(
                    model_id = %model_id,
                    "ENCRYPTION_KEY does not match the database — a stored model \
                     API key failed to decrypt (HMAC verification failed). Upstream \
                     LLM calls will return 401. This usually means a Postgres volume \
                     was reused from a PREVIOUS install while running a NEW trial \
                     download (each download mints unique secrets). Fix: wipe the \
                     stale volume (`docker compose down -v` then `up`) to re-seed \
                     under the current .env, OR restore the ORIGINAL ENCRYPTION_KEY \
                     from your previous download's .env."
                ),
            },
            Ok(None) => { /* no stored credentials yet — nothing to verify */ }
            Err(e) => tracing::debug!("ENCRYPTION_KEY canary skipped: {e}"),
        }

        // Per-tenant evidence keys are Fernet-sealed under ENCRYPTION_KEY.
        // Installed before the background tasks start: the witness retry loop is one of them.
        services::evidence::key::install_encryption(state.encryption.clone());
        services::evidence::key::warn_on_deployment_drift(&state.db).await;

        Ok(gateway)
    }

    /// The OpenAPI document a composition of `extensions` serves: the
    /// gateway's, with each extension's fragment merged in
    /// ([`Extension::openapi`]) and the licence of the composition as
    /// `info.license`: the Community gateway's ([`COMMUNITY_API_LICENCE`]),
    /// or the one an extension states ([`Extension::published_licence`]).
    /// The OpenAPI snapshot and export are this document for the Community
    /// gateway (no extensions). Refused as a whole, as the assembly is.
    pub fn openapi_document(
        extensions: &[Box<dyn Extension>],
    ) -> anyhow::Result<serde_json::Value> {
        let mut document = openapi::document()?;
        let mut licence: Option<(&'static str, ApiLicence)> = None;
        for ext in extensions {
            if let Some(fragment) = ext.openapi() {
                merge_openapi(&mut document, &fragment, ext.name())?;
            }
            if let Some(stated) = ext.published_licence() {
                if let Some((first, _)) = licence {
                    return Err(AssemblyError::LicenceStatedTwice {
                        first: first.to_string(),
                        second: ext.name().to_string(),
                    }
                    .into());
                }
                licence = Some((ext.name(), stated));
            }
        }
        let licence = licence.map_or(COMMUNITY_API_LICENCE, |(_, stated)| stated);
        let info = document
            .get_mut("info")
            .and_then(|info| info.as_object_mut())
            .ok_or_else(|| anyhow::anyhow!("the OpenAPI document has no info object"))?;
        info.insert(
            "license".to_string(),
            serde_json::json!({ "name": licence.name, "identifier": licence.identifier }),
        );
        Ok(document)
    }

    /// Compose a built state with the extensions: their state, invalidation
    /// handlers, routes, OpenAPI fragments and tasks. Pure with respect to
    /// the outside world (no connection is made, nothing is started), so a
    /// test can drive it with a state of its own. Refuses as a whole.
    ///
    /// It awaits nothing, so [`Extension::prepare`] and
    /// [`Extension::after_assembly`] are not run here:
    /// [`Self::assemble_checked`] runs them around this.
    pub fn assemble(
        mut app_state: AppState,
        extensions: Vec<Box<dyn Extension>>,
    ) -> anyhow::Result<Gateway> {
        // Versions, names and each extension's own check first: everything
        // later is reported against the names, and nothing of an extension
        // that is refused is installed.
        Self::admit(&app_state.config, &extensions)?;
        // … and nothing of an extension the composition is not entitled to.
        let extensions = Self::entitled(&mut app_state.composition, extensions);
        let names: Vec<&'static str> = extensions.iter().map(|e| e.name()).collect();

        // Everything that can be refused is checked on copies before any of
        // it is installed: tasks, invalidation handlers, the OpenAPI document.
        let mut all_tasks: Vec<BackgroundTask> = tasks::BACKGROUND_TASKS.to_vec();
        let mut task_names: BTreeSet<&'static str> = all_tasks.iter().map(|t| t.name).collect();
        let mut invalidation = InvalidationRegistry::builtin();
        let document = Self::openapi_document(&extensions)?;
        for ext in &extensions {
            for task in ext.tasks() {
                if task.name.is_empty() || !task_names.insert(task.name) {
                    return Err(AssemblyError::TaskNameTaken {
                        extension: ext.name().to_string(),
                        task: task.name.to_string(),
                    }
                    .into());
                }
                all_tasks.push(*task);
            }
            for handler in ext.invalidation_handlers() {
                invalidation.register(*handler).map_err(|e| {
                    AssemblyError::InvalidationHandler {
                        extension: ext.name().to_string(),
                        reason: e.to_string(),
                    }
                })?;
            }
        }

        // The state: the composed invalidation registry, then each
        // extension's own values. A refusal here has installed nothing that
        // is observable: the state is dropped with the error.
        app_state.invalidation = invalidation;
        for ext in &extensions {
            ext.install_state(&mut app_state)
                .map_err(|e| AssemblyError::State {
                    extension: ext.name().to_string(),
                    reason: e.to_string(),
                })?;
        }
        let state: SharedState = Arc::new(app_state);

        let extension_routes: Vec<Router<SharedState>> =
            extensions.iter().filter_map(|e| e.routes()).collect();
        let app = app::router_with(state.clone(), extension_routes, document)?;

        Ok(Gateway {
            state,
            app,
            extensions: names,
            tasks: all_tasks,
            installed: extensions,
        })
    }

    /// What the extensions of a composition grant it, together; `None` when
    /// none of them grants entitlements. An entitlement is held when a
    /// grantor says so.
    fn granted(extensions: &[Box<dyn Extension>]) -> Option<BTreeMap<String, bool>> {
        let mut granted: Option<BTreeMap<String, bool>> = None;
        for grant in extensions.iter().filter_map(|e| e.entitlements()) {
            let held = granted.get_or_insert_with(BTreeMap::new);
            for (name, value) in grant.features {
                *held.entry(name).or_insert(false) |= value;
            }
        }
        granted
    }

    /// Whether `extension` may be composed under `granted`: it needs no
    /// entitlement, or the composition has no grantor, or the entitlement is
    /// held. One a grantor does not know is not held.
    ///
    /// A composition without a grantor composes everything it names: what
    /// makes a distribution subject to entitlements is the extension that
    /// knows them, as what makes it need a licence is the licence gate
    /// (`tests/licence_guard.rs` pins that the Enterprise binary composes
    /// it).
    fn is_entitled(granted: Option<&BTreeMap<String, bool>>, extension: &dyn Extension) -> bool {
        match (granted, extension.entitlement()) {
            (Some(granted), Some(needed)) => granted.get(needed).copied().unwrap_or(false),
            _ => true,
        }
    }

    /// The extensions the composition is entitled to, in order, with what
    /// the grantor states and what was left out recorded on `composition`
    /// (`GET /v1/version` reports both). One log line per extension left
    /// out. Asked after [`Self::admit`]: a grantor that refused the start
    /// has granted nothing. Running it again on its own result changes
    /// nothing.
    fn entitled(
        composition: &mut services::edition::Composition,
        extensions: Vec<Box<dyn Extension>>,
    ) -> Vec<Box<dyn Extension>> {
        let Some(granted) = Self::granted(&extensions) else {
            return extensions;
        };
        let mut kept = Vec::with_capacity(extensions.len());
        for ext in extensions {
            if Self::is_entitled(Some(&granted), ext.as_ref()) {
                kept.push(ext);
                continue;
            }
            let needed = ext.entitlement().unwrap_or_default();
            let already = composition.withheld().contains(&(needed, ext.name()));
            composition.withhold(needed, ext.name());
            if !already {
                tracing::warn!(
                    extension = ext.name(),
                    feature = needed,
                    "Extension withheld: the license does not include the feature '{needed}'. \
                     Its routes, tasks and state are not part of this gateway."
                );
            }
        }
        composition.set_entitlements(granted);
        kept
    }

    /// The checks that need nothing but the extensions and the
    /// configuration, in this order: every extension has a name, was
    /// written for this version of the interface, shares its name with no
    /// other, and does not refuse the start. Returns the names, in order.
    fn admit(
        config: &AppConfig,
        extensions: &[Box<dyn Extension>],
    ) -> Result<Vec<&'static str>, AssemblyError> {
        let mut names: Vec<&'static str> = Vec::new();
        for ext in extensions {
            let name = ext.name();
            if name.is_empty() {
                return Err(AssemblyError::ExtensionNameEmpty);
            }
            let built_for = ext.interface_version();
            if built_for != EXTENSION_INTERFACE_VERSION {
                return Err(AssemblyError::InterfaceVersion {
                    extension: name.to_string(),
                    built_for,
                    supported: EXTENSION_INTERFACE_VERSION,
                });
            }
            if names.contains(&name) {
                return Err(AssemblyError::ExtensionNameTaken(name.to_string()));
            }
            names.push(name);
        }
        // Every extension is known to be one this gateway can assemble
        // before any of them is asked anything else.
        for ext in extensions {
            ext.preflight(config)
                .map_err(|e| AssemblyError::Preflight {
                    extension: ext.name().to_string(),
                    reason: e.to_string(),
                })?;
        }
        Ok(names)
    }

    /// [`Self::assemble`], then the Enterprise-data check at start
    /// (`services::edition_guard`): a Community composition does not
    /// start on a database the Enterprise Edition has used, or on one where
    /// that cannot be decided. The refusal is the error, logged once; `main`
    /// exits non-zero with it, as it does without Redis. No `Gateway` exists
    /// for a refused start, so nothing can be served and no task started.
    ///
    /// Around the assembly, the async steps of the extensions
    /// ([`Extension::prepare`] before it, on the state not yet shared;
    /// [`Extension::after_assembly`] after the check). An extension that is
    /// refused (another interface version, a name taken, its own check
    /// before start) has prepared nothing; one whose step fails refuses the
    /// start as a whole.
    pub async fn assemble_checked(
        mut app_state: AppState,
        extensions: Vec<Box<dyn Extension>>,
    ) -> anyhow::Result<Gateway> {
        Self::admit(&app_state.config, &extensions)?;
        // An extension the composition is not entitled to prepares nothing.
        let extensions = Self::entitled(&mut app_state.composition, extensions);
        for ext in &extensions {
            ext.prepare(&mut app_state)
                .await
                .map_err(|e| AssemblyError::Prepare {
                    extension: ext.name().to_string(),
                    reason: e.to_string(),
                })?;
        }
        let gateway = Self::assemble(app_state, extensions)?;
        if let Err(refusal) = services::edition_guard::enforce_at_start(&gateway.state).await {
            tracing::error!("{refusal}");
            return Err(refusal.into());
        }
        for ext in &gateway.installed {
            ext.after_assembly(&gateway.state)
                .await
                .map_err(|e| AssemblyError::AfterAssembly {
                    extension: ext.name().to_string(),
                    reason: e.to_string(),
                })?;
        }
        Ok(gateway)
    }

    /// Build, start the tasks, serve until a shutdown signal, drain.
    ///
    /// Started with `--print-env`, a binary prints the registry of its
    /// composition as JSON instead and reads no variable.
    pub async fn run(self) -> anyhow::Result<()> {
        if let config::Source::Environment(distribution) = &self.config
            && env_registry::print_requested()
        {
            print!(
                "{}",
                self.env_registry(distribution.registry_title).json_text()
            );
            return Ok(());
        }
        let gateway = self.build().await?;
        let state = gateway.state.clone();
        let config = &state.config;

        if !gateway.extensions.is_empty() {
            tracing::info!(extensions = ?gateway.extensions, "Extensions assembled");
        }

        // The background tasks, in the order of `tasks::BACKGROUND_TASKS`,
        // then each extension's.
        gateway.start_tasks();

        let app = gateway.app;

        if let Some(warning) = config.loopback_public_url_warning() {
            tracing::warn!("{warning}");
        }

        let listener = TcpListener::bind(&config.listen_addr).await?;
        tracing::info!("Listening on {}", config.listen_addr);

        // Metrics on a listener of their own, when telemetry exposes them.
        if state.telemetry.prometheus_handle.is_some() {
            let metrics_listener = TcpListener::bind(&config.metrics_listen_addr).await?;
            tracing::info!("Metrics on {}", config.metrics_listen_addr);
            let metrics_app = app::metrics_router(state.clone());
            let metrics_shutdown = state.shutdown_token.clone();
            tokio::spawn(async move {
                if let Err(e) = axum::serve(metrics_listener, metrics_app)
                    .with_graceful_shutdown(async move { metrics_shutdown.cancelled().await })
                    .await
                {
                    tracing::error!(error = %e, "metrics listener stopped");
                }
            });
        }

        let shutdown_state = state.clone();
        let shutdown_fut = async move {
            shutdown_signal().await;
            shutdown_state.shutdown_token.cancel();
            tracing::info!("Cancellation token fired — SSE streams will terminate");
        };

        axum::serve(
            listener,
            app.into_make_service_with_connect_info::<SocketAddr>(),
        )
        .with_graceful_shutdown(shutdown_fut)
        .await?;

        // Every refused-agent-credential record still pending goes to the
        // audit writer before it drains: a stop loses none.
        services::refused_agent_credential::flush_due(
            &state,
            std::time::Instant::now(),
            std::time::Duration::ZERO,
        )
        .await;
        state
            .background_writer
            .drain(std::time::Duration::from_secs(15))
            .await;

        // A graceful stop gives the instance lease back, so that the next
        // start does not wait for it to lapse.
        if let Some(lease) = &state.instance_lease {
            lease.release().await;
        }

        services::telemetry::shutdown();
        tracing::info!("Gateway shutdown complete");

        Ok(())
    }
}

/// Merge an extension's OpenAPI fragment into the document: its `paths` and
/// `components.schemas`, each refused when the document has the key.
fn merge_openapi(
    document: &mut serde_json::Value,
    fragment: &serde_json::Value,
    extension: &str,
) -> anyhow::Result<()> {
    let shape = |reason: &str| AssemblyError::OpenApiShape {
        extension: extension.to_string(),
        reason: reason.to_string(),
    };
    let Some(fragment) = fragment.as_object() else {
        return Err(shape("not an object").into());
    };
    // Check first, then apply: a refusal leaves the document as it was.
    let mut new_paths = Vec::new();
    if let Some(paths) = fragment.get("paths") {
        let paths = paths
            .as_object()
            .ok_or_else(|| shape("paths is not an object"))?;
        let present = document
            .get("paths")
            .and_then(|p| p.as_object())
            .ok_or_else(|| shape("the document has no paths object"))?;
        for (path, item) in paths {
            if present.contains_key(path) {
                return Err(AssemblyError::OpenApiPathTaken {
                    extension: extension.to_string(),
                    path: path.clone(),
                }
                .into());
            }
            new_paths.push((path.clone(), item.clone()));
        }
    }
    let mut new_schemas = Vec::new();
    if let Some(schemas) = fragment.get("components").and_then(|c| c.get("schemas")) {
        let schemas = schemas
            .as_object()
            .ok_or_else(|| shape("components.schemas is not an object"))?;
        let present = document
            .get("components")
            .and_then(|c| c.get("schemas"))
            .and_then(|s| s.as_object());
        for (name, schema) in schemas {
            if present.is_some_and(|p| p.contains_key(name)) {
                return Err(AssemblyError::OpenApiSchemaTaken {
                    extension: extension.to_string(),
                    schema: name.clone(),
                }
                .into());
            }
            new_schemas.push((name.clone(), schema.clone()));
        }
    }
    if let Some(paths) = document.get_mut("paths").and_then(|p| p.as_object_mut()) {
        for (path, item) in new_paths {
            paths.insert(path, item);
        }
    }
    if !new_schemas.is_empty() {
        let components = document
            .as_object_mut()
            .ok_or_else(|| shape("the document is not an object"))?
            .entry("components")
            .or_insert_with(|| serde_json::json!({}));
        let schemas = components
            .as_object_mut()
            .ok_or_else(|| shape("the document's components is not an object"))?
            .entry("schemas")
            .or_insert_with(|| serde_json::json!({}));
        let schemas = schemas
            .as_object_mut()
            .ok_or_else(|| shape("the document's components.schemas is not an object"))?;
        for (name, schema) in new_schemas {
            schemas.insert(name, schema);
        }
    }
    Ok(())
}

/// How long the gateway waits for Redis at start before it refuses to
/// start. Redis is required (see `build`); a start that cannot reach it must
/// end, with the reason, so that whatever runs the gateway can restart it.
const REDIS_CONNECT_DEADLINE: std::time::Duration = std::time::Duration::from_secs(10);

async fn connect_redis(url: Option<&str>) -> Result<redis::aio::ConnectionManager, String> {
    connect_redis_within(url, REDIS_CONNECT_DEADLINE).await
}

/// Connect to Redis, or say why not, within `deadline`.
///
/// This was `ConnectionManager::new`, whose defaults
/// have no connect timeout and retry six times with a delay that grows by a
/// factor of 100 from one second, capped at a minute: a Redis that refuses
/// the connection kept the gateway neither serving nor exiting for more than
/// five minutes, and one that accepts and never answers (or an address that
/// swallows packets) for as long as it stayed that way.
///
/// Now one attempt is made with a deadline before the manager is built, so
/// an unreachable Redis is a refusal in at most `deadline`. The manager is
/// then built with the same deadline on each of its own (re)connections.
/// The reasons name no address and no credential.
async fn connect_redis_within(
    url: Option<&str>,
    deadline: std::time::Duration,
) -> Result<redis::aio::ConnectionManager, String> {
    let url = url.ok_or_else(|| "no Redis URL is configured (REDIS_URL)".to_string())?;
    let client = redis::Client::open(url)
        .map_err(|e| format!("REDIS_URL is not a valid Redis URL ({})", e.category()))?;
    let unanswered = || {
        format!(
            "Redis did not answer within {} s (REDIS_URL)",
            deadline.as_secs()
        )
    };

    // The probe: one connection, handshake included, under the deadline.
    let probe = tokio::time::timeout(
        deadline,
        client.get_multiplexed_async_connection_with_config(
            &redis::AsyncConnectionConfig::new().set_connection_timeout(deadline),
        ),
    )
    .await
    .map_err(|_| unanswered())?;
    match probe {
        Ok(connection) => drop(connection),
        Err(e) if e.is_timeout() => return Err(unanswered()),
        Err(e) => {
            // The error names the failure (connection refused, a refused
            // AUTH), never the URL or its password.
            return Err(format!("Redis could not be reached (REDIS_URL): {e}"));
        }
    }

    let config = redis::aio::ConnectionManagerConfig::new().set_connection_timeout(deadline);
    let manager = tokio::time::timeout(
        deadline,
        redis::aio::ConnectionManager::new_with_config(client, config),
    )
    .await
    .map_err(|_| unanswered())?
    .map_err(|e| format!("Redis could not be reached (REDIS_URL): {}", e.category()))?;
    tracing::info!("Redis connection established");
    Ok(manager)
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("failed to install CTRL+C signal handler");
    };

    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => tracing::info!("Shutdown signal received (SIGINT)"),
        _ = terminate => tracing::info!("Shutdown signal received (SIGTERM)"),
    }
}

#[cfg(test)]
mod tests {
    // ── Redis at start ───────────────────────────

    /// A server that accepts the connection and never answers: what a
    /// firewall that swallows packets, a hung Redis or a wrong service on
    /// the port looks like from here. A listening socket nobody accepts
    /// from: the kernel completes the TCP handshake and nothing is ever
    /// read or written.
    fn a_silent_server() -> (String, std::net::TcpListener) {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().expect("addr").port();
        (format!("redis://127.0.0.1:{port}/0"), listener)
    }

    /// The gateway is documented as refusing to start without Redis. With a
    /// Redis that accepts and never answers it neither started nor refused:
    /// the connection manager waits for the handshake with no timeout, on
    /// each of its attempts. The connection now has a deadline, and missing
    /// it is the refusal, with the reason.
    #[tokio::test]
    async fn an_unanswering_redis_refuses_the_start_within_a_bounded_time() {
        let (url, held) = a_silent_server();
        let deadline = std::time::Duration::from_secs(2);
        let started = std::time::Instant::now();
        let outcome = tokio::time::timeout(
            std::time::Duration::from_secs(20),
            super::connect_redis_within(Some(&url), deadline),
        )
        .await
        .expect("connecting to an unanswering Redis did not return in 20 s");
        drop(held);
        let reason = outcome.err().expect("no connection was made");
        assert!(
            started.elapsed() < deadline + std::time::Duration::from_secs(3),
            "returned after {:?}",
            started.elapsed()
        );
        assert!(reason.contains("did not answer within 2 s"), "{reason}");
        assert!(
            !reason.contains("127.0.0.1"),
            "the reason names no address: {reason}"
        );
    }

    /// Nothing listens there: refused at once, and said so.
    #[tokio::test]
    async fn an_unreachable_redis_refuses_the_start_with_the_reason() {
        let port = {
            let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
            listener.local_addr().expect("addr").port()
        };
        let started = std::time::Instant::now();
        let reason = super::connect_redis_within(
            Some(&format!("redis://127.0.0.1:{port}/0")),
            std::time::Duration::from_secs(5),
        )
        .await
        .err()
        .expect("no connection was made");
        assert!(
            started.elapsed() < std::time::Duration::from_secs(8),
            "returned after {:?}",
            started.elapsed()
        );
        assert!(reason.contains("could not be reached"), "{reason}");
    }

    #[tokio::test]
    async fn no_redis_url_and_a_malformed_one_are_refusals_too() {
        let d = std::time::Duration::from_secs(1);
        let none = super::connect_redis_within(None, d)
            .await
            .err()
            .expect("refused");
        assert!(none.contains("no Redis URL"), "{none}");
        let bad = super::connect_redis_within(Some("not a url"), d)
            .await
            .err()
            .expect("refused");
        assert!(bad.contains("not a valid Redis URL"), "{bad}");
    }

    use super::*;

    fn doc(paths: &[&str], schemas: &[&str]) -> serde_json::Value {
        let mut d =
            serde_json::json!({ "openapi": "3.1.0", "paths": {}, "components": { "schemas": {} } });
        for p in paths {
            d["paths"][*p] = serde_json::json!({ "get": { "responses": {} } });
        }
        for s in schemas {
            d["components"]["schemas"][*s] = serde_json::json!({ "type": "object" });
        }
        d
    }

    #[test]
    fn a_fragment_adds_its_paths_and_schemas() {
        let mut document = doc(&["/v1/a"], &["A"]);
        merge_openapi(&mut document, &doc(&["/v1/ext/b"], &["B"]), "ext").unwrap();
        assert!(
            document["paths"]["/v1/a"].is_object() && document["paths"]["/v1/ext/b"].is_object()
        );
        assert!(document["components"]["schemas"]["A"].is_object());
        assert!(document["components"]["schemas"]["B"].is_object());
    }

    #[test]
    fn a_fragment_that_redocuments_a_path_or_schema_is_refused_whole() {
        let before = doc(&["/v1/a"], &["A"]);
        let mut document = before.clone();
        let err = merge_openapi(
            &mut document,
            &doc(&["/v1/ext/new", "/v1/a"], &["B"]),
            "ext",
        )
        .unwrap_err();
        assert!(err.to_string().contains("/v1/a"), "{err}");
        assert_eq!(
            document, before,
            "nothing of the refused fragment is in the document"
        );
        let err = merge_openapi(&mut document, &doc(&["/v1/ext/new"], &["A"]), "ext").unwrap_err();
        assert!(err.to_string().contains("schema A"), "{err}");
        assert_eq!(document, before);
    }

    #[test]
    fn a_fragment_of_the_wrong_shape_is_refused() {
        let before = doc(&[], &[]);
        let mut document = before.clone();
        for bad in [
            serde_json::json!([]),
            serde_json::json!({ "paths": [] }),
            serde_json::json!({ "components": { "schemas": 3 } }),
        ] {
            let err = merge_openapi(&mut document, &bad, "ext").unwrap_err();
            assert!(err.to_string().contains("OpenAPI fragment"), "{err}");
            assert_eq!(document, before);
        }
    }

    #[test]
    fn a_fragment_without_components_is_fine_and_a_document_without_gets_one() {
        let mut document = serde_json::json!({ "openapi": "3.1.0", "paths": {} });
        merge_openapi(
            &mut document,
            &serde_json::json!({ "paths": { "/v1/ext": {} } }),
            "ext",
        )
        .unwrap();
        assert!(document["paths"]["/v1/ext"].is_object());
        assert!(document.get("components").is_none());
        merge_openapi(&mut document, &doc(&[], &["Z"]), "ext").unwrap();
        assert!(document["components"]["schemas"]["Z"].is_object());
    }

    // ── The licence the document states ───────────────────

    /// An extension that states a licence of its own.
    struct Licensed(&'static str, ApiLicence);

    #[async_trait::async_trait]
    impl crate::builder::Extension for Licensed {
        fn name(&self) -> &'static str {
            self.0
        }
        fn interface_version(&self) -> u32 {
            1
        }
        fn published_licence(&self) -> Option<ApiLicence> {
            Some(self.1)
        }
    }

    const OTHER: ApiLicence = ApiLicence {
        name: "Another Licence",
        identifier: "LicenseRef-Another",
    };

    /// Without an extension that states one, the document states the
    /// Community gateway's licence; an extension that states one replaces
    /// it; and an extension that states none leaves it as it is.
    #[test]
    fn the_document_states_the_licence_of_the_composition() {
        let licence = |extensions: Vec<Box<dyn Extension>>| {
            GatewayBuilder::openapi_document(&extensions).unwrap()["info"]["license"].clone()
        };
        assert_eq!(
            licence(Vec::new()),
            serde_json::json!({ "name": "Apache License 2.0", "identifier": "Apache-2.0" })
        );
        assert_eq!(
            licence(vec![
                Box::new(Versioned("plain", 1)),
                Box::new(Licensed("licensed", OTHER)),
            ]),
            serde_json::json!({ "name": "Another Licence", "identifier": "LicenseRef-Another" })
        );
        assert_eq!(
            licence(vec![Box::new(Versioned("plain", 1))]),
            licence(Vec::new())
        );
    }

    /// Two extensions that each state a licence are refused: which one the
    /// composition is published under is not decided by their order.
    #[test]
    fn two_extensions_that_state_a_licence_are_refused() {
        let extensions: Vec<Box<dyn Extension>> = vec![
            Box::new(Licensed("first", OTHER)),
            Box::new(Licensed("second", COMMUNITY_API_LICENCE)),
        ];
        let err = GatewayBuilder::openapi_document(&extensions).unwrap_err();
        assert!(
            matches!(
                err.downcast_ref::<AssemblyError>(),
                Some(AssemblyError::LicenceStatedTwice { first, second })
                    if first == "first" && second == "second"
            ),
            "{err}"
        );
    }

    // ── The extension interface version ───────────────────

    struct Versioned(&'static str, u32);

    #[async_trait::async_trait]
    impl crate::builder::Extension for Versioned {
        fn name(&self) -> &'static str {
            self.0
        }
        fn interface_version(&self) -> u32 {
            self.1
        }
    }

    /// An extension written for another version of the interface refuses
    /// the start, before anything is connected, and the message names the
    /// extension, the version it was written for and the version this
    /// gateway implements. The database URL names nothing that listens: an
    /// extension of the right version gets as far as connecting and fails
    /// there, for that reason.
    #[tokio::test]
    async fn an_extension_of_another_interface_version_refuses_the_start() {
        use crate::builder::{EXTENSION_INTERFACE_VERSION, GatewayBuilder};
        let config = || {
            crate::config::AppConfig::for_test(
                "postgresql://nobody:nothing@127.0.0.1:1/absent".to_string(),
            )
        };
        for built_for in [0, EXTENSION_INTERFACE_VERSION + 1] {
            let refusal = match GatewayBuilder::new(config())
                .extension(Versioned("current", EXTENSION_INTERFACE_VERSION))
                .extension(Versioned("stale-extension", built_for))
                .build()
                .await
            {
                Ok(_) => panic!("version {built_for} was assembled"),
                Err(e) => e.to_string(),
            };
            assert_eq!(
                refusal,
                format!(
                    "extension stale-extension was written for version {built_for} of the \
                     extension interface and this gateway implements version \
                     {EXTENSION_INTERFACE_VERSION}: refusing to assemble it. Build the \
                     extension against this gateway's interface."
                )
            );
        }
        let connect = match GatewayBuilder::new(config())
            .extension(Versioned("current", EXTENSION_INTERFACE_VERSION))
            .build()
            .await
        {
            Ok(_) => panic!("built with no database"),
            Err(e) => e.to_string(),
        };
        assert!(
            !connect.contains("extension interface"),
            "the right version is admitted: {connect}"
        );
    }

    // ── Entitlements ────────────────────────────────────────────────────

    /// An extension that grants entitlements, or needs one, or neither.
    struct Entitled {
        name: &'static str,
        needs: Option<&'static str>,
        grants: Option<&'static [(&'static str, bool)]>,
        multi: bool,
    }

    impl super::Extension for Entitled {
        fn name(&self) -> &'static str {
            self.name
        }
        fn interface_version(&self) -> u32 {
            super::EXTENSION_INTERFACE_VERSION
        }
        fn allows_multiple_instances(&self) -> bool {
            self.multi
        }
        fn entitlement(&self) -> Option<&'static str> {
            self.needs
        }
        fn entitlements(&self) -> Option<super::Entitlements> {
            self.grants.map(|grants| super::Entitlements {
                features: grants
                    .iter()
                    .map(|(name, held)| ((*name).to_string(), *held))
                    .collect(),
                unknown: Vec::new(),
            })
        }
    }

    fn needs(name: &'static str, entitlement: &'static str) -> Box<dyn super::Extension> {
        Box::new(Entitled {
            name,
            needs: Some(entitlement),
            grants: None,
            multi: false,
        })
    }

    fn grantor(grants: &'static [(&'static str, bool)]) -> Box<dyn super::Extension> {
        Box::new(Entitled {
            name: "grantor",
            needs: None,
            grants: Some(grants),
            multi: false,
        })
    }

    fn names(extensions: &[Box<dyn super::Extension>]) -> Vec<&'static str> {
        extensions.iter().map(|e| e.name()).collect()
    }

    /// An extension whose entitlement the grantor does not hold, holds as
    /// `false`, or does not know, is left out and recorded; one it holds
    /// and one that needs none stay, in order. Doing it again changes
    /// nothing.
    #[test]
    fn an_extension_without_its_entitlement_is_left_out() {
        use crate::services::edition::Composition;
        let compose = || {
            vec![
                grantor(&[("kb", true), ("search", false)]),
                needs("kb-ext", "kb"),
                needs("search-ext", "search"),
                needs("unknown-ext", "never-heard-of-it"),
                Box::new(Versioned("plain", super::EXTENSION_INTERFACE_VERSION))
                    as Box<dyn super::Extension>,
            ]
        };
        let mut composition = Composition::community();
        let kept = super::GatewayBuilder::entitled(&mut composition, compose());
        assert_eq!(names(&kept), ["grantor", "kb-ext", "plain"]);
        assert_eq!(
            composition.withheld(),
            [
                ("search", "search-ext"),
                ("never-heard-of-it", "unknown-ext")
            ]
        );
        let entitlements = composition.entitlements().expect("stated");
        assert_eq!(entitlements.get("kb"), Some(&true));
        assert_eq!(entitlements.get("search"), Some(&false));
        // Again, on its own result: the same, and nothing recorded twice.
        let kept = super::GatewayBuilder::entitled(&mut composition, kept);
        assert_eq!(names(&kept), ["grantor", "kb-ext", "plain"]);
        assert_eq!(composition.withheld().len(), 2);
    }

    /// A composition with no grantor states no entitlements and leaves
    /// nothing out: the Community Edition, and every composition of a test
    /// that names no grantor.
    #[test]
    fn a_composition_with_no_grantor_composes_what_it_names() {
        use crate::services::edition::Composition;
        let mut composition = Composition::community();
        let kept = super::GatewayBuilder::entitled(
            &mut composition,
            vec![needs("kb-ext", "kb"), needs("search-ext", "search")],
        );
        assert_eq!(names(&kept), ["kb-ext", "search-ext"]);
        assert!(composition.entitlements().is_none());
        assert!(composition.withheld().is_empty());
    }

    /// An extension that is left out does not make the composition one that
    /// may run as several instances.
    #[test]
    fn a_withheld_extension_does_not_allow_several_instances() {
        let multi = |held: bool| -> Vec<Box<dyn super::Extension>> {
            vec![
                grantor(if held {
                    &[("cluster", true)]
                } else {
                    &[("cluster", false)]
                }),
                Box::new(Entitled {
                    name: "cluster-ext",
                    needs: Some("cluster"),
                    grants: None,
                    multi: true,
                }),
            ]
        };
        let allowed = |extensions: &[Box<dyn super::Extension>]| {
            let granted = super::GatewayBuilder::granted(extensions);
            extensions
                .iter()
                .filter(|e| super::GatewayBuilder::is_entitled(granted.as_ref(), e.as_ref()))
                .any(|e| e.allows_multiple_instances())
        };
        assert!(allowed(&multi(true)));
        assert!(!allowed(&multi(false)));
    }

    /// `GET /v1/version` of a composition: the edition's answer, and the
    /// entitlements and what was withheld when a grantor states them.
    #[test]
    fn the_version_answer_states_the_entitlements_of_a_composition_that_has_them() {
        use crate::routes::gateway::{version_info, version_of};
        use crate::services::edition::{Composition, Edition};
        let mut composition = Composition::community();
        assert_eq!(
            version_of(&composition),
            version_info(Edition::Community),
            "a Community composition answers what it always did"
        );
        let kept = super::GatewayBuilder::entitled(
            &mut composition,
            vec![grantor(&[("kb", false)]), needs("kb-ext", "kb")],
        );
        assert_eq!(names(&kept), ["grantor"]);
        let version = version_of(&composition);
        assert_eq!(version["entitlements"], serde_json::json!({"kb": false}));
        assert_eq!(
            version["withheld"],
            serde_json::json!([{"entitlement": "kb", "extension": "kb-ext"}])
        );
    }
}
