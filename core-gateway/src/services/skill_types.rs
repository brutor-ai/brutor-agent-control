// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Domain types for skill execution results.
//!
//! These describe the *outcome* of a skill execution and how it maps
//! into the MCP `tools/call` response shape. Kept in a small,
//! dependency-free module so both the runner client and the MCP
//! call-site can share them without depending on the long-removed
//! in-proxy executor.
//!
//! The actual subprocess execution lives in `brutor-skill-runner` —
//! the runner returns a wire-shape `ExecuteResponse` which the runner
//! client maps into [`SkillExecutionOutput`] (see
//! `skill_runner_client::to_legacy_output`).

use std::collections::HashMap;

use serde_json::Value;

/// Complete output of a skill execution.
///
/// `success` reflects the overall outcome — true only when every
/// script returned exit 0 AND no `Unresolved` references were
/// reported by the runner. Per-script detail lives in
/// `script_results`.
#[allow(dead_code)]
pub struct SkillExecutionOutput {
    pub success: bool,
    pub output: String,
    pub error: Option<String>,

    // Intermediate results for audit / debugging.
    pub script_results: HashMap<String, ScriptResult>,
    pub loaded_assets: HashMap<String, String>,
    pub loaded_resources: HashMap<String, String>,
    pub unresolved_refs: Vec<String>,
}

impl SkillExecutionOutput {
    /// Convert to MCP content blocks for a `tools/call` response.
    ///
    /// Preference order:
    ///   1. Primary `output` if non-empty.
    ///   2. Concatenated per-script output otherwise.
    ///   3. The recorded `error` (or a default placeholder) if even
    ///      that's empty — guarantees the MCP client always gets at
    ///      least one text block.
    pub fn to_content_blocks(&self) -> Vec<Value> {
        let mut blocks = Vec::new();

        if !self.output.is_empty() {
            blocks.push(serde_json::json!({"type": "text", "text": self.output}));
        }

        if self.output.is_empty() && !self.script_results.is_empty() {
            for (filename, result) in &self.script_results {
                blocks.push(serde_json::json!({
                    "type": "text",
                    "text": format!("[{filename}]\n{}", result.output()),
                }));
            }
        }

        if blocks.is_empty() {
            let text = self
                .error
                .as_deref()
                .unwrap_or("Skill execution completed with no output.");
            blocks.push(serde_json::json!({"type": "text", "text": text}));
        }

        blocks
    }
}

/// Outcome of a single script's subprocess execution.
pub struct ScriptResult {
    pub success: bool,
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
    pub duration_ms: f64,
    pub error: Option<String>,
    pub language: Option<String>,
    pub execution_mode: Option<String>,
}

impl ScriptResult {
    /// Primary output — stdout (trimmed) for success, error description
    /// for failure. Used when building MCP content blocks from
    /// per-script output.
    pub fn output(&self) -> String {
        if self.success {
            self.stdout.trim().to_string()
        } else {
            self.error.clone().unwrap_or_else(|| {
                let s = self.stderr.trim();
                if s.is_empty() {
                    format!("Script exited with code {}", self.exit_code)
                } else {
                    s.to_string()
                }
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_success() -> SkillExecutionOutput {
        SkillExecutionOutput {
            success: true,
            output: "primary text".into(),
            error: None,
            script_results: HashMap::new(),
            loaded_assets: HashMap::new(),
            loaded_resources: HashMap::new(),
            unresolved_refs: vec![],
        }
    }

    #[test]
    fn to_content_blocks_uses_primary_output_when_present() {
        let out = sample_success();
        let blocks = out.to_content_blocks();
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0]["type"], "text");
        assert_eq!(blocks[0]["text"], "primary text");
    }

    #[test]
    fn to_content_blocks_falls_back_to_per_script_when_output_empty() {
        let mut script_results = HashMap::new();
        script_results.insert(
            "first.py".to_string(),
            ScriptResult {
                success: true,
                stdout: "hello\n".into(),
                stderr: String::new(),
                exit_code: 0,
                duration_ms: 0.0,
                error: None,
                language: Some("PYTHON".into()),
                execution_mode: Some("SANDBOX".into()),
            },
        );
        let out = SkillExecutionOutput {
            success: true,
            output: String::new(),
            error: None,
            script_results,
            loaded_assets: HashMap::new(),
            loaded_resources: HashMap::new(),
            unresolved_refs: vec![],
        };
        let blocks = out.to_content_blocks();
        assert_eq!(blocks.len(), 1);
        let text = blocks[0]["text"].as_str().unwrap();
        assert!(text.starts_with("[first.py]"));
        assert!(text.contains("hello"));
    }

    #[test]
    fn to_content_blocks_emits_error_when_everything_else_empty() {
        let out = SkillExecutionOutput {
            success: false,
            output: String::new(),
            error: Some("blew up".into()),
            script_results: HashMap::new(),
            loaded_assets: HashMap::new(),
            loaded_resources: HashMap::new(),
            unresolved_refs: vec![],
        };
        let blocks = out.to_content_blocks();
        assert_eq!(blocks.len(), 1);
        assert_eq!(blocks[0]["text"], "blew up");
    }

    #[test]
    fn to_content_blocks_emits_default_when_error_also_missing() {
        let out = SkillExecutionOutput {
            success: false,
            output: String::new(),
            error: None,
            script_results: HashMap::new(),
            loaded_assets: HashMap::new(),
            loaded_resources: HashMap::new(),
            unresolved_refs: vec![],
        };
        let blocks = out.to_content_blocks();
        assert_eq!(blocks.len(), 1);
        let text = blocks[0]["text"].as_str().unwrap();
        assert!(text.contains("no output"), "got {text:?}");
    }

    #[test]
    fn script_result_output_returns_trimmed_stdout_on_success() {
        let sr = ScriptResult {
            success: true,
            stdout: "  result  \n".into(),
            stderr: String::new(),
            exit_code: 0,
            duration_ms: 0.0,
            error: None,
            language: None,
            execution_mode: None,
        };
        assert_eq!(sr.output(), "result");
    }

    #[test]
    fn script_result_output_falls_back_to_stderr_on_failure() {
        let sr = ScriptResult {
            success: false,
            stdout: String::new(),
            stderr: " boom \n".into(),
            exit_code: 1,
            duration_ms: 0.0,
            error: None,
            language: None,
            execution_mode: None,
        };
        assert_eq!(sr.output(), "boom");
    }

    #[test]
    fn script_result_output_falls_back_to_exit_code_when_no_signal() {
        let sr = ScriptResult {
            success: false,
            stdout: String::new(),
            stderr: String::new(),
            exit_code: 7,
            duration_ms: 0.0,
            error: None,
            language: None,
            execution_mode: None,
        };
        assert_eq!(sr.output(), "Script exited with code 7");
    }
}
