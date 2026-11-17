// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! `Config::from_env`: every setting comes from the variable the registry
//! names, each one changes the configuration, and a malformed value is
//! refused rather than ignored.

use brutor_skill_runner::config::Config;
use brutor_skill_runner::env_registry::{Env, EnvError};
use brutor_skill_runner::env_vars::REGISTRY;

const SECRET: &str = "0123456789abcdef0123456789abcdef";

fn env(pairs: &[(&str, &str)]) -> Result<Env, EnvError> {
    let mut all = vec![("BRUTOR_SKILL_RUNNER_JWT_SECRET", SECRET)];
    all.extend_from_slice(pairs);
    Env::from_pairs(&REGISTRY, all)
}

fn config(pairs: &[(&str, &str)]) -> Config {
    Config::from_env(&env(pairs).unwrap()).unwrap()
}

#[test]
fn defaults_apply_when_only_the_secret_is_set() {
    let c = config(&[]);
    assert_eq!(c.jwt_secret, SECRET);
    assert_eq!(c.listen_addr.to_string(), "0.0.0.0:8210");
    assert_eq!(c.sandbox_timeout_secs, 30);
    assert_eq!(c.trusted_timeout_secs, 120);
    assert!(c.workspace_root.ends_with("brutor-skill-workspaces"));
    // Only the default list (OTLP endpoint, OTel service name, LC_ALL, TZ)
    // can be handed on: never a proxy variable or the OTLP headers.
    for (name, _) in c.trusted_env.pairs() {
        assert!(
            [
                "OTEL_EXPORTER_OTLP_ENDPOINT",
                "OTEL_SERVICE_NAME",
                "LC_ALL",
                "TZ"
            ]
            .contains(&name.as_str()),
            "{name}"
        );
    }
}

#[test]
fn each_variable_changes_the_configuration() {
    let c = config(&[
        ("BRUTOR_SKILL_RUNNER_LISTEN_ADDR", "127.0.0.1:9"),
        ("BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS", "3"),
        ("BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS", "5"),
        ("BRUTOR_SKILL_RUNNER_WORKSPACE_ROOT", "/srv/workspaces"),
        (
            "BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST",
            "ENVREG_CONFIG_TEST_UNSET",
        ),
    ]);
    assert_eq!(c.listen_addr.to_string(), "127.0.0.1:9");
    assert_eq!(c.sandbox_timeout_secs, 3);
    assert_eq!(c.trusted_timeout_secs, 5);
    assert_eq!(c.workspace_root.to_str(), Some("/srv/workspaces"));
    // The list replaces the default; a name that is not set hands nothing.
    assert!(c.trusted_env.pairs().is_empty());
}

#[test]
fn the_temporary_directory_is_where_workspaces_go_by_default() {
    let c = config(&[("TMPDIR", "/scratch")]);
    assert_eq!(
        c.workspace_root.to_str(),
        Some("/scratch/brutor-skill-workspaces")
    );
    // The explicit root wins.
    let c = config(&[
        ("TMPDIR", "/scratch"),
        ("BRUTOR_SKILL_RUNNER_WORKSPACE_ROOT", "/srv/w"),
    ]);
    assert_eq!(c.workspace_root.to_str(), Some("/srv/w"));
}

#[test]
fn the_secret_is_required_and_must_be_long_and_is_never_in_the_message() {
    let none = Env::from_pairs(&REGISTRY, [("TMPDIR", "/tmp")]).unwrap();
    let err = Config::from_env(&none).unwrap_err().to_string();
    assert!(
        err.contains("BRUTOR_SKILL_RUNNER_JWT_SECRET is required"),
        "{err}"
    );

    let short = Env::from_pairs(
        &REGISTRY,
        [("BRUTOR_SKILL_RUNNER_JWT_SECRET", "short-secret-value")],
    )
    .unwrap();
    let err = Config::from_env(&short).unwrap_err().to_string();
    assert!(err.contains("at least 32 characters"), "{err}");
    assert!(!err.contains("short-secret-value"), "{err}");
}

#[test]
fn a_malformed_or_out_of_range_value_stops_the_runner() {
    for (name, bad) in [
        ("BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS", "soon"),
        ("BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS", "-1"),
        ("BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS", ""),
        ("BRUTOR_SKILL_RUNNER_LISTEN_ADDR", "8210"),
    ] {
        let err = env(&[(name, bad)]).unwrap_err().to_string();
        assert!(err.contains(name), "{bad:?}: {err}");
    }
    // A host name passes the registry's host:port check; the runner binds an
    // IP address and says so.
    let e = env(&[("BRUTOR_SKILL_RUNNER_LISTEN_ADDR", "localhost:8210")]).unwrap();
    let err = Config::from_env(&e).unwrap_err().to_string();
    assert!(err.contains("BRUTOR_SKILL_RUNNER_LISTEN_ADDR"), "{err}");
}
