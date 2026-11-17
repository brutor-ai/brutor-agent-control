// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

use std::sync::Arc;

use crate::env_registry::{Env, EnvError, EnvVar, Problem};
use crate::env_vars;

/// Values that were environment variables and are fixed in code: internal
/// tuning nobody set to anything but these. `AppConfig` still carries them so a test can pass
/// its own.
pub mod fixed {
    /// The diagnostic pool (slow-request captures, pool gauges, the
    /// saturation alert) is always opened; a failure to open it is logged
    /// and the gateway serves without it.
    pub const DIAGNOSTIC_POOL_ENABLED: bool = true;
    /// Connections of the diagnostic pool.
    pub const DIAGNOSTIC_POOL_SIZE: u32 = 2;
    /// Seconds between samples of the PostgreSQL pool.
    pub const POOL_SAMPLER_INTERVAL_SECS: u64 = 5;
    /// Seconds the pool must stay without an idle connection before the
    /// saturation alert.
    pub const POOL_SATURATION_SUSTAINED_SECS: u64 = 60;
    /// Seconds between writes of the per-model counters.
    pub const MODEL_METRICS_FLUSH_INTERVAL_SECS: u64 = 5;
    /// Largest opaque MCP request state, in bytes.
    pub const MCP_REQUEST_STATE_MAX_BYTES: usize = 64 * 1024;
    /// Bound, in seconds, on guardrail-cache staleness when a Redis
    /// invalidation is lost.
    pub const GUARDRAIL_CACHE_TTL_SECS: u64 = 60;
    /// Seconds between media retention sweeps.
    pub const MEDIA_GC_INTERVAL_SECS: u64 = 21_600;
}

#[derive(Clone, Debug)]
pub struct AppConfig {
    pub listen_addr: String,
    /// Where `/metrics` is served when `BRUTOR_METRICS_ENABLED`: its own
    /// listener, never the public one. `BRUTOR_METRICS_LISTEN_ADDR`.
    pub metrics_listen_addr: String,
    /// Origins that may read the non-protocol routes cross-origin
    /// (`middleware::cors`). `BRUTOR_CORS_ORIGINS`, shared with the control plane.
    pub cors_origins: Vec<String>,
    /// Proxies whose `X-Forwarded-For` is believed
    /// (`services::trusted_proxies`). `BRUTOR_TRUSTED_PROXIES`; empty: none.
    pub trusted_proxies: crate::services::trusted_proxies::TrustedProxies,
    pub public_url: String,
    pub database_url: String,
    pub db_pool_size: u32,
    pub redis_url: Option<String>,
    pub jwt_secret: String,
    pub jwt_algorithm: String,
    pub default_mcp_timeout_secs: u64,
    pub guardrail_cache_ttl_secs: u64,
    pub encryption_key: String,
    pub jwt_expires_in: i64,

    // Multimodal Files API. Backend selection:
    //   - "filesystem" → files under BRUTOR_MEDIA_FILESYSTEM_DIR, one
    //                gateway instance; the default when that is set.
    //   - "memory" → in-process bytes (dev / unit tests only); the default
    //                otherwise.
    //   - "s3"     → any number of instances. Hits BRUTOR_MEDIA_S3_ENDPOINT
    //                if set (MinIO / R2 / etc.) or AWS S3 by region.
    pub media_storage_backend: String,
    /// `BRUTOR_MEDIA_FILESYSTEM_DIR`: the filesystem store's directory.
    pub media_filesystem_dir: Option<std::path::PathBuf>,
    /// `BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES`: its size limit.
    pub media_filesystem_max_bytes: u64,
    pub media_bucket: Option<String>,
    pub media_s3_region: String,
    pub media_s3_endpoint: Option<String>,
    /// Public S3 endpoint used **only** when minting presigned
    /// download URLs for browsers. Set this when the proxy and the
    /// browser see different hosts for the same store — most
    /// importantly the trial-bundle layout where the proxy talks
    /// to MinIO over the docker network at `http://minio:9000`
    /// but the browser must reach `http://localhost:9000`. Leave
    /// unset on AWS S3 — the SDK's region-based resolver is the
    /// public host.
    pub media_s3_public_endpoint: Option<String>,
    pub media_s3_force_path_style: bool,
    pub media_s3_access_key_id: Option<String>,
    pub media_s3_secret_access_key: Option<String>,
    pub media_signed_url_ttl_secs: u64,
    pub media_gc_interval_secs: u64,

    /// Ceiling on the opaque MRTR `requestState` an
    /// upstream may hand our clients (spec sets no limit; a governance
    /// proxy states one). Default 64 KiB.
    pub mcp_request_state_max_bytes: usize,

    pub embedding_model: Option<String>,
    /// Deployment-level fallback judge model for semantic policies
    /// (`BRUTOR_JUDGE_MODEL`). Mirrors `BRUTOR_EMBEDDING_MODEL`: the tenant's
    /// `default_judge_model_id` wins, this is the floor beneath it. When
    /// neither is set the policy has no judge and fails open — which is
    /// deliberate and loud, rather than silently grabbing an arbitrary
    /// chat model whose cost and suitability nobody chose.
    pub judge_model: Option<String>,

    // Operations diagnostics. The diagnostic pool is a
    // tiny dedicated `PgPool` used by the slow-request middleware
    // and the pool-saturation monitor for `pg_stat_activity`
    // captures. Reachable when the main pool is starved — the
    // entire reason it exists. Default-on with 2 connections.
    /// `false` to disable the request diagnostics (no slow-request
    /// captures, no pool gauges). Defaults to `true`.
    pub diagnostic_pool_enabled: bool,
    /// Connections in the diagnostic pool. Tiny by design — keep
    /// at 2 unless you have multiple slow requests overlapping
    /// regularly enough that captures back up.
    pub diagnostic_pool_size: u32,
    /// Seconds between background samples of `pool.size()` /
    /// `pool.num_idle()` pushed to Prometheus. Default 5 s.
    pub pool_sampler_interval_secs: u64,

    // Model metrics aggregator. Batched flush of
    // `total_requests` / `failed_requests` / `total_tokens_consumed`
    // increments so the per-request UPDATE no longer fights for the
    // `llm_model` row lock with admin reset-resilience.
    /// Flush interval. Trade-off: lower = fresher dashboard counters
    /// but more UPDATEs per minute; higher = larger crash-loss window.
    /// [`fixed::MODEL_METRICS_FLUSH_INTERVAL_SECS`] in a gateway; a test
    /// passes 0 (interpreted as 100 ms internally).
    pub model_metrics_flush_interval_secs: u64,

    // Saturation alerting.
    /// Sustained-window seconds. Saturation must hold for this long
    /// before the alert fires — prevents one-shot spikes from
    /// page-storming the on-call.
    pub pool_saturation_sustained_secs: u64,
}

/// Placeholder fragments that appear in example configuration files. A
/// secret containing one of them is the value from a public repository,
/// not a secret; refusing to start is the only honest behaviour, because
/// every admin, portal and gateway token is signed with `JWT_SECRET` and
/// every stored provider key is wrapped with `ENCRYPTION_KEY`.
pub const PLACEHOLDER_SECRET_MARKERS: &[&str] = &["your-", "change-this", "changeme", "example"];

/// Whether `value` is missing, blank, too short, or one of the published
/// example values.
pub fn is_placeholder_secret(value: &str) -> bool {
    let v = value.trim().to_ascii_lowercase();
    v.len() < 32 || PLACEHOLDER_SECRET_MARKERS.iter().any(|m| v.contains(m))
}

/// A secret the gateway does not start without, and not with a publicly
/// known value either.
fn required_secret(env: &Env, var: &'static EnvVar) -> Result<String, EnvError> {
    let value = env.required_string(var)?;
    if is_placeholder_secret(&value) {
        return Err(env_vars::invalid(
            var,
            "at least 32 characters and not a placeholder from an example file: generate a \
             real value (openssl rand -hex 32); the gateway refuses to start with a publicly \
             known secret",
        ));
    }
    Ok(value)
}

/// The values of a configuration being read, and what was wrong with them:
/// every problem is found, not only the first.
#[derive(Default)]
struct Reading {
    problems: Vec<Problem>,
}

impl Reading {
    /// The value, or a placeholder when it was refused (the configuration
    /// is not returned then).
    fn take<T: Default>(&mut self, value: Result<T, EnvError>) -> T {
        value.unwrap_or_else(|refusal| {
            self.problems.extend(refusal.0);
            T::default()
        })
    }
}

impl AppConfig {
    /// The gateway's configuration from `env`. A value that is out of range
    /// or not one of the accepted ones is refused, every one found, naming
    /// the variable and never the value: nothing falls back to a default.
    pub fn from_env(env: &Env) -> Result<Self, EnvError> {
        use env_vars::positive;

        let mut reading = Reading::default();
        let listen_addr = reading.take(env.required_string(&env_vars::BRUTOR_GATEWAY_LISTEN_ADDR));
        let public_url = env
            .string(&env_vars::BRUTOR_GATEWAY_PUBLIC_URL)
            .unwrap_or_else(|| format!("http://{}", listen_addr.replace("0.0.0.0", "localhost")));
        let media_storage_backend = reading.take(media_storage_backend(env));
        let jwt_algorithm = reading.take(jwt_algorithm(env));

        let config = Self {
            metrics_listen_addr: reading
                .take(env.required_string(&env_vars::BRUTOR_METRICS_LISTEN_ADDR)),
            cors_origins: crate::middleware::cors::parse_origins(
                env.string(&env_vars::BRUTOR_CORS_ORIGINS).as_deref(),
            ),
            trusted_proxies: reading.take(trusted_proxies(env)),
            listen_addr,
            public_url,
            database_url: reading.take(env.required_string(&env_vars::DATABASE_URL)),
            db_pool_size: reading.take(positive(env, &env_vars::BRUTOR_GATEWAY_DB_POOL_SIZE)),
            // Redis is required: `env_vars::load` refuses a start without
            // REDIS_URL, and the builder refuses one it cannot reach.
            redis_url: env.string(&env_vars::REDIS_URL),
            jwt_secret: reading.take(required_secret(env, &env_vars::JWT_SECRET)),
            jwt_algorithm,
            default_mcp_timeout_secs: reading
                .take(positive(env, &env_vars::BRUTOR_MCP_TIMEOUT_SECS)),
            guardrail_cache_ttl_secs: fixed::GUARDRAIL_CACHE_TTL_SECS,
            encryption_key: reading.take(required_secret(env, &env_vars::ENCRYPTION_KEY)),
            jwt_expires_in: reading.take(positive(env, &env_vars::JWT_EXPIRES_IN_SECS)),

            // Unset, the backend is "filesystem" when its directory is set
            // and "memory" otherwise, so an install from source starts
            // without a bucket or a volume.
            media_storage_backend,
            media_filesystem_dir: env.path(&env_vars::BRUTOR_MEDIA_FILESYSTEM_DIR),
            media_filesystem_max_bytes: reading
                .take(positive(env, &env_vars::BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES)),
            media_bucket: env.string(&env_vars::BRUTOR_MEDIA_BUCKET),
            media_s3_region: env
                .string(&env_vars::BRUTOR_MEDIA_S3_REGION)
                .or_else(|| env.string(&env_vars::AWS_REGION))
                .unwrap_or_else(|| "us-east-1".into()),
            media_s3_endpoint: env.string(&env_vars::BRUTOR_MEDIA_S3_ENDPOINT),
            media_s3_public_endpoint: env.string(&env_vars::BRUTOR_MEDIA_S3_PUBLIC_ENDPOINT),
            media_s3_force_path_style: env
                .flag(&env_vars::BRUTOR_MEDIA_S3_FORCE_PATH_STYLE_ENABLED),
            media_s3_access_key_id: env.string(&env_vars::BRUTOR_MEDIA_S3_ACCESS_KEY_ID),
            media_s3_secret_access_key: env.string(&env_vars::BRUTOR_MEDIA_S3_SECRET_ACCESS_KEY),
            media_signed_url_ttl_secs: reading
                .take(env.number(&env_vars::BRUTOR_MEDIA_SIGNED_URL_TTL_SECS)),
            media_gc_interval_secs: fixed::MEDIA_GC_INTERVAL_SECS,

            mcp_request_state_max_bytes: fixed::MCP_REQUEST_STATE_MAX_BYTES,

            embedding_model: env.string(&env_vars::BRUTOR_EMBEDDING_MODEL),
            judge_model: env.string(&env_vars::BRUTOR_JUDGE_MODEL),

            diagnostic_pool_enabled: fixed::DIAGNOSTIC_POOL_ENABLED,
            diagnostic_pool_size: fixed::DIAGNOSTIC_POOL_SIZE,
            pool_sampler_interval_secs: fixed::POOL_SAMPLER_INTERVAL_SECS,
            model_metrics_flush_interval_secs: fixed::MODEL_METRICS_FLUSH_INTERVAL_SECS,
            pool_saturation_sustained_secs: fixed::POOL_SATURATION_SUSTAINED_SECS,
        };
        if reading.problems.is_empty() {
            Ok(config)
        } else {
            Err(EnvError(reading.problems))
        }
    }

    /// The warning a start logs when `BRUTOR_TRUSTED_PROXIES` says the
    /// gateway is behind a proxy and `BRUTOR_GATEWAY_PUBLIC_URL` is a
    /// loopback address (or unset, which is one): every OAuth URL the gateway
    /// advertises is built from the public URL alone, so clients would be
    /// sent to localhost. The operator's setting to fix; never refused.
    pub fn loopback_public_url_warning(&self) -> Option<&'static str> {
        if self.trusted_proxies.is_empty() || !is_loopback_url(&self.public_url) {
            return None;
        }
        Some(
            "BRUTOR_GATEWAY_PUBLIC_URL is a loopback address (or unset) while \
             BRUTOR_TRUSTED_PROXIES is set: the OAuth issuer, metadata and redirect URIs \
             the gateway advertises are built from the public URL only and will point at \
             localhost. Set BRUTOR_GATEWAY_PUBLIC_URL to the URL clients reach the gateway at.",
        )
    }

    /// Construct a minimal `AppConfig` for integration tests.
    ///
    /// Caller supplies the database URL (typically from `sqlx::test`'s
    /// per-test ephemeral DB). Every other field is set to a safe default
    /// that keeps the test in-process: no Redis, in-memory media storage,
    /// diagnostics off, fast metrics flush.
    pub fn for_test(database_url: String) -> Self {
        Self {
            listen_addr: "127.0.0.1:0".to_string(),
            metrics_listen_addr: "127.0.0.1:0".to_string(),
            cors_origins: crate::middleware::cors::parse_origins(None),
            trusted_proxies: Default::default(),
            public_url: "http://localhost:8100".to_string(),
            database_url,
            db_pool_size: 2,
            redis_url: None,
            jwt_secret: "test-jwt-secret-for-integration-tests-min32chars!!".to_string(),
            jwt_algorithm: "HS256".to_string(),
            default_mcp_timeout_secs: 30,
            guardrail_cache_ttl_secs: 60,
            encryption_key: "test-encryption-key-32-chars-ok!".to_string(),
            jwt_expires_in: 3600,
            media_storage_backend: "memory".to_string(),
            media_filesystem_dir: None,
            media_filesystem_max_bytes: 10 * 1024 * 1024 * 1024,
            media_bucket: None,
            media_s3_region: "us-east-1".to_string(),
            media_s3_endpoint: None,
            media_s3_public_endpoint: None,
            media_s3_force_path_style: true,
            media_s3_access_key_id: None,
            media_s3_secret_access_key: None,
            media_signed_url_ttl_secs: 900,
            media_gc_interval_secs: 21600,
            mcp_request_state_max_bytes: 64 * 1024,
            embedding_model: None,
            judge_model: None,
            diagnostic_pool_enabled: false,
            diagnostic_pool_size: 2,
            pool_sampler_interval_secs: 5,
            model_metrics_flush_interval_secs: 0,
            pool_saturation_sustained_secs: 60,
        }
    }
}

/// Whether `url`'s host is a loopback address: `localhost` (or a name
/// under it), `127.0.0.0/8`, `::1`. A URL that does not parse counts as one
/// (it is no public address either).
fn is_loopback_url(url: &str) -> bool {
    let Ok(parsed) = url::Url::parse(url) else {
        return true;
    };
    match parsed.host() {
        Some(url::Host::Domain(name)) => {
            let name = name.trim_end_matches('.').to_ascii_lowercase();
            name == "localhost" || name.ends_with(".localhost")
        }
        Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
        Some(url::Host::Ipv6(ip)) => {
            ip.is_loopback() || ip.to_ipv4_mapped().is_some_and(|v4| v4.is_loopback())
        }
        None => true,
    }
}

/// `BRUTOR_TRUSTED_PROXIES`: addresses and CIDR ranges. An entry that is
/// neither refuses the start (the name, never the value).
fn trusted_proxies(
    env: &Env,
) -> Result<crate::services::trusted_proxies::TrustedProxies, EnvError> {
    let var = &env_vars::BRUTOR_TRUSTED_PROXIES;
    crate::services::trusted_proxies::TrustedProxies::parse(&env.string(var).unwrap_or_default())
        .map_err(|_| env_vars::invalid(var, var.accepted))
}

/// `BRUTOR_MEDIA_STORAGE_BACKEND`: one of the three stores, in lower case.
/// Unset: `filesystem` when `BRUTOR_MEDIA_FILESYSTEM_DIR` is set (what the
/// packages do), `memory` otherwise. The filesystem store without a
/// directory is refused.
fn media_storage_backend(env: &Env) -> Result<String, EnvError> {
    let var = &env_vars::BRUTOR_MEDIA_STORAGE_BACKEND;
    let dir = &env_vars::BRUTOR_MEDIA_FILESYSTEM_DIR;
    let dir_set = env.string(dir).is_some_and(|d| !d.trim().is_empty());
    let backend = match env.string(var) {
        Some(raw) => raw.trim().to_ascii_lowercase(),
        None if dir_set => "filesystem".to_string(),
        None => "memory".to_string(),
    };
    match backend.as_str() {
        "filesystem" if !dir_set => Err(env_vars::invalid(
            dir,
            "a directory: BRUTOR_MEDIA_STORAGE_BACKEND is filesystem",
        )),
        "filesystem" | "memory" | "s3" => Ok(backend),
        _ => Err(env_vars::invalid(var, var.accepted)),
    }
}

/// `JWT_ALGORITHM`: one of the three the token manager signs with.
fn jwt_algorithm(env: &Env) -> Result<String, EnvError> {
    let var = &env_vars::JWT_ALGORITHM;
    let algorithm = env.required_string(var)?;
    match algorithm.as_str() {
        "HS256" | "HS384" | "HS512" => Ok(algorithm),
        _ => Err(env_vars::invalid(var, var.accepted)),
    }
}

/// What is wrong with the settings the services read where they use them
/// (the outbound policy, the autonomy floor, the audit and evidence keys,
/// the skill runner, the log filter): each is parsed here once, so that a
/// value none of them could use refuses the start instead of being replaced
/// by a default at its first use.
pub fn check(env: &Env) -> Result<(), EnvError> {
    use crate::services::{a2a_delegation, audit_chain, autonomy, evidence, skill_runner_client};
    env_vars::all_of([
        autonomy::fallback_floor_from(env).map(drop),
        a2a_delegation::max_age_from(env).map(drop),
        audit_chain::signing_key_from(env).map(drop),
        audit_chain::trusted_keys_from(env).map(drop),
        evidence::key::signing_key_from(env).map(drop),
        evidence::key::retired_kids_from(env).map(drop),
        evidence::heads::cadence_from(env).map(drop),
        skill_runner_client::check(env),
        crate::services::outbound_policy::OutboundPolicy::try_from_env(env).map(drop),
        crate::services::telemetry::log_filter_from(env).map(drop),
    ])
}

/// What a distribution is called, where the gateway says so.
#[derive(Clone, Copy, Debug)]
pub struct Distribution {
    /// As the start-up log names it (`Community Edition`).
    pub edition: &'static str,
    /// The heading of what its registry generates (`.env.example`).
    pub registry_title: &'static str,
}

/// The Community gateway.
pub const COMMUNITY: Distribution = Distribution {
    edition: "Community Edition",
    registry_title: env_vars::REGISTRY.title,
};

/// Where the builder takes the gateway's configuration from.
pub enum Source {
    /// A configuration that is already made (a test's).
    Given(Box<AppConfig>),
    /// The process environment, which the builder reads once, first thing
    /// in `run`, through the registry of its composition (`load`). What a
    /// binary's `main` hands over.
    Environment(Distribution),
}

impl Source {
    /// The environment of this process, for the distribution `main` is.
    pub fn environment(distribution: Distribution) -> Self {
        Self::Environment(distribution)
    }
}

impl From<AppConfig> for Source {
    fn from(config: AppConfig) -> Self {
        Self::Given(Box::new(config))
    }
}

/// The environment and the configuration of a start, read through
/// `registry` (the composition's: `env_vars::assemble`). In this order:
///
/// 1. a retired or removed name that is set, a value that is not of its
///    variable's kind and a required variable that is missing are refused,
///    all of them, before any setting is used;
/// 2. a value that is out of range or not an accepted one is refused.
///
/// One line per name, never a value. On success the environment is what
/// [`env_vars::process`] returns from then on. `extensions` is the same
/// second step for the variables the composition's extensions declare
/// (`Extension::check_env`).
pub fn load(
    registry: &'static crate::env_registry::Registry,
    extensions: impl FnOnce(&Env) -> Result<(), EnvError>,
) -> Result<(Arc<Env>, AppConfig), EnvError> {
    let env = env_vars::load(registry)?;
    let config = AppConfig::from_env(&env);
    env_vars::all_of([
        check(&env),
        config.as_ref().map(drop).map_err(Clone::clone),
        extensions(&env),
    ])?;
    let config = config?;
    Ok((env, config))
}

#[cfg(test)]
mod tests {
    use super::*;

    const JWT: &str = "test-jwt-secret-for-config-tests-min32chars!!";

    /// `AppConfig` from the required variables plus `vars`, read through the
    /// registry: no test touches the process environment.
    fn read(vars: &[(&str, &str)]) -> Result<AppConfig, EnvError> {
        let mut pairs = vec![
            ("DATABASE_URL", "postgres://test:test@localhost/test"),
            ("REDIS_URL", "redis://localhost:6379/0"),
            ("JWT_SECRET", JWT),
            (
                "ENCRYPTION_KEY",
                "test-encryption-key-for-config-tests-32c!",
            ),
        ];
        for (name, value) in vars {
            pairs.retain(|(known, _)| known != name);
            pairs.push((name, value));
        }
        let env = Env::from_pairs(&env_vars::REGISTRY, pairs)?;
        AppConfig::from_env(&env)
    }

    fn config(vars: &[(&str, &str)]) -> AppConfig {
        read(vars).unwrap_or_else(|refusal| panic!("{refusal}"))
    }

    fn refusal(vars: &[(&str, &str)]) -> String {
        match read(vars) {
            Ok(_) => panic!(
                "accepted: {:?}",
                vars.iter().map(|(n, _)| n).collect::<Vec<_>>()
            ),
            Err(refusal) => refusal.to_string(),
        }
    }

    #[test]
    fn test_default_listen_addr() {
        let config = config(&[]);

        assert_eq!(config.listen_addr, "0.0.0.0:8100");
        assert_eq!(config.public_url, "http://localhost:8100");
        assert_eq!(config.db_pool_size, 20);
        assert_eq!(
            config.redis_url,
            Some("redis://localhost:6379/0".to_string())
        );
        assert_eq!(config.jwt_algorithm, "HS256");
        assert_eq!(config.default_mcp_timeout_secs, 30);
        assert_eq!(config.guardrail_cache_ttl_secs, 60);
        assert_eq!(config.jwt_expires_in, 3600);
    }

    #[test]
    fn trusted_proxies_are_addresses_or_ranges_and_anything_else_refuses_the_start() {
        assert!(config(&[]).trusted_proxies.is_empty());
        assert!(
            config(&[("BRUTOR_TRUSTED_PROXIES", " , ")])
                .trusted_proxies
                .is_empty()
        );
        assert!(
            !config(&[(
                "BRUTOR_TRUSTED_PROXIES",
                "10.0.0.0/8, fd00::/8,192.168.1.10"
            )])
            .trusted_proxies
            .is_empty()
        );
        for bad in [
            "proxy.internal",
            "10.0.0.0/8,secret-host-name",
            "10.0.0.0/40",
        ] {
            let text = refusal(&[("BRUTOR_TRUSTED_PROXIES", bad)]);
            assert!(text.contains("BRUTOR_TRUSTED_PROXIES"), "{text}");
            assert!(!text.contains(bad), "the refusal printed the value: {text}");
        }
    }

    #[test]
    fn a_loopback_public_url_behind_a_trusted_proxy_is_warned_about() {
        let warned = |vars: &[(&str, &str)]| config(vars).loopback_public_url_warning().is_some();
        let proxied = ("BRUTOR_TRUSTED_PROXIES", "10.0.0.0/8");
        // no proxy configured: nothing to warn about
        assert!(!warned(&[]));
        assert!(!warned(&[(
            "BRUTOR_GATEWAY_PUBLIC_URL",
            "http://localhost:8100"
        )]));
        // behind a proxy: unset, localhost, 127.x, ::1 are warned about
        assert!(warned(&[proxied]));
        for url in [
            "http://localhost:8100",
            "http://LOCALHOST.:8100/",
            "http://gw.localhost",
            "http://127.0.0.1:8100",
            "http://127.9.9.9",
            "http://[::1]:8100",
        ] {
            assert!(
                warned(&[proxied, ("BRUTOR_GATEWAY_PUBLIC_URL", url)]),
                "{url}"
            );
        }
        // a public URL, even one that merely contains the word
        for url in [
            "https://gateway.example.com",
            "https://localhost.example.com",
            "http://10.0.0.5:8100",
        ] {
            assert!(
                !warned(&[proxied, ("BRUTOR_GATEWAY_PUBLIC_URL", url)]),
                "{url}"
            );
        }
    }

    #[test]
    fn test_custom_listen_addr_via_env() {
        let config = config(&[("BRUTOR_GATEWAY_LISTEN_ADDR", "127.0.0.1:9000")]);
        assert_eq!(config.listen_addr, "127.0.0.1:9000");
    }

    #[test]
    fn test_public_url_derived_from_listen_addr() {
        let config = config(&[("BRUTOR_GATEWAY_LISTEN_ADDR", "0.0.0.0:9999")]);
        // 0.0.0.0 is replaced with localhost in the derived URL
        assert_eq!(config.public_url, "http://localhost:9999");
    }

    #[test]
    fn test_explicit_public_url_overrides_derived() {
        let config = config(&[("BRUTOR_GATEWAY_PUBLIC_URL", "https://proxy.example.com")]);
        assert_eq!(config.public_url, "https://proxy.example.com");
    }

    #[test]
    fn test_db_pool_size_parsing() {
        assert_eq!(
            config(&[("BRUTOR_GATEWAY_DB_POOL_SIZE", "50")]).db_pool_size,
            50
        );
    }

    /// It used to fall back to 20.
    #[test]
    fn a_pool_size_that_is_not_a_number_refuses_the_start() {
        let text = refusal(&[("BRUTOR_GATEWAY_DB_POOL_SIZE", "not_a_number")]);
        assert!(
            text.contains("BRUTOR_GATEWAY_DB_POOL_SIZE is not valid"),
            "{text}"
        );
        assert!(!text.contains("not_a_number"), "{text}");
    }

    #[test]
    fn test_jwt_expires_in_custom_value() {
        assert_eq!(
            config(&[("JWT_EXPIRES_IN_SECS", "7200")]).jwt_expires_in,
            7200
        );
    }

    #[test]
    fn a_placeholder_secret_still_refuses_to_start() {
        let text = refusal(&[(
            "JWT_SECRET",
            "your-super-secret-jwt-key-change-this-in-production",
        )]);
        assert!(text.contains("JWT_SECRET is not valid"), "{text}");
        assert!(!text.contains("your-super-secret"), "{text}");
    }

    /// Every problem is reported, not only the first.
    #[test]
    fn every_refused_value_is_named_in_one_refusal() {
        let text = refusal(&[
            ("BRUTOR_GATEWAY_DB_POOL_SIZE", "0"),
            ("BRUTOR_MEDIA_STORAGE_BACKEND", "gcs"),
            ("JWT_ALGORITHM", "RS256"),
            ("ENCRYPTION_KEY", "short"),
        ]);
        for name in [
            "BRUTOR_GATEWAY_DB_POOL_SIZE",
            "BRUTOR_MEDIA_STORAGE_BACKEND",
            "JWT_ALGORITHM",
            "ENCRYPTION_KEY",
        ] {
            assert!(text.contains(&format!("{name} is not valid")), "{text}");
        }
        assert!(!text.contains("gcs") && !text.contains("RS256"), "{text}");
    }
}

#[cfg(test)]
mod placeholder_secret_tests {
    use super::is_placeholder_secret;

    #[test]
    fn placeholder_secrets_are_recognised() {
        // The exact values from the trial bundle's checked-in .env.
        assert!(is_placeholder_secret(
            "your-super-secret-jwt-key-change-this-in-production"
        ));
        assert!(is_placeholder_secret(
            "your-secure-encryption-key-here-at-least-32-characters-long"
        ));
        assert!(is_placeholder_secret("short"));
        assert!(is_placeholder_secret("   "));
    }

    #[test]
    fn real_secrets_pass() {
        assert!(!is_placeholder_secret(
            "0123456789abcdef0123456789abcdef0123456789abcdef"
        ));
        assert!(!is_placeholder_secret(
            "test-jwt-secret-for-integration-tests-min32chars!!"
        ));
    }
}
