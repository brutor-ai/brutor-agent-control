// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The execution limits of an MCP call: its timeout, its payload ceiling and
//! its concurrency allowances, read from the two places an administrator
//! states MCP limits.
//!
//! * the group's `mcp_global_limits`, merged across its ancestor chain and
//!   folded (min wins) with the server's tenant-wide `mcp_servers.
//!   tenant_limits` ([`McpLimitSources::group`]);
//! * the group's binding of the server, `resource_group_server_configs.
//!   limits` ([`McpLimitSources::server`]).
//!
//! The two are read once per request (`usage::load_mcp_limit_sources`), and
//! the frequency limits (`usage::mcp_rate_dims`) and these limits are both
//! taken from that one read. The most restrictive statement wins: the lower
//! timeout and payload ceiling, and every concurrency allowance must admit.
//!
//! A field that is absent is unset. A field that is present and is not a
//! positive number refuses the call as `limits_unavailable` ([`exec_limits`]
//! returns [`LimitsUnreadable`]): a new control fails closed on a value it
//! cannot read (the frequency limits read such a value as unset). The
//! chain merge keeps an ancestor's limit over a level's unreadable value
//! (`usage::restrictive_merge`: an inherited limit is never removed), so
//! every level of the chain, and the tenant-wide default, is read on its
//! own too ([`first_unreadable`], [`McpLimitSources::unreadable`]).

use std::time::Duration;

use serde_json::Value;

use std::sync::atomic::{AtomicI64, Ordering};

use tokio::sync::OwnedSemaphorePermit;

use crate::models::auth::AuthContext;
use crate::services::cache::usage_state::{Dim, HoldGuard};
use crate::services::limits::{DimRefusal, LimitDim, RateRetry};
use crate::services::usage::McpRateLimitBlock;

/// `mcp_servers.max_concurrent_requests` when it is NULL: requests to one
/// server in flight at once on one gateway instance.
pub const DEFAULT_MAX_CONCURRENT_REQUESTS: i32 = 10;

/// What the gateway reads of one call's MCP limits.
#[derive(Debug, Clone, Default)]
pub struct McpLimitSources {
    /// The group's chain-merged `mcp_global_limits` folded with the server's
    /// `tenant_limits`.
    pub group: Option<Value>,
    /// The group's binding of this server (`resource_group_server_configs`).
    pub server: Option<Value>,
    /// The server's `tenant_limits` alone (the frequency limits keep a
    /// tenant-wide scope of their own).
    pub tenant: Option<Value>,
    /// The first execution limit stated at one level of the group's chain,
    /// or in `tenant`, that is not a positive number: the call is refused.
    pub unreadable: Option<LimitsUnreadable>,
}

/// The execution limits of one call, the most restrictive of both sources.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct McpExecLimits {
    /// `execution.timeout_seconds`: the lower of the two, at most
    /// `configured_duration::MAX_CONFIGURED_DURATION` (a positive number
    /// above it is readable and is the ceiling).
    pub timeout: Option<Duration>,
    /// `data.max_payload_size_mb` in bytes (`floor(mb * 1 048 576)`): the
    /// lower of the two.
    pub max_payload_bytes: Option<usize>,
    /// `execution.max_concurrency_per_group`: `[group source, server source]`.
    pub per_group: [Option<i64>; 2],
    /// `execution.max_concurrency_per_user`: `[group source, server source]`.
    pub per_user: [Option<i64>; 2],
}

/// A stated limit that is not a positive number.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LimitsUnreadable {
    /// `section.field` of the value.
    pub field: String,
}

impl std::fmt::Display for LimitsUnreadable {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "the MCP limit `{}` is not a positive number", self.field)
    }
}

impl std::error::Error for LimitsUnreadable {}

const MIB: f64 = 1_048_576.0;

/// The positive number at `section.field` of `doc`: `Ok(None)` when absent
/// (or JSON null), `Err` when present and not a positive number.
fn positive(
    doc: Option<&Value>,
    section: &str,
    field: &str,
) -> Result<Option<f64>, LimitsUnreadable> {
    let unreadable = || LimitsUnreadable {
        field: format!("{section}.{field}"),
    };
    let Some(doc) = doc else { return Ok(None) };
    let sec = match doc.get(section) {
        None | Some(Value::Null) => return Ok(None),
        Some(sec @ Value::Object(_)) => sec,
        Some(_) => {
            return Err(LimitsUnreadable {
                field: section.to_string(),
            });
        }
    };
    match sec.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Number(n)) => match n.as_f64() {
            Some(v) if v.is_finite() && v > 0.0 => Ok(Some(v)),
            _ => Err(unreadable()),
        },
        Some(_) => Err(unreadable()),
    }
}

/// A count limit: a positive whole number (an integral float such as `2.0`
/// is that number; `2.5` is unreadable).
fn count(doc: Option<&Value>, section: &str, field: &str) -> Result<Option<i64>, LimitsUnreadable> {
    match positive(doc, section, field)? {
        None => Ok(None),
        Some(v) if v.fract() == 0.0 && v <= i64::MAX as f64 => Ok(Some(v as i64)),
        Some(_) => Err(LimitsUnreadable {
            field: format!("{section}.{field}"),
        }),
    }
}

fn lower<T: PartialOrd + Copy>(a: Option<T>, b: Option<T>) -> Option<T> {
    match (a, b) {
        (Some(x), Some(y)) => Some(if y < x { y } else { x }),
        (x, None) => x,
        (None, y) => y,
    }
}

/// The execution limits both sources state, or the first value that cannot
/// be read.
pub fn exec_limits(src: &McpLimitSources) -> Result<McpExecLimits, LimitsUnreadable> {
    if let Some(e) = &src.unreadable {
        return Err(e.clone());
    }
    read_exec_limits(src.group.as_ref(), src.server.as_ref())
}

/// The first execution limit one of `levels` states that is not a positive
/// number, each level read on its own.
pub fn first_unreadable<'a>(
    levels: impl IntoIterator<Item = &'a Value>,
) -> Option<LimitsUnreadable> {
    levels
        .into_iter()
        .find_map(|level| read_exec_limits(Some(level), None).err())
}

fn read_exec_limits(
    g: Option<&Value>,
    s: Option<&Value>,
) -> Result<McpExecLimits, LimitsUnreadable> {
    let timeout = lower(
        positive(g, "execution", "timeout_seconds")?,
        positive(s, "execution", "timeout_seconds")?,
    )
    .and_then(crate::services::configured_duration::positive_secs);
    let max_payload_bytes = lower(
        positive(g, "data", "max_payload_size_mb")?,
        positive(s, "data", "max_payload_size_mb")?,
    )
    .map(|mb| ((mb * MIB).floor() as usize).max(1));
    Ok(McpExecLimits {
        timeout,
        max_payload_bytes,
        per_group: [
            count(g, "execution", "max_concurrency_per_group")?,
            count(s, "execution", "max_concurrency_per_group")?,
        ],
        per_user: [
            count(g, "execution", "max_concurrency_per_user")?,
            count(s, "execution", "max_concurrency_per_user")?,
        ],
    })
}

/// Who a per-user concurrency allowance belongs to, first match:
/// the agent (`agent:{id}`), the end user (`user:{id}`: every key and
/// session of one user share one allowance), the identity provider's
/// subject (`idp:{sha256 prefix}`), the API key of a group-scoped key
/// (`key:{id}`), else `anon` (every unidentified caller shares one
/// allowance: the most restrictive reading).
pub fn principal_key(auth: &AuthContext) -> String {
    use sha2::{Digest, Sha256};
    if let Some(agent) = auth.actor_agent_id.as_deref() {
        return format!("agent:{agent}");
    }
    if let Some(user) = auth.guardrail_user_id() {
        return format!("user:{user}");
    }
    if let Some(subject) = auth.idp_user_id.as_deref() {
        let digest = hex::encode(Sha256::digest(subject.as_bytes()));
        return format!("idp:{}", &digest[..32]);
    }
    if let Some(key) = auth.api_key_id.as_deref() {
        return format!("key:{key}");
    }
    "anon".to_string()
}

/// The Redis key of a concurrency allowance at `scope` (`group`, `server`,
/// `group-user`, `server-user`). Always under the calling group: an
/// ancestor's limit applies to each descendant group separately.
pub fn slot_key(
    scope: &str,
    tenant_id: &str,
    group_id: &str,
    mcp_server_id: &str,
    principal: &str,
) -> String {
    let base = format!("brutor:mcp_slot:t:{tenant_id}:g:{group_id}");
    match scope {
        "group" => base,
        "server" => format!("{base}:s:{mcp_server_id}"),
        "group-user" => format!("{base}:u:{principal}"),
        _ => format!("{base}:s:{mcp_server_id}:u:{principal}"),
    }
}

/// The refusal of a call whose stated limit cannot be read.
pub fn unreadable_block(e: &LimitsUnreadable) -> McpRateLimitBlock {
    tracing::error!(field = %e.field, "MCP limit unreadable; refusing (fail closed)");
    McpRateLimitBlock {
        scope: "limits",
        limit_type: "limits_unavailable",
        message: format!("Refused: {e}"),
        retry_after_secs: 5,
    }
}

/// The most any MCP request body may carry, whatever is configured.
pub const REQUEST_BODY_CEILING: usize = 10 * 1024 * 1024;

impl McpExecLimits {
    /// The request body ceiling: the configured payload limit, never above
    /// [`REQUEST_BODY_CEILING`] (a configured limit does not raise it).
    pub fn request_body_limit(&self) -> usize {
        self.max_payload_bytes
            .map_or(REQUEST_BODY_CEILING, |l| l.min(REQUEST_BODY_CEILING))
    }

    /// The concurrency allowances of one `tools/call` of `principal` in
    /// `group_id` on `mcp_server_id`, as in-flight slots admitted with its
    /// frequency limits in one step: none when nothing is configured (and
    /// then the admission is exactly what it was without them).
    pub fn slot_dims(
        &self,
        tenant_id: &str,
        group_id: &str,
        mcp_server_id: &str,
        principal: &str,
    ) -> Vec<LimitDim> {
        [
            ("group", self.per_group[0]),
            ("server", self.per_group[1]),
            ("group-user", self.per_user[0]),
            ("server-user", self.per_user[1]),
        ]
        .into_iter()
        .filter_map(|(scope, max)| {
            let max = max?;
            Some(LimitDim {
                dim: Dim::slot(
                    slot_key(scope, tenant_id, group_id, mcp_server_id, principal),
                    max,
                ),
                refusal: DimRefusal::Rate {
                    scope,
                    limit_type: "concurrency",
                    what: format!(
                        "MCP concurrency limit reached at {scope} scope ({max} simultaneous)"
                    ),
                    retry: RateRetry::Bucket {
                        cooldown_secs: None,
                    },
                },
            })
        })
        .collect()
    }
}

/// How reading an upstream answer failed.
#[derive(Debug)]
pub enum BodyReadError {
    /// The answer is larger than the payload limit (its bytes).
    TooLarge(usize),
    /// The upstream broke off.
    Upstream(String),
    /// The call's deadline passed.
    Deadline,
}

/// Read a buffered upstream answer: at most `limit` bytes of it (`None`:
/// no limit) and before `deadline` (`None`: none). An answer whose
/// `Content-Length` is over the limit is refused before any of it is read;
/// a read error is an error, never an empty answer.
pub async fn read_capped(
    mut resp: reqwest::Response,
    limit: Option<usize>,
    deadline: Option<tokio::time::Instant>,
) -> Result<bytes::Bytes, BodyReadError> {
    if let (Some(limit), Some(len)) = (limit, resp.content_length()) {
        if len > limit as u64 {
            return Err(BodyReadError::TooLarge(limit));
        }
    }
    let read = async {
        let mut buf = bytes::BytesMut::new();
        loop {
            match resp.chunk().await {
                Ok(Some(chunk)) => {
                    if let Some(limit) = limit {
                        if buf.len() + chunk.len() > limit {
                            return Err(BodyReadError::TooLarge(limit));
                        }
                    }
                    buf.extend_from_slice(&chunk);
                }
                Ok(None) => return Ok(buf.freeze()),
                Err(e) => return Err(BodyReadError::Upstream(e.to_string())),
            }
        }
    };
    match deadline {
        Some(d) => tokio::time::timeout_at(d, read)
            .await
            .unwrap_or(Err(BodyReadError::Deadline)),
        None => read.await,
    }
}

/// The JSON-RPC error of an answer over the payload limit (`id`: the
/// call's).
pub fn payload_too_large_error(id: Option<&Value>, limit: usize) -> Value {
    serde_json::json!({
        "jsonrpc": "2.0",
        "id": id.cloned().unwrap_or(Value::Null),
        "error": {
            "code": -32000,
            "message": format!(
                "The MCP server's answer exceeds the payload limit of {limit} bytes"
            ),
            "data": {
                "code": "payload_too_large",
                "direction": "response",
                "limit_bytes": limit,
            }
        }
    })
}

/// The permits of one MCP server on this gateway instance
/// (`mcp_servers.max_concurrent_requests`), and the size they were made
/// with.
#[derive(Debug)]
pub struct ServerPermits {
    size: usize,
    semaphore: std::sync::Arc<tokio::sync::Semaphore>,
}

impl ServerPermits {
    fn new(size: usize) -> Self {
        Self {
            size,
            semaphore: std::sync::Arc::new(tokio::sync::Semaphore::new(size)),
        }
    }

    /// Permits free right now.
    pub fn available_permits(&self) -> usize {
        self.semaphore.available_permits()
    }

    /// The size the permits were made with.
    pub fn size(&self) -> usize {
        self.size
    }
}

/// The permits of the server at `key` with `size` permits. A size that
/// differs from the one they were made with (the administrator changed
/// `max_concurrent_requests`) makes new ones of the new size: a changed
/// value takes effect without a restart. A call holding a permit of the old
/// ones keeps it until it ends, and gives it back to them.
pub fn server_permits(
    map: &dashmap::DashMap<String, ServerPermits>,
    key: &str,
    size: usize,
) -> std::sync::Arc<tokio::sync::Semaphore> {
    let mut entry = map
        .entry(key.to_string())
        .or_insert_with(|| ServerPermits::new(size));
    if entry.size != size {
        *entry = ServerPermits::new(size);
    }
    entry.semaphore.clone()
}

/// The refusal of a call for want of a permit of the server's
/// `max_concurrent_requests` on this gateway instance (scope
/// `server-instance`).
pub fn server_instance_block(max: i32) -> McpRateLimitBlock {
    McpRateLimitBlock {
        scope: "server-instance",
        limit_type: "concurrency",
        message: format!(
            "MCP concurrency limit reached at server-instance scope ({max} simultaneous \
             requests to this server on this gateway instance) — retry in 1s"
        ),
        retry_after_secs: 1,
    }
}

static LIVE_SLOT_HOLDS: AtomicI64 = AtomicI64::new(0);

/// Calls of this process holding concurrency slots right now (the
/// `mcp_concurrency_slot_holds` gauge). A number that only grows is a
/// leaked hold: renewed for as long as the process lives, it would lock its
/// group out.
pub fn live_slot_holds() -> i64 {
    LIVE_SLOT_HOLDS.load(Ordering::Acquire)
}

fn count_slot_hold(delta: i64) {
    let now = LIVE_SLOT_HOLDS.fetch_add(delta, Ordering::AcqRel) + delta;
    metrics::gauge!("mcp_concurrency_slot_holds").set(now as f64);
}

/// What one dispatched MCP call holds while it is in flight: the server's
/// permit on this instance and the `mcp-rate` hold when it carries
/// concurrency slots. Moved by value to whoever finishes the call (the
/// handler for a buffered answer, the relay task for a stream); never
/// cloned. Every way it ends gives both back: [`McpInFlight::finish`] ends
/// the hold as a completed count; dropping it (a hang-up, an error, a
/// panic) abandons the hold, which keeps its counts and frees its slots.
pub struct McpInFlight {
    permit: Option<OwnedSemaphorePermit>,
    slots: Option<HoldGuard>,
}

impl McpInFlight {
    /// The call is dispatched holding `permit` and `slots`. From here a
    /// dropped hold is abandoned, never released: the call ran.
    pub fn new(permit: Option<OwnedSemaphorePermit>, slots: Option<HoldGuard>) -> Self {
        if let Some(hold) = &slots {
            hold.mark_dispatched();
            count_slot_hold(1);
        }
        Self { permit, slots }
    }

    /// Give the server permit back now (a GET listener, at headers: a
    /// listener never ends, and open listeners must not starve the
    /// server's calls).
    pub fn release_permit(&mut self) {
        self.permit = None;
    }

    /// The call is over: its permit is back, its slots are freed and its
    /// counts stay spent.
    pub async fn finish(mut self) {
        self.permit = None;
        if let Some(hold) = self.slots.take() {
            count_slot_hold(-1);
            hold.commit().await;
        }
    }
}

impl Drop for McpInFlight {
    fn drop(&mut self) {
        // The hold's own Drop abandons it (it was marked dispatched).
        if self.slots.take().is_some() {
            count_slot_hold(-1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn sources(group: Value, server: Value) -> McpLimitSources {
        McpLimitSources {
            group: Some(group),
            server: Some(server),
            ..Default::default()
        }
    }

    #[test]
    fn absent_limits_are_unset() {
        assert_eq!(
            exec_limits(&McpLimitSources::default()).unwrap(),
            McpExecLimits::default()
        );
        let l = exec_limits(&sources(
            json!({"frequency": {}}),
            json!({"execution": null}),
        ))
        .unwrap();
        assert_eq!(l, McpExecLimits::default());
    }

    #[test]
    fn the_lower_of_both_sources_wins() {
        let l = exec_limits(&sources(
            json!({"execution": {"timeout_seconds": 30, "max_concurrency_per_group": 20,
                                 "max_concurrency_per_user": 5},
                   "data": {"max_payload_size_mb": 2}}),
            json!({"execution": {"timeout_seconds": 10.0, "max_concurrency_per_user": 1},
                   "data": {"max_payload_size_mb": 0.5}}),
        ))
        .unwrap();
        assert_eq!(l.timeout, Some(Duration::from_secs(10)));
        assert_eq!(l.max_payload_bytes, Some(524_288));
        assert_eq!(l.per_group, [Some(20), None]);
        assert_eq!(l.per_user, [Some(5), Some(1)]);
        let l = exec_limits(&sources(
            json!({"data": {"max_payload_size_mb": 2}}),
            json!({}),
        ))
        .unwrap();
        assert_eq!(l.max_payload_bytes, Some(2 * 1_048_576));
    }

    #[test]
    fn a_timeout_above_the_ceiling_reads_as_the_ceiling() {
        // A positive number is readable, however large: the control plane
        // accepts it, and a timeout no call can reach is the ceiling. The
        // call's deadline is taken from it, so it must be addable to now.
        let ceiling = crate::services::configured_duration::MAX_CONFIGURED_DURATION;
        for huge in [1e300, f64::MAX, 1.9e19, 1e15] {
            let l = exec_limits(&sources(
                json!({"execution": {"timeout_seconds": huge}}),
                json!({}),
            ))
            .unwrap();
            assert_eq!(l.timeout, Some(ceiling), "{huge}");
            let t = l.timeout.unwrap();
            let _deadline = tokio::time::Instant::now() + t;
            let _request_timeout = t + Duration::from_secs(5);
        }
        // The lower source still wins against a clamped one.
        let l = exec_limits(&sources(
            json!({"execution": {"timeout_seconds": 1e300}}),
            json!({"execution": {"timeout_seconds": 30}}),
        ))
        .unwrap();
        assert_eq!(l.timeout, Some(Duration::from_secs(30)));
    }

    #[test]
    fn a_stated_value_that_is_not_a_positive_number_is_unreadable() {
        for bad in [
            json!({"execution": {"timeout_seconds": 0}}),
            json!({"execution": {"timeout_seconds": -5}}),
            json!({"execution": {"timeout_seconds": "30"}}),
            json!({"execution": {"max_concurrency_per_user": 2.5}}),
            json!({"execution": {"max_concurrency_per_group": true}}),
            json!({"data": {"max_payload_size_mb": 0}}),
            json!({"execution": 3}),
        ] {
            assert!(
                exec_limits(&sources(json!({}), bad.clone())).is_err(),
                "{bad}"
            );
            assert!(
                exec_limits(&sources(bad.clone(), json!({}))).is_err(),
                "{bad}"
            );
        }
        let l = exec_limits(&sources(
            json!({"execution": {"max_concurrency_per_user": 2.0}}),
            json!({}),
        ))
        .unwrap();
        assert_eq!(
            l.per_user,
            [Some(2), None],
            "an integral float is that number"
        );
    }

    fn caller() -> AuthContext {
        AuthContext {
            credential_kind: crate::models::auth::CredentialKind::ApiKey,
            credential_assurance: Some(crate::models::auth::CredentialAssurance::Key),
            credential_ref: None,
            credential_detail: Default::default(),
            identity_verdict: None,
            auth_type: crate::models::auth::AuthType::ApiKey,
            tenant_id: "t1".into(),
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
            client_ip: None,
            session_key: None,
            session_token_type: None,
            resolved_group_id: None,
            actor_agent_id: None,
            agent_impl: crate::services::agent_impl::request_carriers(),
            admission_hold: None,
        }
    }

    /// One allowance per identity: an agent, then an end user (every key
    /// of one user shares it), then an identity provider's subject, then a
    /// group key; everyone else shares one.
    #[test]
    fn the_principal_is_the_callers_identity() {
        let mut agent = caller();
        agent.actor_agent_id = Some("ag-1".into());
        agent.api_key_id = Some("k-1".into());
        assert_eq!(principal_key(&agent), "agent:ag-1");

        let mut user_key = caller();
        user_key.api_key_id = Some("k-2".into());
        user_key.api_key_user_id = Some(42);
        let mut same_user_other_key = user_key.clone();
        same_user_other_key.api_key_id = Some("k-3".into());
        assert_eq!(principal_key(&user_key), "user:42");
        assert_eq!(
            principal_key(&user_key),
            principal_key(&same_user_other_key)
        );

        let mut idp = caller();
        idp.idp_user_id = Some("subject-1".into());
        let key = principal_key(&idp);
        assert!(key.starts_with("idp:") && key.len() == 4 + 32, "{key}");
        assert!(!key.contains("subject-1"));

        let mut group_key = caller();
        group_key.api_key_id = Some("k-4".into());
        group_key.api_key_group_id = Some("rg-1".into());
        group_key.gateway_user_id = Some("rg-1".into());
        let mut other_group_key = group_key.clone();
        other_group_key.api_key_id = Some("k-5".into());
        assert_eq!(principal_key(&group_key), "key:k-4");
        assert_ne!(principal_key(&group_key), principal_key(&other_group_key));

        assert_eq!(principal_key(&caller()), "anon");
    }

    #[test]
    fn the_request_ceiling_is_never_raised() {
        let none = McpExecLimits::default();
        assert_eq!(none.request_body_limit(), REQUEST_BODY_CEILING);
        let low = McpExecLimits {
            max_payload_bytes: Some(1000),
            ..Default::default()
        };
        assert_eq!(low.request_body_limit(), 1000);
        let high = McpExecLimits {
            max_payload_bytes: Some(50 * 1_048_576),
            ..Default::default()
        };
        assert_eq!(high.request_body_limit(), REQUEST_BODY_CEILING);
    }

    #[test]
    fn slots_are_dims_only_for_stated_allowances() {
        assert!(
            McpExecLimits::default()
                .slot_dims("t", "g", "s", "user:1")
                .is_empty(),
            "nothing configured: no slot, no Redis round trip"
        );
        let l = McpExecLimits {
            per_group: [Some(4), Some(2)],
            per_user: [None, Some(1)],
            ..Default::default()
        };
        let dims = l.slot_dims("t", "g", "s", "user:1");
        let keys: Vec<(&str, i64)> = dims
            .iter()
            .map(|d| match &d.dim.kind {
                crate::services::cache::usage_state::DimKind::Slot { key, max } => {
                    (key.as_str(), *max)
                }
                other => panic!("not a slot: {other:?}"),
            })
            .collect();
        assert_eq!(
            keys,
            vec![
                ("brutor:mcp_slot:t:t:g:g", 4),
                ("brutor:mcp_slot:t:t:g:g:s:s", 2),
                ("brutor:mcp_slot:t:t:g:g:s:s:u:user:1", 1),
            ]
        );
        for d in &dims {
            match &d.refusal {
                DimRefusal::Rate { limit_type, .. } => assert_eq!(*limit_type, "concurrency"),
                other => panic!("not a rate refusal: {other:?}"),
            }
        }
    }

    #[test]
    fn server_permits_follow_a_changed_size() {
        let map = dashmap::DashMap::new();
        let one = server_permits(&map, "t:s", 1);
        let held = one.clone().try_acquire_owned().expect("a permit");
        assert!(std::sync::Arc::ptr_eq(
            &one,
            &server_permits(&map, "t:s", 1)
        ));
        let three = server_permits(&map, "t:s", 3);
        assert_eq!(
            three.available_permits(),
            3,
            "the new size, without a restart"
        );
        assert_eq!(map.get("t:s").unwrap().size(), 3);
        drop(held);
        assert_eq!(
            one.available_permits(),
            1,
            "an old permit goes back to its own"
        );
        assert_eq!(three.available_permits(), 3);
    }

    #[test]
    fn every_level_is_read_on_its_own() {
        let ok = json!({"execution": {"timeout_seconds": 30}});
        let bad = json!({"execution": {"timeout_seconds": "30"}});
        assert_eq!(first_unreadable([&ok, &json!({})]), None);
        assert_eq!(
            first_unreadable([&ok, &bad]).map(|e| e.field),
            Some("execution.timeout_seconds".to_string())
        );
        let src = McpLimitSources {
            group: Some(ok),
            unreadable: first_unreadable([&bad]),
            ..Default::default()
        };
        assert!(
            exec_limits(&src).is_err(),
            "a level's unreadable value refuses"
        );
    }

    #[test]
    fn slot_keys_are_under_the_group() {
        assert_eq!(
            slot_key("group", "t", "g", "s", "user:1"),
            "brutor:mcp_slot:t:t:g:g"
        );
        assert_eq!(
            slot_key("server", "t", "g", "s", "user:1"),
            "brutor:mcp_slot:t:t:g:g:s:s"
        );
        assert_eq!(
            slot_key("group-user", "t", "g", "s", "user:1"),
            "brutor:mcp_slot:t:t:g:g:u:user:1"
        );
        assert_eq!(
            slot_key("server-user", "t", "g", "s", "user:1"),
            "brutor:mcp_slot:t:t:g:g:s:s:u:user:1"
        );
    }
}
