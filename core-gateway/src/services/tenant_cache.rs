// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Tenant + API-key cache
//!
//! Caches the two DB lookups the tenant middleware does on every
//! request:
//!
//! 1. `api_keys.key_hash → tenant_id` (when the request comes in
//!    with an `Authorization: Bearer …` or `X-API-Key` header).
//! 2. `tenants.id → (suspended, suspension_reason)` — needed to
//!    short-circuit suspended tenants.
//!
//! Both lookups are by primary-key, so the per-call DB cost is
//! tiny in absolute terms — but at scale they multiply: 50 RPS
//! sustained chat traffic × 2 lookups = 100 connection-acquires per
//! second on the hot path. Caching for 5 s drops this to a tiny
//! fraction (one acquire per cache key per 5 s) and frees pool
//! capacity for the actual upstream calls.
//!
//! ### TTL trade-off
//!
//! 5 s of staleness on `tenants.suspended` is acceptable: a
//! sysadmin suspending a tenant is not an emergency-grade
//! operation; by the time they navigate the admin UI, click, and
//! verify, several seconds will have passed regardless. Documented
//! explicitly so operators don't get surprised.
//!
//! For api-key → tenant_id the cache is even safer because keys
//! are immutable in practice (revoked keys must hit a 401 within
//! the cache window — see `KEY_TTL_SECS`).

use std::time::Duration;

use moka::sync::Cache;

/// Cached tenant suspension state. Mirrors the two columns the
/// tenant middleware reads.
#[derive(Debug, Clone)]
pub struct TenantStatus {
    pub suspended: bool,
    pub suspension_reason: Option<String>,
    /// The tenant's traffic-carrying feature switches.
    pub features: crate::services::tenant_features::TenantFeatures,
    /// The tenant's identity policy default (`tenants.identity_policy_default`),
    /// read fail closed; `None` when the tenant set none.
    pub identity_policy_default: Option<crate::services::identity_policy::IdentityPolicyDefault>,
}

/// Cached api-key → tenant resolution. `None` means "we hit the DB,
/// the key wasn't found / was disabled / was revoked" — caching
/// negative results blocks credential-stuffing at line rate.
pub type ApiKeyResolution = Option<String>;

/// TTL for tenant status. 5 s — see module docs.
pub const TENANT_TTL_SECS: u64 = 5;
/// TTL for api-key resolution. 30 s — keys are effectively
/// immutable so we cache more aggressively. A revoked key still hits
/// a 401 within 30 s of the operator pressing revoke.
pub const KEY_TTL_SECS: u64 = 30;
/// Max entries per cache. Bounds memory under malicious inputs
/// (e.g. an attacker firing tens of thousands of distinct API keys).
pub const MAX_ENTRIES: u64 = 100_000;

/// Composite middleware-side cache. Constructed once at boot,
/// cloned cheaply (moka caches are Arc-shared internally).
#[derive(Clone)]
pub struct TenantCache {
    /// `key_hash → tenant_id (Option)`. Negative cache included.
    api_keys: Cache<String, ApiKeyResolution>,
    /// `tenant_id → status`.
    tenants: Cache<String, TenantStatus>,
}

impl Default for TenantCache {
    fn default() -> Self {
        Self::new()
    }
}

impl TenantCache {
    pub fn new() -> Self {
        Self {
            api_keys: Cache::builder()
                .max_capacity(MAX_ENTRIES)
                .time_to_live(Duration::from_secs(KEY_TTL_SECS))
                .build(),
            tenants: Cache::builder()
                .max_capacity(MAX_ENTRIES)
                .time_to_live(Duration::from_secs(TENANT_TTL_SECS))
                .build(),
        }
    }

    /// Read a cached api-key resolution. Returns `None` if the
    /// cache has no entry (caller should hit DB and call
    /// `insert_api_key`); returns `Some(None)` if the cache has a
    /// negative entry (caller should reject as unauthenticated
    /// without hitting DB).
    pub fn get_api_key(&self, key_hash: &str) -> Option<ApiKeyResolution> {
        self.api_keys.get(key_hash)
    }

    pub fn insert_api_key(&self, key_hash: String, resolution: ApiKeyResolution) {
        self.api_keys.insert(key_hash, resolution);
    }

    pub fn get_tenant(&self, tenant_id: &str) -> Option<TenantStatus> {
        self.tenants.get(tenant_id)
    }

    pub fn insert_tenant(&self, tenant_id: String, status: TenantStatus) {
        self.tenants.insert(tenant_id, status);
    }

    /// Drop one tenant's status. The invalidation listener calls it on the
    /// message the control plane publishes when a tenant's settings change
    /// (`cache::invalidation`), so a new identity policy default reaches
    /// the next call rather than waiting out the 5 s TTL.
    pub fn invalidate_tenant(&self, tenant_id: &str) {
        self.tenants.invalidate(tenant_id);
    }

    pub fn invalidate_api_key(&self, key_hash: &str) {
        self.api_keys.invalidate(key_hash);
    }

    /// Drop every tenant's status: after the invalidation listener
    /// (re)subscribes, a change published while it was away is not missed.
    pub fn invalidate_all_tenants(&self) {
        self.tenants.invalidate_all();
    }
}

/// Helper used by tests / the saturation monitor — returns the
/// approximate entry count. `moka::sync::Cache` exposes this via
/// `entry_count` after a maintenance cycle; for tests it's good
/// enough to verify cache hits / misses are happening.
pub fn approximate_entry_counts(cache: &TenantCache) -> (u64, u64) {
    cache.api_keys.run_pending_tasks();
    cache.tenants.run_pending_tasks();
    (cache.api_keys.entry_count(), cache.tenants.entry_count())
}

/// Re-export of the timing struct used internally; exposed for
/// tests to probe Drop-on-cancel paths in the future. Not used in
/// the hot path.
#[cfg(test)]
pub fn _make_test_status(suspended: bool) -> TenantStatus {
    TenantStatus {
        suspended,
        suspension_reason: None,
        features: Default::default(),
        identity_policy_default: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn api_key_miss_returns_none() {
        let c = TenantCache::new();
        assert_eq!(c.get_api_key("nope"), None);
    }

    #[test]
    fn api_key_hit_returns_resolution() {
        let c = TenantCache::new();
        c.insert_api_key("hash-1".into(), Some("tenant-a".into()));
        assert_eq!(c.get_api_key("hash-1"), Some(Some("tenant-a".into())));
    }

    #[test]
    fn api_key_negative_cache_works() {
        // Negative cache: a revoked / unknown key gets cached as
        // Some(None) so subsequent attempts don't re-hit the DB
        // until TTL expires. This is the credential-stuffing
        // mitigation.
        let c = TenantCache::new();
        c.insert_api_key("hash-bad".into(), None);
        assert_eq!(c.get_api_key("hash-bad"), Some(None));
    }

    #[test]
    fn tenant_status_round_trip() {
        let c = TenantCache::new();
        c.insert_tenant("t-1".into(), _make_test_status(true));
        let got = c.get_tenant("t-1").unwrap();
        assert!(got.suspended);
    }

    #[test]
    fn invalidate_drops_entry() {
        let c = TenantCache::new();
        c.insert_tenant("t-1".into(), _make_test_status(false));
        assert!(c.get_tenant("t-1").is_some());
        c.invalidate_tenant("t-1");
        // moka may keep the value momentarily until pending tasks
        // run; force a flush.
        c.tenants.run_pending_tasks();
        assert!(c.get_tenant("t-1").is_none());
    }

    #[test]
    fn separate_namespaces_dont_collide() {
        // An "api_key hash that happens to match a tenant_id"
        // shouldn't see cross-cache pollution.
        let c = TenantCache::new();
        c.insert_api_key("collision".into(), Some("api-key-tenant".into()));
        c.insert_tenant("collision".into(), _make_test_status(true));
        let api_resolution = c.get_api_key("collision").unwrap();
        let tenant_status = c.get_tenant("collision").unwrap();
        assert_eq!(api_resolution, Some("api-key-tenant".into()));
        assert!(tenant_status.suspended);
    }
}
