// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Skill runner client — the proxy's interface to the isolated
//! `brutor-skill-runner` container.
//!
//! Replaces the in-process `skill_executor::execute()` call at
//! `system_server.rs:911`. The proxy holds the source of truth in
//! Postgres; this module:
//!   1. Bulk-loads the skill's scripts + assets + resources from DB
//!      (same query the legacy executor did)
//!   2. Mints a short-lived JWT bound to this execution_id
//!   3. POSTs the self-contained payload to the runner
//!   4. Maps the runner's response back to the legacy
//!      `SkillExecutionOutput` shape so the call site doesn't notice
//!      the indirection
//!
//! The legacy executor stays in the tree as a fallback for
//! integration tests that don't run a runner container — see
//! [`execute_with_fallback`] for the gate.

use std::collections::HashMap;
use std::time::Duration;

use jsonwebtoken::{EncodingKey, Header, encode};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::PgPool;

use crate::env_registry::{Env, EnvError};
use crate::env_vars;
use crate::models::db::{AgentSkill, AgentSkillAsset, AgentSkillResource, AgentSkillScript};
use crate::services::skill_types::{ScriptResult, SkillExecutionOutput};

// =============================================================================
// Public API — drop-in for skill_executor::execute
// =============================================================================

/// Runtime config for the runner client. Taken at call time from the
/// environment the gateway read at start (`env_vars::process`), which
/// avoids threading `AppConfig` through eight callers: a map lookup, not a
/// read of the process environment.
struct RunnerConfig {
    url: String,
    jwt_secret: String,
    /// The runner's wall-clock limit of a SANDBOX script
    /// (`BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS`, read by both).
    sandbox_timeout_secs: u64,
    /// The runner's wall-clock limit of a TRUSTED script
    /// (`BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS`, read by both).
    trusted_timeout_secs: u64,
}

/// Lifetime, in seconds, of an execution token. One is minted per call and
/// checked by the runner when the call arrives.
const EXECUTION_TOKEN_TTL_SECS: u64 = 300;

/// What the gateway waits beyond the runner's own limit: the request, the
/// workspace set-up and the answer.
pub const RUNNER_TIMEOUT_MARGIN_SECS: u64 = 15;

impl std::fmt::Debug for RunnerConfig {
    /// Without the secret.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RunnerConfig")
            .field("url", &self.url)
            .field("sandbox_timeout_secs", &self.sandbox_timeout_secs)
            .field("trusted_timeout_secs", &self.trusted_timeout_secs)
            .finish_non_exhaustive()
    }
}

impl RunnerConfig {
    /// The runner settings `env` configures. A timeout of 0 is refused: the
    /// gateway does not start with it (`config::check`).
    fn from_env(env: &Env) -> Result<Self, EnvError> {
        Ok(Self {
            url: env.required_string(&env_vars::BRUTOR_SKILL_RUNNER_URL)?,
            // Unset is not refused here: the gateway starts, and an
            // execution fails with a message that names the variable.
            jwt_secret: env
                .string(&env_vars::BRUTOR_SKILL_RUNNER_JWT_SECRET)
                .unwrap_or_default(),
            sandbox_timeout_secs: env_vars::positive(
                env,
                &env_vars::BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS,
            )?,
            trusted_timeout_secs: env_vars::positive(
                env,
                &env_vars::BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS,
            )?,
        })
    }

    /// How long the gateway waits for a runner call that runs scripts of
    /// `modes` (each once, in turn): the runner's limit for each, plus
    /// [`RUNNER_TIMEOUT_MARGIN_SECS`]. A mode other than SANDBOX gets the
    /// TRUSTED limit, as the runner gives it.
    fn call_timeout<'a>(&self, modes: impl IntoIterator<Item = &'a str>) -> Duration {
        let scripts: u64 = modes
            .into_iter()
            .map(|mode| {
                if mode.eq_ignore_ascii_case("sandbox") {
                    self.sandbox_timeout_secs
                } else {
                    self.trusted_timeout_secs
                }
            })
            .fold(0u64, u64::saturating_add);
        Duration::from_secs(scripts.saturating_add(RUNNER_TIMEOUT_MARGIN_SECS))
    }

    /// The settings of this process. The start refused what cannot be read
    /// (`config::check`); a configuration without a secret refuses every
    /// call, which is what stands in for one that cannot be read at all.
    fn of_process() -> Self {
        Self::from_env(&env_vars::process()).unwrap_or_else(|_| Self {
            url: String::new(),
            jwt_secret: String::new(),
            sandbox_timeout_secs: 0,
            trusted_timeout_secs: 0,
        })
    }
}

/// What is wrong with the runner settings of `env`: see
/// [`RunnerConfig::from_env`].
pub fn check(env: &Env) -> Result<(), EnvError> {
    RunnerConfig::from_env(env).map(drop)
}

/// Execute via the remote runner. Mirrors the legacy
/// `skill_executor::execute` signature so callers swap one line.
///
/// Returns the same `SkillExecutionOutput` shape — the call site
/// remains agnostic about where the subprocess actually ran.
/// The shared HTTP client for every runner call.
///
/// `execute` and `run_script` each built a fresh
/// `reqwest::Client` per invocation. A client owns a connection pool and a
/// TLS context, so building one per call throws away keep-alive and pays a
/// new TLS handshake for every skill execution, against a service that is
/// almost always the same host on the same network.
///
/// A `OnceLock` rather than `state.http_client` because neither entry point
/// takes `SharedState` — they take a pool — and threading state through both
/// and their callers to save an allocation would be a worse trade than one
/// process-wide client. It has no overall timeout of its own: each call sets
/// one from the runner's configured limits ([`RunnerConfig::call_timeout`]),
/// so a runner allowed to run a script for longer is waited for.
fn runner_client() -> &'static reqwest::Client {
    static CLIENT: std::sync::OnceLock<reqwest::Client> = std::sync::OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .build()
            // A client with default settings is strictly better than no skill
            // execution at all; the only way `build` fails here is a broken
            // TLS backend, which `Client::new` would hit too.
            .unwrap_or_else(|e| {
                tracing::error!(error = %e, "runner client build failed — falling back to defaults");
                reqwest::Client::new()
            })
    })
}

pub async fn execute(
    db: &PgPool,
    skill: &AgentSkill,
    input_params: Option<&Value>,
) -> SkillExecutionOutput {
    let config = RunnerConfig::of_process();
    match execute_inner(db, &config, skill, input_params).await {
        Ok(out) => out,
        Err(e) => {
            tracing::error!(
                skill_id = %skill.id,
                error = %e,
                "skill runner call failed; returning structured failure"
            );
            SkillExecutionOutput {
                success: false,
                output: String::new(),
                error: Some(format!("Skill runner unavailable: {e}")),
                script_results: HashMap::new(),
                loaded_assets: HashMap::new(),
                loaded_resources: HashMap::new(),
                unresolved_refs: Vec::new(),
            }
        }
    }
}

/// Result of a `run_script` — the model-facing output only. The
/// script source never leaves the runner.
#[derive(Debug, Clone)]
pub struct RunScriptResult {
    pub success: bool,
    pub output: String,
    pub stderr: String,
    pub exit_code: i32,
    pub error: Option<String>,
}

/// Run ONE named script (chosen by the model from the L2
/// manifest) in the caller's session workspace, seeding the skill's
/// assets/resources alongside it. `session_id` is the MCP correlation id, so a
/// workflow's script runs share a workspace. Enforces the manifest allowlist
/// (the script must exist in this skill and not be DISABLED).
pub async fn run_script(
    db: &PgPool,
    skill: &AgentSkill,
    script_filename: &str,
    args: Option<&Value>,
    session_id: &str,
) -> Result<RunScriptResult, String> {
    let config = RunnerConfig::of_process();
    if config.jwt_secret.is_empty() {
        return Err(
            "BRUTOR_SKILL_RUNNER_JWT_SECRET is unset — refusing to call runner without auth"
                .to_string(),
        );
    }

    // Allowlist: the named script must belong to this skill and be runnable.
    // Callers refuse earlier (`runnable_script`); this re-check keeps the
    // runner call safe on its own.
    let script = runnable_script(db, skill, script_filename)
        .await?
        .map_err(|refusal| refusal.message)?;

    // Seed the skill's assets/resources so the script can read referenced files.
    let assets = load_assets(db, &skill.id).await?;
    let resources = load_resources(db, &skill.id).await?;

    let execution_id = ulid::Ulid::new().to_string();
    let timeout = config.call_timeout([script.execution_mode.as_str()]);
    let req = RunScriptRequest {
        skill_id: skill.id.clone(),
        skill_name: skill.name.clone(),
        skill_version: skill.version.clone(),
        execution_id: execution_id.clone(),
        tenant_id: skill.tenant_id.clone(),
        session_id: session_id.to_string(),
        script: ScriptInput {
            filename: script.filename,
            language: script.language,
            execution_mode: script.execution_mode,
            script_content: script.script_content,
        },
        args: args.cloned(),
        assets: assets
            .into_iter()
            .map(|a| AssetInput {
                filename: a.filename,
                content: a.content,
            })
            .collect(),
        resources: resources
            .into_iter()
            .map(|r| ResourceInput {
                filename: r.filename,
                content: r.content,
            })
            .collect(),
    };

    let token = mint_token(
        &config.jwt_secret,
        &execution_id,
        &skill.tenant_id,
        &skill.id,
        EXECUTION_TOKEN_TTL_SECS,
    )?;

    // Refused here, naming the limit, rather than with the runner's 413.
    let body = super::skill_limits::runner_request_body(&req)?;
    let client = runner_client();
    let url = format!("{}/v1/runner/run_script", config.url.trim_end_matches('/'));
    let resp = client
        .post(&url)
        .timeout(timeout)
        .bearer_auth(&token)
        .header(reqwest::header::CONTENT_TYPE, "application/json")
        .body(body)
        .send()
        .await
        .map_err(|e| format!("runner request: {e}"))?;

    let status = resp.status();
    let body_bytes = resp
        .bytes()
        .await
        .map_err(|e| format!("read runner response: {e}"))?;
    if !status.is_success() {
        let body = String::from_utf8_lossy(&body_bytes);
        return Err(format!(
            "runner returned HTTP {} — body: {}",
            status,
            body.chars().take(500).collect::<String>()
        ));
    }

    let rr: RunScriptResponse =
        serde_json::from_slice(&body_bytes).map_err(|e| format!("decode runner response: {e}"))?;
    Ok(RunScriptResult {
        success: rr.success,
        output: rr.output,
        stderr: rr.stderr,
        exit_code: rr.exit_code,
        error: rr.error,
    })
}

async fn execute_inner(
    db: &PgPool,
    config: &RunnerConfig,
    skill: &AgentSkill,
    input_params: Option<&Value>,
) -> Result<SkillExecutionOutput, String> {
    if config.jwt_secret.is_empty() {
        return Err(
            "BRUTOR_SKILL_RUNNER_JWT_SECRET is unset — refusing to call runner without auth"
                .to_string(),
        );
    }

    // 1. Load scripts/assets/resources from DB. Same bulk SELECTs the
    //    legacy executor's resolve_references did — kept here so the
    //    runner stays stateless (no DB credentials shipped to it).
    let scripts = load_scripts(db, &skill.id).await?;
    let assets = load_assets(db, &skill.id).await?;
    let resources = load_resources(db, &skill.id).await?;

    // 2. Build the wire request.
    let execution_id = ulid::Ulid::new().to_string();
    let timeout = config.call_timeout(scripts.iter().map(|s| s.execution_mode.as_str()));
    let req = ExecuteRequest {
        skill_id: skill.id.clone(),
        skill_name: skill.name.clone(),
        skill_version: skill.version.clone(),
        skill_description: skill.description.clone(),
        execution_id: execution_id.clone(),
        tenant_id: skill.tenant_id.clone(),
        skill_md_content: skill.skill_md_content.clone().unwrap_or_default(),
        input_params: input_params.cloned(),
        scripts: scripts
            .into_iter()
            .map(|s| ScriptInput {
                filename: s.filename,
                language: s.language,
                execution_mode: s.execution_mode,
                script_content: s.script_content,
            })
            .collect(),
        assets: assets
            .into_iter()
            .map(|a| AssetInput {
                filename: a.filename,
                content: a.content,
            })
            .collect(),
        resources: resources
            .into_iter()
            .map(|r| ResourceInput {
                filename: r.filename,
                content: r.content,
            })
            .collect(),
    };

    // 3. Mint JWT.
    let token = mint_token(
        &config.jwt_secret,
        &execution_id,
        &skill.tenant_id,
        &skill.id,
        EXECUTION_TOKEN_TTL_SECS,
    )?;

    // 4. POST, waiting as long as the runner may take for every script it
    //    was given (each once) plus the margin.
    //    A request larger than the runner accepts is refused here, naming
    //    the limit, rather than with the runner's 413.
    let body = super::skill_limits::runner_request_body(&req)?;
    let client = runner_client();

    let url = format!("{}/v1/runner/execute", config.url.trim_end_matches('/'));
    let resp = client
        .post(&url)
        .timeout(timeout)
        .bearer_auth(&token)
        .header(reqwest::header::CONTENT_TYPE, "application/json")
        .body(body)
        .send()
        .await
        .map_err(|e| format!("runner request: {e}"))?;

    let status = resp.status();
    let body_bytes = resp
        .bytes()
        .await
        .map_err(|e| format!("read runner response: {e}"))?;

    if !status.is_success() {
        let body = String::from_utf8_lossy(&body_bytes);
        return Err(format!(
            "runner returned HTTP {} — body: {}",
            status,
            // Cap the body to keep our logs sane.
            body.chars().take(500).collect::<String>()
        ));
    }

    let runner_resp: ExecuteResponse =
        serde_json::from_slice(&body_bytes).map_err(|e| format!("decode runner response: {e}"))?;

    Ok(to_legacy_output(runner_resp))
}

// =============================================================================
// DB loaders — identical SQL to the legacy resolve_references
// =============================================================================

/// Why a named script may not run in a skill.
#[derive(Debug)]
pub struct ScriptRefusal {
    pub message: String,
}

/// The manifest allowlist: the named script must belong to this skill and
/// not be DISABLED. `Ok(Err(_))` is a refusal of the caller's request (4xx);
/// `Err(_)` is a database failure, which the caller must treat as one
/// (fail closed). Callers check this before any gate admits the call, so a
/// script the skill does not allow never charges the group or records an
/// execution.
pub async fn runnable_script(
    db: &PgPool,
    skill: &AgentSkill,
    script_filename: &str,
) -> Result<Result<AgentSkillScript, ScriptRefusal>, String> {
    let scripts = load_scripts(db, &skill.id).await?;
    let Some(script) = scripts.into_iter().find(|s| s.filename == script_filename) else {
        return Ok(Err(ScriptRefusal {
            message: format!(
                "script '{script_filename}' is not part of skill '{}'",
                skill.name
            ),
        }));
    };
    if script.execution_mode.eq_ignore_ascii_case("disabled") {
        return Ok(Err(ScriptRefusal {
            message: format!("script '{script_filename}' is disabled"),
        }));
    }
    Ok(Ok(script))
}

async fn load_scripts(db: &PgPool, skill_id: &str) -> Result<Vec<AgentSkillScript>, String> {
    sqlx::query_as(
        r#"SELECT id, tenant_id, skill_id, filename, description, script_content,
                  script_hash, language::text AS language,
                  execution_mode::text AS execution_mode,
                  approved_by, approved_at, created_at, updated_at
           FROM agent_skill_scripts WHERE skill_id = $1"#,
    )
    .bind(skill_id)
    .fetch_all(db)
    .await
    .map_err(|e| format!("load scripts: {e}"))
}

async fn load_assets(db: &PgPool, skill_id: &str) -> Result<Vec<AgentSkillAsset>, String> {
    sqlx::query_as(
        r#"SELECT id, tenant_id, skill_id, asset_type::text AS asset_type, filename,
                  description, content, content_hash, mime_type, size_bytes,
                  encoding, created_at, updated_at
           FROM agent_skill_assets WHERE skill_id = $1"#,
    )
    .bind(skill_id)
    .fetch_all(db)
    .await
    .map_err(|e| format!("load assets: {e}"))
}

async fn load_resources(db: &PgPool, skill_id: &str) -> Result<Vec<AgentSkillResource>, String> {
    sqlx::query_as(
        r#"SELECT id, tenant_id, skill_id, resource_type::text AS resource_type,
                  filename, content, content_hash, mime_type, size_bytes,
                  created_at, updated_at
           FROM agent_skill_resources WHERE skill_id = $1"#,
    )
    .bind(skill_id)
    .fetch_all(db)
    .await
    .map_err(|e| format!("load resources: {e}"))
}

// =============================================================================
// JWT minting
// =============================================================================

#[derive(Debug, Serialize)]
struct Claims<'a> {
    exec: &'a str,
    tnt: &'a str,
    skl: &'a str,
    exp: u64,
}

fn mint_token(
    secret: &str,
    execution_id: &str,
    tenant_id: &str,
    skill_id: &str,
    ttl_secs: u64,
) -> Result<String, String> {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| format!("clock: {e}"))?
        .as_secs();
    let exp = now + ttl_secs;
    let claims = Claims {
        exec: execution_id,
        tnt: tenant_id,
        skl: skill_id,
        exp,
    };
    encode(
        &Header::default(),
        &claims,
        &EncodingKey::from_secret(secret.as_bytes()),
    )
    .map_err(|e| format!("jwt sign: {e}"))
}

// =============================================================================
// Wire types — mirror brutor-skill-runner::types exactly
// =============================================================================

#[derive(Debug, Serialize)]
struct ExecuteRequest {
    skill_id: String,
    skill_name: String,
    skill_version: String,
    skill_description: String,
    execution_id: String,
    tenant_id: String,
    skill_md_content: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    input_params: Option<Value>,
    scripts: Vec<ScriptInput>,
    assets: Vec<AssetInput>,
    resources: Vec<ResourceInput>,
}

#[derive(Debug, Serialize)]
struct ScriptInput {
    filename: String,
    language: String,
    execution_mode: String,
    script_content: String,
}

#[derive(Debug, Serialize)]
struct AssetInput {
    filename: String,
    content: String,
}

#[derive(Debug, Serialize)]
struct ResourceInput {
    filename: String,
    content: String,
}

// Mirror brutor-skill-runner::types::{RunScriptRequest,RunScriptResponse}.
#[derive(Debug, Serialize)]
struct RunScriptRequest {
    skill_id: String,
    skill_name: String,
    skill_version: String,
    execution_id: String,
    tenant_id: String,
    session_id: String,
    script: ScriptInput,
    #[serde(skip_serializing_if = "Option::is_none")]
    args: Option<Value>,
    assets: Vec<AssetInput>,
    resources: Vec<ResourceInput>,
}

#[derive(Debug, Deserialize)]
struct RunScriptResponse {
    success: bool,
    output: String,
    stderr: String,
    exit_code: i32,
    #[allow(dead_code)]
    duration_ms: f64,
    error: Option<String>,
    #[allow(dead_code)]
    #[serde(default)]
    workspace_files: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct ExecuteResponse {
    success: bool,
    output: String,
    error: Option<String>,
    script_results: HashMap<String, ScriptResultOut>,
    loaded_assets: HashMap<String, String>,
    loaded_resources: HashMap<String, String>,
    unresolved_refs: Vec<String>,
    #[allow(dead_code)]
    duration_ms: f64,
}

#[derive(Debug, Deserialize)]
struct ScriptResultOut {
    success: bool,
    stdout: String,
    stderr: String,
    exit_code: i32,
    duration_ms: f64,
    error: Option<String>,
    language: String,
    execution_mode: String,
}

// =============================================================================
// Response mapping — runner → legacy SkillExecutionOutput
// =============================================================================

fn to_legacy_output(resp: ExecuteResponse) -> SkillExecutionOutput {
    let script_results: HashMap<String, ScriptResult> = resp
        .script_results
        .into_iter()
        .map(|(filename, sr)| {
            (
                filename,
                ScriptResult {
                    success: sr.success,
                    stdout: sr.stdout,
                    stderr: sr.stderr,
                    exit_code: sr.exit_code,
                    duration_ms: sr.duration_ms,
                    error: sr.error,
                    language: if sr.language.is_empty() {
                        None
                    } else {
                        Some(sr.language)
                    },
                    execution_mode: if sr.execution_mode.is_empty() {
                        None
                    } else {
                        Some(sr.execution_mode)
                    },
                },
            )
        })
        .collect();

    SkillExecutionOutput {
        success: resp.success,
        output: resp.output,
        error: resp.error,
        script_results,
        loaded_assets: resp.loaded_assets,
        loaded_resources: resp.loaded_resources,
        unresolved_refs: resp.unresolved_refs,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode};
    use serde::Deserialize;

    const SECRET: &str = "unit-test-secret-32-chars-long-zzz";

    #[derive(Debug, Deserialize)]
    struct VerifiedClaims {
        exec: String,
        tnt: String,
        skl: String,
        exp: u64,
    }

    fn decode_token(token: &str) -> VerifiedClaims {
        let mut v = Validation::new(Algorithm::HS256);
        v.leeway = 60;
        v.set_required_spec_claims(&["exp"]);
        decode::<VerifiedClaims>(token, &DecodingKey::from_secret(SECRET.as_bytes()), &v)
            .expect("token should decode and verify under the test secret")
            .claims
    }

    #[test]
    fn mint_token_round_trips_under_shared_secret() {
        let tok = mint_token(SECRET, "exec-abc", "tenant-42", "skill-x", 300)
            .expect("mint_token should not fail under valid inputs");
        let claims = decode_token(&tok);
        assert_eq!(claims.exec, "exec-abc");
        assert_eq!(claims.tnt, "tenant-42");
        assert_eq!(claims.skl, "skill-x");
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        assert!(
            claims.exp >= now + 290 && claims.exp <= now + 310,
            "exp should be roughly now + 300; got exp={} now={}",
            claims.exp,
            now
        );
    }

    #[test]
    fn mint_token_signature_is_secret_specific() {
        let tok = mint_token(SECRET, "exec-x", "t", "s", 60).unwrap();
        // Verify the token DOES NOT validate under a different secret —
        // confirms HS256 with the right key, not unsigned alg.
        let mut v = Validation::new(Algorithm::HS256);
        v.leeway = 60;
        let result = decode::<VerifiedClaims>(
            &tok,
            &DecodingKey::from_secret(b"different-secret-not-the-right-one"),
            &v,
        );
        assert!(
            result.is_err(),
            "token should not verify under a wrong secret"
        );
    }

    #[test]
    fn to_legacy_output_maps_script_result_fields() {
        let mut script_results = HashMap::new();
        script_results.insert(
            "echo.py".to_string(),
            ScriptResultOut {
                success: true,
                stdout: "hi\n".into(),
                stderr: String::new(),
                exit_code: 0,
                duration_ms: 12.5,
                error: None,
                language: "PYTHON".into(),
                execution_mode: "SANDBOX".into(),
            },
        );
        let resp = ExecuteResponse {
            success: true,
            output: "hi".into(),
            error: None,
            script_results,
            loaded_assets: HashMap::new(),
            loaded_resources: HashMap::new(),
            unresolved_refs: vec![],
            duration_ms: 18.0,
        };
        let legacy = to_legacy_output(resp);
        assert!(legacy.success);
        assert_eq!(legacy.output, "hi");
        let sr = legacy.script_results.get("echo.py").unwrap();
        assert!(sr.success);
        assert_eq!(sr.exit_code, 0);
        assert_eq!(sr.duration_ms, 12.5);
        assert_eq!(sr.language.as_deref(), Some("PYTHON"));
        assert_eq!(sr.execution_mode.as_deref(), Some("SANDBOX"));
    }

    #[test]
    fn to_legacy_output_treats_empty_language_as_none() {
        let mut script_results = HashMap::new();
        script_results.insert(
            "x.sh".to_string(),
            ScriptResultOut {
                success: false,
                stdout: String::new(),
                stderr: "boom".into(),
                exit_code: 1,
                duration_ms: 0.0,
                error: Some("nope".into()),
                language: String::new(),
                execution_mode: String::new(),
            },
        );
        let resp = ExecuteResponse {
            success: false,
            output: String::new(),
            error: Some("script failure".into()),
            script_results,
            loaded_assets: HashMap::new(),
            loaded_resources: HashMap::new(),
            unresolved_refs: vec![],
            duration_ms: 0.0,
        };
        let legacy = to_legacy_output(resp);
        let sr = legacy.script_results.get("x.sh").unwrap();
        // Empty strings on the wire become None on the legacy struct,
        // matching how the in-proxy executor reports unknown values.
        assert!(sr.language.is_none(), "empty language → None");
        assert!(sr.execution_mode.is_none(), "empty execution_mode → None");
        assert_eq!(sr.error.as_deref(), Some("nope"));
    }

    #[test]
    fn runner_config_defaults_when_env_unset() {
        let env = Env::from_pairs(&env_vars::REGISTRY, [] as [(&str, &str); 0]).unwrap();
        let cfg = RunnerConfig::from_env(&env).unwrap();
        assert_eq!(cfg.url, "http://skill-runner:8210");
        assert_eq!(cfg.jwt_secret, "");
        // The runner's own defaults.
        assert_eq!(
            (cfg.sandbox_timeout_secs, cfg.trusted_timeout_secs),
            (30, 120)
        );
    }

    /// The gateway waits as long as the runner may run, plus a margin, instead of a fixed 180 s that cut off any trusted run the
    /// runner allowed more than about 175 s.
    #[test]
    fn the_call_timeout_follows_the_runners_configured_limits() {
        let env = Env::from_pairs(
            &env_vars::REGISTRY,
            [
                ("BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS", "45"),
                ("BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS", "300"),
            ],
        )
        .unwrap();
        let cfg = RunnerConfig::from_env(&env).unwrap();
        let margin = RUNNER_TIMEOUT_MARGIN_SECS;
        assert!(margin > 0 && margin <= 30, "a small margin");
        let secs = |modes: &[&str]| cfg.call_timeout(modes.iter().copied()).as_secs();
        assert_eq!(secs(&["TRUSTED"]), 300 + margin);
        assert!(secs(&["TRUSTED"]) > 180, "longer than the old fixed 180 s");
        assert_eq!(secs(&["SANDBOX"]), 45 + margin);
        assert_eq!(secs(&["sandbox", "TRUSTED"]), 345 + margin);
        // Anything the runner does not run as SANDBOX gets the longer limit.
        assert_eq!(secs(&["UNKNOWN"]), 300 + margin);
        assert_eq!(secs(&[]), margin);

        let defaults = Env::from_pairs(&env_vars::REGISTRY, [] as [(&str, &str); 0]).unwrap();
        let cfg = RunnerConfig::from_env(&defaults).unwrap();
        assert_eq!(
            cfg.call_timeout(["TRUSTED"]).as_secs(),
            120 + RUNNER_TIMEOUT_MARGIN_SECS
        );
    }

    #[test]
    fn the_runner_is_configured_by_the_new_names_and_its_secret_is_not_printed() {
        let env = Env::from_pairs(
            &env_vars::REGISTRY,
            [
                ("BRUTOR_SKILL_RUNNER_URL", "http://runner.test:9"),
                (
                    "BRUTOR_SKILL_RUNNER_JWT_SECRET",
                    "value-that-must-never-be-printed",
                ),
            ],
        )
        .unwrap();
        let cfg = RunnerConfig::from_env(&env).unwrap();
        assert_eq!(cfg.url, "http://runner.test:9");
        assert_eq!(cfg.jwt_secret, "value-that-must-never-be-printed");
        assert!(!format!("{cfg:?}").contains("never-be-printed"));

        for name in [
            "BRUTOR_SKILL_RUNNER_SANDBOX_TIMEOUT_SECS",
            "BRUTOR_SKILL_RUNNER_TRUSTED_TIMEOUT_SECS",
        ] {
            let env = Env::from_pairs(&env_vars::REGISTRY, [(name, "0")]).unwrap();
            let refusal = check(&env).unwrap_err().to_string();
            assert!(
                refusal.contains(&format!("{name} is not valid")),
                "{refusal}"
            );
        }
    }
}
