// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Per-surface override resolver.
//!
//! Every built-in guardrail check carries a single scalar
//! `<check>_action` / `<check>_provider` / `<check>_threshold`
//! column. There is also a nullable JSONB column per
//! check that lets operators override those scalars on a
//! per-surface basis:
//!
//! ```json
//! "pii_per_surface": {
//!     "chat_input":  {"action": "redact"},
//!     "chat_output": {"action": "block", "provider": "presidio"}
//! }
//! ```
//!
//! This module is the single dispatch-time read site. Every
//! orchestrator check call resolves `(check, surface, field)` →
//! effective value via the helpers below. The resolution order
//! is identical to the Python helper in
//! `brutor-gateway-control/app/services/guardrails/per_surface.py`,
//! so cross-language round-trip tests pin the contract.
//!
//! Resolution:
//!   1. `<check>_per_surface[surface][field]` — operator override
//!   2. Scalar `<check>_<field>` column — config-level default
//!   3. Built-in default (`warn` for action, `built_in` for
//!      provider, 0.8 for threshold) — last-resort
//!
//! The resolver never panics on malformed data. A corrupt JSON
//! shape (non-object root, surface entry that's not an object)
//! falls through to the scalar — never killing the dispatch path.

use serde_json::Value;

use crate::models::db::GuardrailConfig;

/// Reserved surface tokens. Match Python's `ALLOWED_CHECK_SURFACES`
/// in `app/api/v1/models/admin/guardrail.py` exactly. Per-mode
/// surfaces only — operators configure each route directly.
pub const VALID_SURFACE_TOKENS: &[&str] = &[
    // Per-mode LLM surfaces.
    "chat_input",
    "chat_output",
    "embeddings_input",
    "image_gen_input",
    "image_gen_output",
    "audio_tts_input",
    "audio_tts_output",
    "audio_stt_input",
    "audio_stt_output",
    "video_gen_input",
    "video_gen_output",
    "batch_input",
    "batch_output",
    "moderation_input",
    // Non-LLM surfaces.
    "mcp_input",
    "mcp_output",
    // `mcp_registration` runs at server-bind time, not per-request.
    // It still needs to validate when an operator writes it into a
    // per-check *_surfaces array via PATCH — otherwise the Python
    // validator accepts it but the Rust validator rejects, and the
    // config round-trip silently drops the entry.
    "mcp_registration",
    "skill_input",
    "skill_output",
    "a2a_inbound",
    "a2a_outbound",
];

/// Per-mode LLM input surfaces. Mirror of Python's
/// `LLM_INPUT_MODE_SURFACES`. Order MUST match Python so JSONB array
/// equality holds across the schema-drift CI gate.
pub const LLM_INPUT_MODE_SURFACES: &[&str] = &[
    "chat_input",
    "embeddings_input",
    "image_gen_input",
    "audio_tts_input",
    "audio_stt_input",
    "video_gen_input",
    "batch_input",
    "moderation_input",
];

/// Per-mode LLM surfaces that produce text output today. The image /
/// audio_tts / video output surfaces exist as gate columns but are
/// behind future NSFW classifiers — running text-based checks on them
/// is a no-op until those land.
pub const LLM_TEXT_OUTPUT_MODE_SURFACES: &[&str] =
    &["chat_output", "audio_stt_output", "batch_output"];

/// Built-in check names. Used for the per-check column dispatch
/// in `per_surface_json_for`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Check {
    Pii,
    PromptInjection,
    Jailbreak,
    ToxicContent,
    Secrets,
    BannedWords,
    BannedPatterns,
}

impl Check {
    /// Map the enum to the JSONB column on the config row.
    pub fn per_surface_json(self, c: &GuardrailConfig) -> Option<&Value> {
        match self {
            Check::Pii => c.pii_per_surface.as_ref(),
            Check::PromptInjection => c.prompt_injection_per_surface.as_ref(),
            Check::Jailbreak => c.jailbreak_per_surface.as_ref(),
            Check::ToxicContent => c.toxic_content_per_surface.as_ref(),
            Check::Secrets => c.secrets_per_surface.as_ref(),
            Check::BannedWords => c.banned_words_per_surface.as_ref(),
            Check::BannedPatterns => c.banned_patterns_per_surface.as_ref(),
        }
    }
}

/// Look up the per-surface override entry for `(check, surface)`.
/// Returns the inner object or `None` when the column is NULL,
/// the surface key is missing, or the entry isn't an object.
fn entry<'a>(
    config: &'a GuardrailConfig,
    check: Check,
    surface: &str,
) -> Option<&'a serde_json::Map<String, Value>> {
    let raw = check.per_surface_json(config)?;
    let map = raw.as_object()?;
    let entry = map.get(surface)?;
    entry.as_object()
}

/// Resolve the effective ACTION for `(check, surface)`. Returns
/// `Option<String>` rather than imposing a default — `None` means
/// "fall through to the legacy code path", which for the orchestrator
/// dispatches into `Action::parse(None) → Action::Block`. This keeps
/// pre-a075 wire semantics identical when no override is set.
///
/// The override path returns `Some(<override action>)`; the scalar
/// path returns the scalar if set, else None.
pub fn action_for<'a>(
    config: &'a GuardrailConfig,
    check: Check,
    surface: &str,
    scalar: Option<&'a str>,
) -> Option<String> {
    if let Some(e) = entry(config, check, surface) {
        if let Some(Value::String(s)) = e.get("action") {
            return Some(s.to_string());
        }
    }
    scalar.map(|s| s.to_string())
}

/// Resolve the effective PROVIDER for `(check, surface)`. Same
/// `Option<String>` semantics as `action_for` — `None` lets the
/// caller's legacy match-arm decide (e.g. fall into the OpenAI
/// Moderation builder for toxic-content when no provider is set).
pub fn provider_for<'a>(
    config: &'a GuardrailConfig,
    check: Check,
    surface: &str,
    scalar: Option<&'a str>,
) -> Option<String> {
    if let Some(e) = entry(config, check, surface) {
        if let Some(Value::String(s)) = e.get("provider") {
            return Some(s.to_string());
        }
    }
    scalar.map(|s| s.to_string())
}

/// Resolve the effective THRESHOLD for `(check, surface)`. Used
/// by toxic-content and secrets. Returns the scalar (or 0.8 if
/// no scalar) when no per-surface override is set.
pub fn threshold_for(
    config: &GuardrailConfig,
    check: Check,
    surface: &str,
    scalar: Option<f64>,
) -> f64 {
    if let Some(e) = entry(config, check, surface) {
        if let Some(v) = e.get("threshold") {
            if let Some(f) = v.as_f64() {
                return f;
            }
        }
    }
    scalar.unwrap_or(0.8)
}

/// Resolve the effective THRESHOLD for `(check, surface)` WITHOUT
/// substituting a system default. Returns `None` when neither the
/// per-surface override nor the scalar carries a value — the caller
/// is then free to fall through to the detector's own default
/// (e.g. Presidio's `/analyze.score_threshold` default of ~0.5).
///
/// PII uses this variant: forwarding "absent" to Presidio is
/// distinct from "0.5 specifically" — operators who haven't set a
/// threshold should get whatever the Presidio image was built with.
pub fn threshold_for_opt(
    config: &GuardrailConfig,
    check: Check,
    surface: &str,
    scalar: Option<f64>,
) -> Option<f64> {
    if let Some(e) = entry(config, check, surface) {
        if let Some(v) = e.get("threshold") {
            if let Some(f) = v.as_f64() {
                return Some(f);
            }
        }
    }
    scalar
}

/// Resolve the effective ENABLED flag for `(check, surface)`. The
/// per-surface override can flip a globally-enabled check OFF on
/// a specific surface (or turn a globally-disabled check ON,
/// although that's a less common operator move). Returns the
/// scalar fallback if the override doesn't carry an `enabled`
/// key.
pub fn enabled_for(config: &GuardrailConfig, check: Check, surface: &str, scalar: bool) -> bool {
    if let Some(e) = entry(config, check, surface) {
        if let Some(Value::Bool(b)) = e.get("enabled") {
            return *b;
        }
    }
    scalar
}

/// Resolve the effective banned-WORDS list for the given surface.
/// Per-surface entry's `words` array REPLACES the global
/// `banned_words` list for that surface — matches Python's
/// `resolve_words` exactly. Returns an empty Vec if neither is set.
pub fn words_for(config: &GuardrailConfig, surface: &str) -> Vec<String> {
    if let Some(e) = entry(config, Check::BannedWords, surface) {
        if let Some(Value::Array(arr)) = e.get("words") {
            return arr
                .iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect();
        }
    }
    json_string_array(&config.banned_words)
}

/// Resolve the effective banned-PATTERNS list for the given
/// surface. Replacement semantics — mirrors `words_for`.
pub fn patterns_for(config: &GuardrailConfig, surface: &str) -> Vec<String> {
    if let Some(e) = entry(config, Check::BannedPatterns, surface) {
        if let Some(Value::Array(arr)) = e.get("patterns") {
            return arr
                .iter()
                .filter_map(|v| v.as_str().map(|s| s.to_string()))
                .collect();
        }
    }
    json_string_array(&config.banned_patterns)
}

/// Helper: convert an `Option<Value>` carrying a JSON array of
/// strings into a `Vec<String>`. Non-array / non-string entries
/// silently dropped (defensive — a corrupt row shouldn't crash
/// the dispatch).
fn json_string_array(v: &Option<Value>) -> Vec<String> {
    match v {
        Some(Value::Array(arr)) => arr
            .iter()
            .filter_map(|v| v.as_str().map(|s| s.to_string()))
            .collect(),
        _ => Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::db::GuardrailConfig;
    use serde_json::json;

    fn empty_config() -> GuardrailConfig {
        // Fill the FromRow-derived struct with neutral defaults.
        // We only mutate the fields each test cares about.
        GuardrailConfig {
            id: String::new(),
            tenant_id: String::new(),
            name: String::new(),
            enabled: false,
            is_default: false,
            priority: None,
            description: None,
            chat_input_enabled: Some(false),
            chat_output_enabled: Some(false),
            streaming_output_mode: None,
            embeddings_input_enabled: Some(false),
            image_gen_input_enabled: Some(false),
            image_gen_output_enabled: Some(false),
            audio_tts_input_enabled: Some(false),
            audio_tts_output_enabled: Some(false),
            audio_stt_input_enabled: Some(false),
            audio_stt_output_enabled: Some(false),
            video_gen_input_enabled: Some(false),
            video_gen_output_enabled: Some(false),
            batch_input_enabled: Some(false),
            batch_output_enabled: Some(false),
            moderation_input_enabled: Some(false),
            mcp_input_enabled: Some(false),
            mcp_output_enabled: Some(false),
            mcp_registration_enabled: Some(false),
            skill_input_enabled: Some(false),
            skill_output_enabled: Some(false),
            a2a_inbound_enabled: Some(false),
            a2a_outbound_enabled: Some(false),
            pii_detection_enabled: false,
            pii_action: None,
            pii_provider: None,
            pii_categories: None,
            pii_threshold: None,
            prompt_injection_enabled: false,
            prompt_injection_action: None,
            prompt_injection_sensitivity: None,
            prompt_injection_provider: None,
            jailbreak_detection_enabled: false,
            jailbreak_action: None,
            jailbreak_provider: None,
            toxic_content_enabled: false,
            toxic_content_action: None,
            toxic_content_threshold: None,
            toxic_content_provider: None,
            toxic_content_category_thresholds: None,
            banned_words: None,
            banned_patterns: None,
            banned_words_match_mode: None,
            pii_surfaces: None,
            prompt_injection_surfaces: None,
            jailbreak_surfaces: None,
            toxic_content_surfaces: None,
            banned_words_surfaces: None,
            banned_patterns_surfaces: None,
            secrets_detection_enabled: false,
            secrets_action: None,
            secrets_categories: None,
            secrets_surfaces: None,
            secrets_threshold: None,
            pii_per_surface: None,
            prompt_injection_per_surface: None,
            jailbreak_per_surface: None,
            toxic_content_per_surface: None,
            secrets_per_surface: None,
            banned_words_per_surface: None,
            banned_patterns_per_surface: None,
            providers: None,
            fail_open_overrides: None,
            max_check_timeout_ms: None,
            fail_open: false,
            log_all_checks: false,
            scope_mcp_server_id: None,
            scope_tool_name: None,
            created_at: None,
            updated_at: None,
        }
    }

    #[test]
    fn scalar_fallback_when_no_overrides() {
        let mut c = empty_config();
        c.pii_action = Some("redact".into());
        assert_eq!(
            action_for(&c, Check::Pii, "chat_input", c.pii_action.as_deref()).as_deref(),
            Some("redact")
        );
    }

    #[test]
    fn override_wins_over_scalar() {
        let mut c = empty_config();
        c.pii_action = Some("redact".into());
        c.pii_per_surface = Some(json!({
            "chat_output": {"action": "block"}
        }));
        assert_eq!(
            action_for(&c, Check::Pii, "chat_input", c.pii_action.as_deref()).as_deref(),
            Some("redact")
        );
        assert_eq!(
            action_for(&c, Check::Pii, "chat_output", c.pii_action.as_deref()).as_deref(),
            Some("block")
        );
    }

    #[test]
    fn override_provider_isolated_from_action() {
        let mut c = empty_config();
        c.pii_action = Some("redact".into());
        c.pii_provider = Some("built_in".into());
        c.pii_per_surface = Some(json!({
            "chat_output": {"provider": "presidio"}
        }));
        // Action falls through to scalar — only provider was overridden.
        assert_eq!(
            action_for(&c, Check::Pii, "chat_output", c.pii_action.as_deref()).as_deref(),
            Some("redact")
        );
        assert_eq!(
            provider_for(&c, Check::Pii, "chat_output", c.pii_provider.as_deref()).as_deref(),
            Some("presidio")
        );
        // Other surface: both fields use the scalar.
        assert_eq!(
            provider_for(&c, Check::Pii, "mcp_input", c.pii_provider.as_deref()).as_deref(),
            Some("built_in")
        );
    }

    #[test]
    fn threshold_override_for_toxic() {
        let mut c = empty_config();
        c.toxic_content_threshold = Some(0.5);
        c.toxic_content_per_surface = Some(json!({
            "chat_input": {"threshold": 0.7}
        }));
        assert!(
            (threshold_for(
                &c,
                Check::ToxicContent,
                "chat_input",
                c.toxic_content_threshold
            ) - 0.7)
                .abs()
                < 1e-9
        );
        assert!(
            (threshold_for(
                &c,
                Check::ToxicContent,
                "chat_output",
                c.toxic_content_threshold
            ) - 0.5)
                .abs()
                < 1e-9
        );
    }

    #[test]
    fn enabled_override_can_disable_on_one_surface() {
        let mut c = empty_config();
        c.pii_per_surface = Some(json!({
            "mcp_output": {"enabled": false}
        }));
        // Scalar TRUE — override forces FALSE on mcp_output only.
        assert!(enabled_for(&c, Check::Pii, "chat_input", true));
        assert!(!enabled_for(&c, Check::Pii, "mcp_output", true));
    }

    #[test]
    fn banned_words_per_surface_replaces_global() {
        let mut c = empty_config();
        c.banned_words = Some(json!(["secret", "password"]));
        c.banned_words_per_surface = Some(json!({
            "mcp_input": {"words": ["DELETE FROM", "DROP TABLE"]}
        }));
        // Per-surface entry on mcp_input REPLACES the global list.
        assert_eq!(
            words_for(&c, "mcp_input"),
            vec!["DELETE FROM".to_string(), "DROP TABLE".to_string()]
        );
        // Other surfaces still get the global list.
        assert_eq!(
            words_for(&c, "chat_input"),
            vec!["secret".to_string(), "password".to_string()]
        );
    }

    #[test]
    fn banned_patterns_per_surface_replaces_global() {
        let mut c = empty_config();
        c.banned_patterns = Some(json!(["foo.*", "bar.*"]));
        c.banned_patterns_per_surface = Some(json!({
            "chat_output": {"patterns": ["competitor_a", "competitor_b"]}
        }));
        assert_eq!(
            patterns_for(&c, "chat_output"),
            vec!["competitor_a".to_string(), "competitor_b".to_string()]
        );
        assert_eq!(
            patterns_for(&c, "chat_input"),
            vec!["foo.*".to_string(), "bar.*".to_string()]
        );
    }

    #[test]
    fn malformed_per_surface_falls_back_gracefully() {
        let mut c = empty_config();
        c.pii_action = Some("redact".into());
        // String instead of object — should NOT panic; should fall through.
        c.pii_per_surface = Some(json!("not_an_object"));
        assert_eq!(
            action_for(&c, Check::Pii, "chat_input", c.pii_action.as_deref()).as_deref(),
            Some("redact")
        );

        // Object but the per-surface entry is a string.
        c.pii_per_surface = Some(json!({"chat_input": "not_an_object"}));
        assert_eq!(
            action_for(&c, Check::Pii, "chat_input", c.pii_action.as_deref()).as_deref(),
            Some("redact")
        );

        // Object, entry is object, but action key is the wrong type.
        c.pii_per_surface = Some(json!({"chat_input": {"action": 42}}));
        assert_eq!(
            action_for(&c, Check::Pii, "chat_input", c.pii_action.as_deref()).as_deref(),
            Some("redact")
        );
    }

    #[test]
    fn unknown_surface_returns_scalar() {
        let mut c = empty_config();
        c.pii_action = Some("redact".into());
        c.pii_per_surface = Some(json!({"some_made_up_surface": {"action": "block"}}));
        // Lookup against a real surface gets scalar; lookup against the
        // bogus surface ALSO gets scalar (the override exists in the
        // JSON but the orchestrator never asks for that surface).
        assert_eq!(
            action_for(&c, Check::Pii, "chat_input", c.pii_action.as_deref()).as_deref(),
            Some("redact")
        );
        assert_eq!(
            action_for(
                &c,
                Check::Pii,
                "some_made_up_surface",
                c.pii_action.as_deref()
            )
            .as_deref(),
            Some("block")
        );
    }
}
