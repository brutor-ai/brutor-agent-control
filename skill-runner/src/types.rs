// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Wire types for the runner's HTTP API.
//!
//! The proxy serialises everything it needs to execute the skill
//! into [`ExecuteRequest`] — no database lookup happens on the
//! runner side. This is the boundary that makes the runner stateless
//! and credential-free: the proxy holds the source of truth in
//! Postgres, the runner only sees the slice the proxy sends.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

// ============================================================================
// Request
// ============================================================================

/// One subprocess execution request from the proxy.
///
/// The proxy fetches the skill row + its scripts/assets/resources
/// from Postgres, validates RBAC + quota, and serialises everything
/// into this struct. The runner trusts only that the JWT (in the
/// `Authorization` header) was signed with the shared secret and
/// matches `execution_id`.
#[derive(Debug, Deserialize)]
pub struct ExecuteRequest {
    /// Skill identifier (informational — used for logs + metrics only).
    pub skill_id: String,
    /// Skill display name (for `skill_name` template variable).
    pub skill_name: String,
    /// Skill version (for `skill_version` template variable).
    pub skill_version: String,
    /// Skill description (for `skill_description` template variable).
    pub skill_description: String,
    /// Execution ID minted by the orchestrator. The runner asserts
    /// this matches the `exec` claim on the JWT — prevents one
    /// token from being replayed against a different execution.
    pub execution_id: String,
    /// Tenant ID (informational — logged for correlation).
    pub tenant_id: String,

    /// The SKILL.md body that drives the workflow (markdown with
    /// `[link](filename.ext)` references). Parsed by the runner to
    /// determine which scripts/assets/resources to consume in which
    /// order.
    pub skill_md_content: String,

    /// Optional user input. Forwarded into the execution context as
    /// `input_params` and into each script via the `SKILL_INPUT` env
    /// var + stdin payload.
    #[serde(default)]
    pub input_params: Option<serde_json::Value>,

    /// All scripts referenced by SKILL.md. The proxy includes only
    /// the ones it has DB rows for; unresolved refs are reported
    /// back in the response.
    #[serde(default)]
    pub scripts: Vec<ScriptInput>,

    /// All assets referenced by SKILL.md.
    #[serde(default)]
    pub assets: Vec<AssetInput>,

    /// All resources referenced by SKILL.md.
    #[serde(default)]
    pub resources: Vec<ResourceInput>,
}

/// One script (Python/JavaScript/TypeScript/Bash) the runner may
/// execute as a subprocess if SKILL.md references it.
#[derive(Debug, Deserialize)]
pub struct ScriptInput {
    pub filename: String,
    /// Language enum: `PYTHON | JAVASCRIPT | TYPESCRIPT | BASH`. Case-
    /// sensitive to match the proxy's DB enum exactly.
    pub language: String,
    /// Execution mode: `SANDBOX | TRUSTED | DISABLED`. SANDBOX is the
    /// default and what the runner is hardened for. DISABLED skips
    /// the script and returns an error. TRUSTED relaxes timeout +
    /// output cap but NOT environment scrubbing — the runner has no
    /// proxy secrets to leak regardless of mode.
    pub execution_mode: String,
    /// Raw script body.
    pub script_content: String,
}

/// One asset (static blob — JSON config, schema, sample data) the
/// runner loads when SKILL.md references it.
#[derive(Debug, Deserialize)]
pub struct AssetInput {
    pub filename: String,
    pub content: String,
}

/// One resource (text resource — template fragment, fixture). Same
/// shape as assets; kept as a distinct type to mirror the proxy's
/// DB schema and to allow future divergence (e.g. binary resources).
#[derive(Debug, Deserialize)]
pub struct ResourceInput {
    pub filename: String,
    pub content: String,
}

// ============================================================================
// L3 (act) — run one NAMED script in a per-session workspace
// ============================================================================

/// Run a single named script (chosen by the model from the L2 manifest) with
/// caller-supplied args, inside a session-scoped workspace so a workflow's
/// script runs can pass files to each other. Unlike [`ExecuteRequest`] this
/// does NOT walk SKILL.md — the proxy has already resolved the one script and
/// (optionally) the assets/resources to seed alongside it.
#[derive(Debug, Deserialize)]
pub struct RunScriptRequest {
    /// Skill identifier (informational — logs/metrics).
    pub skill_id: String,
    /// Skill display name (informational).
    pub skill_name: String,
    /// Skill version (informational).
    pub skill_version: String,
    /// Execution ID minted by the proxy per run — asserted against the JWT
    /// `exec` claim (reuses the existing `verify_bearer` binding).
    pub execution_id: String,
    /// Tenant ID — namespaces the workspace path (tenant isolation).
    pub tenant_id: String,
    /// Session key (the MCP correlation id) — the workspace is
    /// `<workspace_root>/<tenant_id>/<session_id>`; reused across the runs of
    /// one workflow so files persist between them.
    pub session_id: String,
    /// The one named script to run (already allowlist-checked by the proxy).
    pub script: ScriptInput,
    /// Caller-supplied args — forwarded as `input_params` via `SKILL_INPUT`
    /// env + stdin (same contract as [`ExecuteRequest::input_params`]).
    #[serde(default)]
    pub args: Option<serde_json::Value>,
    /// Assets to seed into the workspace so the script can `open()` them.
    #[serde(default)]
    pub assets: Vec<AssetInput>,
    /// Resources to seed into the workspace.
    #[serde(default)]
    pub resources: Vec<ResourceInput>,
}

/// Result of one L3 `run_script` — only the model-facing output, never the
/// script source. `success = exit_code == 0`.
#[derive(Debug, Serialize)]
pub struct RunScriptResponse {
    pub success: bool,
    pub output: String,
    pub stderr: String,
    pub exit_code: i32,
    pub duration_ms: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// Files present in the session workspace after the run (basenames only) —
    /// lets the model/proxy see what a script produced for the next step.
    pub workspace_files: Vec<String>,
}

// ============================================================================
// Response
// ============================================================================

/// What the proxy gets back after the runner finishes executing.
///
/// `success: false` means script-level failure — the runner ran what
/// it was asked to and at least one script returned non-zero.
/// Transport-level errors (bad JWT, malformed JSON, runner crash)
/// surface as HTTP 4xx/5xx with a JSON error envelope, NOT as
/// `success: false` on a 200.
#[derive(Debug, Serialize)]
pub struct ExecuteResponse {
    /// Overall success — true iff every script that ran returned exit
    /// code 0 AND no `Unresolved` refs were reported.
    pub success: bool,
    /// The "final output" the proxy hands back to the calling LLM.
    /// Computed via the same `build_final_output` rules as the
    /// in-proxy executor (template > script-output-concat >
    /// SKILL.md body).
    pub output: String,
    /// Top-level error if execution couldn't even start (e.g. empty
    /// SKILL.md content). Distinct from per-script errors.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,

    /// Per-script results, keyed by filename. Always populated for
    /// any script that was attempted (including failures and timeouts).
    pub script_results: HashMap<String, ScriptResultOut>,
    /// Asset content the runner loaded (filename → content), for
    /// audit. The proxy may persist this in the execution audit
    /// row if compliance requires it.
    pub loaded_assets: HashMap<String, String>,
    /// Resource content the runner loaded.
    pub loaded_resources: HashMap<String, String>,
    /// SKILL.md references that didn't match any script/asset/resource
    /// the proxy sent. Empty list = clean run.
    pub unresolved_refs: Vec<String>,

    /// Total wall-clock time for the runner's execution pipeline (ms).
    pub duration_ms: f64,
}

/// Outcome of one script's subprocess execution.
#[derive(Debug, Serialize)]
pub struct ScriptResultOut {
    pub success: bool,
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
    pub duration_ms: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub language: String,
    pub execution_mode: String,
}

// ============================================================================
// Error envelope
// ============================================================================

/// JSON body returned for HTTP 4xx / 5xx. Field shape matches the
/// proxy's existing client-error pattern so the existing reqwest
/// error decoders Just Work without a new code path.
#[derive(Debug, Serialize)]
pub struct ErrorEnvelope {
    pub error: String,
    pub message: String,
}
