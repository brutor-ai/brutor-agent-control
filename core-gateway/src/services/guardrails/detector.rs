// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Detector trait + outcome types
//!
//! This module defines the production guardrails contract every check
//! implements. The legacy `check_input` / `check_output` orchestrator in
//! `super::mod` returns `Result<(), GuardrailViolation>`; that contract
//! is preserved so the rest of the proxy stays
//! green while individual detectors migrate; the
//! orchestrator works in [`CheckOutcome`].
//!
//! ## Why a trait?
//!
//! Each guardrail check (PII, prompt injection, jailbreak, toxic
//! content, banned words, banned regex) used to live as a free function
//! with bespoke error handling. That made it impossible to:
//!
//!   * register new detectors without editing the orchestrator;
//!   * exercise a single detector in isolation in tests;
//!   * compose multiple detectors and apply per-detector actions
//!     (block / redact / warn) deterministically;
//!   * gate enabling at the admin API layer based on whether the
//!     detector's provider config is complete.
//!
//! Putting every check behind [`Detector`] solves all four. The
//! orchestrator becomes a small loop that walks enabled detectors and
//! collapses their results into a single [`CheckOutcome`].
//!
//! ## Action semantics
//!
//! Each [`Detection::Hit`] carries enough context for the orchestrator
//! to apply the configured action without re-running the detector:
//!
//!   * `block` → orchestrator returns [`CheckOutcome::Block`] with the
//!     [`Violation`] populated. Caller short-circuits the request.
//!   * `redact` → orchestrator rewrites the content using each
//!     [`Span::redaction`] and returns [`CheckOutcome::Redacted`] with
//!     the rewritten string. Caller swaps the request body before
//!     forwarding upstream. Only PII supports redact today; other
//!     detectors emit `Span::redaction = None`.
//!   * `warn` → orchestrator emits a system alert, records the metric,
//!     and returns [`CheckOutcome::Warn`] with the [`Violation`]. The
//!     caller continues with the original content unchanged.
//!
//! ## Provider failures (fail-open vs fail-closed)
//!
//! [`DetectorError`] is what a detector returns when its provider call
//! itself fails (timeout, 5xx, malformed JSON, missing api_key). The
//! orchestrator translates that into either:
//!
//!   * `Pass` — when the policy's `fail_open` flag is true (the
//!     default), or
//!   * `Block` — when `fail_open` is false, or when the per-check
//!     `fail_open_overrides` map says false for this check (PII is the
//!     usual override target since silently letting PII through on a
//!     provider hiccup defeats the point).
//!
//! Either way the failure is logged at WARN per the proxy error
//! taxonomy memory rule.

use std::fmt;

use crate::services::guardrails::GuardrailViolation;

// ---------------------------------------------------------------------------
// Detector — the trait every check implements
// ---------------------------------------------------------------------------

/// A single guardrail check. Implementors are stateless wrappers
/// around a built-in detection routine (PII regex bank, prompt-injection
/// pattern ensemble) or an external-provider HTTP client (Lakera Guard,
/// OpenAI Moderation).
///
/// The contract is intentionally narrow: detectors don't decide what to
/// do on a hit — they describe **what** they found. The orchestrator
/// owns action policy. This keeps detectors trivially testable and
/// makes per-check action overrides (block on PII / warn on jailbreak,
/// say) a pure orchestrator concern.
#[async_trait::async_trait]
pub trait Detector: Send + Sync {
    /// Stable identifier used in logs, metrics, alerts, and the
    /// `GuardrailViolation::check` field surfaced to the admin UI's
    /// audit feed. Snake-case, namespaced where helpful: `pii`,
    /// `prompt_injection`, `jailbreak`, `toxic_content`, `banned_words`,
    /// `banned_pattern`.
    fn name(&self) -> &'static str;

    /// Run the check against `content`. Return:
    ///
    ///   * `Ok(Detection::Pass)` — content cleared this check.
    ///   * `Ok(Detection::Hit { … })` — content matched. The
    ///     orchestrator decides the action.
    ///   * `Err(DetectorError)` — the detector itself failed (provider
    ///     timeout, bad JSON, missing credentials). The orchestrator
    ///     applies the policy's fail-open / fail-closed rule.
    async fn detect(&self, content: &str) -> Result<Detection, DetectorError>;
}

// ---------------------------------------------------------------------------
// Detection result
// ---------------------------------------------------------------------------

/// What a single detector returns from `detect`. A clean miss is
/// `Pass`; a positive is `Hit` with one or more matched spans.
#[derive(Debug, Clone, PartialEq)]
pub enum Detection {
    /// No match — content cleared this detector.
    Pass,
    /// Content matched. `category` and `matches` describe the hit;
    /// `score` carries the provider's confidence when available
    /// (Lakera, OpenAI Moderation) and `None` for binary built-in
    /// checks (banned words, banned regex).
    Hit {
        /// What flavour of hit this is — `email` / `ssn` for PII,
        /// `prompt_injection` / `jailbreak` for the respective
        /// detectors, `banned_words` / `banned_pattern` for the
        /// per-tenant lists. Surfaces to the admin audit feed.
        category: String,
        /// Locations in `content` that matched. Always non-empty when
        /// the variant is `Hit`. For external providers that don't
        /// return offsets (Lakera, OpenAI Moderation) the slice is a
        /// single `Span` covering the whole input.
        matches: Vec<Span>,
        /// Confidence in [0.0, 1.0]; `None` when the provider doesn't
        /// expose one. Used by the orchestrator to apply per-category
        /// thresholds (toxic content) without the detector having to
        /// know the policy's threshold map.
        score: Option<f64>,
    },
}

/// One matched location inside the content, with an optional
/// redaction string. The orchestrator stitches the rewritten content
/// for `redact` actions by walking the span list in reverse offset
/// order and substituting `redaction` where present.
#[derive(Debug, Clone, PartialEq)]
pub struct Span {
    /// Byte offset of the match start, inclusive.
    pub start: usize,
    /// Byte offset of the match end, exclusive.
    pub end: usize,
    /// Replacement string for `redact` actions. `None` when this
    /// detector doesn't support redaction (everything except PII
    /// today). The orchestrator silently falls back to `block` when
    /// action is `redact` but the detector returns no replacements.
    pub redaction: Option<String>,
}

impl Span {
    /// Convenience: a single span covering the whole input. Used by
    /// detectors that don't return per-match offsets (external
    /// providers).
    pub fn whole(content: &str) -> Self {
        Self {
            start: 0,
            end: content.len(),
            redaction: None,
        }
    }
}

// ---------------------------------------------------------------------------
// Detector error
// ---------------------------------------------------------------------------

/// Why a detector couldn't reach a verdict. Note this is **not** a
/// "content failed the check" signal — that's `Detection::Hit`. This
/// is for the detector itself failing.
#[derive(Debug)]
pub enum DetectorError {
    /// The detector requires a provider config (api_key, endpoint,
    /// etc.) that is missing or empty. This should be caught by the
    /// admin API validator before the policy is saved, but the
    /// detector still has to handle it defensively at runtime.
    MissingProviderConfig {
        provider: &'static str,
        field: &'static str,
    },
    /// A secret of the provider entry is stored as a ciphertext the gateway
    /// could not decrypt. The check cannot be
    /// built: the stored value is never used as the secret.
    UnreadableSecret {
        provider: &'static str,
        field: &'static str,
    },
    /// HTTP request to the provider timed out.
    Timeout { provider: &'static str },
    /// The check exceeded the policy's `max_check_timeout_ms` budget and the
    /// orchestrator cut it off.
    ///
    /// Deliberately distinct from [`DetectorError::Timeout`]: that one says
    /// the provider was slow, this one says *we* stopped waiting. They lead
    /// an operator to different places — the vendor, or the budget field.
    BudgetExceeded {
        provider: &'static str,
        budget_ms: u128,
    },
    /// Provider returned a 4xx / 5xx response.
    ProviderHttp {
        provider: &'static str,
        status: u16,
        body_excerpt: String,
    },
    /// Provider returned a 200 but the JSON shape didn't match the
    /// expected schema. Usually a provider-side regression.
    MalformedResponse {
        provider: &'static str,
        reason: String,
    },
    /// Anything else that prevents a verdict — connect-refused, DNS
    /// failure, etc.
    Other {
        provider: &'static str,
        message: String,
    },
}

/// The key `GuardrailsCache` adds to a provider entry whose secret did not
/// decrypt; its value is the name of the field. Not a configuration key: a
/// stored entry that carries it is unbuildable all the same.
pub(crate) const UNREADABLE_SECRET_KEY: &str = "__unreadable_secret";

/// The first thing every provider detector's `build` asks its entry: an
/// entry marked by `GuardrailsCache` as holding a secret that did not
/// decrypt is not built.
pub(crate) fn refuse_unreadable_secret(
    entry: &serde_json::Value,
    provider: &'static str,
) -> Result<(), DetectorError> {
    let Some(marked) = entry.get(UNREADABLE_SECRET_KEY) else {
        return Ok(());
    };
    let field = match marked.as_str() {
        Some("api_key") => "api_key",
        Some("aws_secret_access_key") => "aws_secret_access_key",
        Some("aws_session_token") => "aws_session_token",
        _ => "secret",
    };
    Err(DetectorError::UnreadableSecret { provider, field })
}

impl fmt::Display for DetectorError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DetectorError::MissingProviderConfig { provider, field } => {
                write!(
                    f,
                    "{provider}: missing provider config field `{field}` (configure on the Providers tab)"
                )
            }
            DetectorError::UnreadableSecret { provider, field } => {
                write!(
                    f,
                    "{provider}: the stored `{field}` cannot be decrypted with this gateway's \
                     ENCRYPTION_KEY (save it again on the Providers tab)"
                )
            }
            DetectorError::Timeout { provider } => {
                write!(f, "{provider}: request timed out")
            }
            DetectorError::BudgetExceeded {
                provider,
                budget_ms,
            } => {
                write!(
                    f,
                    "{provider}: exceeded the policy's {budget_ms}ms check budget \
                     (raise `max_check_timeout_ms` or disable this check)"
                )
            }
            DetectorError::ProviderHttp {
                provider,
                status,
                body_excerpt,
            } => {
                write!(f, "{provider}: HTTP {status} — {body_excerpt}")
            }
            DetectorError::MalformedResponse { provider, reason } => {
                write!(f, "{provider}: malformed response — {reason}")
            }
            DetectorError::Other { provider, message } => {
                write!(f, "{provider}: {message}")
            }
        }
    }
}

impl std::error::Error for DetectorError {}

/// Refuse a provider endpoint the outbound policy denies. Guardrail
/// endpoints are tenant-configured (`providers[].endpoint`); the policy
/// resolver covers hostnames, but an IP literal (`http://169.254.169.254/`)
/// would be called directly. Every detector `build()` that takes an
/// endpoint calls this, so the refusal surfaces as a build warning and the
/// check fails closed like any other unbuildable detector.
pub fn check_endpoint(provider: &'static str, endpoint: &str) -> Result<(), DetectorError> {
    crate::services::outbound_policy::check_url(endpoint).map_err(|denied| {
        tracing::warn!(provider, host = %denied.host, reason = denied.reason, "guardrail endpoint refused by outbound policy");
        DetectorError::Other {
            provider,
            message: denied.to_string(),
        }
    })
}

impl DetectorError {
    /// The provider name, surfaced to logs / alerts so operators can
    /// jump to the right Providers tab on a recurring error.
    pub fn provider(&self) -> &'static str {
        match self {
            DetectorError::MissingProviderConfig { provider, .. }
            | DetectorError::UnreadableSecret { provider, .. }
            | DetectorError::Timeout { provider }
            | DetectorError::BudgetExceeded { provider, .. }
            | DetectorError::ProviderHttp { provider, .. }
            | DetectorError::MalformedResponse { provider, .. }
            | DetectorError::Other { provider, .. } => provider,
        }
    }
}

// ---------------------------------------------------------------------------
// Orchestrator outcome
// ---------------------------------------------------------------------------

/// What the orchestrator returns to the request-path caller after
/// running every enabled detector for a config. The plan is to
/// migrate `check_input` / `check_output` to this shape; until then
/// the legacy `Result<(), GuardrailViolation>` API stays in place so
/// existing call sites compile.
#[derive(Debug, Clone, PartialEq)]
pub enum CheckOutcome {
    /// Every detector cleared the content unchanged.
    Pass,
    /// At least one detector hit with action `redact`. `content` is
    /// the rewritten string the caller must forward upstream in place
    /// of the original. May still carry warnings (zero or more
    /// non-blocking violations); blocking violations short-circuit to
    /// `Block` and never reach this variant.
    Redacted {
        content: String,
        warnings: Vec<GuardrailViolation>,
    },
    /// At least one detector hit with action `block`. Caller returns
    /// 403 + structured error to the requester. The first blocking
    /// violation in detector-priority order is the canonical one.
    Block(GuardrailViolation),
    /// At least one detector hit but only with action `warn`. Caller
    /// continues with the original content; the orchestrator has
    /// already emitted alerts + metrics.
    Warn { warnings: Vec<GuardrailViolation> },
}

impl CheckOutcome {
    /// `true` when the caller should short-circuit (Block) or rewrite
    /// the body (Redacted). `false` for Pass and Warn.
    pub fn requires_action(&self) -> bool {
        matches!(self, CheckOutcome::Block(_) | CheckOutcome::Redacted { .. })
    }
}

// ---------------------------------------------------------------------------
// Action — what to do when a detector hits
// ---------------------------------------------------------------------------

/// Per-detector action policy. Stored as a string column on
/// `guardrail_configs` (`pii_action`, `prompt_injection_action`, …);
/// parsed into this enum at the orchestrator boundary so the rest of
/// the code path is type-safe.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    /// Reject the request with a 403 + structured violation.
    Block,
    /// Rewrite the content to scrub the matches; pass the rewritten
    /// content upstream. Falls back to `Block` when the detector emits
    /// no `redaction` strings on its spans.
    Redact,
    /// Pass the request through unchanged; emit an alert + metric so
    /// operators can audit hits.
    Warn,
}

impl Action {
    pub fn parse(value: Option<&str>) -> Self {
        match value.unwrap_or("block").to_ascii_lowercase().as_str() {
            "block" => Action::Block,
            "redact" => Action::Redact,
            "warn" => Action::Warn,
            // Unknown values fall back to `Block` as the safe default.
            // The admin API validator should reject these on save, so
            // hitting this branch indicates either a hand-edited row
            // or a schema migration regression.
            _ => Action::Block,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Action::Block => "block",
            Action::Redact => "redact",
            Action::Warn => "warn",
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    /// A guardrail endpoint naming a denied IP literal is refused.
    #[test]
    fn check_endpoint_refuses_denied_ip_literals() {
        let err =
            check_endpoint("custom_webhook", "http://169.254.169.254/check").expect_err("denied");
        assert!(matches!(
            err,
            DetectorError::Other {
                provider: "custom_webhook",
                ..
            }
        ));
        assert!(err.to_string().contains("refused by policy"), "{err}");
        assert!(check_endpoint("presidio", "http://presidio:3000/analyze").is_ok());
    }

    // ----- Span -------------------------------------------------------------

    #[test]
    fn span_whole_covers_full_content() {
        let s = Span::whole("hello");
        assert_eq!(s.start, 0);
        assert_eq!(s.end, 5);
        assert!(s.redaction.is_none());
    }

    #[test]
    fn span_whole_handles_empty() {
        let s = Span::whole("");
        assert_eq!(s.start, 0);
        assert_eq!(s.end, 0);
    }

    #[test]
    fn span_whole_handles_unicode_byte_offsets() {
        // Byte length, not char length — non-ASCII content has a
        // longer byte length than char count. Spans are byte-indexed
        // throughout because that's what `&str` slicing requires.
        let s = Span::whole("café");
        assert_eq!(s.end, 5); // 'c','a','f' = 3 bytes; 'é' = 2 bytes
    }

    // ----- Action -----------------------------------------------------------

    #[test]
    fn action_parse_known_values() {
        assert_eq!(Action::parse(Some("block")), Action::Block);
        assert_eq!(Action::parse(Some("redact")), Action::Redact);
        assert_eq!(Action::parse(Some("warn")), Action::Warn);
    }

    #[test]
    fn action_parse_case_insensitive() {
        assert_eq!(Action::parse(Some("BLOCK")), Action::Block);
        assert_eq!(Action::parse(Some("Redact")), Action::Redact);
    }

    #[test]
    fn action_parse_none_defaults_to_block() {
        // Block is the safe fallback — operators who haven't picked
        // an action yet should not silently get warn-mode (i.e.
        // unenforced).
        assert_eq!(Action::parse(None), Action::Block);
    }

    #[test]
    fn action_parse_unknown_defaults_to_block() {
        // Defensive: malformed DB rows must not relax enforcement.
        assert_eq!(Action::parse(Some("yolo")), Action::Block);
        assert_eq!(Action::parse(Some("")), Action::Block);
    }

    #[test]
    fn action_round_trip_via_as_str() {
        // Serialising back through `as_str` then re-parsing must yield
        // the same variant — covers everything the API surface emits.
        for action in [Action::Block, Action::Redact, Action::Warn] {
            assert_eq!(Action::parse(Some(action.as_str())), action);
        }
    }

    // ----- DetectorError ----------------------------------------------------

    #[test]
    fn detector_error_provider_accessor() {
        let e = DetectorError::Timeout {
            provider: "lakera_guard",
        };
        assert_eq!(e.provider(), "lakera_guard");

        let e = DetectorError::ProviderHttp {
            provider: "openai_moderation",
            status: 500,
            body_excerpt: "internal error".into(),
        };
        assert_eq!(e.provider(), "openai_moderation");
    }

    #[test]
    fn detector_error_display_includes_provider_and_context() {
        let e = DetectorError::MissingProviderConfig {
            provider: "lakera_guard",
            field: "api_key",
        };
        let s = e.to_string();
        assert!(s.contains("lakera_guard"));
        assert!(s.contains("api_key"));
    }

    #[test]
    fn detector_error_implements_std_error() {
        // `?` propagation through `Box<dyn std::error::Error>` is a
        // common pattern in the rest of the proxy — pin the impl.
        let e: Box<dyn std::error::Error> = Box::new(DetectorError::Timeout {
            provider: "lakera_guard",
        });
        assert!(e.to_string().contains("timed out"));
    }

    // ----- Detection --------------------------------------------------------

    #[test]
    fn detection_pass_is_distinct_from_hit() {
        let pass = Detection::Pass;
        let hit = Detection::Hit {
            category: "email".into(),
            matches: vec![Span::whole("test")],
            score: None,
        };
        assert_ne!(pass, hit);
    }

    // ----- CheckOutcome -----------------------------------------------------

    fn dummy_violation() -> GuardrailViolation {
        GuardrailViolation {
            config_id: "cfg-test".to_string(),
            action: "block".to_string(),
            config_name: "default".into(),
            check: "banned_words".into(),
            message: "matched: secret".into(),
            surface: "llm".into(),
        }
    }

    #[test]
    fn check_outcome_requires_action_block() {
        assert!(CheckOutcome::Block(dummy_violation()).requires_action());
    }

    #[test]
    fn check_outcome_requires_action_redacted() {
        assert!(
            CheckOutcome::Redacted {
                content: "rewritten".into(),
                warnings: vec![],
            }
            .requires_action()
        );
    }

    #[test]
    fn check_outcome_no_action_for_pass() {
        assert!(!CheckOutcome::Pass.requires_action());
    }

    #[test]
    fn check_outcome_no_action_for_warn() {
        // Warn is informational — caller continues with the original
        // content. requires_action distinguishes this from Block /
        // Redacted which both demand the caller change behaviour.
        assert!(
            !CheckOutcome::Warn {
                warnings: vec![dummy_violation()],
            }
            .requires_action()
        );
    }

    // ----- Detector trait shape --------------------------------------------

    /// Smoke test: the trait is object-safe and a stub implementation
    /// compiles + can be invoked through a trait object.
    struct PassDetector;

    #[async_trait::async_trait]
    impl Detector for PassDetector {
        fn name(&self) -> &'static str {
            "pass"
        }
        async fn detect(&self, _content: &str) -> Result<Detection, DetectorError> {
            Ok(Detection::Pass)
        }
    }

    #[tokio::test]
    async fn detector_trait_object_invocable() {
        let d: Box<dyn Detector> = Box::new(PassDetector);
        assert_eq!(d.name(), "pass");
        assert_eq!(d.detect("anything").await.unwrap(), Detection::Pass);
    }
}
