// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The Skill Runner's environment variables: every one it reads, defined
//! once. `.env.example`, the configuration table in `README.md` and
//! `--print-env` are generated from [`REGISTRY`]; `src/config.rs` reads
//! through it. Adding a variable here without a reader, or reading the
//! environment anywhere else, fails `tests/env_registry.rs`.

use crate::env_registry::{Edition, EnvVar, Kind, Registry};

pub static BRUTOR_SKILL_RUNNER_JWT_SECRET: EnvVar = EnvVar {
    name: "BRUTOR_SKILL_RUNNER_JWT_SECRET",
    kind: Kind::String,
    default: None,
    required: true,
    secret: true,
    edition: Edition::Both,
    description: "The secret the Core Gateway signs execution tokens with (HS256). The runner \
                  only verifies tokens. Must equal the Core Gateway's \
                  BRUTOR_SKILL_RUNNER_JWT_SECRET.",
    example: "<output of: openssl rand -hex 32>",
    accepted: "at least 32 characters",
};

pub static BRUTOR_SKILL_RUNNER_LISTEN_ADDR: EnvVar = EnvVar {
    name: "BRUTOR_SKILL_RUNNER_LISTEN_ADDR",
    kind: Kind::SocketAddr,
    default: Some("0.0.0.0:8210"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Address the HTTP server binds. The Core Gateway's BRUTOR_SKILL_RUNNER_URL \
                  must point at it.",
    example: "127.0.0.1:8210",
    accepted: "an IP address and a port (ip:port)",
};

pub static BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS: EnvVar = EnvVar {
    name: "BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS",
    kind: Kind::Integer,
    default: Some("30"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Advanced. Wall-clock limit, in seconds, of a script in SANDBOX mode.",
    example: "60",
    accepted: "a whole number of seconds",
};

pub static BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS: EnvVar = EnvVar {
    name: "BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS",
    kind: Kind::Integer,
    default: Some("120"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Advanced. Wall-clock limit, in seconds, of a script in TRUSTED mode.",
    example: "300",
    accepted: "a whole number of seconds",
};

pub static BRUTOR_SKILL_RUNNER_WORKSPACE_ROOT: EnvVar = EnvVar {
    name: "BRUTOR_SKILL_RUNNER_WORKSPACE_ROOT",
    kind: Kind::Path,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Advanced. Directory under which session workspaces live (<root>/<tenant>/<session>). \
                  Unset: brutor-skill-workspaces under the temporary directory (TMPDIR).",
    example: "/var/lib/brutor/skill-workspaces",
    accepted: "a directory the runner can write to",
};

pub static BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST: EnvVar = EnvVar {
    name: "BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST",
    kind: Kind::List,
    default: Some("OTEL_EXPORTER_OTLP_ENDPOINT,OTEL_SERVICE_NAME,LC_ALL,TZ"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Names of the runner's environment variables a TRUSTED script is handed, when \
                  set. Setting it replaces the default list. Proxy variables (HTTP_PROXY, \
                  HTTPS_PROXY, NO_PROXY and the lower-case forms) and OTEL_EXPORTER_OTLP_HEADERS \
                  may carry credentials and are handed on only when named here. The runner's own \
                  variables (this table) cannot be added. SANDBOX scripts see none.",
    example: "OTEL_EXPORTER_OTLP_ENDPOINT,OTEL_SERVICE_NAME,LC_ALL,TZ,HTTPS_PROXY,NO_PROXY",
    accepted: "comma-separated variable names",
};

pub static BRUTOR_LOG_JSON_ENABLED: EnvVar = EnvVar {
    name: "BRUTOR_LOG_JSON_ENABLED",
    kind: Kind::Boolean,
    default: Some("false"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Write each log line as one JSON object (time, level, target, span, fields), \
                  for a log pipeline. false: human-readable text lines with the same fields.",
    example: "true",
    accepted: "true or false (also 1/0, yes/no, on/off)",
};

pub static RUST_LOG: EnvVar = EnvVar {
    name: "RUST_LOG",
    kind: Kind::String,
    default: Some("brutor_skill_runner=info,tower_http=info"),
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "Log filter. A filter that does not parse is replaced by the default.",
    example: "brutor_skill_runner=debug,tower_http=info",
    accepted: "tracing filter directives (target=level, comma-separated)",
};

pub static TMPDIR: EnvVar = EnvVar {
    name: "TMPDIR",
    kind: Kind::Path,
    default: None,
    required: false,
    secret: false,
    edition: Edition::Both,
    description: "The platform's temporary directory. Only the default of \
                  BRUTOR_SKILL_RUNNER_WORKSPACE_ROOT uses it. Unset: the platform default \
                  (/tmp).",
    example: "/tmp",
    accepted: "a directory",
};

pub static REGISTRY: Registry = Registry {
    service: "skill-runner",
    title: "Brutor Agent Control, Skill Runner (both editions)",
    vars: &[
        &BRUTOR_SKILL_RUNNER_JWT_SECRET,
        &BRUTOR_SKILL_RUNNER_LISTEN_ADDR,
        &BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS,
        &BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS,
        &BRUTOR_SKILL_RUNNER_WORKSPACE_ROOT,
        &BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST,
        &BRUTOR_LOG_JSON_ENABLED,
        &RUST_LOG,
        &TMPDIR,
    ],
};
