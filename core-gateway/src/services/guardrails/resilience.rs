// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Guardrail degradation ladder
//!
//! Guardrails fail closed. Alone that is too blunt: one
//! timeout would go straight to "deny". The objection is fair — a single slow
//! packet should not refuse a request, and a detector vendor having a bad
//! afternoon should not take the tenant offline.
//!
//! But the fix is not to hand the choice back to the code. The reason every
//! competing gateway fails open is that a fail-closed error is loud and
//! attributable while a fail-open one is invisible, so defaults drift toward
//! the option with no observable cost. Removing the operator's ability to
//! refuse would put us back there.
//!
//! So the ladder separates two failures that a single boolean conflates:
//!
//!   1. **This request was unlucky** — a blip, a slow packet, a GC pause.
//!      Uncorrelated. Costs one 503 if we refuse, which nobody notices.
//!      Handled by **retry**, and it never reaches a policy decision.
//!   2. **This provider is down** — correlated across every request at once.
//!      *This* is the outage an operator fears. Handled by the **breaker**
//!      (which is what turns "unlucky" into a diagnosis) and then by
//!      **local fallback**, because degraded coverage beats none.
//!
//! Only when all of that is exhausted does the configured fail-open /
//! fail-closed rule apply — by which point it is a rare, well-understood
//! state rather than a coin flip on every request.
//!
//! `break_glass` sits above the whole ladder: a deliberate, expiring,
//! attributed operator action for the case where the outage is real and
//! availability genuinely outranks the control. That is the same decision a
//! hardcoded fail-open would make — except somebody's name is on it, it
//! shows up in the audit row, and it expires on its own.

use super::detector::DetectorError;

// ---------------------------------------------------------------------------
// Failure classification
// ---------------------------------------------------------------------------

/// Why a detector failed, in the only three flavours that change what we do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FailureClass {
    /// Blip. Worth retrying, and evidence the provider may be unhealthy.
    Transient,
    /// The provider answered, but wrongly, and will answer wrongly again.
    /// Not worth retrying; still evidence the provider is unhealthy.
    Permanent,
    /// *Our* misconfiguration, not the provider's health. Never retried and
    /// deliberately kept away from the breaker: opening a circuit for a
    /// missing API key would dress a config bug up as an outage, and the
    /// half-open probe could never succeed to close it again.
    Config,
}

pub fn classify(err: &DetectorError) -> FailureClass {
    match err {
        // Missing credentials/endpoint. Retrying cannot fix it and the
        // provider is blameless.
        DetectorError::MissingProviderConfig { .. } => FailureClass::Config,
        DetectorError::UnreadableSecret { .. } => FailureClass::Config,

        // We stopped waiting. Retrying would spend a budget we have already
        // spent — the caller is out of time by definition.
        DetectorError::BudgetExceeded { .. } => FailureClass::Permanent,

        // Classic transients.
        DetectorError::Timeout { .. } => FailureClass::Transient,
        DetectorError::Other { .. } => FailureClass::Transient,

        // 5xx and 429 are transient; 4xx means we are asking wrongly and a
        // retry just burns the budget. 401/403 are a credentials problem, so
        // they are Config rather than the provider being unhealthy.
        DetectorError::ProviderHttp { status, .. } => match status {
            401 | 403 => FailureClass::Config,
            429 => FailureClass::Transient,
            s if *s >= 500 => FailureClass::Transient,
            _ => FailureClass::Permanent,
        },

        // Provider-side schema regression. Deterministic, but it *is* the
        // provider being broken, so it counts against the breaker.
        DetectorError::MalformedResponse { .. } => FailureClass::Permanent,
    }
}

/// Retry only the transients.
pub fn is_retryable(err: &DetectorError) -> bool {
    classify(err) == FailureClass::Transient
}

/// Whether this failure is evidence that the *provider* is unhealthy.
///
/// Config errors are excluded on purpose — see [`FailureClass::Config`].
pub fn counts_against_breaker(err: &DetectorError) -> bool {
    !matches!(classify(err), FailureClass::Config)
}

// ---------------------------------------------------------------------------
// Which rung answered
// ---------------------------------------------------------------------------

/// How a check was ultimately resolved. Recorded on the audit row so that
/// "the check ran" and "the check ran on the good detector" are never the
/// same claim.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResolutionTier {
    /// Primary detector answered first time.
    Primary,
    /// Primary answered, but only after one or more retries.
    Retried,
    /// The remote detector was unavailable and the local built-in for the
    /// same check answered instead. Real coverage, lower fidelity — and it
    /// must be disclosed as such.
    Fallback,
    /// An operator has declared an outage. The check did not run and traffic
    /// was allowed by an attributed, expiring human decision.
    BreakGlass,
    /// Nothing answered. The configured fail-open / fail-closed rule decided.
    Policy,
}

impl ResolutionTier {
    pub fn as_str(&self) -> &'static str {
        match self {
            ResolutionTier::Primary => "primary",
            ResolutionTier::Retried => "retried",
            ResolutionTier::Fallback => "fallback",
            ResolutionTier::BreakGlass => "break_glass",
            ResolutionTier::Policy => "policy",
        }
    }

    /// Did a detector actually inspect the content on this tier?
    ///
    /// The assurance report needs this: `Fallback` inspected the content and
    /// is real evidence, `BreakGlass` and `Policy` did not and are not. A
    /// coverage claim that counts them is a false claim.
    pub fn content_was_inspected(&self) -> bool {
        matches!(
            self,
            ResolutionTier::Primary | ResolutionTier::Retried | ResolutionTier::Fallback
        )
    }

    /// Should this tier be disclosed as degraded on the audit row?
    pub fn is_degraded(&self) -> bool {
        !matches!(self, ResolutionTier::Primary)
    }
}

/// Per-detector retry budget.
///
/// Two attempts total, not more: a detector sits in the request's critical
/// path, and a third attempt buys little while spending latency the caller
/// experiences directly. The breaker, not the retry count, is what handles a
/// provider that is genuinely down.
pub const MAX_DETECTOR_ATTEMPTS: u32 = 2;

/// Consecutive failures before a detector provider is declared down.
///
/// Deliberately higher than the retry count: retries absorb single blips, so
/// reaching this threshold means several *independent* requests all failed,
/// which is the correlated signature the breaker exists to catch.
pub const DETECTOR_FAILURE_THRESHOLD: u32 = 5;

// Otherwise retries alone could trip the breaker and a single unlucky request
// would be diagnosed as a provider outage. Checked at compile time so the
// relationship cannot drift silently.
const _: () = assert!(
    DETECTOR_FAILURE_THRESHOLD > MAX_DETECTOR_ATTEMPTS,
    "threshold must require independent failures, not just retries"
);

/// How long the breaker stays open before a probe is allowed through.
pub const DETECTOR_RECOVERY_SECS: u64 = 30;

/// Breaker key for a detector.
///
/// Keyed by provider + check rather than by guardrail config: when Presidio
/// is down it is down for every config that points at it, and per-config keys
/// would make each config rediscover the same outage independently.
pub fn breaker_key(provider: &str, check: &str) -> String {
    format!("guardrail:{provider}:{check}")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn http(status: u16) -> DetectorError {
        DetectorError::ProviderHttp {
            provider: "p",
            status,
            body_excerpt: String::new(),
        }
    }

    // --- classification ----------------------------------------------------

    #[test]
    fn timeouts_and_connect_errors_are_transient() {
        assert_eq!(
            classify(&DetectorError::Timeout { provider: "p" }),
            FailureClass::Transient
        );
        assert_eq!(
            classify(&DetectorError::Other {
                provider: "p",
                message: "connection refused".into()
            }),
            FailureClass::Transient
        );
    }

    #[test]
    fn server_errors_and_rate_limits_are_transient() {
        for s in [429, 500, 502, 503, 504] {
            assert_eq!(classify(&http(s)), FailureClass::Transient, "status {s}");
        }
    }

    #[test]
    fn client_errors_are_permanent_not_worth_retrying() {
        for s in [400, 404, 422] {
            assert_eq!(classify(&http(s)), FailureClass::Permanent, "status {s}");
        }
    }

    #[test]
    fn auth_failures_are_config_not_provider_health() {
        // 401/403 means our credentials are wrong. Tripping the breaker would
        // present a config bug as a provider outage, and the half-open probe
        // could never close it.
        for s in [401, 403] {
            assert_eq!(classify(&http(s)), FailureClass::Config, "status {s}");
            assert!(!counts_against_breaker(&http(s)));
        }
    }

    #[test]
    fn missing_provider_config_never_touches_the_breaker() {
        let e = DetectorError::MissingProviderConfig {
            provider: "p",
            field: "api_key",
        };
        assert_eq!(classify(&e), FailureClass::Config);
        assert!(!counts_against_breaker(&e));
        assert!(!is_retryable(&e));
    }

    #[test]
    fn budget_exceeded_is_never_retried() {
        // We already spent the caller's time; a retry spends it twice.
        let e = DetectorError::BudgetExceeded {
            provider: "p",
            budget_ms: 100,
        };
        assert!(!is_retryable(&e));
        assert!(counts_against_breaker(&e));
    }

    #[test]
    fn malformed_response_counts_against_the_provider_but_is_not_retried() {
        let e = DetectorError::MalformedResponse {
            provider: "p",
            reason: "no `results` key".into(),
        };
        assert!(!is_retryable(&e));
        assert!(counts_against_breaker(&e));
    }

    #[test]
    fn only_transients_are_retryable() {
        assert!(is_retryable(&DetectorError::Timeout { provider: "p" }));
        assert!(is_retryable(&http(503)));
        assert!(!is_retryable(&http(400)));
    }

    // --- tiers -------------------------------------------------------------

    #[test]
    fn fallback_counts_as_content_inspected_but_degraded() {
        // The local built-in really did look at the content — that is real
        // evidence, just lower fidelity. Both halves matter.
        assert!(ResolutionTier::Fallback.content_was_inspected());
        assert!(ResolutionTier::Fallback.is_degraded());
    }

    #[test]
    fn break_glass_and_policy_never_count_as_inspected() {
        // The failure this guards: an assurance report claiming guardrail
        // coverage for calls where no detector ever saw the content.
        for t in [ResolutionTier::BreakGlass, ResolutionTier::Policy] {
            assert!(!t.content_was_inspected(), "{t:?} must not count");
            assert!(t.is_degraded());
        }
    }

    #[test]
    fn only_a_clean_primary_is_undegraded() {
        assert!(!ResolutionTier::Primary.is_degraded());
        assert!(ResolutionTier::Retried.is_degraded());
    }

    #[test]
    fn tier_names_are_stable_wire_values() {
        // Persisted in proxy_logs and read by the admin UI.
        assert_eq!(ResolutionTier::Primary.as_str(), "primary");
        assert_eq!(ResolutionTier::Retried.as_str(), "retried");
        assert_eq!(ResolutionTier::Fallback.as_str(), "fallback");
        assert_eq!(ResolutionTier::BreakGlass.as_str(), "break_glass");
        assert_eq!(ResolutionTier::Policy.as_str(), "policy");
    }

    #[test]
    fn breaker_key_groups_by_provider_and_check_not_by_config() {
        // One broken Presidio is one outage, however many configs point at it.
        assert_eq!(breaker_key("presidio", "pii"), "guardrail:presidio:pii");
        assert_ne!(breaker_key("presidio", "pii"), breaker_key("lakera", "pii"));
        assert_ne!(
            breaker_key("lakera", "pii"),
            breaker_key("lakera", "jailbreak")
        );
    }
}
