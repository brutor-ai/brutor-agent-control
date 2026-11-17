// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Data residency enforcement.
//!
//! Two boundary functions:
//!
//!   * [`parse_profile`] — extract the per-tenant residency
//!     profile from the `tenants.data_residency` JSONB blob.
//!     Returns `None` when the tenant has no profile (existing
//!     tenants keep working) or the JSON is malformed (logged at
//!     warn-level; the malformed-profile case is treated as
//!     "no enforcement" rather than refusing every request — a
//!     fail-open default for an operator misconfiguration).
//!
//!   * [`check`] — given a parsed profile and an upstream
//!     region, return [`ResidencyDecision::Allowed`] or
//!     [`ResidencyDecision::Blocked`] with a reason string for
//!     the audit row's `residency_class`. Pure function; no I/O.
//!
//! Both are written so the LLM and MCP route handlers can call
//! them without depending on each other's wiring.
//!
//! ## Availability failures fail CLOSED
//!
//! The paragraph above is about *content* we successfully read and could
//! not interpret. It does not extend to *not being able to read at all*.
//! A DB error used to resolve to "no enforcement", which meant a tenant
//! with an EU-only policy silently lost that policy for the duration of a
//! database blip — a legal boundary disappearing on an availability event.
//!
//! [`load_tenant_profile`] now degrades in three tiers, mirroring the
//! autonomy kill switch:
//!
//!   1. **live** — a fresh successful read;
//!   2. **last_known_good** — the last profile learned for this tenant
//!      (retained 24h, separate from the 60s hot cache), when the DB is
//!      unreadable. Includes the legitimate "known to have no profile",
//!      because *knowing* there is no policy and being *unable to tell*
//!      must not resolve the same way;
//!   3. **deny-all floor** — nothing was ever learned for this tenant and
//!      the DB is unreadable (a cold process during an outage). Cross-region
//!      routing is refused until it recovers.
//!

use std::sync::OnceLock;
use std::time::Duration;

use moka::sync::Cache;
use serde::Deserialize;
use sqlx::PgPool;

/// Audit string the runtime writes into
/// `proxy_logs.residency_class` for an allowed request.
pub const CLASS_ALLOWED: &str = "allowed";

/// Audit string for a blocked request. Pairs with
/// `region_routed_to = NULL` since the request never reached an
/// upstream.
pub const CLASS_BLOCKED: &str = "blocked";

/// Audit string for the no-profile case. Existing tenants
/// pre-a069 have NULL `data_residency` → no enforcement →
/// `residency_class = not_evaluated`. Kept distinct from NULL
/// so the audit dashboard can distinguish "old row from before
/// the feature" from "modern row where no policy applied".
pub const CLASS_NOT_EVALUATED: &str = "not_evaluated";

/// Parsed per-tenant residency profile.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct ResidencyProfile {
    /// Regions this tenant is permitted to route to. Lowercase
    /// alnum + hyphen + underscore tokens; format-enforced at
    /// admin-API save time by `app/api/v1/models/admin/residency.py`.
    #[serde(default)]
    pub allowed_regions: Vec<String>,

    /// When true, refuse providers whose region is `"global"` or
    /// NULL, even if `allowed_regions` is empty.
    #[serde(default)]
    pub block_global: bool,
}

impl ResidencyProfile {
    /// Constant-time-ish region match: lowercase byte comparison
    /// against each entry. The profile's allowed list is bounded
    /// (typically ≤4 regions) so a linear scan is fastest.
    pub fn allows_region(&self, region: &str) -> bool {
        self.allowed_regions
            .iter()
            .any(|r| r.eq_ignore_ascii_case(region))
    }
}

/// Outcome of a residency check. Stamped onto the audit row
/// regardless of which branch fired so operators can produce
/// monthly residency reports straight off `proxy_logs`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResidencyDecision {
    /// Request is allowed to proceed. The `region` carried here
    /// goes into `proxy_logs.region_routed_to` so a successful
    /// row records which region actually served the request.
    /// `None` means the request was allowed but the upstream had
    /// no region declared and the tenant didn't have a profile —
    /// in that case `region_routed_to` stays NULL.
    Allowed { region: Option<String> },

    /// Request must be refused with `403 region_not_allowed`. The
    /// `reason` string is included in the structured error body
    /// AND logged onto the audit row via a `residency_violation`
    /// proxy_subtype + the audit row's error_message field.
    Blocked { reason: String },
}

impl ResidencyDecision {
    /// Audit class string for `proxy_logs.residency_class`.
    pub fn class(&self) -> &'static str {
        match self {
            ResidencyDecision::Allowed { .. } => CLASS_ALLOWED,
            ResidencyDecision::Blocked { .. } => CLASS_BLOCKED,
        }
    }

    /// True when the request must be refused.
    pub fn is_blocked(&self) -> bool {
        matches!(self, ResidencyDecision::Blocked { .. })
    }
}

/// Extract a profile from the tenant's `data_residency` JSONB
/// blob. Returns `None` when the tenant has no profile OR the
/// blob is malformed (warn-logged). Both cases short-circuit
/// enforcement so a typo in the admin API doesn't take a tenant
/// offline.
pub fn parse_profile(raw: Option<&serde_json::Value>) -> Option<ResidencyProfile> {
    let v = raw?;
    if v.is_null() {
        return None;
    }
    match serde_json::from_value::<ResidencyProfile>(v.clone()) {
        Ok(p) => Some(p),
        Err(e) => {
            tracing::warn!(
                error = %e,
                raw = %v,
                "Malformed tenants.data_residency JSON — treating as no enforcement",
            );
            None
        }
    }
}

/// The core decision function. Called at the entry of every
/// request that resolves an upstream provider/server.
///
/// Semantics:
///   * `profile = None`     → not evaluated (decision absent;
///     callers can stamp
///     `residency_class = not_evaluated`
///     but proceed).
///   * `region = "global"`  → blocked iff `block_global = true`,
///     else allowed iff `allowed_regions`
///     is empty OR contains "global"
///     literally.
///   * `region = None`      → same logic as "global" (the
///     convention being that an
///     unspecified region is no commitment).
///   * Specific region     → allowed iff in `allowed_regions`.
pub fn check(
    profile: Option<&ResidencyProfile>,
    provider_region: Option<&str>,
) -> Option<ResidencyDecision> {
    // No profile = no enforcement. Caller stamps not_evaluated.
    let profile = profile?;

    let region = provider_region.map(|s| s.trim()).filter(|s| !s.is_empty());

    // Treat literal "global" and NULL the same for block_global.
    let is_global_or_unspecified =
        region.is_none() || region.is_some_and(|r| r.eq_ignore_ascii_case("global"));

    if is_global_or_unspecified {
        if profile.block_global {
            return Some(ResidencyDecision::Blocked {
                reason: "provider region is global or unspecified and tenant has block_global=true"
                    .to_string(),
            });
        }
        // If the tenant explicitly allowed "global", honour that.
        if profile.allows_region("global") {
            return Some(ResidencyDecision::Allowed {
                region: region.map(String::from),
            });
        }
        // If the allowlist is non-empty AND doesn't contain
        // "global", we deny — the operator listed specific
        // regions, so unspecified is presumed unsafe.
        if !profile.allowed_regions.is_empty() {
            return Some(ResidencyDecision::Blocked {
                reason:
                    "provider region is global or unspecified but tenant requires a specific region"
                        .to_string(),
            });
        }
        // Empty allowlist + block_global=false → no constraint.
        return Some(ResidencyDecision::Allowed {
            region: region.map(String::from),
        });
    }

    // Specific region — must be in the allowlist.
    let r = region.unwrap();
    if profile.allows_region(r) {
        Some(ResidencyDecision::Allowed {
            region: Some(r.to_string()),
        })
    } else {
        Some(ResidencyDecision::Blocked {
            reason: format!("provider region '{r}' not in tenant's allowed_regions"),
        })
    }
}

/// Load a tenant's residency profile from the DB. One-row SELECT
/// against `tenants.data_residency`. Returns `None` when the row
/// is missing (shouldn't happen — middleware would have rejected
/// the request earlier) OR the column is NULL OR the JSON is
/// malformed (warn-logged). All three cases collapse to "no
/// enforcement" because a residency feature should never take a
/// tenant offline through an operator config error.
///
/// Process-wide TTL cache for parsed residency profiles, keyed by tenant_id.
/// `data_residency` changes rarely and most tenants have NO profile at all, so
/// the per-request `SELECT data_residency` was pure overhead on the LLM/MCP/A2A
/// hot paths. We cache the `None` (no-profile) result too so the common case
/// also skips the DB. 60s TTL bounds staleness after an operator edits the
/// policy; `invalidate_residency_cache` is available for explicit eviction.
fn residency_cache() -> &'static Cache<String, Option<ResidencyProfile>> {
    static CACHE: OnceLock<Cache<String, Option<ResidencyProfile>>> = OnceLock::new();
    CACHE.get_or_init(|| {
        Cache::builder()
            .max_capacity(10_000)
            .time_to_live(Duration::from_secs(60))
            .build()
    })
}

/// Last-known-good residency knowledge, retained far longer than the hot
/// cache and consulted **only** when the DB read fails.
///
/// This is deliberately a second store rather than a longer TTL on the hot
/// cache: the hot cache exists to bound *staleness* after an operator edit
/// (60s), while this one exists to bound *blast radius* during an outage.
/// Merging them would force one TTL to serve two opposite goals.
///
/// It stores the full `Option<ResidencyProfile>` — i.e. the *knowledge*,
/// including the legitimate "this tenant has no profile". That distinction is
/// the whole point: "we know there is no policy" and "we cannot tell whether
/// there is a policy" must not resolve the same way.
fn residency_last_known() -> &'static Cache<String, Option<ResidencyProfile>> {
    static LAST: OnceLock<Cache<String, Option<ResidencyProfile>>> = OnceLock::new();
    LAST.get_or_init(|| {
        Cache::builder()
            .max_capacity(10_000)
            .time_to_live(Duration::from_secs(24 * 60 * 60))
            .build()
    })
}

/// The fail-closed floor: permit nothing that is not explicitly allowed.
///
/// An empty `allowed_regions` with `block_global` rejects a global/unspecified
/// upstream on the `block_global` branch and every named region on the
/// allow-list branch, so it denies uniformly without needing a new variant in
/// [`ResidencyDecision`].
///
/// Reached only when residency is unreadable AND nothing was ever learned for
/// the tenant — a cold process during a database outage. Data residency is a
/// legal boundary, so "we cannot verify where this data would go" must not
/// resolve to "send it anywhere". Mirrors the autonomy kill switch's
/// `FALLBACK_FLOOR`: degrade to the safe posture rather than fail open.
fn deny_all_floor() -> ResidencyProfile {
    ResidencyProfile {
        allowed_regions: Vec::new(),
        block_global: true,
    }
}

/// Evict a tenant's cached residency profile (call when the control plane
/// updates `tenants.data_residency`).
pub fn invalidate_residency_cache(tenant_id: &str) {
    residency_cache().invalidate(tenant_id);
}

/// Load a tenant's residency profile, TTL-cached. Only SUCCESSFUL reads
/// (a real profile or a genuine "no profile") are cached — a DB error is NOT
/// cached, so a transient blip doesn't pin the fail-open (no-enforcement)
/// result for the whole TTL window.
pub async fn load_tenant_profile(pool: &PgPool, tenant_id: &str) -> Option<ResidencyProfile> {
    if let Some(cached) = residency_cache().get(tenant_id) {
        return cached;
    }
    match query_tenant_profile(pool, tenant_id).await {
        Ok(profile) => {
            residency_cache().insert(tenant_id.to_string(), profile.clone());
            // Retain the knowledge for the outage path. Written on every
            // successful read so it tracks operator edits.
            residency_last_known().insert(tenant_id.to_string(), profile.clone());
            profile
        }
        Err(e) => {
            // FAIL CLOSED. Residency is a legal boundary; an unreadable policy
            // must never resolve to "no policy". Three tiers, matching the
            // autonomy kill switch's live / last_known_good / floor ladder.
            match residency_last_known().get(tenant_id) {
                Some(known) => {
                    tracing::warn!(
                        tenant_id,
                        error = %e,
                        enforcing = "last_known_good",
                        "Residency lookup failed — enforcing last known profile",
                    );
                    known
                }
                None => {
                    tracing::error!(
                        tenant_id,
                        error = %e,
                        enforcing = "deny_all_floor",
                        "Residency lookup failed with nothing learned for this \
                         tenant — denying cross-region routing until it recovers",
                    );
                    Some(deny_all_floor())
                }
            }
        }
    }
}

/// One-row `SELECT data_residency FROM tenants`. Returns `Ok(None)` for a
/// missing row or NULL/malformed JSON (both = no enforcement), `Err` only on
/// an actual DB error so the caller can avoid caching it.
async fn query_tenant_profile(
    pool: &PgPool,
    tenant_id: &str,
) -> Result<Option<ResidencyProfile>, sqlx::Error> {
    let row: Option<(Option<serde_json::Value>,)> =
        sqlx::query_as("SELECT data_residency FROM tenants WHERE id = $1")
            .bind(tenant_id)
            .fetch_optional(pool)
            .await?;

    Ok(match row {
        Some((raw,)) => parse_profile(raw.as_ref()),
        None => {
            tracing::warn!(
                tenant_id,
                "Residency profile lookup: tenant row missing — treating as no enforcement",
            );
            None
        }
    })
}

// ===========================================================================
// Tests
// ===========================================================================
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn profile(allowed: &[&str], block_global: bool) -> ResidencyProfile {
        ResidencyProfile {
            allowed_regions: allowed.iter().map(|s| s.to_string()).collect(),
            block_global,
        }
    }

    // ---- parse_profile ---------------------------------------------------

    #[test]
    fn parse_returns_none_for_null_input() {
        assert!(parse_profile(None).is_none());
        assert!(parse_profile(Some(&serde_json::Value::Null)).is_none());
    }

    #[test]
    fn parse_extracts_allowed_regions_and_block_global() {
        let raw = json!({
            "allowed_regions": ["eu-west-1", "eu-central-1"],
            "block_global": true
        });
        let p = parse_profile(Some(&raw)).expect("should parse");
        assert_eq!(p.allowed_regions, vec!["eu-west-1", "eu-central-1"]);
        assert!(p.block_global);
    }

    #[test]
    fn parse_uses_defaults_for_missing_keys() {
        // An empty object is valid — defaults to no regions and
        // block_global=false. (Equivalent to "no enforcement"
        // because allowed_regions is empty.)
        let p = parse_profile(Some(&json!({}))).expect("should parse");
        assert!(p.allowed_regions.is_empty());
        assert!(!p.block_global);
    }

    #[test]
    fn parse_returns_none_for_malformed_blob() {
        // Wrong shape — fails Deserialize. parse_profile
        // warn-logs and returns None so a misconfigured admin
        // doesn't take the tenant offline.
        let raw = json!({"allowed_regions": "not-a-list", "block_global": "yes"});
        assert!(parse_profile(Some(&raw)).is_none());
    }

    // ---- check: no profile ----------------------------------------------

    #[test]
    fn check_without_profile_returns_none() {
        assert!(check(None, Some("us-east-1")).is_none());
        assert!(check(None, None).is_none());
    }

    // ---- check: allowed regions ------------------------------------------

    #[test]
    fn check_allows_exact_match() {
        let p = profile(&["eu-west-1", "eu-central-1"], false);
        let d = check(Some(&p), Some("eu-west-1")).unwrap();
        assert!(
            matches!(d, ResidencyDecision::Allowed { region: Some(ref r) } if r == "eu-west-1")
        );
    }

    #[test]
    fn check_match_is_case_insensitive() {
        // Operator-typed uppercase shouldn't slip past the gate
        // (the admin API lowercases on save, but provider rows
        // pre-a069 might carry whatever the operator wrote).
        let p = profile(&["eu-west-1"], false);
        let d = check(Some(&p), Some("EU-WEST-1")).unwrap();
        assert!(matches!(d, ResidencyDecision::Allowed { .. }));
    }

    #[test]
    fn check_blocks_when_region_not_in_allowlist() {
        let p = profile(&["eu-west-1"], false);
        let d = check(Some(&p), Some("us-east-1")).unwrap();
        assert!(
            matches!(d, ResidencyDecision::Blocked { ref reason } if reason.contains("us-east-1"))
        );
    }

    // ---- check: global / unspecified -------------------------------------

    #[test]
    fn check_blocks_global_when_block_global_true() {
        let p = profile(&["eu-west-1"], true);
        let d = check(Some(&p), Some("global")).unwrap();
        assert!(d.is_blocked());
    }

    #[test]
    fn check_blocks_unspecified_when_block_global_true() {
        let p = profile(&["eu-west-1"], true);
        let d = check(Some(&p), None).unwrap();
        assert!(d.is_blocked());
    }

    #[test]
    fn check_blocks_unspecified_when_allowed_regions_explicit() {
        // Tenant listed specific regions → unspecified is unsafe
        // even without block_global=true. The operator clearly
        // wants region pinning; an unknown region defeats that.
        let p = profile(&["eu-west-1"], false);
        let d = check(Some(&p), None).unwrap();
        assert!(d.is_blocked());
    }

    #[test]
    fn check_allows_global_when_explicitly_allowed() {
        // "global" can appear in allowed_regions for tenants that
        // don't care — and that should not collide with
        // block_global=false.
        let p = profile(&["global", "eu-west-1"], false);
        let d = check(Some(&p), Some("global")).unwrap();
        assert!(matches!(d, ResidencyDecision::Allowed { .. }));
    }

    #[test]
    fn check_allows_unspecified_when_profile_empty_and_block_global_false() {
        // The "no enforcement" shape: empty allowed_regions +
        // block_global=false. Useful as a default profile state.
        let p = profile(&[], false);
        let d = check(Some(&p), None).unwrap();
        assert!(matches!(d, ResidencyDecision::Allowed { region: None }));
    }

    // ---- audit class strings ---------------------------------------------

    #[test]
    fn decision_class_strings_are_stable() {
        let allowed = ResidencyDecision::Allowed {
            region: Some("eu-west-1".to_string()),
        };
        let blocked = ResidencyDecision::Blocked {
            reason: "x".to_string(),
        };
        // These strings end up in `proxy_logs.residency_class`
        // which the audit dashboard filters on. Pin them so a
        // future refactor doesn't break operator-saved filters.
        assert_eq!(allowed.class(), "allowed");
        assert_eq!(blocked.class(), "blocked");
        assert_eq!(CLASS_NOT_EVALUATED, "not_evaluated");
    }

    #[test]
    fn decision_is_blocked_classifier() {
        assert!(
            ResidencyDecision::Blocked {
                reason: "x".to_string()
            }
            .is_blocked()
        );
        assert!(
            !ResidencyDecision::Allowed {
                region: Some("eu-west-1".to_string())
            }
            .is_blocked()
        );
    }

    // ------------------------------------------------------------------
    // Fail-closed on availability failure.
    //
    // The DB-error path itself needs a live Postgres (covered in the
    // integration suite); these pin the decision rules that path relies
    // on — which is where a regression would actually land. If the floor
    // ever stops denying, residency silently evaporates during an outage.
    // ------------------------------------------------------------------

    #[test]
    fn the_deny_all_floor_blocks_every_named_region() {
        let floor = deny_all_floor();
        for region in ["us-east-1", "eu-west-1", "ap-south-1", "GLOBAL"] {
            assert!(
                check(Some(&floor), Some(region))
                    .expect("floor always decides")
                    .is_blocked(),
                "floor must block {region}",
            );
        }
    }

    #[test]
    fn the_deny_all_floor_blocks_unspecified_regions() {
        // The subtle one: an upstream with no declared region must not slip
        // through the allow-list branch when we cannot verify policy.
        let floor = deny_all_floor();
        assert!(check(Some(&floor), None).expect("decides").is_blocked());
        assert!(check(Some(&floor), Some("")).expect("decides").is_blocked());
    }

    #[test]
    fn the_floor_is_not_an_empty_permissive_profile() {
        // A profile with no allowed_regions and block_global=false would
        // permit every named region — the exact inversion this guards.
        let floor = deny_all_floor();
        assert!(floor.block_global, "floor must block global/unspecified");
        assert!(
            floor.allowed_regions.is_empty(),
            "floor must not allow any region",
        );
    }

    #[test]
    fn a_known_absent_profile_still_means_no_enforcement() {
        // Tier 2 must preserve the difference between "we know there is no
        // policy" (None → no enforcement, tenants without residency keep
        // working) and "we cannot tell" (floor → deny).
        assert!(check(None, Some("us-east-1")).is_none());
    }
}
