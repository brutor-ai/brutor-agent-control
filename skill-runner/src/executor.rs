// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Skill executor — the subprocess runner that gives the runner its
//! reason for being.
//!
//! The pieces are the SKILL.md parser, the subprocess runner, the
//! template renderer and the final-output builder. The module has no
//! database: `resolve_references()` works on an in-memory index over the
//! scripts/assets/resources the proxy ships in the request.
//!
//! ## What this module does NOT do
//!
//! * It does NOT load proxy secrets into the subprocess env. In TRUSTED
//!   mode only an explicit allowlist of non-secret operational vars is
//!   forwarded (see [`build_subprocess_env`]); in SANDBOX mode the env is
//!   the minimal fixed set. Neither mode forwards a secret it wasn't told
//!   to.
//! * It does NOT trust the proxy beyond JWT verification at the API
//!   layer. Once the request is admitted, the SKILL.md body and all
//!   script content are treated as untrusted: parser is regex-only
//!   (no eval), interpreter is spawned via direct exec (no shell
//!   interpolation).
//!
//! ## What "SANDBOX" does and does NOT guarantee
//!
//! SANDBOX vs TRUSTED differ in two dimensions this module controls:
//! **env** (SANDBOX gets the minimal fixed set; TRUSTED gets the allowlist
//! too) and **resource caps** (tighter timeout + output limit). That is the
//! full extent of the in-process isolation.
//!
//! SANDBOX does **NOT** isolate the network or restrict syscalls: a SANDBOX
//! script runs `python3`/`node`/`bash` with whatever outbound connectivity
//! the process has. Network egress isolation (blocking reach to internal
//! services, other tenants' endpoints, and the cloud metadata endpoint) is
//! the **deployment's** responsibility and MUST be enforced outside this
//! process — a locked-down container network policy / egress firewall, or a
//! syscall sandbox (nsjail/seccomp) wrapping the interpreter. Do not treat
//! `execution_mode = SANDBOX` alone as a security boundary against a hostile
//! script; it is a resource-and-env boundary.
//!
//! See `THREAT-MODEL.md` at the repository root for the full attack
//! surface walk.

use std::collections::HashMap;
use std::path::Path;
use std::time::Instant;

use regex_lite::Regex;
use serde_json::Value;

use crate::types::{
    AssetInput, ExecuteRequest, ExecuteResponse, ResourceInput, RunScriptRequest,
    RunScriptResponse, ScriptInput, ScriptResultOut,
};

// ============================================================================
// Constants — the defaults. Overridable via config.
// ============================================================================

const DEFAULT_SANDBOX_TIMEOUT_SECONDS: u64 = 30;
const SANDBOX_MAX_OUTPUT_BYTES: usize = 1024 * 1024; // 1 MB
const DEFAULT_TRUSTED_TIMEOUT_SECONDS: u64 = 120;
const TRUSTED_MAX_OUTPUT_BYTES: usize = 10 * 1024 * 1024; // 10 MB

// ============================================================================
// Internal types
// ============================================================================

#[derive(Debug, Clone, PartialEq)]
enum FileRefType {
    Script,
    Asset,
    Resource,
    Unresolved,
}

struct FileReference {
    filename: String,
    ref_type: FileRefType,
    content: Option<String>,
    script: Option<ScriptInput>,
    error: Option<String>,
}

struct ParsedSkillMd {
    references: Vec<FileReference>,
    raw_body: String,
}

/// Tuning knobs the API layer forwards from config. They are explicit
/// rather than hard-coded so SaaS can give regulated tenants longer
/// timeouts without rebuilding.
#[derive(Debug, Clone)]
pub struct ExecutorTuning {
    pub sandbox_timeout_secs: u64,
    pub trusted_timeout_secs: u64,
    /// What a TRUSTED script is handed from the runner's environment.
    pub trusted_env: TrustedEnv,
}

/// `(name, value)` pairs a TRUSTED script is handed from the runner's
/// environment, resolved once at start by `Config::from_env`. A SANDBOX
/// script is handed none. `Debug` shows the names only: a proxy URL or an
/// OTLP header can carry a credential.
#[derive(Clone, Default)]
pub struct TrustedEnv(Vec<(String, String)>);

impl TrustedEnv {
    pub fn new(pairs: Vec<(String, String)>) -> Self {
        Self(pairs)
    }

    pub fn pairs(&self) -> &[(String, String)] {
        &self.0
    }
}

impl std::fmt::Debug for TrustedEnv {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_list()
            .entries(self.0.iter().map(|(name, _)| name))
            .finish()
    }
}

impl Default for ExecutorTuning {
    fn default() -> Self {
        Self {
            sandbox_timeout_secs: DEFAULT_SANDBOX_TIMEOUT_SECONDS,
            trusted_timeout_secs: DEFAULT_TRUSTED_TIMEOUT_SECONDS,
            trusted_env: TrustedEnv::default(),
        }
    }
}

// ============================================================================
// Public entry point
// ============================================================================

/// Execute a skill end-to-end. The pipeline:
/// parse SKILL.md → resolve refs → walk → produce final output.
///
/// `tuning` lets the caller override the timeouts; the output cap
/// stays a compile-time constant (changing it would loosen the
/// safety envelope rather than just shift latency).
pub async fn execute(
    req: ExecuteRequest,
    tuning: &ExecutorTuning,
    workspace_root: &Path,
) -> ExecuteResponse {
    let started = Instant::now();

    tracing::debug!(
        skill_id = %req.skill_id,
        skill_name = %req.skill_name,
        execution_id = %req.execution_id,
        scripts = req.scripts.len(),
        assets = req.assets.len(),
        resources = req.resources.len(),
        "execute: start"
    );

    // 1. Reject empty SKILL.md upfront, in the error shape the proxy
    // expects.
    if req.skill_md_content.is_empty() {
        return ExecuteResponse {
            success: false,
            output: String::new(),
            error: Some("Skill has no SKILL.md content".to_string()),
            script_results: HashMap::new(),
            loaded_assets: HashMap::new(),
            loaded_resources: HashMap::new(),
            unresolved_refs: Vec::new(),
            duration_ms: started.elapsed().as_secs_f64() * 1000.0,
        };
    }

    // 2. Parse SKILL.md → workflow references.
    let mut parsed = parse_skill_md(&req.skill_md_content);

    // 3. Resolve references against the inline scripts/assets/resources.
    resolve_references(&mut parsed, &req.scripts, &req.assets, &req.resources);

    // 4. Build initial context.
    let mut context: HashMap<String, String> = HashMap::new();
    context.insert("skill_name".to_string(), req.skill_name.clone());
    context.insert("skill_version".to_string(), req.skill_version.clone());
    context.insert(
        "skill_description".to_string(),
        req.skill_description.clone(),
    );

    if let Some(ref params) = req.input_params {
        if let Some(obj) = params.as_object() {
            for (k, v) in obj {
                let val_str = if let Some(s) = v.as_str() {
                    s.to_string()
                } else {
                    v.to_string()
                };
                context.insert(k.clone(), val_str);
            }
        }
        context.insert("input_params".to_string(), params.to_string());
    }

    // 5. Walk references in document order, executing scripts and
    //    loading assets/resources in turn.
    let mut script_results: HashMap<String, ScriptResultOut> = HashMap::new();
    let mut loaded_assets: HashMap<String, String> = HashMap::new();
    let mut loaded_resources: HashMap<String, String> = HashMap::new();
    let mut unresolved_refs: Vec<String> = Vec::new();
    let mut last_template_content: Option<String> = None;

    let references = std::mem::take(&mut parsed.references);

    // The whole-SKILL.md walk runs each script in a per-execution scratch
    // directory under the workspace root, private to this (tenant,
    // execution) and with no cross-run persistence; L3 `run_named_script`
    // uses a per-session workspace instead. A directory shared between
    // tenants (the process temp dir with `HOME`/`TMPDIR` pointed at it)
    // would let concurrent executions read each other's script files and
    // outputs. Removed when the execution completes; the workspace GC
    // covers a crash.
    let exec_dir = workspace_root
        .join(crate::workspace::safe_component(&req.tenant_id))
        .join(format!(
            "exec-{}",
            crate::workspace::safe_component(&req.execution_id)
        ));
    if let Err(e) = std::fs::create_dir_all(&exec_dir) {
        return ExecuteResponse {
            success: false,
            output: String::new(),
            error: Some(format!("failed to create execution workspace: {e}")),
            script_results: HashMap::new(),
            loaded_assets: HashMap::new(),
            loaded_resources: HashMap::new(),
            unresolved_refs: Vec::new(),
            duration_ms: started.elapsed().as_secs_f64() * 1000.0,
        };
    }
    crate::workspace::set_private_perms(&exec_dir);
    let _exec_dir_cleanup = DirCleanup(exec_dir.clone());
    let legacy_workdir = exec_dir;

    for file_ref in references {
        match file_ref.ref_type {
            FileRefType::Script => {
                if let Some(script) = file_ref.script {
                    let result = execute_script(&script, &context, tuning, &legacy_workdir).await;
                    let output = script_output(&result);

                    // Inject output into context for subsequent steps
                    // (three keys per script).
                    context.insert(
                        format!("script_output:{}", file_ref.filename),
                        output.clone(),
                    );
                    context.insert(file_ref.filename.clone(), output.clone());
                    if let Some(stem) = file_ref.filename.rsplit_once('.').map(|(s, _)| s) {
                        context.entry(stem.to_string()).or_insert(output);
                    }

                    if !result.success {
                        tracing::warn!(
                            skill_id = %req.skill_id,
                            filename = %file_ref.filename,
                            error = ?result.error,
                            exit_code = result.exit_code,
                            "script failed"
                        );
                    }

                    script_results.insert(file_ref.filename, result);
                }
            }
            FileRefType::Asset => {
                let content = file_ref.content.unwrap_or_default();
                context.insert(format!("asset:{}", file_ref.filename), content.clone());
                context.insert(file_ref.filename.clone(), content.clone());
                last_template_content = Some(content.clone());
                loaded_assets.insert(file_ref.filename, content);
            }
            FileRefType::Resource => {
                let content = file_ref.content.unwrap_or_default();
                context.insert(format!("resource:{}", file_ref.filename), content.clone());
                context.insert(file_ref.filename.clone(), content.clone());
                loaded_resources.insert(file_ref.filename, content);
            }
            FileRefType::Unresolved => {
                tracing::warn!(
                    skill_id = %req.skill_id,
                    filename = %file_ref.filename,
                    error = ?file_ref.error,
                    "unresolved file reference"
                );
                unresolved_refs.push(file_ref.filename);
            }
        }
    }

    // 6. Compute the final output.
    let output = build_final_output(
        &parsed.raw_body,
        &context,
        last_template_content.as_deref(),
        &script_results,
    );

    let success = unresolved_refs.is_empty() && script_results.values().all(|r| r.success);

    ExecuteResponse {
        success,
        output,
        error: None,
        script_results,
        loaded_assets,
        loaded_resources,
        unresolved_refs,
        duration_ms: started.elapsed().as_secs_f64() * 1000.0,
    }
}

// ============================================================================
// L3 (act) — run ONE named script in a per-session workspace
// ============================================================================

/// Run a single named script (no SKILL.md walk) inside the caller's session
/// workspace, seeding referenced assets/resources so the script can read them.
/// Returns only `{output, exit_code, stderr}` — the script source never leaves.
pub async fn run_named_script(
    req: RunScriptRequest,
    tuning: &ExecutorTuning,
    workspace_root: &Path,
    workspace_ttl_secs: u64,
) -> RunScriptResponse {
    let started = Instant::now();

    let fail = |msg: String, elapsed: f64| RunScriptResponse {
        success: false,
        output: String::new(),
        stderr: String::new(),
        exit_code: -1,
        duration_ms: elapsed,
        error: Some(msg),
        workspace_files: Vec::new(),
    };

    // Defence-in-depth: the proxy already allowlists the script against the
    // pinned manifest, but the runner re-validates the basename before writing.
    if let Err(e) = crate::workspace::safe_filename(&req.script.filename) {
        return fail(e, started.elapsed().as_secs_f64() * 1000.0);
    }

    // Resolve/create the traversal-proof, tenant-namespaced session workspace.
    let workspace = match crate::workspace::ensure_session(
        workspace_root,
        &req.tenant_id,
        &req.session_id,
        workspace_ttl_secs,
    ) {
        Ok(w) => w,
        Err(e) => {
            return fail(
                format!("failed to create session workspace: {e}"),
                started.elapsed().as_secs_f64() * 1000.0,
            );
        }
    };

    // Seed referenced assets/resources into the workspace (validated basenames)
    // so the script can `open()` them. Bad names are skipped, not fatal.
    for a in &req.assets {
        if let Ok(name) = crate::workspace::safe_filename(&a.filename) {
            let _ = tokio::fs::write(workspace.join(&name), &a.content).await;
        }
    }
    for r in &req.resources {
        if let Ok(name) = crate::workspace::safe_filename(&r.filename) {
            let _ = tokio::fs::write(workspace.join(&name), &r.content).await;
        }
    }

    // Build the execution context — caller args become `input_params`, the same
    // contract the whole-skill `execute()` path uses (SKILL_INPUT env + stdin).
    let mut context: HashMap<String, String> = HashMap::new();
    context.insert("skill_name".to_string(), req.skill_name.clone());
    context.insert("skill_version".to_string(), req.skill_version.clone());
    if let Some(ref params) = req.args {
        if let Some(obj) = params.as_object() {
            for (k, v) in obj {
                let val_str = if let Some(s) = v.as_str() {
                    s.to_string()
                } else {
                    v.to_string()
                };
                context.insert(k.clone(), val_str);
            }
        }
        context.insert("input_params".to_string(), params.to_string());
    }

    let result = execute_script(&req.script, &context, tuning, &workspace).await;
    let output = script_output(&result);
    let workspace_files = crate::workspace::list_files(&workspace);

    tracing::debug!(
        skill_id = %req.skill_id,
        script = %req.script.filename,
        session_id = %req.session_id,
        exit_code = result.exit_code,
        success = result.success,
        "run_named_script complete"
    );

    RunScriptResponse {
        success: result.success,
        output,
        stderr: result.stderr,
        exit_code: result.exit_code,
        duration_ms: started.elapsed().as_secs_f64() * 1000.0,
        error: result.error,
        workspace_files,
    }
}

// ============================================================================
// SKILL.md parser — pure
// ============================================================================

fn parse_skill_md(content: &str) -> ParsedSkillMd {
    let link_re = Regex::new(r"\[([^\]]+)\]\(([^)]+)\)").unwrap();
    let url_prefixes = ["http://", "https://", "ftp://", "mailto:", "#"];

    let mut references = Vec::new();

    for line in content.lines() {
        for cap in link_re.captures_iter(line) {
            let filename = cap[2].trim().to_string();

            // Skip URLs / anchors / extensionless refs.
            if url_prefixes.iter().any(|p| filename.starts_with(p)) {
                continue;
            }
            if !filename.contains('.') {
                continue;
            }

            references.push(FileReference {
                filename,
                ref_type: FileRefType::Unresolved,
                content: None,
                script: None,
                error: None,
            });
        }
    }

    ParsedSkillMd {
        references,
        raw_body: content.to_string(),
    }
}

// ============================================================================
// Reference resolution — over inline inputs (NOT the DB).
// ============================================================================

fn resolve_references(
    parsed: &mut ParsedSkillMd,
    scripts: &[ScriptInput],
    assets: &[AssetInput],
    resources: &[ResourceInput],
) {
    // Indexes by filename; lookup order is
    // script → asset → resource → unresolved.
    let script_idx: HashMap<&str, &ScriptInput> =
        scripts.iter().map(|s| (s.filename.as_str(), s)).collect();
    let asset_idx: HashMap<&str, &AssetInput> =
        assets.iter().map(|a| (a.filename.as_str(), a)).collect();
    let resource_idx: HashMap<&str, &ResourceInput> =
        resources.iter().map(|r| (r.filename.as_str(), r)).collect();

    for file_ref in &mut parsed.references {
        if let Some(s) = script_idx.get(file_ref.filename.as_str()) {
            // Clone the small ScriptInput so the executor owns it
            // through the await boundary.
            file_ref.ref_type = FileRefType::Script;
            file_ref.script = Some(ScriptInput {
                filename: s.filename.clone(),
                language: s.language.clone(),
                execution_mode: s.execution_mode.clone(),
                script_content: s.script_content.clone(),
            });
        } else if let Some(a) = asset_idx.get(file_ref.filename.as_str()) {
            file_ref.ref_type = FileRefType::Asset;
            file_ref.content = Some(a.content.clone());
        } else if let Some(r) = resource_idx.get(file_ref.filename.as_str()) {
            file_ref.ref_type = FileRefType::Resource;
            file_ref.content = Some(r.content.clone());
        } else {
            file_ref.error = Some(format!("'{}' not found in payload", file_ref.filename));
        }
    }
}

// ============================================================================
// Per-script execution
// ============================================================================

async fn execute_script(
    script: &ScriptInput,
    context: &HashMap<String, String>,
    tuning: &ExecutorTuning,
    work_dir: &Path,
) -> ScriptResultOut {
    let start = Instant::now();
    let language = &script.language;
    let execution_mode = &script.execution_mode;

    // DISABLED short-circuits to a clear error.
    if execution_mode == "DISABLED" {
        return ScriptResultOut {
            success: false,
            stdout: String::new(),
            stderr: String::new(),
            exit_code: -1,
            duration_ms: 0.0,
            error: Some(format!("Script '{}' is disabled", script.filename)),
            language: language.clone(),
            execution_mode: execution_mode.clone(),
        };
    }

    let interpreter: Vec<&str> = match language.as_str() {
        "PYTHON" => vec!["python3"],
        "JAVASCRIPT" => vec!["node"],
        // `tsx` is installed globally in the image. Not `npx tsx`: npx
        // falls back to fetching from the npm registry when it cannot
        // find a package, and the runner has no network.
        "TYPESCRIPT" => vec!["tsx"],
        "BASH" => vec!["bash"],
        _ => {
            return ScriptResultOut {
                success: false,
                stdout: String::new(),
                stderr: String::new(),
                exit_code: -1,
                duration_ms: 0.0,
                error: Some(format!("Unsupported script language: {language}")),
                language: language.clone(),
                execution_mode: execution_mode.clone(),
            };
        }
    };

    let is_sandbox = execution_mode == "SANDBOX";
    let timeout_secs = if is_sandbox {
        tuning.sandbox_timeout_secs
    } else {
        tuning.trusted_timeout_secs
    };
    let max_output = if is_sandbox {
        SANDBOX_MAX_OUTPUT_BYTES
    } else {
        TRUSTED_MAX_OUTPUT_BYTES
    };

    match run_subprocess(
        &script.script_content,
        &interpreter,
        context,
        timeout_secs,
        max_output,
        is_sandbox,
        &script.filename,
        work_dir,
        &tuning.trusted_env,
    )
    .await
    {
        Ok(mut result) => {
            result.duration_ms = start.elapsed().as_secs_f64() * 1000.0;
            result.language = language.clone();
            result.execution_mode = execution_mode.clone();
            result
        }
        Err(e) => ScriptResultOut {
            success: false,
            stdout: String::new(),
            stderr: String::new(),
            exit_code: -1,
            duration_ms: start.elapsed().as_secs_f64() * 1000.0,
            error: Some(format!("Script runner error: {e}")),
            language: language.clone(),
            execution_mode: execution_mode.clone(),
        },
    }
}

/// Build the env handed to the subprocess.
///
/// Neither mode inherits the parent process's env. The parent process
/// is the runner container, which has NO gateway secrets, and even
/// TRUSTED mode gets only what is listed here.
///
/// TRUSTED mode is handed `trusted_env` and nothing else of the runner's
/// environment: the variables named in
/// `BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST`, by default the OTel endpoint,
/// the OTel service name and the locale. Proxy variables and the OTLP headers
/// can carry credentials, so they reach a script only when the operator names
/// them. It is an allowlist and not a
/// denylist (forward everything except a handful of known secret names): a
/// denylist silently leaks any secret it doesn't enumerate (a provider key,
/// cloud creds, or any secret added to the container later) into
/// tenant-authored trusted code. An allowlist fails closed instead. The runner's own
/// variables, its token secret included, cannot be put on the operator's
/// list (`Env::forwarded_by_name`).
fn build_subprocess_env(
    is_sandbox: bool,
    work_dir: &Path,
    trusted_env: &TrustedEnv,
) -> HashMap<String, String> {
    let mut env = HashMap::new();
    // HOME/TMPDIR point at the working directory so temp writes land inside the
    // (session) workspace, keeping the process's filesystem footprint contained.
    let work = work_dir.to_string_lossy().to_string();

    // Minimum needed to run python3/node/tsx/bash + locate user code.
    env.insert(
        "PATH".to_string(),
        "/usr/local/bin:/usr/bin:/bin".to_string(),
    );
    env.insert("HOME".to_string(), work.clone());
    env.insert("TMPDIR".to_string(), work);
    env.insert("LANG".to_string(), "en_US.UTF-8".to_string());

    if !is_sandbox {
        for (name, value) in trusted_env.pairs() {
            env.entry(name.clone()).or_insert_with(|| value.clone());
        }
    }

    env
}

// Nine parameters, two over clippy's default. They are all distinct,
// independently-sourced values for a single subprocess launch; bundling them
// into a struct would just move the same list one level out. Same call the
// gateway-core codebase makes for its multi-argument evaluate() entry points.
#[allow(clippy::too_many_arguments)]
async fn run_subprocess(
    script_content: &str,
    interpreter: &[&str],
    context: &HashMap<String, String>,
    timeout_secs: u64,
    max_output: usize,
    is_sandbox: bool,
    filename: &str,
    work_dir: &Path,
    trusted_env: &TrustedEnv,
) -> Result<ScriptResultOut, String> {
    use tokio::io::AsyncWriteExt;

    let mut env = build_subprocess_env(is_sandbox, work_dir, trusted_env);

    // Build SKILL_INPUT JSON + stdin payload.
    let input_json = {
        let mut input_context = serde_json::Map::new();
        if let Some(params_str) = context.get("input_params")
            && let Ok(v) = serde_json::from_str::<Value>(params_str)
        {
            input_context.insert("input_params".to_string(), v);
        }
        for (key, val) in context {
            if key.starts_with("script_output:") {
                input_context.insert(key.clone(), Value::String(val.clone()));
            }
        }
        if input_context.is_empty() {
            None
        } else {
            let json_str = serde_json::to_string(&input_context).unwrap_or_default();
            env.insert("SKILL_INPUT".to_string(), json_str.clone());
            Some(json_str)
        }
    };

    // Write the script into the working directory under a generated name. The
    // ULID name avoids collision with any seeded file; the cleanup guard deletes
    // ONLY this script file (never the workspace or the seeded/produced files).
    let suffix = filename
        .rsplit_once('.')
        .map(|(_, ext)| format!(".{ext}"))
        .unwrap_or_else(|| ".txt".to_string());
    let tmp_path = work_dir.join(format!("skill_script_{}{}", ulid::Ulid::new(), suffix));
    tokio::fs::write(&tmp_path, script_content)
        .await
        .map_err(|e| format!("failed to write temp script: {e}"))?;
    let _cleanup = TempFileCleanup(tmp_path.clone());

    let mut cmd = tokio::process::Command::new(interpreter[0]);
    for arg in &interpreter[1..] {
        cmd.arg(arg);
    }
    cmd.arg(&tmp_path);
    cmd.env_clear();
    cmd.envs(&env);
    cmd.current_dir(work_dir);
    cmd.stdin(std::process::Stdio::piped());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());
    // A timed-out script must actually stop. Merely dropping the wait on
    // timeout would report "timed out" and leave the process running:
    // with `pids_limit 64` a handful of runaway scripts starve every later
    // spawn for every tenant. Kill on drop covers the direct child; its
    // own process group lets us reap whatever it forked.
    cmd.kill_on_drop(true);
    #[cfg(unix)]
    cmd.process_group(0);

    let mut child = cmd
        .spawn()
        .map_err(|e| format!("failed to spawn script process: {e}"))?;
    let pid = child.id();

    if let Some(ref json_data) = input_json {
        if let Some(mut stdin) = child.stdin.take() {
            let _ = stdin.write_all(json_data.as_bytes()).await;
            drop(stdin);
        }
    } else {
        drop(child.stdin.take());
    }

    // Read both pipes concurrently, bounded. `wait_with_output` would
    // collect everything the script prints *before* the cap is applied, so
    // a script that prints faster than the cap could OOM the runner (256 MB
    // limit) before truncation happens. At most `max_output` bytes of
    // each stream are ever held; the rest is drained and discarded so the
    // child never blocks on a full pipe.
    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();
    let stdout_task = tokio::spawn(read_capped(stdout_pipe, max_output));
    let stderr_task = tokio::spawn(read_capped(stderr_pipe, max_output));

    let timeout = std::time::Duration::from_secs(timeout_secs);
    let waited = tokio::time::timeout(timeout, child.wait()).await;

    let status = match waited {
        Ok(Ok(status)) => Some(status),
        Ok(Err(e)) => {
            kill_process_group(pid, &mut child).await;
            return Err(format!("process execution error: {e}"));
        }
        Err(_elapsed) => {
            kill_process_group(pid, &mut child).await;
            None
        }
    };

    let (stdout, stdout_truncated) = stdout_task.await.unwrap_or_default();
    let (stderr, stderr_truncated) = stderr_task.await.unwrap_or_default();
    if stdout_truncated || stderr_truncated {
        tracing::warn!(
            filename,
            max_output,
            stdout_truncated,
            stderr_truncated,
            "script output truncated at the cap"
        );
    }

    match status {
        None => Ok(ScriptResultOut {
            success: false,
            stdout,
            stderr,
            exit_code: -9,
            duration_ms: 0.0,
            error: Some(format!(
                "Script '{filename}' timed out after {timeout_secs}s"
            )),
            language: String::new(),
            execution_mode: String::new(),
        }),
        Some(status) => {
            let exit_code = status.code().unwrap_or(-1);
            Ok(ScriptResultOut {
                success: exit_code == 0,
                stdout,
                stderr,
                exit_code,
                duration_ms: 0.0,
                error: None,
                language: String::new(),
                execution_mode: String::new(),
            })
        }
    }
}

/// Read a child's pipe keeping at most `cap` bytes; the remainder is
/// drained (so the child never blocks on a full pipe) and dropped. Returns
/// the kept bytes (lossily decoded) and whether anything was discarded.
async fn read_capped<R>(pipe: Option<R>, cap: usize) -> (String, bool)
where
    R: tokio::io::AsyncRead + Unpin,
{
    use tokio::io::AsyncReadExt;
    let Some(mut pipe) = pipe else {
        return (String::new(), false);
    };
    let mut kept: Vec<u8> = Vec::new();
    let mut truncated = false;
    let mut buf = [0u8; 16 * 1024];
    loop {
        match pipe.read(&mut buf).await {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                let room = cap.saturating_sub(kept.len());
                if n > room {
                    truncated = true;
                }
                kept.extend_from_slice(&buf[..n.min(room)]);
            }
        }
    }
    (String::from_utf8_lossy(&kept).to_string(), truncated)
}

/// Stop a script and everything it forked. The child was spawned as its
/// own process-group leader, so `kill(-pgid)` reaches grandchildren that a
/// plain `Child::kill` would orphan (`bash -c "… &"`). Falls back to the
/// direct child on platforms without process groups, then reaps it.
async fn kill_process_group(pid: Option<u32>, child: &mut tokio::process::Child) {
    #[cfg(unix)]
    if let Some(pid) = pid {
        // SAFETY: plain libc call with a pid we spawned; a stale pid is
        // harmless here because the group id equals the child's pid and
        // the child is still un-reaped (we hold the `Child`).
        unsafe {
            libc::kill(-(pid as i32), libc::SIGKILL);
        }
    }
    let _ = child.kill().await;
    let _ = child.wait().await;
}

/// Removes a per-execution directory (and everything a script left in
/// it) when the execution finishes, however it finishes.
struct DirCleanup(std::path::PathBuf);
impl Drop for DirCleanup {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

struct TempFileCleanup(std::path::PathBuf);
impl Drop for TempFileCleanup {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

// ============================================================================
// Template + final-output helpers — pure
// ============================================================================

fn script_output(result: &ScriptResultOut) -> String {
    if result.success {
        result.stdout.trim().to_string()
    } else {
        result.error.clone().unwrap_or_else(|| {
            let s = result.stderr.trim();
            if s.is_empty() {
                format!("Script exited with code {}", result.exit_code)
            } else {
                s.to_string()
            }
        })
    }
}

fn render_template(template_content: &str, context: &HashMap<String, String>) -> String {
    let mut result = template_content.to_string();
    let var_re = Regex::new(r"\{\{(\s*\w+\s*)\}\}").unwrap();
    result = var_re
        .replace_all(&result, |caps: &regex_lite::Captures| {
            let var_name = caps[1].trim();
            context
                .get(var_name)
                .cloned()
                .unwrap_or_else(|| caps[0].to_string())
        })
        .to_string();
    let script_re = Regex::new(r"\{script_output:([^}]+)\}").unwrap();
    result = script_re
        .replace_all(&result, |caps: &regex_lite::Captures| {
            let filename = caps[1].trim();
            let key = format!("script_output:{filename}");
            context
                .get(&key)
                .or_else(|| context.get(filename))
                .cloned()
                .unwrap_or_else(|| caps[0].to_string())
        })
        .to_string();
    result
}

fn build_final_output(
    raw_body: &str,
    context: &HashMap<String, String>,
    last_template_content: Option<&str>,
    script_results: &HashMap<String, ScriptResultOut>,
) -> String {
    if let Some(template) = last_template_content {
        return render_template(template, context);
    }
    if !script_results.is_empty() {
        let parts: Vec<String> = script_results
            .iter()
            .map(|(filename, result)| {
                if result.success && !result.stdout.trim().is_empty() {
                    result.stdout.trim().to_string()
                } else if !result.success {
                    format!(
                        "[{filename} failed: {}]",
                        result.error.as_deref().unwrap_or("unknown error")
                    )
                } else {
                    String::new()
                }
            })
            .filter(|s| !s.is_empty())
            .collect();
        if !parts.is_empty() {
            return parts.join("\n\n");
        }
    }
    raw_body.to_string()
}

// ============================================================================
// Tests
// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    fn req(skill_md: &str) -> ExecuteRequest {
        ExecuteRequest {
            skill_id: "skl-test".into(),
            skill_name: "Test".into(),
            skill_version: "1.0".into(),
            skill_description: "Test skill".into(),
            // Unique per test: executions get a private directory keyed by
            // (tenant, execution id) and the tests run in parallel.
            execution_id: format!("exec-{}", ulid::Ulid::new()),
            tenant_id: "t-1".into(),
            skill_md_content: skill_md.into(),
            input_params: None,
            scripts: Vec::new(),
            assets: Vec::new(),
            resources: Vec::new(),
        }
    }

    #[tokio::test]
    async fn empty_skill_md_returns_error() {
        let resp = execute(req(""), &ExecutorTuning::default(), &std::env::temp_dir()).await;
        assert!(!resp.success);
        assert!(resp.error.is_some());
    }

    #[tokio::test]
    async fn skill_with_no_refs_returns_md_as_output() {
        let resp = execute(
            req("Just a prompt with no [link](file.py) refs that look like files."),
            &ExecutorTuning::default(),
            &std::env::temp_dir(),
        )
        .await;
        // No scripts referenced (file.py is unresolved), no assets,
        // no resources → final output falls through to the SKILL.md
        // body itself.
        assert!(!resp.success); // unresolved file.py
        assert_eq!(resp.unresolved_refs, vec!["file.py".to_string()]);
        assert!(resp.output.contains("Just a prompt"));
    }

    #[tokio::test]
    async fn python_script_executes_and_stdout_captured() {
        let mut r = req("Run [hello](hello.py).");
        r.scripts.push(ScriptInput {
            filename: "hello.py".into(),
            language: "PYTHON".into(),
            execution_mode: "SANDBOX".into(),
            script_content: "print('hello from python')".into(),
        });
        let resp = execute(r, &ExecutorTuning::default(), &std::env::temp_dir()).await;
        assert!(resp.success, "got: {resp:?}");
        assert!(resp.output.contains("hello from python"));
        let result = resp.script_results.get("hello.py").unwrap();
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.language, "PYTHON");
    }

    #[tokio::test]
    async fn bash_script_with_stdin_input() {
        let mut r = req("Process [stdin](read.sh).");
        r.scripts.push(ScriptInput {
            filename: "read.sh".into(),
            language: "BASH".into(),
            execution_mode: "SANDBOX".into(),
            // Read whole stdin and echo it back uppercased so we
            // can tell the runner actually piped the input.
            script_content: "cat | tr a-z A-Z".into(),
        });
        r.input_params = Some(serde_json::json!({"who": "world"}));
        let resp = execute(r, &ExecutorTuning::default(), &std::env::temp_dir()).await;
        assert!(resp.success);
        assert!(
            resp.output.contains("WORLD"),
            "expected uppercased stdin in output: {}",
            resp.output
        );
    }

    #[tokio::test]
    async fn script_timeout_is_enforced() {
        let mut r = req("Sleeps [forever](sleep.sh).");
        r.scripts.push(ScriptInput {
            filename: "sleep.sh".into(),
            language: "BASH".into(),
            execution_mode: "SANDBOX".into(),
            script_content: "sleep 5".into(),
        });
        let tuning = ExecutorTuning {
            sandbox_timeout_secs: 1, // force a quick timeout
            trusted_timeout_secs: 120,
            trusted_env: TrustedEnv::default(),
        };
        let resp = execute(r, &tuning, &std::env::temp_dir()).await;
        assert!(!resp.success);
        let result = resp.script_results.get("sleep.sh").unwrap();
        assert_eq!(result.exit_code, -9);
        assert!(result.error.as_ref().unwrap().contains("timed out"));
    }

    fn pid_is_alive(pid: &str) -> bool {
        std::process::Command::new("kill")
            .args(["-0", pid])
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    #[tokio::test]
    async fn a_timed_out_script_is_actually_killed() {
        // The script records its pid and the pid of a background child it
        // forks, then sleeps past the timeout. Both must be gone afterwards:
        // reporting "timed out" while the processes keep running is not enough.
        let pidfile = std::env::temp_dir().join(format!("skill-runner-pid-{}", ulid::Ulid::new()));
        let mut r = req("Hang [forever](hang.sh).");
        r.scripts.push(ScriptInput {
            filename: "hang.sh".into(),
            language: "BASH".into(),
            execution_mode: "SANDBOX".into(),
            script_content: format!("sleep 60 & echo \"$$ $!\" > {}; wait", pidfile.display()),
        });
        let tuning = ExecutorTuning {
            sandbox_timeout_secs: 1,
            trusted_timeout_secs: 120,
            trusted_env: TrustedEnv::default(),
        };
        let resp = execute(r, &tuning, &std::env::temp_dir()).await;
        assert!(!resp.success);
        assert_eq!(resp.script_results.get("hang.sh").unwrap().exit_code, -9);

        let pids = std::fs::read_to_string(&pidfile).expect("script wrote its pids");
        let _ = std::fs::remove_file(&pidfile);
        // Give the kernel a moment to reap the group.
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        for pid in pids.split_whitespace() {
            assert!(
                !pid_is_alive(pid),
                "process {pid} survived the timeout (pids recorded: {pids:?})"
            );
        }
    }

    #[tokio::test]
    async fn output_is_capped_without_buffering_the_rest() {
        // 4 MiB of stdout against a 1 MiB sandbox cap: the result holds at
        // most the cap and the script still exits 0 (its pipe was drained).
        let mut r = req("Spew [lots](spew.py).");
        r.scripts.push(ScriptInput {
            filename: "spew.py".into(),
            language: "PYTHON".into(),
            execution_mode: "SANDBOX".into(),
            script_content: "import sys\nsys.stdout.write('x' * (4 * 1024 * 1024))\n".into(),
        });
        let resp = execute(r, &ExecutorTuning::default(), &std::env::temp_dir()).await;
        let result = resp.script_results.get("spew.py").unwrap();
        assert_eq!(result.exit_code, 0, "{:?}", result.error);
        assert!(result.stdout.len() <= SANDBOX_MAX_OUTPUT_BYTES);
        assert!(result.stdout.len() >= SANDBOX_MAX_OUTPUT_BYTES - 16);
    }

    #[tokio::test]
    async fn each_execution_gets_a_private_directory_that_is_removed_afterwards() {
        let root = std::env::temp_dir().join(format!("skill-runner-ws-{}", ulid::Ulid::new()));
        let mut r = req("Where am I? [pwd](pwd.sh)");
        r.tenant_id = "tenant-a".into();
        r.execution_id = "exec-1".into();
        r.scripts.push(ScriptInput {
            filename: "pwd.sh".into(),
            language: "BASH".into(),
            execution_mode: "SANDBOX".into(),
            script_content: "echo \"$PWD|$HOME|$TMPDIR\"; touch leftover.txt".into(),
        });
        let resp = execute(r, &ExecutorTuning::default(), &root).await;
        assert!(resp.success, "{resp:?}");
        let parts: Vec<&str> = resp.output.split('|').collect();
        let expected = root.join("tenant-a").join("exec-exec-1");
        for p in &parts {
            assert!(
                std::path::Path::new(p).ends_with("tenant-a/exec-exec-1"),
                "script must run in its own per-execution dir, got {p} (expected {})",
                expected.display()
            );
        }
        assert!(
            !expected.exists(),
            "the execution directory (and the script's leftover file) must be removed"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[tokio::test]
    async fn disabled_script_short_circuits() {
        let mut r = req("Run [thing](thing.py).");
        r.scripts.push(ScriptInput {
            filename: "thing.py".into(),
            language: "PYTHON".into(),
            execution_mode: "DISABLED".into(),
            script_content: "print('should not run')".into(),
        });
        let resp = execute(r, &ExecutorTuning::default(), &std::env::temp_dir()).await;
        assert!(!resp.success);
        let result = resp.script_results.get("thing.py").unwrap();
        assert!(result.error.as_ref().unwrap().contains("disabled"));
    }

    #[tokio::test]
    async fn unsupported_language_errors_cleanly() {
        let mut r = req("Run [thing](thing.rb).");
        r.scripts.push(ScriptInput {
            filename: "thing.rb".into(),
            language: "RUBY".into(),
            execution_mode: "SANDBOX".into(),
            script_content: "puts 'nope'".into(),
        });
        let resp = execute(r, &ExecutorTuning::default(), &std::env::temp_dir()).await;
        assert!(!resp.success);
        let result = resp.script_results.get("thing.rb").unwrap();
        assert!(
            result.error.as_ref().unwrap().contains("Unsupported"),
            "got: {:?}",
            result.error
        );
    }

    /// Held by every test that sets a variable of the process environment the
    /// runner forwards from, so one test's value never reaches another's
    /// configuration.
    static PROCESS_ENV: std::sync::Mutex<()> = std::sync::Mutex::new(());

    /// The runner's configuration for the given environment.
    fn config_for(pairs: &[(&str, &str)]) -> crate::config::Config {
        let mut all = vec![(
            "BRUTOR_SKILL_RUNNER_JWT_SECRET",
            "0123456789abcdef0123456789abcdef",
        )];
        all.extend_from_slice(pairs);
        let env = crate::env_registry::Env::from_pairs(&crate::env_vars::REGISTRY, all).unwrap();
        crate::config::Config::from_env(&env).unwrap()
    }

    #[test]
    fn collector_credentials_and_proxies_reach_no_trusted_script_by_default() {
        // The OTLP headers usually carry the collector's credentials and
        // a proxy URL may embed a user and password. A TRUSTED script gets
        // them only when the operator names them in
        // BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST.
        const NAMES: [&str; 7] = [
            "OTEL_EXPORTER_OTLP_HEADERS",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        ];
        let _guard = PROCESS_ENV.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: the process environment is what the runner forwards from;
        // the lock keeps the other tests that set it out, and the names are
        // removed below.
        unsafe {
            for name in NAMES {
                std::env::set_var(name, "s10-must-not-leak");
            }
        }
        let pairs: Vec<(&str, &str)> = NAMES.iter().map(|n| (*n, "s10-must-not-leak")).collect();
        let work = std::env::temp_dir();
        let by_default = build_subprocess_env(false, &work, &config_for(&pairs).trusted_env);
        let mut with_allowlist = pairs.clone();
        with_allowlist.push((
            "BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST",
            "OTEL_EXPORTER_OTLP_HEADERS,HTTPS_PROXY",
        ));
        let named = build_subprocess_env(false, &work, &config_for(&with_allowlist).trusted_env);
        unsafe {
            for name in NAMES {
                std::env::remove_var(name);
            }
        }
        for name in NAMES {
            assert!(
                !by_default.contains_key(name),
                "{name} reached a TRUSTED script without the operator naming it"
            );
        }
        assert_eq!(
            named.get("OTEL_EXPORTER_OTLP_HEADERS").map(String::as_str),
            Some("s10-must-not-leak")
        );
        assert_eq!(
            named.get("HTTPS_PROXY").map(String::as_str),
            Some("s10-must-not-leak")
        );
        assert!(!named.contains_key("HTTP_PROXY"));
    }

    #[test]
    fn trusted_env_is_allowlist_not_denylist() {
        // TRUSTED mode must forward ONLY allowlisted variables. An arbitrary
        // secret an operator never named (e.g. a provider key) must NOT reach
        // the subprocess; a proxy hint the operator named must.
        let _guard = PROCESS_ENV.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: under the lock; removed below.
        unsafe {
            std::env::set_var("OPENAI_API_KEY", "sk-should-not-leak");
            std::env::set_var("AWS_SECRET_ACCESS_KEY", "aws-should-not-leak");
            std::env::set_var("HTTPS_PROXY", "http://proxy.internal:3128");
        }
        let config = config_for(&[("BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST", "HTTPS_PROXY")]);
        unsafe {
            std::env::remove_var("OPENAI_API_KEY");
            std::env::remove_var("AWS_SECRET_ACCESS_KEY");
            std::env::remove_var("HTTPS_PROXY");
        }

        let work = std::env::temp_dir();
        let env = build_subprocess_env(/* is_sandbox */ false, &work, &config.trusted_env);

        assert!(
            !env.contains_key("OPENAI_API_KEY"),
            "non-allowlisted provider key must not be forwarded to trusted scripts"
        );
        assert!(
            !env.contains_key("AWS_SECRET_ACCESS_KEY"),
            "non-allowlisted cloud credential must not be forwarded"
        );
        assert_eq!(
            env.get("HTTPS_PROXY").map(String::as_str),
            Some("http://proxy.internal:3128"),
            "a proxy hint the operator named should be forwarded"
        );
        assert!(
            !env.contains_key("BRUTOR_SKILL_RUNNER_JWT_SECRET"),
            "the runner's token secret must not be forwarded"
        );

        // SANDBOX mode forwards none of it.
        let sandbox_env = build_subprocess_env(true, &work, &config.trusted_env);
        assert!(!sandbox_env.contains_key("HTTPS_PROXY"));
        assert!(!sandbox_env.contains_key("OPENAI_API_KEY"));

        // The names appear when the forwarded set is printed, the values do not.
        let printed = format!("{:?}", config.trusted_env);
        assert!(printed.contains("HTTPS_PROXY") && !printed.contains("proxy.internal"));
    }

    #[test]
    fn the_default_list_reaches_a_trusted_script_and_no_sandbox_script() {
        const DEFAULT: [&str; 4] = [
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_SERVICE_NAME",
            "LC_ALL",
            "TZ",
        ];
        assert_eq!(
            crate::env_vars::BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST.default,
            Some(DEFAULT.join(",").as_str())
        );
        let _guard = PROCESS_ENV.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: under the lock; removed below.
        unsafe {
            for name in DEFAULT {
                std::env::set_var(name, "forwarded-value");
            }
        }
        let config = config_for(&[]);
        unsafe {
            for name in DEFAULT {
                std::env::remove_var(name);
            }
        }
        let work = std::env::temp_dir();
        let trusted = build_subprocess_env(false, &work, &config.trusted_env);
        let sandbox = build_subprocess_env(true, &work, &config.trusted_env);
        for name in DEFAULT {
            assert_eq!(
                trusted.get(name).map(String::as_str),
                Some("forwarded-value"),
                "{name}"
            );
            assert!(!sandbox.contains_key(name), "{name}");
        }
    }

    #[test]
    fn the_operators_allowlist_adds_names_but_never_the_runners_own_variables() {
        let _guard = PROCESS_ENV.lock().unwrap_or_else(|e| e.into_inner());
        // SAFETY: process-wide setenv of names only this test uses, removed
        // below. The allowlist names variables of the process, so the
        // process has to hold them.
        unsafe {
            std::env::set_var("ENVREG_TEST_PIP_INDEX_URL", "https://pypi.internal/simple");
            std::env::set_var("ENVREG_TEST_NOT_LISTED", "stays-here");
        }
        let config = config_for(&[(
            "BRUTOR_SKILL_RUNNER_TRUSTED_ENV_ALLOWLIST",
            " ENVREG_TEST_PIP_INDEX_URL , BRUTOR_SKILL_RUNNER_JWT_SECRET,,ENVREG_TEST_ABSENT",
        )]);
        let work = std::env::temp_dir();
        let trusted = build_subprocess_env(false, &work, &config.trusted_env);
        assert_eq!(
            trusted.get("ENVREG_TEST_PIP_INDEX_URL").map(String::as_str),
            Some("https://pypi.internal/simple")
        );
        assert!(!trusted.contains_key("ENVREG_TEST_NOT_LISTED"));
        assert!(!trusted.contains_key("ENVREG_TEST_ABSENT"));
        assert!(
            !trusted.contains_key("BRUTOR_SKILL_RUNNER_JWT_SECRET"),
            "the allowlist cannot hand the token secret to a script"
        );
        let sandbox = build_subprocess_env(true, &work, &config.trusted_env);
        assert!(!sandbox.contains_key("ENVREG_TEST_PIP_INDEX_URL"));

        // Without the allowlist the same variable stays in the runner.
        let plain = build_subprocess_env(false, &work, &config_for(&[]).trusted_env);
        assert!(!plain.contains_key("ENVREG_TEST_PIP_INDEX_URL"));
        unsafe {
            std::env::remove_var("ENVREG_TEST_PIP_INDEX_URL");
            std::env::remove_var("ENVREG_TEST_NOT_LISTED");
        }
    }

    #[tokio::test]
    async fn env_scrub_blocks_proxy_secrets_even_in_trusted_mode() {
        // Set a proxy-shaped secret in the runner's env; the
        // subprocess must NOT see it even though we're in TRUSTED
        // mode (which inherits parent env).
        // SAFETY: tests run in the same process; setenv affects all
        // future spawned procs, which is what we want here.
        unsafe {
            std::env::set_var("JWT_SECRET", "DO-NOT-LEAK-ME");
            std::env::set_var("BRUTOR_SKILL_RUNNER_JWT_SECRET", "ALSO-DO-NOT-LEAK");
        }

        let mut r = req("Run [leak](leak.sh).");
        r.scripts.push(ScriptInput {
            filename: "leak.sh".into(),
            language: "BASH".into(),
            execution_mode: "TRUSTED".into(),
            script_content:
                r#"echo "JWT=${JWT_SECRET:-absent} SR=${BRUTOR_SKILL_RUNNER_JWT_SECRET:-absent}""#
                    .into(),
        });
        let resp = execute(r, &ExecutorTuning::default(), &std::env::temp_dir()).await;
        let stdout = &resp.script_results.get("leak.sh").unwrap().stdout;
        assert!(
            stdout.contains("JWT=absent") && stdout.contains("SR=absent"),
            "secrets leaked into subprocess env: {stdout}"
        );

        unsafe {
            std::env::remove_var("JWT_SECRET");
            std::env::remove_var("BRUTOR_SKILL_RUNNER_JWT_SECRET");
        }
    }

    #[tokio::test]
    async fn asset_template_rendering_uses_context() {
        let mut r = req("Final: [template](out.tpl)");
        r.assets.push(AssetInput {
            filename: "out.tpl".into(),
            content: "Hello {{skill_name}} v{{skill_version}}".into(),
        });
        let resp = execute(r, &ExecutorTuning::default(), &std::env::temp_dir()).await;
        assert!(resp.success);
        assert_eq!(resp.output, "Hello Test v1.0");
    }
}
