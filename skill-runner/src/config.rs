// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Configuration, read from the environment through the registry
//! (`src/env_vars.rs`).
//!
//! Deliberately small surface. The runner has **no** Postgres,
//! Redis, or upstream-API config — if a future change starts adding
//! those, it's a strong signal something is creeping back across
//! the trust boundary that exists for a reason.

use std::net::SocketAddr;
use std::path::PathBuf;

use crate::env_registry::Env;
use crate::env_vars;
use crate::executor::TrustedEnv;

/// Clock-skew tolerance, in seconds, on an execution token's expiry. The Core
/// Gateway mints five-minute tokens, so 30 s of skew needs no tuning.
pub const JWT_LEEWAY_SECS: u64 = 30;

/// The largest skill, in bytes of its files uncompressed: the Control Plane's
/// `SKILL_MAX_BYTES` (`app/constants/agent_skill.py`), which it checks at
/// upload, as Anthropic's Agent Skills do (30 MB per skill).
pub const SKILL_MAX_BYTES: usize = 30_000_000;

/// What a request carries besides the files: the JSON around them (field
/// names, file names, the script's arguments, the quoting of text files).
pub const REQUEST_FRAMING_BYTES: usize = 1_048_576;

/// Largest request body the runner accepts, in bytes; a larger one gets 413.
///
/// Derived from the largest skill: the Core Gateway sends a skill's SKILL.md,
/// scripts, assets and resources in one JSON request, a binary file
/// base64-encoded (4 bytes for every 3, rounded up), so the largest skill
/// arrives as at most `SKILL_MAX_BYTES` × 4/3 plus the framing:
/// 30,000,000 × 4/3 = 40,000,000, + 1,048,576 = 41,048,576 bytes (about
/// 41 MB). The Core Gateway (`src/services/skill_limits.rs`) writes the same
/// three definitions and refuses to send a larger request;
/// `tests/request_limit.rs` holds the two together.
pub const MAX_REQUEST_BYTES: usize = SKILL_MAX_BYTES.div_ceil(3) * 4 + REQUEST_FRAMING_BYTES;

/// Age, in seconds, after which a session workspace is removed.
pub const WORKSPACE_TTL_SECS: u64 = 3600;

#[derive(Debug, Clone)]
pub struct Config {
    /// The address the axum server binds. Default `0.0.0.0:8210`
    /// — picked to NOT collide with the kb-connector-sync service
    /// on `:8200` in the trial bundle.
    pub listen_addr: SocketAddr,

    /// HS256 secret used to verify JWTs minted by the proxy. MUST
    /// match `BRUTOR_SKILL_RUNNER_JWT_SECRET` on the Core Gateway side.
    /// Loaded at startup; the runner refuses to start if absent.
    pub jwt_secret: String,

    /// Optional override of the sandbox subprocess timeout. Default
    /// is 30s. Set higher for noisy demos.
    pub sandbox_timeout_secs: u64,

    /// Optional override of the trusted subprocess timeout. Default
    /// 120s.
    pub trusted_timeout_secs: u64,

    /// Root directory under which per-session skill workspaces
    /// live (`<root>/<tenant>/<session>`). L3 `skills__run_script` runs in a
    /// session-scoped workspace so a workflow's script runs can pass files to
    /// each other. Default `<temp_dir>/brutor-skill-workspaces`.
    pub workspace_root: PathBuf,

    /// What a TRUSTED script is handed from the runner's environment: the
    /// variables named in `BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST` (by
    /// default the OTLP endpoint, the OTel service name, `LC_ALL` and `TZ`)
    /// that are set.
    pub trusted_env: TrustedEnv,
}

impl Config {
    /// Build from the environment. Returns Err for missing /
    /// malformed values — the binary fails to start, which is the
    /// behaviour we want (silent fallback to defaults on secrets
    /// would be a security smell).
    pub fn from_env(env: &Env) -> anyhow::Result<Self> {
        let listen_addr: SocketAddr = env.number(&env_vars::BRUTOR_SKILL_RUNNER_LISTEN_ADDR)?;

        let jwt_secret = env.required_string(&env_vars::BRUTOR_SKILL_RUNNER_JWT_SECRET)?;
        if jwt_secret.len() < 32 {
            anyhow::bail!(
                "{} must be at least 32 characters; generate with `openssl rand -hex 32`",
                env_vars::BRUTOR_SKILL_RUNNER_JWT_SECRET.name
            );
        }

        let workspace_root = env
            .path(&env_vars::BRUTOR_SKILL_RUNNER_WORKSPACE_ROOT)
            .unwrap_or_else(|| {
                env.temp_dir(&env_vars::TMPDIR)
                    .join("brutor-skill-workspaces")
            });

        let trusted_env =
            env.forwarded_by_name(&env.list(&env_vars::BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST));

        Ok(Self {
            listen_addr,
            jwt_secret,
            sandbox_timeout_secs: env
                .number(&env_vars::BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS)?,
            trusted_timeout_secs: env
                .number(&env_vars::BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS)?,
            workspace_root,
            trusted_env: TrustedEnv::new(trusted_env),
        })
    }
}
