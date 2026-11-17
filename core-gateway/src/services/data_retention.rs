// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Data retention at the write path — the `store_bodies` half.
//!
//! The control plane owns the policy (`tenants.data_retention`) and the
//! sweeper that enforces the time windows. The proxy owns one decision the
//! sweeper cannot make after the fact: what gets written in the first place.
//! `store_bodies` is applied here, in the audit writer at flush time, so it
//! holds for every surface (LLM, MCP, A2A, Skills, VMCP) without each of the
//! twenty-odd `body_preview(...)` sites having to know about it.
//!
//! Three modes:
//!
//! * `full`     — the historic behaviour, now capped at [`MAX_BODY_BYTES`].
//! * `redacted` — every stored body runs through the local PII redactor,
//!   independent of whether a PII guardrail is configured for the request.
//!   Pattern-based; see `guardrails::pii`.
//! * `none`     — no body or header text is stored at all.
//!
//! Bodies and headers are not covered by the audit hash chain, so nothing
//! here affects verification. Lookups are cached for 60 s and fall back to
//! the last-known value on a database error — the failure direction that
//! never stores *more* than the tenant asked for is to keep the last policy,
//! not to reset to `full`.

use std::sync::OnceLock;
use std::time::Duration;

use moka::sync::Cache;
use sqlx::PgPool;

use crate::models::db::ProxyLogInsert;

/// Upper bound on a stored body, in bytes. The column was always documented
/// as a preview; this makes that true. 64 KiB keeps the largest realistic
/// chat request whole and stops a multi-megabyte upload from becoming a
/// multi-megabyte audit row.
pub const MAX_BODY_BYTES: usize = 64 * 1024;

pub const TRUNCATION_MARKER: &str = "…[truncated by store policy]";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum StoreBodies {
    #[default]
    Full,
    Redacted,
    None,
}

impl StoreBodies {
    /// Wire vocabulary shared with `app/api/v1/models/admin/retention.py`.
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "full" => Some(Self::Full),
            "redacted" => Some(Self::Redacted),
            "none" => Some(Self::None),
            _ => Option::None,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Full => "full",
            Self::Redacted => "redacted",
            Self::None => "none",
        }
    }
}

/// Read `store_bodies` out of the tenant's JSONB policy. Anything missing or
/// unparseable is `Full` — the pre-a166 behaviour — and a malformed value is
/// logged once per cache period rather than every request.
pub fn parse_store_bodies(raw: Option<&serde_json::Value>) -> StoreBodies {
    let Some(v) = raw else {
        return StoreBodies::Full;
    };
    match v.get("store_bodies").and_then(|m| m.as_str()) {
        Option::None => StoreBodies::Full,
        Some(mode) => StoreBodies::parse(mode).unwrap_or_else(|| {
            tracing::warn!(
                mode,
                "Unknown tenants.data_retention.store_bodies — storing bodies in full"
            );
            StoreBodies::Full
        }),
    }
}

/// Cap a body at [`MAX_BODY_BYTES`] on a char boundary.
pub fn truncate_body(s: &str) -> String {
    if s.len() <= MAX_BODY_BYTES {
        return s.to_string();
    }
    let mut cut = MAX_BODY_BYTES;
    while !s.is_char_boundary(cut) {
        cut -= 1;
    }
    let mut out = String::with_capacity(cut + TRUNCATION_MARKER.len());
    out.push_str(&s[..cut]);
    out.push_str(TRUNCATION_MARKER);
    out
}

/// Seal SHA-256 digests over the request and response content as
/// captured, *before* any retention mode truncates, redacts or drops it.
///
/// This is what lets a body be pruned, redacted or never stored while the
/// row still proves what the content was: a recipient holding the original
/// bytes can recompute the digest, and the digest itself is inside the row
/// hash. Idempotent — a retried flush must not re-hash an already-mutated
/// preview.
pub fn seal_content_digests(log: &mut ProxyLogInsert) {
    use sha2::Digest as _;
    let digest = |s: &str| hex::encode(sha2::Sha256::digest(s.as_bytes()));
    if log.request_content_hash.is_none() {
        log.request_content_hash = log.request_body_preview.as_deref().map(digest);
    }
    if log.response_content_hash.is_none() {
        log.response_content_hash = log.response_body_preview.as_deref().map(digest);
    }
}

/// Apply a mode to one row. Pure, so it is unit-testable without a pool.
pub fn apply_store_bodies(log: &mut ProxyLogInsert, mode: StoreBodies) {
    match mode {
        StoreBodies::None => {
            log.request_body_preview = Option::None;
            log.response_body_preview = Option::None;
            log.request_headers = Option::None;
            log.response_headers = Option::None;
        }
        StoreBodies::Redacted => {
            for slot in [
                &mut log.request_body_preview,
                &mut log.response_body_preview,
            ] {
                if let Some(body) = slot.take() {
                    // Truncate on BOTH sides of the redactor.
                    //
                    // Before, so the redactor never scans more than the cap.
                    // After, because redaction is not length-preserving: it
                    // substitutes markers like `[REDACTED_EMAIL]` for the
                    // matched span, and a marker is routinely longer than the
                    // text it replaces. A body full of short matches therefore
                    // came out of `redacted` mode LARGER than `full` mode
                    // would have stored — the stricter policy producing the
                    // bigger row, and one that breaks the cap the column was
                    // sized against.
                    let (redacted, _) =
                        crate::services::guardrails::pii::redact_all_pii(&truncate_body(&body));
                    *slot = Some(truncate_body(&redacted));
                }
            }
        }
        StoreBodies::Full => {
            for slot in [
                &mut log.request_body_preview,
                &mut log.response_body_preview,
            ] {
                if let Some(body) = slot.as_deref() {
                    if body.len() > MAX_BODY_BYTES {
                        *slot = Some(truncate_body(body));
                    }
                }
            }
        }
    }
}

fn cache() -> &'static Cache<String, StoreBodies> {
    static CACHE: OnceLock<Cache<String, StoreBodies>> = OnceLock::new();
    CACHE.get_or_init(|| {
        Cache::builder()
            .max_capacity(10_000)
            .time_to_live(Duration::from_secs(60))
            .build()
    })
}

fn last_known() -> &'static Cache<String, StoreBodies> {
    static LAST: OnceLock<Cache<String, StoreBodies>> = OnceLock::new();
    LAST.get_or_init(|| {
        Cache::builder()
            .max_capacity(10_000)
            .time_to_live(Duration::from_secs(24 * 60 * 60))
            .build()
    })
}

/// Drop a tenant's cached `store_bodies` mode.
///
/// Called from tests only, for the same reason as
/// `assurance_checks::invalidate_tenant`: the retention policy lives on the
/// tenant row and the control plane publishes no invalidation for it, so a
/// change rides the 60 s TTL. That window is stated in the module docs and is
/// the intended behaviour, not a gap this function is failing to close.
pub fn invalidate(tenant_id: &str) {
    cache().invalidate(tenant_id);
}

/// The tenant's `store_bodies`, cached. On a lookup failure the last value
/// this process saw for the tenant is used; a tenant never seen before gets
/// `Full`, which is the only honest default for a policy we cannot read.
pub async fn load_store_bodies(pool: &PgPool, tenant_id: &str) -> StoreBodies {
    if let Some(mode) = cache().get(tenant_id) {
        return mode;
    }
    let row: Result<Option<(Option<serde_json::Value>,)>, sqlx::Error> =
        sqlx::query_as("SELECT data_retention FROM tenants WHERE id = $1")
            .bind(tenant_id)
            .fetch_optional(pool)
            .await;
    match row {
        Ok(row) => {
            let mode = parse_store_bodies(row.and_then(|(raw,)| raw).as_ref());
            cache().insert(tenant_id.to_string(), mode);
            last_known().insert(tenant_id.to_string(), mode);
            mode
        }
        Err(e) => {
            let mode = last_known().get(tenant_id).unwrap_or_default();
            tracing::warn!(
                tenant_id,
                error = %e,
                using = mode.as_str(),
                "data_retention lookup failed — using last known store_bodies",
            );
            mode
        }
    }
}

#[cfg(test)]
mod tests {

    // -----------------------------------------------------------------------
    // `redacted` mode must respect the same size cap as `full`
    // -----------------------------------------------------------------------

    /// Redaction substitutes a marker for each match, and the marker is
    /// usually longer than what it replaces. A body of many short matches
    /// therefore GREW under redaction, and the stricter retention mode
    /// produced a bigger row than the permissive one.
    #[test]
    fn redacted_bodies_are_capped_after_redaction_not_only_before() {
        // Emails are short and expand to a marker when redacted. Fill the cap
        // with them so the post-redaction string must exceed it.
        let one = "a@b.co ";
        let body = one.repeat(MAX_BODY_BYTES / one.len() + 16);
        assert!(body.len() > MAX_BODY_BYTES);

        let mut log = row(&body, &body);
        apply_store_bodies(&mut log, StoreBodies::Redacted);

        for slot in [&log.request_body_preview, &log.response_body_preview] {
            let stored = slot.as_deref().expect("redacted mode keeps a body");
            assert!(
                stored.len() <= MAX_BODY_BYTES + TRUNCATION_MARKER.len(),
                "redacted body is {} bytes, over the {MAX_BODY_BYTES}-byte cap — \
                 redaction is not length-preserving, so the cap has to be applied \
                 after it as well as before",
                stored.len()
            );
        }
    }

    /// The cap must not cost redaction: a body that fits is still scrubbed.
    #[test]
    fn a_small_body_is_still_fully_redacted() {
        let mut log = row("contact me at alice@example.com", "ok");
        apply_store_bodies(&mut log, StoreBodies::Redacted);
        let stored = log.request_body_preview.unwrap();
        assert!(
            !stored.contains("alice@example.com"),
            "redaction must still happen for bodies under the cap: {stored}"
        );
    }

    use super::*;

    pub(super) fn row(req: &str, resp: &str) -> ProxyLogInsert {
        let auth = crate::models::auth::AuthContext {
            auth_type: crate::models::auth::AuthType::ApiKey,
            tenant_id: "t".into(),
            gateway_user_id: None,
            gateway_username: None,
            gateway_tenant_id: None,
            idp_user_id: None,
            idp_username: None,
            idp_email: None,
            api_key_id: None,
            api_key_name: None,
            api_key_scope: None,
            api_key_group_id: None,
            api_key_is_platform: false,
            api_key_user_id: None,
            allowed_skill_ids: None,
            session_key: None,
            client_ip: None,
            resolved_group_id: None,
            actor_agent_id: None,
            session_token_type: None,
            agent_impl: crate::services::agent_impl::request_carriers(),
            admission_hold: None,
        };
        let mut log = crate::services::proxy_logger::build_proxy_log(
            "llm", &auth, "POST", "/v1/chat", None, None, 200, 1.0, true, None, "corr",
        );
        log.request_body_preview = Some(req.to_string());
        log.response_body_preview = Some(resp.to_string());
        log.request_headers = Some("{\"h\":1}".into());
        log
    }

    #[test]
    fn parse_is_lenient_and_defaults_to_full() {
        assert_eq!(parse_store_bodies(None), StoreBodies::Full);
        assert_eq!(
            parse_store_bodies(Some(&serde_json::json!({"store_bodies": "none"}))),
            StoreBodies::None
        );
        assert_eq!(
            parse_store_bodies(Some(&serde_json::json!({"store_bodies": "shred"}))),
            StoreBodies::Full
        );
        assert_eq!(
            parse_store_bodies(Some(&serde_json::json!({}))),
            StoreBodies::Full
        );
    }

    #[test]
    fn none_drops_bodies_and_headers_but_nothing_else() {
        let mut log = row("prompt", "answer");
        apply_store_bodies(&mut log, StoreBodies::None);
        assert!(log.request_body_preview.is_none());
        assert!(log.response_body_preview.is_none());
        assert!(log.request_headers.is_none());
        assert_eq!(log.endpoint, "/v1/chat");
    }

    #[test]
    fn redacted_removes_pii_from_stored_bodies() {
        let mut log = row(
            "my card is 4111-1111-1111-1111 and my email is bob@example.com",
            "thanks bob@example.com",
        );
        apply_store_bodies(&mut log, StoreBodies::Redacted);
        let req = log.request_body_preview.unwrap();
        assert!(!req.contains("4111"), "{req}");
        assert!(!req.contains("bob@example.com"), "{req}");
        assert!(req.contains("[REDACTED-"), "{req}");
        assert!(
            !log.response_body_preview
                .unwrap()
                .contains("bob@example.com")
        );
    }

    #[test]
    fn full_only_caps_the_size() {
        let big = "x".repeat(MAX_BODY_BYTES + 10);
        let mut log = row(&big, "short");
        apply_store_bodies(&mut log, StoreBodies::Full);
        let req = log.request_body_preview.unwrap();
        assert!(req.ends_with(TRUNCATION_MARKER));
        assert_eq!(req.len(), MAX_BODY_BYTES + TRUNCATION_MARKER.len());
        assert_eq!(log.response_body_preview.as_deref(), Some("short"));
    }

    #[test]
    fn truncation_respects_char_boundaries() {
        let s = "é".repeat(MAX_BODY_BYTES); // 2 bytes each
        let t = truncate_body(&s);
        assert!(t.ends_with(TRUNCATION_MARKER));
        assert!(std::str::from_utf8(t.as_bytes()).is_ok());
    }
}

#[cfg(test)]
mod content_digest_tests {
    use super::*;

    fn row(req: &str, resp: &str) -> ProxyLogInsert {
        super::tests::row(req, resp)
    }

    #[test]
    fn digests_are_sealed_over_the_full_content_and_survive_every_mode() {
        use sha2::Digest as _;
        let big = "x".repeat(MAX_BODY_BYTES + 100);
        let expect_req = hex::encode(sha2::Sha256::digest(big.as_bytes()));
        let expect_resp = hex::encode(sha2::Sha256::digest(b"call me at 555-0100"));
        for mode in [StoreBodies::None, StoreBodies::Redacted, StoreBodies::Full] {
            let mut l = row(&big, "call me at 555-0100");
            seal_content_digests(&mut l);
            apply_store_bodies(&mut l, mode);
            assert_eq!(
                l.request_content_hash.as_deref(),
                Some(expect_req.as_str()),
                "{mode:?}"
            );
            assert_eq!(
                l.response_content_hash.as_deref(),
                Some(expect_resp.as_str()),
                "{mode:?}"
            );
        }
    }

    #[test]
    fn sealing_is_idempotent_so_a_retried_flush_cannot_rehash_a_pruned_body() {
        let mut l = row("original", "original");
        seal_content_digests(&mut l);
        let first = l.request_content_hash.clone();
        apply_store_bodies(&mut l, StoreBodies::None);
        seal_content_digests(&mut l);
        assert_eq!(l.request_content_hash, first);
        assert!(l.request_body_preview.is_none());
    }

    #[test]
    fn a_row_with_no_body_gets_no_digest() {
        let mut l = row("", "");
        l.request_body_preview = None;
        l.response_body_preview = None;
        seal_content_digests(&mut l);
        assert_eq!(l.request_content_hash, None);
        assert_eq!(l.response_content_hash, None);
    }
}
