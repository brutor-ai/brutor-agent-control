// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every variable of the Community registry does what its definition says:
//! set under its registered name, it changes what the gateway is configured
//! with (liveness); set to a value the gateway cannot use, it is refused.
//!
//! Each variable is read here through the function the gateway reads it
//! with: `AppConfig::from_env` for the configuration the builder and the
//! routes use, and for the settings a service reads where it uses them, that
//! service's own reader (`config::check` runs each of those at start). No
//! test changes the process environment: every one passes its own pairs.
//!
//! The last test holds this file to the registry: a variable that is
//! registered and has no test here fails it.

use std::collections::BTreeSet;

use brutor_gateway_core::config::{self, AppConfig};
use brutor_gateway_core::env_registry::{Env, EnvError};
use brutor_gateway_core::env_vars::{self, REGISTRY};
use brutor_gateway_core::services::autonomy::Autonomy;
use brutor_gateway_core::services::evidence::{heads, key};
use brutor_gateway_core::services::outbound_policy::OutboundPolicy;
use brutor_gateway_core::services::{a2a_delegation, audit_chain, autonomy, telemetry};

const JWT: &str = "config-env-jwt-secret-0123456789-0123456789";
const ENC: &str = "config-env-encryption-key-0123456789-0123456789";
const SEED: &str = "5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e";
const OTHER: &str = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1";

/// The four required variables, then `vars` (which replace them).
fn env(vars: &[(&str, &str)]) -> Result<Env, EnvError> {
    let mut pairs = vec![
        ("DATABASE_URL", "postgresql://u:p@db.test:5432/brutor_test"),
        ("REDIS_URL", "redis://redis.test:6379/0"),
        ("JWT_SECRET", JWT),
        ("ENCRYPTION_KEY", ENC),
    ];
    for (name, value) in vars {
        pairs.retain(|(known, _)| known != name);
        pairs.push((name, value));
    }
    Env::from_pairs(&REGISTRY, pairs)
}

fn accepted(vars: &[(&str, &str)]) -> Env {
    env(vars).unwrap_or_else(|refusal| panic!("{refusal}"))
}

fn config(vars: &[(&str, &str)]) -> AppConfig {
    AppConfig::from_env(&accepted(vars)).unwrap_or_else(|refusal| panic!("{refusal}"))
}

/// The refusal of `vars`, from whichever step refuses them: the kind of the
/// variable, the configuration, or the services' settings.
fn refusal(vars: &[(&str, &str)]) -> String {
    let env = match env(vars) {
        Ok(env) => env,
        Err(refusal) => return refusal.to_string(),
    };
    let refused = env_vars::all_of([AppConfig::from_env(&env).map(drop), config::check(&env)]);
    match refused {
        Ok(()) => panic!(
            "accepted: {:?}",
            vars.iter().map(|(name, _)| name).collect::<Vec<_>>()
        ),
        Err(refusal) => refusal.to_string(),
    }
}

/// The variables this file has a test for. Each test names the ones it
/// covers through here, so that [`every_registered_variable_is_tested_here`]
/// knows.
const TESTED: &[&str] = &[
    "DATABASE_URL",
    "REDIS_URL",
    "JWT_SECRET",
    "ENCRYPTION_KEY",
    "BRUTOR_GATEWAY_LISTEN_ADDR",
    "BRUTOR_GATEWAY_PUBLIC_URL",
    "BRUTOR_CORS_ORIGINS",
    "BRUTOR_TRUSTED_PROXIES",
    "JWT_ALGORITHM",
    "JWT_EXPIRES_IN_SECS",
    "BRUTOR_GATEWAY_DB_POOL_SIZE",
    "BRUTOR_MCP_TIMEOUT_SECS",
    "BRUTOR_A2A_DELEGATION_MAX_AGE_SECS",
    "BRUTOR_EMBEDDING_MODEL",
    "BRUTOR_JUDGE_MODEL",
    "BRUTOR_MEDIA_STORAGE_BACKEND",
    "BRUTOR_MEDIA_BUCKET",
    "BRUTOR_MEDIA_S3_REGION",
    "AWS_REGION",
    "BRUTOR_MEDIA_S3_ENDPOINT",
    "BRUTOR_MEDIA_S3_PUBLIC_ENDPOINT",
    "BRUTOR_MEDIA_S3_FORCE_PATH_STYLE_ENABLED",
    "BRUTOR_MEDIA_S3_ACCESS_KEY_ID",
    "BRUTOR_MEDIA_S3_SECRET_ACCESS_KEY",
    "BRUTOR_MEDIA_SIGNED_URL_TTL_SECS",
    "BRUTOR_MEDIA_FILESYSTEM_DIR",
    "BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES",
    // `services::skill_runner_client` (its settings are private to it):
    // `the_runner_is_configured_by_the_new_names_and_its_secret_is_not_printed`
    // there, and the refusal below.
    "BRUTOR_SKILL_RUNNER_URL",
    "BRUTOR_SKILL_RUNNER_JWT_SECRET",
    "BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS",
    "BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS",
    "BRUTOR_OUTBOUND_ALLOW_HOSTS",
    "BRUTOR_AUTONOMY_FALLBACK_FLOOR",
    "BRUTOR_AUDIT_SIGNING_KEY",
    "BRUTOR_AUDIT_TRUSTED_KEYS",
    "BRUTOR_EVIDENCE_SIGNING_KEY",
    "BRUTOR_EVIDENCE_RETIRED_KEYS",
    "BRUTOR_DEPLOYMENT_ID",
    "BRUTOR_EVIDENCE_ISSUER",
    "BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES",
    "BRUTOR_EVIDENCE_HEAD_CADENCE_SECS",
    // Read per call from the process environment (`witness::egress_enabled`,
    // `routes::evidence_aac::profile_enabled`): their effect is the
    // integration suite's (`evidence_witness.rs`:
    // `kill_switch_stops_all_egress`; `evidence_api.rs`: the AAC route's
    // 404 and 200). Here: the value is a boolean or the start is refused.
    "BRUTOR_EVIDENCE_WITNESS_ENABLED",
    "BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED",
    // Read by `telemetry::init`, which installs process-wide recorders and
    // cannot run twice in a test process: the recorder (and with it the
    // /metrics listener, `builder.rs`), the OTLP exporter and its
    // service.name. Here: the value is accepted or the start is refused;
    // `tests/env_registry.rs` starts the real binary with them.
    "BRUTOR_METRICS_ENABLED",
    "BRUTOR_METRICS_LISTEN_ADDR",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_SERVICE_NAME",
    "RUST_LOG",
    // The format, `telemetry::log_json_from`; the output of each format is
    // `services::telemetry`'s unit tests.
    "BRUTOR_LOG_JSON_ENABLED",
];

#[test]
fn every_registered_variable_is_tested_here() {
    let registered: BTreeSet<&str> = REGISTRY.vars.iter().map(|var| var.name).collect();
    let tested: BTreeSet<&str> = TESTED.iter().copied().collect();
    assert_eq!(tested.len(), TESTED.len(), "a name is listed twice");
    assert_eq!(
        registered.difference(&tested).collect::<Vec<_>>(),
        Vec::<&&str>::new(),
        "registered and not tested in tests/config_env.rs"
    );
    assert_eq!(
        tested.difference(&registered).collect::<Vec<_>>(),
        Vec::<&&str>::new(),
        "tested here and no longer registered"
    );
}

#[test]
fn the_defaults_are_the_registered_ones() {
    let c = config(&[]);
    assert_eq!(c.listen_addr, "0.0.0.0:8100");
    assert_eq!(c.public_url, "http://localhost:8100");
    assert_eq!(c.metrics_listen_addr, "127.0.0.1:9464");
    assert_eq!(
        c.cors_origins,
        ["http://localhost:3001", "http://localhost:3002"]
    );
    assert_eq!(c.database_url, "postgresql://u:p@db.test:5432/brutor_test");
    assert_eq!(c.redis_url.as_deref(), Some("redis://redis.test:6379/0"));
    assert_eq!(
        (c.jwt_secret.as_str(), c.encryption_key.as_str()),
        (JWT, ENC)
    );
    assert_eq!(
        (c.jwt_algorithm.as_str(), c.jwt_expires_in),
        ("HS256", 3600)
    );
    assert_eq!(c.db_pool_size, 20);
    // Fixed in code (the environment-variable audit), no longer variables.
    assert!(c.diagnostic_pool_enabled);
    assert_eq!(c.diagnostic_pool_size, 2);
    assert_eq!(c.pool_sampler_interval_secs, 5);
    assert_eq!(c.pool_saturation_sustained_secs, 60);
    assert_eq!(c.model_metrics_flush_interval_secs, 5);
    assert_eq!(c.default_mcp_timeout_secs, 30);
    assert_eq!(c.mcp_request_state_max_bytes, 65536);
    assert_eq!(c.guardrail_cache_ttl_secs, 60);
    assert_eq!((c.embedding_model, c.judge_model), (None, None));
    assert_eq!(c.media_storage_backend, "memory");
    assert_eq!(c.media_bucket, None);
    assert_eq!(c.media_s3_region, "us-east-1");
    assert_eq!(
        (c.media_s3_endpoint, c.media_s3_public_endpoint),
        (None, None)
    );
    assert!(c.media_s3_force_path_style);
    assert_eq!(
        (c.media_s3_access_key_id, c.media_s3_secret_access_key),
        (None, None)
    );
    assert_eq!(c.media_signed_url_ttl_secs, 900);
    assert_eq!(c.media_gc_interval_secs, 21600);

    let e = accepted(&[]);
    assert_eq!(a2a_delegation::max_age_from(&e), Ok(600));
    assert!(matches!(
        autonomy::fallback_floor_from(&e),
        Ok(Autonomy::Restricted)
    ));
    let policy = OutboundPolicy::from_env(&e);
    // Private destinations are refused with nothing configured.
    assert!(policy.check_url("http://10.0.0.5:11434/v1").is_err());
    assert!(policy.check_url("http://127.0.0.1:8080/").is_err());
    assert!(audit_chain::signing_key_from(&e).unwrap().is_none());
    assert!(audit_chain::trusted_keys_from(&e).unwrap().is_empty());
    assert!(key::signing_key_from(&e).unwrap().is_none());
    assert!(key::retired_kids_from(&e).unwrap().is_empty());
    assert_eq!(key::deployment_id_from(&e), "default");
    assert_eq!(key::issuer_from(&e), "urn:brutor:deployment:default");
    assert_eq!(heads::cadence_from(&e), Ok((100, 900)));
    assert!(e.flag(&env_vars::BRUTOR_EVIDENCE_WITNESS_ENABLED));
    assert!(!e.flag(&env_vars::BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED));
    assert!(!e.flag(&env_vars::BRUTOR_METRICS_ENABLED));
    assert_eq!(
        telemetry::log_filter(&e).to_string(),
        "brutor_gateway_core=info,tower_http=info"
    );
    assert_eq!(e.string(&env_vars::OTEL_EXPORTER_OTLP_ENDPOINT), None);
    assert_eq!(
        e.string(&env_vars::OTEL_SERVICE_NAME).as_deref(),
        Some("brutor-gateway-core")
    );
    assert_eq!(config::check(&e), Ok(()));
}

#[test]
fn each_variable_changes_the_configuration_under_its_new_name() {
    let c = config(&[
        ("DATABASE_URL", "postgresql://a:b@other.test:5432/other"),
        ("REDIS_URL", "rediss://other.test:6380/3"),
        ("JWT_SECRET", "another-jwt-secret-0123456789-0123456789"),
        (
            "ENCRYPTION_KEY",
            "another-encryption-key-0123456789-0123456789",
        ),
        ("BRUTOR_GATEWAY_LISTEN_ADDR", "127.0.0.1:9000"),
        ("BRUTOR_METRICS_LISTEN_ADDR", "127.0.0.1:9465"),
        ("BRUTOR_CORS_ORIGINS", "https://a.test/, https://b.test"),
        ("BRUTOR_TRUSTED_PROXIES", " 10.0.0.0/8, 192.168.1.10 ,"),
        ("JWT_ALGORITHM", "HS512"),
        ("JWT_EXPIRES_IN_SECS", "7200"),
        ("BRUTOR_GATEWAY_DB_POOL_SIZE", "50"),
        ("BRUTOR_MCP_TIMEOUT_SECS", "61"),
        ("BRUTOR_EMBEDDING_MODEL", "embed-x"),
        ("BRUTOR_JUDGE_MODEL", "judge-x"),
        ("BRUTOR_MEDIA_STORAGE_BACKEND", "S3"),
        ("BRUTOR_MEDIA_BUCKET", "bucket-x"),
        ("BRUTOR_MEDIA_S3_REGION", "eu-north-1"),
        ("AWS_REGION", "us-west-2"),
        ("BRUTOR_MEDIA_S3_ENDPOINT", "http://minio.test:9000"),
        ("BRUTOR_MEDIA_S3_PUBLIC_ENDPOINT", "http://localhost:9000"),
        ("BRUTOR_MEDIA_S3_FORCE_PATH_STYLE_ENABLED", "false"),
        ("BRUTOR_MEDIA_S3_ACCESS_KEY_ID", "key-id-x"),
        ("BRUTOR_MEDIA_S3_SECRET_ACCESS_KEY", "secret-x"),
        ("BRUTOR_MEDIA_SIGNED_URL_TTL_SECS", "301"),
    ]);
    assert_eq!(c.database_url, "postgresql://a:b@other.test:5432/other");
    assert_eq!(c.redis_url.as_deref(), Some("rediss://other.test:6380/3"));
    assert_eq!(c.jwt_secret, "another-jwt-secret-0123456789-0123456789");
    assert_eq!(
        c.encryption_key,
        "another-encryption-key-0123456789-0123456789"
    );
    assert_eq!(c.listen_addr, "127.0.0.1:9000");
    // Derived from the listen address when no public URL is given.
    assert_eq!(c.public_url, "http://127.0.0.1:9000");
    assert_eq!(c.metrics_listen_addr, "127.0.0.1:9465");
    assert_eq!(c.cors_origins, ["https://a.test", "https://b.test"]);
    assert_eq!(
        c.trusted_proxies,
        brutor_gateway_core::services::trusted_proxies::TrustedProxies::parse(
            "10.0.0.0/8,192.168.1.10"
        )
        .unwrap()
    );
    assert!(config(&[]).trusted_proxies.is_empty());
    assert_eq!(
        (c.jwt_algorithm.as_str(), c.jwt_expires_in),
        ("HS512", 7200)
    );
    assert_eq!(c.db_pool_size, 50);
    assert_eq!(c.default_mcp_timeout_secs, 61);
    assert_eq!(c.embedding_model.as_deref(), Some("embed-x"));
    assert_eq!(c.judge_model.as_deref(), Some("judge-x"));
    assert_eq!(c.media_storage_backend, "s3");
    assert_eq!(c.media_bucket.as_deref(), Some("bucket-x"));
    assert_eq!(c.media_s3_region, "eu-north-1");
    assert_eq!(
        c.media_s3_endpoint.as_deref(),
        Some("http://minio.test:9000")
    );
    assert_eq!(
        c.media_s3_public_endpoint.as_deref(),
        Some("http://localhost:9000")
    );
    assert!(!c.media_s3_force_path_style);
    assert_eq!(c.media_s3_access_key_id.as_deref(), Some("key-id-x"));
    assert_eq!(c.media_s3_secret_access_key.as_deref(), Some("secret-x"));
    assert_eq!(c.media_signed_url_ttl_secs, 301);

    // The public URL, given.
    let c = config(&[("BRUTOR_GATEWAY_PUBLIC_URL", "https://gateway.test")]);
    assert_eq!(c.public_url, "https://gateway.test");
    // The media region falls back to the SDK's.
    let c = config(&[("AWS_REGION", "us-west-2")]);
    assert_eq!(c.media_s3_region, "us-west-2");
}

#[test]
fn each_setting_a_service_reads_changes_under_its_new_name() {
    let e = accepted(&[
        ("BRUTOR_A2A_DELEGATION_MAX_AGE_SECS", "900"),
        ("BRUTOR_AUTONOMY_FALLBACK_FLOOR", "suspended"),
        (
            "BRUTOR_OUTBOUND_ALLOW_HOSTS",
            " MCP.Internal., *.corp.example ,10.20.0.0/16, 127.0.0.1",
        ),
        ("BRUTOR_AUDIT_SIGNING_KEY", SEED),
        (
            "BRUTOR_AUDIT_TRUSTED_KEYS",
            &format!("{OTHER}, {}", SEED.to_uppercase()),
        ),
        ("BRUTOR_EVIDENCE_SIGNING_KEY", SEED),
        ("BRUTOR_EVIDENCE_RETIRED_KEYS", OTHER),
        ("BRUTOR_DEPLOYMENT_ID", "acme-prod"),
        ("BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES", "2"),
        ("BRUTOR_EVIDENCE_HEAD_CADENCE_SECS", "30"),
        ("BRUTOR_EVIDENCE_WITNESS_ENABLED", "off"),
        ("BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED", "1"),
        ("BRUTOR_METRICS_ENABLED", "true"),
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4317"),
        ("OTEL_SERVICE_NAME", "gateway-eu"),
        ("RUST_LOG", "brutor_gateway_core=debug"),
        ("BRUTOR_SKILL_RUNNER_URL", "http://runner.test:8210"),
        (
            "BRUTOR_SKILL_RUNNER_JWT_SECRET",
            "runner-secret-0123456789-0123456789",
        ),
        ("BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS", "45"),
        ("BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS", "300"),
    ]);
    assert_eq!(config::check(&e), Ok(()));
    assert_eq!(a2a_delegation::max_age_from(&e), Ok(900));
    assert!(matches!(
        autonomy::fallback_floor_from(&e),
        Ok(Autonomy::Suspended)
    ));
    let policy = OutboundPolicy::from_env(&e);
    assert_eq!(policy.allow_hosts, ["mcp.internal", "*.corp.example"]);
    assert!(policy.check_url("http://127.0.0.1:9/").is_ok());
    assert!(policy.check_url("http://10.20.3.4:9/").is_ok());
    assert!(policy.check_url("http://127.0.0.2:9/").is_err());
    assert!(
        OutboundPolicy::from_env(&accepted(&[]))
            .check_url("http://127.0.0.1:9/")
            .is_err()
    );

    let seed: [u8; 32] = hex::decode(SEED).unwrap().try_into().unwrap();
    assert_eq!(
        audit_chain::signing_key_from(&e)
            .unwrap()
            .unwrap()
            .to_bytes(),
        seed
    );
    assert_eq!(
        audit_chain::trusted_keys_from(&e).unwrap(),
        [OTHER.to_string(), SEED.to_string()]
    );
    assert_eq!(key::signing_key_from(&e).unwrap().unwrap().to_bytes(), seed);
    assert_eq!(key::retired_kids_from(&e).unwrap(), [OTHER.to_string()]);
    assert_eq!(key::deployment_id_from(&e), "acme-prod");
    assert_eq!(key::issuer_from(&e), "urn:brutor:deployment:acme-prod");
    assert_eq!(
        key::issuer_from(&accepted(&[("BRUTOR_EVIDENCE_ISSUER", "did:web:gw.test")])),
        "did:web:gw.test"
    );
    assert_eq!(heads::cadence_from(&e), Ok((2, 30)));
    assert!(!e.flag(&env_vars::BRUTOR_EVIDENCE_WITNESS_ENABLED));
    assert!(e.flag(&env_vars::BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED));
    assert!(e.flag(&env_vars::BRUTOR_METRICS_ENABLED));
    assert_eq!(
        e.string(&env_vars::OTEL_EXPORTER_OTLP_ENDPOINT).as_deref(),
        Some("http://collector.test:4317")
    );
    assert_eq!(
        e.string(&env_vars::OTEL_SERVICE_NAME).as_deref(),
        Some("gateway-eu")
    );
    assert_eq!(
        telemetry::log_filter(&e).to_string(),
        "brutor_gateway_core=debug"
    );
    // A filter that does not parse is refused, not replaced by the default.
    let refused = refusal(&[("RUST_LOG", "a=b=c-never-printed")]);
    assert!(refused.contains("RUST_LOG is not valid"), "{refused}");
    assert!(!refused.contains("never-printed"), "{refused}");
    // Unset, it is the default.
    assert_eq!(
        telemetry::log_filter(&accepted(&[])).to_string(),
        "brutor_gateway_core=info,tower_http=info"
    );
}

/// The media store: `BRUTOR_MEDIA_STORAGE_BACKEND` when it is set; unset,
/// the filesystem store when `BRUTOR_MEDIA_FILESYSTEM_DIR` is set and the
/// in-memory one otherwise. The filesystem store without a directory, and a
/// limit of 0, are refused.
#[test]
fn the_media_store_is_the_filesystem_one_when_its_directory_is_set() {
    let c = config(&[]);
    assert_eq!(c.media_storage_backend, "memory");
    assert_eq!(c.media_filesystem_dir, None);
    assert_eq!(c.media_filesystem_max_bytes, 10 * 1024 * 1024 * 1024);

    let c = config(&[("BRUTOR_MEDIA_FILESYSTEM_DIR", "/var/lib/brutor/media")]);
    assert_eq!(c.media_storage_backend, "filesystem");
    assert_eq!(
        c.media_filesystem_dir.as_deref(),
        Some(std::path::Path::new("/var/lib/brutor/media"))
    );

    let c = config(&[
        ("BRUTOR_MEDIA_FILESYSTEM_DIR", "/data/media"),
        ("BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES", "5000000"),
        ("BRUTOR_MEDIA_STORAGE_BACKEND", "filesystem"),
    ]);
    assert_eq!(c.media_storage_backend, "filesystem");
    assert_eq!(c.media_filesystem_max_bytes, 5_000_000);

    // Chosen explicitly, the backend wins over the directory.
    let c = config(&[
        ("BRUTOR_MEDIA_FILESYSTEM_DIR", "/data/media"),
        ("BRUTOR_MEDIA_STORAGE_BACKEND", "s3"),
    ]);
    assert_eq!(c.media_storage_backend, "s3");
    let c = config(&[
        ("BRUTOR_MEDIA_FILESYSTEM_DIR", "/data/media"),
        ("BRUTOR_MEDIA_STORAGE_BACKEND", "memory"),
    ]);
    assert_eq!(c.media_storage_backend, "memory");

    let text = refusal(&[("BRUTOR_MEDIA_STORAGE_BACKEND", "filesystem")]);
    assert!(text.contains("BRUTOR_MEDIA_FILESYSTEM_DIR"), "{text}");
    for bad in ["0", "10GB", "-1", ""] {
        let text = refusal(&[
            ("BRUTOR_MEDIA_FILESYSTEM_DIR", "/data/media"),
            ("BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES", bad),
        ]);
        assert!(
            text.contains("BRUTOR_MEDIA_FILESYSTEM_MAX_BYTES is not valid"),
            "{bad:?}: {text}"
        );
    }
    // Set to nothing is unset, as for every variable of the registry.
    let c = config(&[("BRUTOR_MEDIA_FILESYSTEM_DIR", "")]);
    assert_eq!(c.media_storage_backend, "memory");
}

/// `BRUTOR_LOG_JSON_ENABLED`: off unless set; a value that is not a boolean
/// is refused.
#[test]
fn the_log_format_is_json_only_when_switched_on() {
    assert!(!telemetry::log_json_from(&accepted(&[])));
    assert!(telemetry::log_json_from(&accepted(&[(
        "BRUTOR_LOG_JSON_ENABLED",
        "true"
    )])));
    assert!(!telemetry::log_json_from(&accepted(&[(
        "BRUTOR_LOG_JSON_ENABLED",
        "off"
    )])));
    let text = refusal(&[("BRUTOR_LOG_JSON_ENABLED", "json")]);
    assert!(
        text.contains("BRUTOR_LOG_JSON_ENABLED is not valid"),
        "{text}"
    );
}

/// What the gateway did with each of these before: an unparsable number, 0
/// where 0 cannot work and an unknown word read as the default; a malformed
/// key read as "no key"; a malformed entry of a key list was dropped.
#[test]
fn a_value_the_gateway_cannot_use_is_refused_and_never_replaced_by_a_default() {
    for (name, bad) in [
        // Not numbers.
        ("BRUTOR_GATEWAY_DB_POOL_SIZE", "twenty"),
        ("BRUTOR_MCP_TIMEOUT_SECS", "30s"),
        ("BRUTOR_MEDIA_SIGNED_URL_TTL_SECS", "15m"),
        ("BRUTOR_A2A_DELEGATION_MAX_AGE_SECS", "ten"),
        ("BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS", "5m"),
        ("BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES", "hundred"),
        ("BRUTOR_EVIDENCE_HEAD_CADENCE_SECS", "15m"),
        ("JWT_EXPIRES_IN_SECS", "1h"),
        // Numbers set to nothing.
        ("BRUTOR_GATEWAY_DB_POOL_SIZE", ""),
        // 0 where the gateway cannot work with 0, and out of range.
        ("BRUTOR_GATEWAY_DB_POOL_SIZE", "0"),
        ("BRUTOR_MCP_TIMEOUT_SECS", "0"),
        ("BRUTOR_A2A_DELEGATION_MAX_AGE_SECS", "0"),
        ("BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS", "0"),
        ("BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS", "0"),
        ("BRUTOR_EVIDENCE_HEAD_CADENCE_ENTRIES", "0"),
        ("BRUTOR_EVIDENCE_HEAD_CADENCE_SECS", "0"),
        ("JWT_EXPIRES_IN_SECS", "0"),
        ("BRUTOR_GATEWAY_DB_POOL_SIZE", "4294967296"),
        // Booleans: only the eight spellings; "set to nothing" is not "off".
        ("BRUTOR_MEDIA_S3_FORCE_PATH_STYLE_ENABLED", "path"),
        ("BRUTOR_EVIDENCE_WITNESS_ENABLED", "disabled"),
        ("BRUTOR_EVIDENCE_PROFILE_AAC_ENABLED", "2"),
        ("BRUTOR_METRICS_ENABLED", "prometheus"),
        // Addresses and URLs.
        ("BRUTOR_TRUSTED_PROXIES", "proxy.internal"),
        ("BRUTOR_TRUSTED_PROXIES", "10.0.0.0/8,*"),
        ("BRUTOR_GATEWAY_LISTEN_ADDR", "8100"),
        ("BRUTOR_METRICS_LISTEN_ADDR", "localhost"),
        ("BRUTOR_GATEWAY_PUBLIC_URL", "gateway.example.com"),
        ("BRUTOR_MEDIA_S3_ENDPOINT", "minio:9000"),
        ("BRUTOR_MEDIA_S3_PUBLIC_ENDPOINT", ""),
        ("BRUTOR_SKILL_RUNNER_URL", "skill-runner"),
        ("DATABASE_URL", "localhost/brutor"),
        ("REDIS_URL", "localhost:6379"),
        // One of a set.
        ("JWT_ALGORITHM", "RS256"),
        ("BRUTOR_MEDIA_STORAGE_BACKEND", "gcs"),
        ("BRUTOR_AUTONOMY_FALLBACK_FLOOR", "careful"),
        ("BRUTOR_AUTONOMY_FALLBACK_FLOOR", "Restricted"),
        // The outbound allow-list: a host, *.domain, an address or a range.
        ("BRUTOR_OUTBOUND_ALLOW_HOSTS", "mcp.internal:8080"),
        ("BRUTOR_OUTBOUND_ALLOW_HOSTS", "10.0.0.0/33"),
        ("BRUTOR_OUTBOUND_ALLOW_HOSTS", "http://mcp.internal"),
        // Keys.
        ("BRUTOR_AUDIT_SIGNING_KEY", "not-a-key"),
        ("BRUTOR_AUDIT_SIGNING_KEY", &SEED[..62]),
        ("BRUTOR_EVIDENCE_SIGNING_KEY", "not-a-key"),
        ("BRUTOR_AUDIT_TRUSTED_KEYS", &format!("{OTHER},typo")),
        ("BRUTOR_EVIDENCE_RETIRED_KEYS", &OTHER[..60]),
        // Secrets the gateway signs and decrypts with.
        ("JWT_SECRET", "short"),
        (
            "ENCRYPTION_KEY",
            "your-secure-encryption-key-here-at-least-32-characters",
        ),
    ] {
        let text = refusal(&[(name, bad)]);
        assert!(
            text.contains(&format!("{name} is not valid")),
            "{name}={bad:?}: {text}"
        );
        assert!(
            bad.len() < 4 || !text.contains(bad),
            "{name}: the value was printed: {text}"
        );
    }
}

#[test]
fn a_missing_required_variable_is_named() {
    // `Env::load` is what the binary calls (tests/env_registry.rs runs it);
    // the configuration asks for each required variable too.
    let empty = Env::from_pairs(&REGISTRY, [] as [(&str, &str); 0]).unwrap();
    let text = AppConfig::from_env(&empty).unwrap_err().to_string();
    for name in ["DATABASE_URL", "JWT_SECRET", "ENCRYPTION_KEY"] {
        assert!(
            text.contains(&format!("{name} is required and is not set")),
            "{text}"
        );
    }
    let required: Vec<&str> = REGISTRY
        .vars
        .iter()
        .filter(|var| var.required)
        .map(|var| var.name)
        .collect();
    assert_eq!(
        required,
        ["DATABASE_URL", "REDIS_URL", "JWT_SECRET", "ENCRYPTION_KEY"]
    );
}
