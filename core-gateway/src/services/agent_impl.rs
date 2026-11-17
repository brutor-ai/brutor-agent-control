// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Agent implementation identity
//!
//! Every call carries three facts about its caller that must be kept apart:
//! **who** is acting (the credential — [`crate::models::auth::AuthContext`]),
//! **what software** is acting (the implementation name), and **which
//! release** of it (version + build). This module owns the last two.
//!
//! ## Carriers and precedence
//!
//! Per request, `(name, version, source)` is taken from the first carrier
//! present, in this order:
//!
//! 1. **Verified token claims** — `software_id` / `software_version` /
//!    `software_build` of a verified workload token. Set only by the
//!    workload-identity branch of `services::auth` (trust `verified`).
//! 2. **MCP `clientInfo`** — the request's `_meta["io.modelcontextprotocol/
//!    clientInfo"]`, else the `initialize` request's `params.clientInfo`,
//!    else the `clientInfo` the session's `initialize` declared.
//! 3. **OpenTelemetry baggage** — `gen_ai.agent.{name,version,build}`.
//! 4. **Brutor headers** — `X-Brutor-Agent-{Name,Version,Build}`.
//!
//! A carrier is *present* when, after normalisation, it names a name or a
//! version. Name and version are never mixed across carriers: a verified
//! name with a declared version would claim a trust level neither value has.
//! The `User-Agent` is not a name source.
//!
//! **Build** comes from the same carrier when it carries one. Otherwise it
//! comes from the highest lower-precedence carrier whose `(name, version)`
//! EQUALS the winner's — never from a carrier that names a different
//! release. MCP `clientInfo` has no build field, so without this an agent
//! that sends both `clientInfo` and `X-Brutor-Agent-Build` recorded build
//! `''` on its MCP rows and the real build on its HTTP rows: one release
//! looked like two. A declared build under a verified (token) name/version
//! is accepted on the same terms — it only refines the release the verified
//! claims already name — and the recorded trust (`agent_impl_source`, and
//! the release row's trust) stays that of the name/version source: the build
//! itself is always as declared as its carrier.
//!
//! ## Normalisation
//!
//! Name `[a-z0-9._-]{1,64}` (lower-cased first), version
//! `[A-Za-z0-9.+_-]{1,64}`, build `[A-Za-z0-9:._-]{1,128}`, after trimming
//! surrounding whitespace. Anything else is **dropped and counted**
//! (`proxy_agent_impl_values_dropped_total{carrier,field}`) — never stored
//! raw. An empty value is simply absent.
//!
//! ## Where it is resolved, where it is recorded
//!
//! The request-scoped carriers (headers, baggage, MCP `clientInfo`, the
//! User-Agent, the client label) are captured ONCE per request by the
//! request-scope middleware ([`crate::middleware::run_end::RunEndLayer`],
//! via [`capture`]) and scoped as a task-local. Every `AuthContext` built
//! inside the request snapshots them ([`request_carriers`]), so rows built in
//! spawned tasks (streaming tails) carry them too. The single writer is
//! [`stamp`], called from `proxy_logger::build_proxy_log` — the one place
//! every `proxy_logs` row is constructed (guarded by
//! `tests/agent_impl_guards.rs`).
//!
//! ## It describes, it never authorises
//!
//! Nothing here authorises anything. The static guard fails if an access
//! path (grants, accepted callers, auth, run controls, autonomy) reads these
//! values.

use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::{HeaderMap, Method, Request, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::constants::agent_impl as wire;
use crate::models::db::ProxyLogInsert;

/// Bounds on a declared value (the column widths).
pub const MAX_NAME_LEN: usize = 64;
pub const MAX_VERSION_LEN: usize = 64;
pub const MAX_BUILD_LEN: usize = 128;

/// W3C Baggage, "Limits": a platform may drop members past 8192 bytes.
const MAX_BAGGAGE_BYTES: usize = 8192;

/// Same cap as the MCP handlers' own `to_bytes` (10 MiB): peeking must never
/// accept a body the handler would refuse, nor refuse one it would accept.
pub const MCP_BODY_LIMIT: usize = 10 * 1024 * 1024;

/// How long a session's `initialize` `clientInfo` is remembered after its
/// last use (refreshed on every read).
pub const SESSION_IDLE: Duration = Duration::from_secs(24 * 3600);
/// Sessions remembered by the single-replica store (no Redis configured).
const SESSION_CAPACITY: u64 = 100_000;
/// The longest a request waits on the shared session store before it treats
/// the session carrier as absent.
const SESSION_STORE_TIMEOUT: Duration = Duration::from_millis(500);
/// Redis key namespace of a session's `clientInfo`.
pub const SESSION_KEY_PREFIX: &str = "brutor:mcp_client_info:v1";
/// Domain tag of the credential hash in a session key.
const SESSION_CREDENTIAL_DOMAIN: &[u8] = b"brutor.mcp_session_credential.v1";

// ---------------------------------------------------------------------------
// Values
// ---------------------------------------------------------------------------

/// A normalised `(name, version, build)` from one carrier.
#[derive(Debug, Clone, Default, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
pub struct Release {
    pub name: Option<String>,
    pub version: Option<String>,
    pub build: Option<String>,
}

impl Release {
    /// Present = names the software or its release. A build alone is not an
    /// implementation identity.
    pub fn is_present(&self) -> bool {
        self.name.is_some() || self.version.is_some()
    }
}

/// The four carriers, in precedence order.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Carrier {
    Token,
    McpClientInfo,
    Baggage,
    Header,
}

impl Carrier {
    pub const PRECEDENCE: [Carrier; 4] = [
        Carrier::Token,
        Carrier::McpClientInfo,
        Carrier::Baggage,
        Carrier::Header,
    ];

    /// `proxy_logs.agent_impl_source`.
    pub fn source(self) -> &'static str {
        match self {
            Carrier::Token => wire::source::TOKEN,
            Carrier::McpClientInfo => wire::source::MCP_CLIENT_INFO,
            Carrier::Baggage => wire::source::BAGGAGE,
            Carrier::Header => wire::source::HEADER,
        }
    }

    /// Only a verified token claim is `verified`; everything else is what
    /// the caller said about itself.
    pub fn trust(self) -> &'static str {
        match self {
            Carrier::Token => wire::trust::VERIFIED,
            _ => wire::trust::DECLARED,
        }
    }
}

/// The trust a stored `agent_impl_source` value carries.
pub fn trust_for_source(source: &str) -> &'static str {
    if source == wire::source::TOKEN {
        wire::trust::VERIFIED
    } else {
        wire::trust::DECLARED
    }
}

/// Which value a drop was counted against.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Field {
    Name,
    Version,
    Build,
    ClientLabel,
}

impl Field {
    fn as_str(self) -> &'static str {
        match self {
            Field::Name => "name",
            Field::Version => "version",
            Field::Build => "build",
            Field::ClientLabel => "client_label",
        }
    }
}

/// One dropped value: which carrier sent it and which field it was.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Dropped {
    pub carrier: &'static str,
    pub field: Field,
}

/// Collects drops for one parse and emits the counter once per drop.
#[derive(Debug, Default)]
pub struct Drops(pub Vec<Dropped>);

impl Drops {
    fn record(&mut self, carrier: &'static str, field: Field) {
        metrics::counter!(
            "proxy_agent_impl_values_dropped_total",
            "carrier" => carrier,
            "field" => field.as_str(),
        )
        .increment(1);
        self.0.push(Dropped { carrier, field });
    }
}

/// Carrier label for a client-label drop (not one of the four sources).
const CLIENT_LABEL_CARRIER_EXPLICIT: &str = "x_brutor_client";
const CLIENT_LABEL_CARRIER_UA: &str = "user_agent";

// ---------------------------------------------------------------------------
// Normalisation — pure; the property test in this file pins the bounds
// ---------------------------------------------------------------------------

/// A present value outside its bound — dropped and counted, never stored.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Illegal;

/// `Ok(None)` = absent (missing or blank); `Err(Illegal)` = present but
/// outside its bound.
pub type Norm = Result<Option<String>, Illegal>;

fn bounded(raw: Option<&str>, max: usize, allowed: impl Fn(u8) -> bool, lower: bool) -> Norm {
    let Some(raw) = raw else { return Ok(None) };
    let t = raw.trim();
    if t.is_empty() {
        return Ok(None);
    }
    let v = if lower {
        t.to_ascii_lowercase()
    } else {
        t.to_string()
    };
    if v.len() > max || !v.bytes().all(allowed) {
        return Err(Illegal);
    }
    Ok(Some(v))
}

pub fn normalise_name(raw: Option<&str>) -> Norm {
    bounded(
        raw,
        MAX_NAME_LEN,
        |b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'_' | b'-'),
        true,
    )
}

pub fn normalise_version(raw: Option<&str>) -> Norm {
    bounded(
        raw,
        MAX_VERSION_LEN,
        |b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'+' | b'_' | b'-'),
        false,
    )
}

pub fn normalise_build(raw: Option<&str>) -> Norm {
    bounded(
        raw,
        MAX_BUILD_LEN,
        |b| b.is_ascii_alphanumeric() || matches!(b, b':' | b'.' | b'_' | b'-'),
        false,
    )
}

/// Normalise one carrier's raw values; every illegal value is dropped and
/// counted against `carrier`.
pub fn normalise_release(
    carrier: &'static str,
    name: Option<&str>,
    version: Option<&str>,
    build: Option<&str>,
    drops: &mut Drops,
) -> Release {
    let mut take = |n: Norm, field: Field| match n {
        Ok(v) => v,
        Err(Illegal) => {
            drops.record(carrier, field);
            None
        }
    };
    Release {
        name: take(normalise_name(name), Field::Name),
        version: take(normalise_version(version), Field::Version),
        build: take(normalise_build(build), Field::Build),
    }
}

// ---------------------------------------------------------------------------
// Client label
// ---------------------------------------------------------------------------

/// `X-Brutor-Client` when present and legal, else the product name of the
/// first User-Agent product token without its version (`python-httpx/0.28.1`
/// → `python-httpx`; `Mozilla/5.0 (…)` → `browser`), normalised like a name.
pub fn client_label(
    explicit: Option<&str>,
    user_agent: Option<&str>,
    drops: &mut Drops,
) -> Option<String> {
    match normalise_name(explicit) {
        Ok(Some(v)) => return Some(v),
        Ok(None) => {}
        Err(Illegal) => drops.record(CLIENT_LABEL_CARRIER_EXPLICIT, Field::ClientLabel),
    }
    let product = user_agent?
        .split_whitespace()
        .next()?
        .split('/')
        .next()
        .unwrap_or_default();
    if product.eq_ignore_ascii_case("mozilla") {
        return Some(wire::BROWSER_CLIENT_LABEL.to_string());
    }
    match normalise_name(Some(product)) {
        Ok(v) => v,
        Err(Illegal) => {
            drops.record(CLIENT_LABEL_CARRIER_UA, Field::ClientLabel);
            None
        }
    }
}

// ---------------------------------------------------------------------------
// Carrier parsing
// ---------------------------------------------------------------------------

/// Raw (un-normalised) values read from one carrier.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Raw {
    pub name: Option<String>,
    pub version: Option<String>,
    pub build: Option<String>,
}

impl Raw {
    fn normalise(&self, carrier: Carrier, drops: &mut Drops) -> Option<Release> {
        let r = normalise_release(
            carrier.source(),
            self.name.as_deref(),
            self.version.as_deref(),
            self.build.as_deref(),
            drops,
        );
        (r != Release::default()).then_some(r)
    }
}

fn percent_decode(s: &str) -> Option<String> {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hex = s.get(i + 1..i + 3)?;
            out.push(u8::from_str_radix(hex, 16).ok()?);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).ok()
}

/// W3C Baggage: `key=value;prop, key2=value2`. Values are percent-decoded;
/// the first occurrence of a key wins; members past 8192 bytes are ignored.
/// A value that fails to decode is kept raw, so normalisation drops (and
/// counts) it rather than it vanishing silently.
pub fn parse_baggage<'a>(values: impl IntoIterator<Item = &'a str>) -> Raw {
    let mut raw = Raw::default();
    let mut seen = 0usize;
    for header in values {
        for member in header.split(',') {
            seen += member.len() + 1;
            if seen > MAX_BAGGAGE_BYTES {
                return raw;
            }
            let kv = member.split(';').next().unwrap_or_default();
            let Some((k, v)) = kv.split_once('=') else {
                continue;
            };
            let v = v.trim();
            let v = percent_decode(v).unwrap_or_else(|| v.to_string());
            let slot = match k.trim() {
                wire::baggage::NAME => &mut raw.name,
                wire::baggage::VERSION => &mut raw.version,
                wire::baggage::BUILD => &mut raw.build,
                _ => continue,
            };
            if slot.is_none() {
                *slot = Some(v);
            }
        }
    }
    raw
}

/// First value of a header as text. A value that is not visible ASCII is
/// returned as a marker the normaliser always rejects, so it is counted.
fn header_text(headers: &HeaderMap, name: &str) -> Option<String> {
    headers.get(name).map(|v| {
        v.to_str()
            .map(str::to_string)
            .unwrap_or_else(|_| "\u{fffd}".into())
    })
}

/// The MCP `clientInfo` a JSON-RPC message declares: the per-request `_meta`
/// key first (2026-07-28), then `initialize`'s `params.clientInfo`. A batch
/// yields its first member that declares one.
pub fn mcp_client_info(message: &Value) -> Option<Raw> {
    if let Some(items) = message.as_array() {
        return items.iter().find_map(mcp_client_info);
    }
    let params = message.get("params")?;
    let from = |ci: &Value| -> Option<Raw> {
        let text = |k: &str| {
            ci.get(k).map(|v| match v {
                Value::String(s) => s.clone(),
                // A non-string is present but illegal: counted, not stored.
                other => format!("\u{fffd}{other}"),
            })
        };
        let raw = Raw {
            name: text("name"),
            version: text("version"),
            build: None,
        };
        (raw.name.is_some() || raw.version.is_some()).then_some(raw)
    };
    if let Some(ci) = params
        .get("_meta")
        .and_then(|m| m.get(crate::constants::mcp_protocol::meta::CLIENT_INFO))
        .and_then(from)
    {
        return Some(ci);
    }
    if message.get("method").and_then(Value::as_str) == Some(wire::mcp::INITIALIZE_METHOD) {
        return params.get(wire::mcp::INITIALIZE_CLIENT_INFO).and_then(from);
    }
    None
}

fn is_initialize(message: &Value) -> bool {
    match message {
        Value::Array(items) => items.iter().any(is_initialize),
        m => m.get("method").and_then(Value::as_str) == Some(wire::mcp::INITIALIZE_METHOD),
    }
}

// ---------------------------------------------------------------------------
// The per-request carrier set
// ---------------------------------------------------------------------------

/// Everything the request said about its software, normalised. Cloned onto
/// every `AuthContext` built inside the request.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Carriers {
    pub token: Option<Release>,
    pub mcp_client_info: Option<Release>,
    pub baggage: Option<Release>,
    pub header: Option<Release>,
    /// The raw `User-Agent`, for `proxy_logs.user_agent` and the
    /// implementation fingerprint. Never a name source.
    pub user_agent: Option<String>,
    /// The normalised client label.
    pub client_label: Option<String>,
}

/// The resolved release of one call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resolved {
    pub name: Option<String>,
    pub version: Option<String>,
    pub build: Option<String>,
    pub source: &'static str,
}

impl Resolved {
    pub fn trust(&self) -> &'static str {
        trust_for_source(self.source)
    }
}

impl Carriers {
    fn get(&self, c: Carrier) -> Option<&Release> {
        match c {
            Carrier::Token => self.token.as_ref(),
            Carrier::McpClientInfo => self.mcp_client_info.as_ref(),
            Carrier::Baggage => self.baggage.as_ref(),
            Carrier::Header => self.header.as_ref(),
        }
    }

    /// Name and version from the highest-trust carrier present; the
    /// build from that carrier, else from the highest lower-trust carrier
    /// naming the SAME `(name, version)` (see the module docs).
    pub fn resolve(&self) -> Option<Resolved> {
        let (at, winner) = Carrier::PRECEDENCE
            .iter()
            .enumerate()
            .find_map(|(i, c)| self.get(*c).filter(|r| r.is_present()).map(|r| (i, (c, r))))?;
        let (carrier, release) = winner;
        let build = release.build.clone().or_else(|| {
            Carrier::PRECEDENCE[at + 1..].iter().find_map(|c| {
                self.get(*c)
                    .filter(|r| r.name == release.name && r.version == release.version)
                    .and_then(|r| r.build.clone())
            })
        });
        Some(Resolved {
            name: release.name.clone(),
            version: release.version.clone(),
            build,
            source: carrier.source(),
        })
    }

    /// The header-borne carriers of a request (baggage, Brutor headers,
    /// User-Agent, client label). MCP `clientInfo` needs the body and the
    /// token needs verification; both are added by their owners.
    pub fn from_headers(headers: &HeaderMap, drops: &mut Drops) -> Carriers {
        let baggage = parse_baggage(
            headers
                .get_all(wire::headers::BAGGAGE)
                .iter()
                .filter_map(|v| v.to_str().ok()),
        )
        .normalise(Carrier::Baggage, drops);
        let header = Raw {
            name: header_text(headers, wire::headers::X_BRUTOR_AGENT_NAME),
            version: header_text(headers, wire::headers::X_BRUTOR_AGENT_VERSION),
            build: header_text(headers, wire::headers::X_BRUTOR_AGENT_BUILD),
        }
        .normalise(Carrier::Header, drops);
        let user_agent = headers
            .get(axum::http::header::USER_AGENT)
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        let explicit = header_text(headers, wire::headers::X_BRUTOR_CLIENT);
        Carriers {
            token: None,
            mcp_client_info: None,
            baggage,
            header,
            client_label: client_label(explicit.as_deref(), user_agent.as_deref(), drops),
            user_agent,
        }
    }

    /// The same carriers with the verified token's claims on top. Called
    /// only by the workload-identity branch of `services::auth`, after the
    /// token's signature, issuer and expiry verified.
    pub fn with_verified_token(mut self, token: Option<Release>) -> Carriers {
        self.token = token.filter(Release::is_present);
        self
    }
}

/// Normalise a verified token's software claims (counted as `token` drops).
pub fn release_from_claims(
    software_id: Option<&str>,
    software_version: Option<&str>,
    software_build: Option<&str>,
) -> Option<Release> {
    let mut drops = Drops::default();
    let r = normalise_release(
        Carrier::Token.source(),
        software_id,
        software_version,
        software_build,
        &mut drops,
    );
    r.is_present().then_some(r)
}

// ---------------------------------------------------------------------------
// Recording — the ONE writer of the proxy_logs columns
// ---------------------------------------------------------------------------

/// Stamp the resolved release, its source and the client label onto a
/// `proxy_logs` row. Called from `build_proxy_log` only. The raw
/// `user_agent` is filled in where the route did not set it — the value is
/// the header verbatim either way.
pub fn stamp(log: &mut ProxyLogInsert, carriers: &Carriers) {
    let resolved = carriers.resolve();
    log.agent_impl_name = resolved.as_ref().and_then(|r| r.name.clone());
    log.agent_impl_version = resolved.as_ref().and_then(|r| r.version.clone());
    log.agent_impl_build = resolved.as_ref().and_then(|r| r.build.clone());
    log.agent_impl_source = resolved.map(|r| r.source.to_string());
    log.client_label = carriers.client_label.clone();
    if log.user_agent.is_none() {
        log.user_agent = carriers.user_agent.clone();
    }
}

// ---------------------------------------------------------------------------
// Implementation fingerprint
// ---------------------------------------------------------------------------

/// Domain tag of the canonical fingerprint encoding. A change to the
/// encoding is a new version, never an edit of this one.
pub const FINGERPRINT_DOMAIN: &[u8] = b"brutor.implementation_fingerprint.v1";

/// `sha256:` + 64 lowercase hex over a canonical, unambiguous encoding of
/// the run's first action's software surface:
///
/// ```text
/// FINGERPRINT_DOMAIN
/// || field(user_agent) || field(mcp_client_name) || field(mcp_client_version)
/// || field(prompt_fingerprint)
///
/// field(None)    = 0x00
/// field(Some(s)) = 0x01 || u32_be(len(utf8(s))) || utf8(s)
/// ```
///
/// Length-prefixing makes the encoding injective (no separator a value could
/// contain). `None` when every input is absent — there is nothing to
/// fingerprint, and a constant hash would read as "same software" for every
/// silent agent.
pub fn implementation_fingerprint(
    user_agent: Option<&str>,
    mcp_client_name: Option<&str>,
    mcp_client_version: Option<&str>,
    prompt_fingerprint: Option<&str>,
) -> Option<String> {
    let fields = [
        user_agent,
        mcp_client_name,
        mcp_client_version,
        prompt_fingerprint,
    ];
    if fields.iter().all(Option::is_none) {
        return None;
    }
    let mut h = Sha256::new();
    h.update(FINGERPRINT_DOMAIN);
    for f in fields {
        match f {
            None => h.update([0u8]),
            Some(s) => {
                h.update([1u8]);
                h.update((s.len() as u32).to_be_bytes());
                h.update(s.as_bytes());
            }
        }
    }
    Some(format!("sha256:{}", hex::encode(h.finalize())))
}

// ---------------------------------------------------------------------------
// Request scope
// ---------------------------------------------------------------------------

tokio::task_local! {
    /// The carriers captured for the current request by [`capture`].
    static REQUEST_CARRIERS: Carriers;
}

/// The current request's carriers; empty outside a request scope
/// (background work, unit tests), which records nothing.
pub fn request_carriers() -> Carriers {
    REQUEST_CARRIERS.try_with(Clone::clone).unwrap_or_default()
}

/// Run `fut` with `carriers` as the request's carriers.
pub async fn scope<F: std::future::Future>(carriers: Carriers, fut: F) -> F::Output {
    REQUEST_CARRIERS.scope(carriers, fut).await
}

// ---------------------------------------------------------------------------
// The session carrier, the second in precedence: an `initialize`'s clientInfo, remembered
// for the session's later requests — across replicas.
// ---------------------------------------------------------------------------

/// Where a session's `initialize` `clientInfo` is kept.
///
/// A session's later requests may land on any replica, so the declaration
/// lives in the store the core already shares across replicas (Redis, the
/// same connection manager the usage counters, run clock and routing state
/// use). There is deliberately NO in-process cache in front of it: a cached
/// copy would keep resolving a session after a re-`initialize` bound it to a
/// different declaration on another replica, or after a `DELETE` there — a
/// stale identity is worse than an absent one.
///
/// Without Redis configured the gateway is a single replica by definition
/// (startup warns that multi-pod deployments need Redis), and the store is
/// in-process. With Redis configured but failing, the session carrier
/// resolves as ABSENT — the request falls back to its per-request carriers,
/// never fails and never guesses — and the failure is counted in
/// `proxy_agent_impl_session_store_unavailable_total{op}`.
#[derive(Clone)]
pub struct SessionStore {
    backend: SessionBackend,
    /// Store operations that could not be answered (mirrors the metric;
    /// readable for diagnostics and tests).
    unavailable: Arc<std::sync::atomic::AtomicU64>,
}

#[derive(Clone)]
enum SessionBackend {
    Shared(Box<redis::aio::ConnectionManager>),
    SingleReplica(Arc<moka::sync::Cache<String, Release>>),
}

impl SessionStore {
    /// The shared store when Redis is configured, else the single-replica
    /// one.
    pub fn new(redis: Option<redis::aio::ConnectionManager>) -> Self {
        let backend = match redis {
            Some(r) => SessionBackend::Shared(Box::new(r)),
            None => SessionBackend::SingleReplica(Arc::new(
                moka::sync::Cache::builder()
                    .max_capacity(SESSION_CAPACITY)
                    .time_to_idle(SESSION_IDLE)
                    .build(),
            )),
        };
        SessionStore {
            backend,
            unavailable: Arc::default(),
        }
    }

    /// How many store operations could not be answered.
    pub fn unavailable_count(&self) -> u64 {
        self.unavailable.load(std::sync::atomic::Ordering::Relaxed)
    }

    pub fn is_shared(&self) -> bool {
        matches!(self.backend, SessionBackend::Shared(_))
    }

    fn unavailable(&self, op: &'static str, error: &dyn std::fmt::Display) {
        self.unavailable
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        metrics::counter!("proxy_agent_impl_session_store_unavailable_total", "op" => op)
            .increment(1);
        // Downstream (Redis): warn, never error — the request carries on
        // with its per-request carriers.
        tracing::warn!(op, %error, "MCP session store unavailable; session clientInfo treated as absent");
    }

    /// Bind a session's declaration (TTL [`SESSION_IDLE`]).
    pub async fn bind(&self, key: &SessionKey, release: &Release) {
        match &self.backend {
            SessionBackend::SingleReplica(c) => c.insert(key.0.clone(), release.clone()),
            SessionBackend::Shared(r) => {
                let value = match serde_json::to_string(release) {
                    Ok(v) => v,
                    Err(e) => {
                        tracing::error!(error = %e, "MCP session clientInfo did not serialise");
                        return;
                    }
                };
                let mut conn = (**r).clone();
                let mut cmd = redis::cmd("SET");
                cmd.arg(&key.0)
                    .arg(value)
                    .arg("EX")
                    .arg(SESSION_IDLE.as_secs());
                let op = cmd.query_async::<()>(&mut conn);
                match tokio::time::timeout(SESSION_STORE_TIMEOUT, op).await {
                    Ok(Ok(())) => {}
                    Ok(Err(e)) => self.unavailable("bind", &e),
                    Err(e) => self.unavailable("bind", &e),
                }
            }
        }
    }

    /// The session's declaration, refreshing its idle TTL; `None` when the
    /// session declared nothing, expired, was deleted — or the store could
    /// not answer.
    pub async fn resolve(&self, key: &SessionKey) -> Option<Release> {
        match &self.backend {
            SessionBackend::SingleReplica(c) => c.get(&key.0),
            SessionBackend::Shared(r) => {
                let mut conn = (**r).clone();
                let mut cmd = redis::cmd("GETEX");
                cmd.arg(&key.0).arg("EX").arg(SESSION_IDLE.as_secs());
                let op = cmd.query_async::<Option<String>>(&mut conn);
                let raw = match tokio::time::timeout(SESSION_STORE_TIMEOUT, op).await {
                    Ok(Ok(v)) => v?,
                    Ok(Err(e)) => {
                        self.unavailable("resolve", &e);
                        return None;
                    }
                    Err(e) => {
                        self.unavailable("resolve", &e);
                        return None;
                    }
                };
                match serde_json::from_str::<Release>(&raw) {
                    Ok(r) => Some(r),
                    Err(e) => {
                        // We wrote it: a value we cannot read is our bug.
                        tracing::error!(error = %e, "MCP session clientInfo in the store is unreadable");
                        None
                    }
                }
            }
        }
    }

    /// Forget a session's declaration (the session's `DELETE`).
    pub async fn forget(&self, key: &SessionKey) {
        match &self.backend {
            SessionBackend::SingleReplica(c) => c.invalidate(&key.0),
            SessionBackend::Shared(r) => {
                let mut conn = (**r).clone();
                let mut cmd = redis::cmd("DEL");
                cmd.arg(&key.0);
                let op = cmd.query_async::<i64>(&mut conn);
                match tokio::time::timeout(SESSION_STORE_TIMEOUT, op).await {
                    Ok(Ok(_)) => {}
                    Ok(Err(e)) => self.unavailable("forget", &e),
                    Err(e) => self.unavailable("forget", &e),
                }
            }
        }
    }
}

/// The session scope of a request: its tenant and a hash of the credential
/// it presented, so one caller can never adopt another caller's session
/// declaration by naming its session id. (Declared data either way — this is
/// hygiene, not access control.) The raw credential never leaves this
/// function.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionScope {
    tenant: String,
    credential_hash: String,
}

impl SessionScope {
    pub fn of(headers: &HeaderMap) -> Self {
        let tenant = headers
            .get(crate::constants::headers::X_TENANT_ID)
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_string();
        let credential = [
            crate::constants::headers::X_API_KEY,
            "authorization",
            crate::constants::headers::X_GATEWAY_AUTHORIZATION,
        ]
        .iter()
        .find_map(|h| headers.get(*h))
        .map(|v| v.as_bytes().to_vec())
        .unwrap_or_default();
        SessionScope {
            tenant,
            credential_hash: credential_hash(&credential),
        }
    }

    /// The store key of `session_id` in this scope.
    pub fn key(&self, session_id: &str) -> SessionKey {
        SessionKey(format!(
            "{SESSION_KEY_PREFIX}:{}:{}:{}",
            key_part(&self.tenant),
            self.credential_hash,
            key_part(session_id)
        ))
    }
}

/// `sha256(domain || credential)`, lowercase hex.
pub fn credential_hash(credential: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(SESSION_CREDENTIAL_DOMAIN);
    h.update(credential);
    hex::encode(h.finalize())
}

/// Escape the key separator so a tenant or session id containing `:` cannot
/// collide with another (tenant, credential, session) triple.
fn key_part(s: &str) -> String {
    s.replace('%', "%25").replace(':', "%3A")
}

/// A session store key (`brutor:mcp_client_info:v1:<tenant>:<cred-hash>:<sid>`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionKey(String);

impl SessionKey {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// An `initialize` whose `clientInfo` must be bound to the session id the
/// server answers with.
#[derive(Debug, Clone)]
pub struct PendingSession {
    scope: SessionScope,
    release: Release,
}

fn is_mcp_path(path: &str) -> bool {
    path.starts_with(crate::constants::routes::MCP_PROXY_PREFIX)
        || path.starts_with(crate::constants::routes::VMCP_PROXY_PREFIX)
}

/// Capture the request's carriers. For a POST to an MCP surface the body is
/// read (bounded by [`MCP_BODY_LIMIT`], the handlers' own cap) to find
/// `clientInfo`, then handed back untouched; a request whose declared length
/// exceeds the cap is passed through for the handler to refuse as before.
///
/// Returns the request to forward, its carriers, and — for an `initialize` —
/// the declaration to bind to the session id in the response
/// ([`bind_session`]).
pub async fn capture(
    req: Request<Body>,
    sessions: &SessionStore,
) -> Result<(Request<Body>, Carriers, Option<PendingSession>), Response> {
    let mut drops = Drops::default();
    let mut carriers = Carriers::from_headers(req.headers(), &mut drops);
    if !is_mcp_path(req.uri().path()) {
        return Ok((req, carriers, None));
    }
    let scope = SessionScope::of(req.headers());
    let session_id = req
        .headers()
        .get(crate::constants::headers::MCP_SESSION_ID)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);

    let mut pending = None;
    let mut req = req;
    let declared_len = req
        .headers()
        .get(axum::http::header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.parse::<usize>().ok());
    if req.method() == Method::POST && declared_len.is_none_or(|n| n <= MCP_BODY_LIMIT) {
        let (parts, body) = req.into_parts();
        let bytes = match axum::body::to_bytes(body, MCP_BODY_LIMIT).await {
            Ok(b) => b,
            Err(e) => {
                return Err((
                    StatusCode::PAYLOAD_TOO_LARGE,
                    axum::Json(serde_json::json!({ "error": e.to_string() })),
                )
                    .into_response());
            }
        };
        if let Ok(message) = serde_json::from_slice::<Value>(&bytes) {
            carriers.mcp_client_info = mcp_client_info(&message)
                .and_then(|raw| raw.normalise(Carrier::McpClientInfo, &mut drops))
                .filter(Release::is_present);
            if is_initialize(&message) {
                if let Some(release) = carriers.mcp_client_info.clone() {
                    pending = Some(PendingSession {
                        scope: scope.clone(),
                        release,
                    });
                }
            }
        }
        req = Request::from_parts(parts, Body::from(bytes));
    }
    if let Some(sid) = session_id.as_deref() {
        let key = scope.key(sid);
        if req.method() == Method::DELETE {
            // Session teardown: the declaration dies with the session.
            sessions.forget(&key).await;
        } else if carriers.mcp_client_info.is_none() {
            carriers.mcp_client_info = sessions.resolve(&key).await;
        }
    }
    Ok((req, carriers, pending))
}

/// Bind an `initialize`'s `clientInfo` to the session id the server issued,
/// so the session's later requests resolve it (the second carrier, MCP 2025-11-25) — on
/// whichever replica they land.
pub async fn bind_session(
    sessions: &SessionStore,
    pending: &PendingSession,
    response_headers: &HeaderMap,
) {
    if let Some(sid) = response_headers
        .get(crate::constants::headers::MCP_SESSION_ID)
        .and_then(|v| v.to_str().ok())
    {
        sessions
            .bind(&pending.scope.key(sid), &pending.release)
            .await;
    }
}

/// Caller-controlled context that must never be forwarded to
/// an upstream. Every loop that copies inbound headers upstream consults
/// this (pinned by `tests/agent_impl_guards.rs`).
pub fn is_caller_only_header(name: &str) -> bool {
    name.eq_ignore_ascii_case(wire::headers::BAGGAGE)
}

#[cfg(test)]
#[path = "agent_impl_tests.rs"]
mod tests;
