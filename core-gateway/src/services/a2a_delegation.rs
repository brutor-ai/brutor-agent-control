// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! A2A Delegation Chain — HMAC-signed propagation.
//!
//! When agent A calls agent B via Brutor's A2A proxy, B's call to C via
//! the proxy needs to know:
//!
//!   * the **root task ID** — the ULID of the originating A2A call, so
//!     every step in the chain audits against the same correlation key
//!     in proxy_logs.
//!   * the **parent task ID** — the step directly above this one.
//!   * the **depth** — how far from root, used to enforce the
//!     policy-defined `max_delegation_depth` cap at the gateway edge
//!     without trusting the caller's self-reported number.
//!
//! These travel as HTTP headers on every inter-agent call the proxy
//! either produces or consumes:
//!
//!   x-brutor-delegation-root   : <ULID>
//!   x-brutor-delegation-parent : <ULID>
//!   x-brutor-delegation-depth  : <u32>
//!   x-brutor-delegation-sig    : base64url HMAC-SHA256 over the above
//!
//! The HMAC is keyed by a per-tenant root key derived from the gateway's
//! ENCRYPTION_KEY. Spoofing depth would let a malicious caller bypass
//! the depth cap; the signature is what stops that. Because the key
//! never leaves the gateway process, even a rogue downstream agent
//! cannot forge a chain — it can only forward what the gateway signed.
//!
//! This module intentionally has zero knowledge of HTTP or Axum. It's
//! a pure function library over headers so it's trivial to unit-test
//! and can be used equally from the outbound route, the
//! inbound route, and the Mission Control aggregation queries.

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use std::fmt;
use std::num::ParseIntError;
use ulid::Ulid;

type HmacSha256 = Hmac<Sha256>;

// ---------------------------------------------------------------------------
// Header names
// ---------------------------------------------------------------------------

pub const HEADER_ROOT: &str = "x-brutor-delegation-root";
pub const HEADER_PARENT: &str = "x-brutor-delegation-parent";
pub const HEADER_DEPTH: &str = "x-brutor-delegation-depth";
pub const HEADER_SIG: &str = "x-brutor-delegation-sig";
/// The agent principal (actor) that originated the chain — propagated
/// unchanged across every step so a downstream peer can prove *who* is really
/// acting, not just which task. Optional; absent on legacy (v1) chains.
pub const HEADER_ACTOR: &str = "x-brutor-delegation-actor";
/// The delegating subject on whose behalf the actor acts, encoded `kind:id`
/// (e.g. `user:42`). Optional; absent on legacy (v1) chains.
pub const HEADER_SUBJECT: &str = "x-brutor-delegation-subject";

/// Client-asserted run correlation.
///
/// An agent that drives its own control flow — a LangGraph or CrewAI process
/// calling the gateway step by step — never receives a signed chain, because
/// the gateway only signs chains it forwards. Every one of its calls would
/// otherwise mint a fresh root and land as a separate run, which makes the
/// run ledger useless for exactly the systems it exists to describe.
///
/// This header lets such a client say "these steps are one unit of work". It
/// is a *correlation* assertion, never a chain assertion: the client supplies
/// an opaque id of its own choosing and the gateway derives the real
/// `root_task_id` from it (see [`derive_asserted_root`]). The client cannot
/// name another principal's run, because the derivation is keyed by the
/// tenant secret and bound to the caller's own credential.
pub const HEADER_RUN_ID: &str = "x-brutor-run-id";

/// Return the run this response belongs to (`x-brutor-run-id`, the response
/// counterpart of the inbound header) so a client can close the run with
/// `POST /v1/runs/{root}/end` or attribute feedback to it. Every LLM surface
/// stamps it the same way, the native Anthropic `/v1/messages` included:
/// without it an Anthropic-SDK agent has no way to learn its root.
pub fn stamp_run_root(headers: &mut axum::http::HeaderMap, root: &impl std::fmt::Display) {
    if let Ok(v) = root.to_string().parse() {
        headers.insert(crate::constants::headers::X_BRUTOR_RUN_ID, v);
    }
}

/// Client-asserted **session** correlation.
///
/// A run is one unit of work — for a chat assistant, one user message. A
/// session is the conversation those runs belong to, and it can last months.
/// The distinction matters because they answer different questions: a run is
/// the unit assurance *compares* (drift baselines need ~50 of them, envelope
/// grading 20), while a session is the scope a human's spend *accumulates*
/// in. A conversation that quietly costs $500 trips no per-run ceiling and
/// no daily user quota; only a session scope sees it.
///
/// Like the run assertion this is a *correlation* claim, not an authorisation
/// one: the client sends an opaque id of its own choosing and the gateway
/// derives the real session key from it (see [`derive_asserted_session`]).
/// A caller cannot name another principal's session, because the derivation
/// is keyed by the tenant secret and bound to the caller's own credential.
///
/// There is deliberately no "session start" signal. The first request
/// carrying a new id starts the session, exactly as the first step of a run
/// starts the run — a separate start call would be one more thing a client
/// can forget, and would make the gateway stateful for no gain.
pub const HEADER_SESSION_ID: &str = "x-brutor-session-id";

/// Upper bound on an asserted session id, mirroring [`MAX_RUN_ID_LEN`].
pub const MAX_SESSION_ID_LEN: usize = 200;

// ---------------------------------------------------------------------------
// Hard caps
// ---------------------------------------------------------------------------

/// Absolute maximum depth the gateway will forward, regardless of what
/// policy allows. Belt-and-braces — if a misconfigured policy disables
/// the depth cap, this prevents runaway chains from turning into
/// exponential fan-outs. Tuned conservatively; the expected production
/// range is 3–8 based on common multi-agent patterns.
pub const ABSOLUTE_MAX_DEPTH: u32 = 32;

/// Default freshness window (seconds) for an inbound delegation chain. A
/// chain whose most recent step (parent ULID) is older than this is rejected
/// as a replay. Generous by default so legitimate slow or clock-skewed chains
/// pass — the point is to bound replay to minutes instead of forever, not to
/// enforce a tight SLA. Override with `BRUTOR_A2A_DELEGATION_MAX_AGE_SECS`.
pub const DEFAULT_DELEGATION_MAX_AGE_SECS: u64 = 600;

/// Clock-skew tolerance (seconds) for the freshness check, applied in both
/// directions — a parent ULID minted slightly in the future on another host
/// must not be rejected.
pub const DELEGATION_FRESHNESS_LEEWAY_SECS: u64 = 60;

/// The freshness window `env` configures
/// (`BRUTOR_A2A_DELEGATION_MAX_AGE_SECS`). 0 and anything that is not a
/// number are refused: the gateway does not start with them
/// (`config::check`).
pub fn max_age_from(env: &crate::env_registry::Env) -> Result<u64, crate::env_registry::EnvError> {
    crate::env_vars::positive(env, &crate::env_vars::BRUTOR_A2A_DELEGATION_MAX_AGE_SECS)
}

/// The configured freshness window, read once.
pub fn delegation_max_age_secs() -> u64 {
    static MAX_AGE: std::sync::OnceLock<u64> = std::sync::OnceLock::new();
    *MAX_AGE.get_or_init(|| {
        max_age_from(&crate::env_vars::process()).unwrap_or(DEFAULT_DELEGATION_MAX_AGE_SECS)
    })
}

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

#[derive(Debug, PartialEq, Eq)]
pub enum DelegationError {
    /// Signature header missing or malformed.
    MissingSignature,
    /// Signature present but does not verify against the tenant key.
    SignatureMismatch,
    /// One of the required headers failed to parse as its expected type.
    MalformedHeader(&'static str),
    /// Depth overflowed the hard cap — hang up before forwarding.
    DepthOverflow { depth: u32 },
    /// The chain's most recent step (parent ULID) is older than the freshness
    /// window — a captured header set being replayed. Bounds replay to the
    /// window instead of forever.
    Stale { age_secs: u64, max_age_secs: u64 },
}

impl fmt::Display for DelegationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            DelegationError::MissingSignature => {
                write!(f, "Delegation signature header missing")
            }
            DelegationError::SignatureMismatch => {
                write!(f, "Delegation signature did not verify")
            }
            DelegationError::MalformedHeader(which) => {
                write!(f, "Malformed delegation header: {}", which)
            }
            DelegationError::DepthOverflow { depth } => write!(
                f,
                "Delegation depth {} exceeds absolute cap {}",
                depth, ABSOLUTE_MAX_DEPTH
            ),
            DelegationError::Stale {
                age_secs,
                max_age_secs,
            } => write!(
                f,
                "Delegation chain is stale ({age_secs}s old > {max_age_secs}s window) — possible replay"
            ),
        }
    }
}

impl std::error::Error for DelegationError {}

impl From<ParseIntError> for DelegationError {
    fn from(_: ParseIntError) -> Self {
        DelegationError::MalformedHeader("depth")
    }
}

// ---------------------------------------------------------------------------
// Chain header struct
// ---------------------------------------------------------------------------

/// A parsed, verified delegation chain header set.
///
/// Construct only through `DelegationChain::root()` (for the originating
/// step) or `DelegationChain::parse_and_verify()` (for an inbound step) —
/// those are the only paths that guarantee invariants (signature valid,
/// depth ≤ ABSOLUTE_MAX_DEPTH).
#[derive(Debug, Clone)]
pub struct DelegationChain {
    pub root_task_id: Ulid,
    pub parent_task_id: Ulid,
    pub depth: u32,
    /// The originating agent principal (actor), propagated unchanged across
    /// steps. `None` on legacy (v1) chains and on non-agent traffic.
    pub actor_agent_id: Option<String>,
    /// The delegating subject, encoded `kind:id` (e.g. `user:42`). Propagated
    /// unchanged across steps. `None` on legacy chains.
    pub subject: Option<String>,
}

impl DelegationChain {
    /// Build a root (depth=0) chain for a new A2A call. The root and
    /// parent IDs are the same — at root, the "parent" of the current
    /// step is itself. The next step flips this: that step's parent
    /// becomes this current_task_id.
    pub fn root(root_task_id: Ulid) -> Self {
        Self {
            root_task_id,
            parent_task_id: root_task_id,
            depth: 0,
            actor_agent_id: None,
            subject: None,
        }
    }

    /// Attach the originating principal (agent actor + delegating subject) to a
    /// root chain. Both are propagated unchanged to every downstream step and
    /// folded into the HMAC so a peer can't forge "this came from principal X".
    pub fn with_principal(mut self, actor: Option<String>, subject: Option<String>) -> Self {
        self.actor_agent_id = actor.filter(|s| !s.is_empty());
        self.subject = subject.filter(|s| !s.is_empty());
        self
    }

    /// Produce the chain for the next step, generating a fresh current
    /// task ID. The new chain's `parent` is the caller's current task
    /// ID (i.e. the ULID the proxy minted when it entered this step),
    /// and depth increments by 1.
    ///
    /// Fails if incrementing would exceed `ABSOLUTE_MAX_DEPTH` — policy
    /// may enforce a lower cap on top, but this module refuses to
    /// produce a chain above the hard ceiling no matter what the caller
    /// asks for.
    pub fn fork_child(&self, new_task_id: Ulid) -> Result<Self, DelegationError> {
        let next = self.depth.saturating_add(1);
        if next > ABSOLUTE_MAX_DEPTH {
            return Err(DelegationError::DepthOverflow { depth: next });
        }
        Ok(Self {
            root_task_id: self.root_task_id,
            parent_task_id: new_task_id,
            depth: next,
            // The principal propagates unchanged — that's the point: a
            // downstream peer learns who originally acted, on whose behalf.
            actor_agent_id: self.actor_agent_id.clone(),
            subject: self.subject.clone(),
        })
    }

    /// True when this chain carries a propagated principal (actor/subject) and
    /// therefore must sign/verify with the versioned (v2) canonical form.
    fn has_principal(&self) -> bool {
        self.actor_agent_id.is_some() || self.subject.is_some()
    }

    /// Reject a replayed chain by bounding the age of its most recent step.
    ///
    /// The `parent_task_id` is a ULID minted when this hop was created, and it
    /// is already inside the HMAC-signed payload — so its embedded timestamp
    /// is tamper-evident without adding any new signed field. A captured
    /// header set replayed later carries an old parent ULID and is rejected.
    ///
    /// `now_ms` is passed in (rather than read here) so the check is
    /// deterministic and unit-testable; the route boundary supplies wall clock.
    /// A parent minted slightly in the future (cross-host skew) is tolerated
    /// up to `leeway_secs`.
    pub fn check_fresh(
        &self,
        now_ms: u64,
        max_age_secs: u64,
        leeway_secs: u64,
    ) -> Result<(), DelegationError> {
        let parent_ms = self.parent_task_id.timestamp_ms();
        // Age in seconds; saturating so a future timestamp yields 0 age.
        let age_secs = now_ms.saturating_sub(parent_ms) / 1000;
        if age_secs > max_age_secs.saturating_add(leeway_secs) {
            return Err(DelegationError::Stale {
                age_secs,
                max_age_secs,
            });
        }
        Ok(())
    }

    /// Build the canonical byte-string the HMAC signs. Null separators are used
    /// rather than a printable delimiter because ULIDs/digits can't contain a
    /// null, so no escaping is needed.
    ///
    /// Two formats, for backward compatibility:
    ///   * **v1** (no principal): `root\0parent\0depth` — byte-identical to the
    ///     pre-principal scheme, so old chains verify on new code and a new
    ///     principal-less root verifies on old code.
    ///   * **v2** (principal present): `"v2"\0root\0parent\0depth\0actor\0subject`
    ///     (absent actor/subject → empty string). The `v2` tag is unambiguous
    ///     vs. v1 because a v1 root value is always a ULID, never `"v2"`.
    fn canonical_signing_bytes(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(96);
        if self.has_principal() {
            out.extend_from_slice(b"v2");
            out.push(0);
        }
        out.extend_from_slice(self.root_task_id.to_string().as_bytes());
        out.push(0);
        out.extend_from_slice(self.parent_task_id.to_string().as_bytes());
        out.push(0);
        out.extend_from_slice(self.depth.to_string().as_bytes());
        if self.has_principal() {
            out.push(0);
            out.extend_from_slice(self.actor_agent_id.as_deref().unwrap_or("").as_bytes());
            out.push(0);
            out.extend_from_slice(self.subject.as_deref().unwrap_or("").as_bytes());
        }
        out
    }

    /// Sign this chain with the tenant's root HMAC key. Returns the
    /// base64url-encoded signature (no padding), suitable for an HTTP
    /// header value.
    pub fn sign(&self, tenant_key: &[u8]) -> String {
        let mut mac =
            HmacSha256::new_from_slice(tenant_key).expect("HMAC-SHA256 accepts keys of any length");
        mac.update(&self.canonical_signing_bytes());
        let tag = mac.finalize().into_bytes();
        URL_SAFE_NO_PAD.encode(tag)
    }

    /// Render the four-header set ready to write onto an HTTP request.
    ///
    /// Returns tuples rather than a HashMap to avoid needing a concrete
    /// HTTP crate import here — callers can push the tuples into
    /// whatever header-map type they use.
    pub fn to_header_pairs(&self, tenant_key: &[u8]) -> Vec<(&'static str, String)> {
        let mut pairs = vec![
            (HEADER_ROOT, self.root_task_id.to_string()),
            (HEADER_PARENT, self.parent_task_id.to_string()),
            (HEADER_DEPTH, self.depth.to_string()),
            (HEADER_SIG, self.sign(tenant_key)),
        ];
        // Emit the principal headers only when present, so principal-less chains
        // remain byte-for-byte the legacy 4-header set.
        if let Some(actor) = self.actor_agent_id.as_deref() {
            pairs.push((HEADER_ACTOR, actor.to_string()));
        }
        if let Some(subject) = self.subject.as_deref() {
            pairs.push((HEADER_SUBJECT, subject.to_string()));
        }
        pairs
    }

    /// Parse a header set produced by an earlier call (possibly by a
    /// different Brutor instance running with the same tenant key)
    /// and verify its signature.
    ///
    /// `getter` is a closure the caller provides so we stay HTTP-crate
    /// agnostic. For Axum's `HeaderMap`, pass
    /// `|name| map.get(name).and_then(|v| v.to_str().ok()).map(str::to_owned)`.
    pub fn parse_and_verify<F>(getter: F, tenant_key: &[u8]) -> Result<Self, DelegationError>
    where
        F: Fn(&str) -> Option<String>,
    {
        let root = getter(HEADER_ROOT).ok_or(DelegationError::MalformedHeader("root"))?;
        let parent = getter(HEADER_PARENT).ok_or(DelegationError::MalformedHeader("parent"))?;
        let depth = getter(HEADER_DEPTH).ok_or(DelegationError::MalformedHeader("depth"))?;
        let sig = getter(HEADER_SIG).ok_or(DelegationError::MissingSignature)?;

        let root_task_id: Ulid = root
            .parse()
            .map_err(|_| DelegationError::MalformedHeader("root"))?;
        let parent_task_id: Ulid = parent
            .parse()
            .map_err(|_| DelegationError::MalformedHeader("parent"))?;
        let depth: u32 = depth.parse()?;

        if depth > ABSOLUTE_MAX_DEPTH {
            return Err(DelegationError::DepthOverflow { depth });
        }

        // Optional principal headers — their presence selects the v2 canonical
        // form in `canonical_signing_bytes`, so a tampered/forged actor fails
        // verification.
        let actor_agent_id = getter(HEADER_ACTOR).filter(|s| !s.is_empty());
        let subject = getter(HEADER_SUBJECT).filter(|s| !s.is_empty());

        let chain = Self {
            root_task_id,
            parent_task_id,
            depth,
            actor_agent_id,
            subject,
        };

        // Verify: recompute the MAC and compare in constant time via
        // the `hmac::verify` helper.
        let expected = URL_SAFE_NO_PAD
            .decode(sig.as_bytes())
            .map_err(|_| DelegationError::MissingSignature)?;
        let mut mac =
            HmacSha256::new_from_slice(tenant_key).expect("HMAC-SHA256 accepts keys of any length");
        mac.update(&chain.canonical_signing_bytes());
        mac.verify_slice(&expected)
            .map_err(|_| DelegationError::SignatureMismatch)?;
        Ok(chain)
    }
}

// ---------------------------------------------------------------------------
// Tenant key derivation
// ---------------------------------------------------------------------------

/// Derive the per-tenant HMAC key from the gateway's master key + the
/// tenant ID. Keeps per-tenant chains unforgeable across tenants even
/// though the master key is shared — rogue tenant A can't craft a
/// chain that verifies as tenant B.
///
/// Uses a single HMAC pass rather than a full KDF because:
///   * keys are not exposed outside the process,
///   * both inputs are server-controlled,
///   * the output is only used as another HMAC key, not material
///     attackers see or that needs side-channel resistance.
///
/// If the threat model changes (e.g. tenant keys start being exported),
/// swap for HKDF-SHA256 and pin the info string to the tenant id.
pub fn derive_tenant_key(master_key: &[u8], tenant_id: &str) -> Vec<u8> {
    let mut mac =
        HmacSha256::new_from_slice(master_key).expect("HMAC-SHA256 accepts keys of any length");
    mac.update(b"brutor-a2a-delegation-v1\0");
    mac.update(tenant_id.as_bytes());
    mac.finalize().into_bytes().to_vec()
}

// ---------------------------------------------------------------------------
// Generalisation to all surfaces
// ---------------------------------------------------------------------------
//
// The DelegationChain primitive above was built specifically for A2A.
// It is now the gateway's canonical call-chain
// identity across LLM, MCP, A2A, and Skill surfaces. The public type
// alias below + the `parse_or_mint` helper give every route handler a
// one-line entry point.

/// Public alias for the gateway's canonical call-chain primitive.
///
/// `DelegationChain` was the original name from the A2A work; we
/// expose `CallChain` so non-A2A surfaces (LLM, MCP, Skill) can call
/// the same machinery without the naming friction. The two are
/// identical — the alias keeps existing A2A code working byte-for-byte.
pub type CallChain = DelegationChain;

/// How a row joined the call chain. Written to `proxy_logs.trace_continuity`
/// so the audit dashboard can surface fragmented or spoofed traces
/// instead of silently masking them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TraceContinuity {
    /// No inbound `x-brutor-delegation-*` headers — gateway minted a
    /// fresh root chain for this call. Normal for the first step of any
    /// agent flow that starts at the gateway.
    Minted,
    /// Inbound headers present and HMAC signature verified — this step
    /// is the legitimate continuation of an existing chain.
    Verified,
    /// Inbound headers were absent on a request that the gateway
    /// expected to carry them (e.g. the body identifies itself as a
    /// `role: tool` follow-up but no chain headers came along). A new
    /// root was minted but the dashboard flags this so operators can
    /// see when their agent SDK is stripping headers.
    ClientDropped,
    /// Inbound headers present but HMAC signature verification failed.
    /// A new root is minted (we don't trust the supplied chain) and the
    /// failure is surfaced — this is the spoofed-trace case.
    SigFailed,
    /// No delegation chain, but the caller supplied `x-brutor-run-id` and the
    /// gateway derived this step's root from it.
    ///
    /// Deliberately distinct from [`Verified`](Self::Verified): the gateway
    /// vouches that these steps came from the same credential, not that it
    /// signed the linkage itself. An operator reading a run must be able to
    /// tell "the gateway forwarded this chain" from "the client grouped these
    /// calls", so the UI never renders the two as the same guarantee.
    ClientAsserted,
}

impl TraceContinuity {
    /// String form used in the `proxy_logs.trace_continuity` column.
    pub fn as_str(self) -> &'static str {
        match self {
            TraceContinuity::Minted => "minted",
            TraceContinuity::Verified => "verified",
            TraceContinuity::ClientDropped => "client_dropped",
            TraceContinuity::SigFailed => "sig_failed",
            TraceContinuity::ClientAsserted => "client_asserted",
        }
    }
}

/// Longest client-supplied run id we will look at. A correlation id is an
/// opaque handle, not a payload; anything longer is a caller bug and we would
/// rather truncate the attack surface than the id.
pub const MAX_RUN_ID_LEN: usize = 128;

/// Fingerprint the caller's credential so an asserted run is scoped to it.
///
/// Returns `None` when the request carries no credential we recognise — in
/// which case no run may be asserted at all. Anonymous callers grouping their
/// own traffic would let anyone write into a shared correlation space.
///
/// The credential is hashed, never stored or logged: this value only ever
/// feeds the HMAC below.
fn credential_fingerprint<F>(getter: &F, tenant_key: &[u8]) -> Option<String>
where
    F: Fn(&str) -> Option<String>,
{
    let raw = getter("authorization")
        .or_else(|| getter("x-api-key"))
        .or_else(|| getter("x-gateway-authorization"))?;
    let raw = raw.trim();
    if raw.is_empty() {
        return None;
    }
    let mut mac = HmacSha256::new_from_slice(tenant_key).expect("HMAC accepts any key length");
    mac.update(b"brutor-run-credential-v1\0");
    mac.update(raw.as_bytes());
    Some(URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes()))
}

/// Derive the real `root_task_id` for a client-asserted run.
///
/// ```text
/// root = ULID( HMAC(tenant_key, "brutor-run-assert-v1" ‖ credential ‖ run_id)[0..16] )
/// ```
///
/// Three properties make this safe to expose to untrusted clients:
///
/// * **Cross-principal isolation.** The caller's credential fingerprint is an
///   input, so two principals sending the *same* `run_id` derive different
///   roots. A client can only ever group its own calls; it cannot append to,
///   read or corrupt another agent's run.
/// * **Unforgeable.** The tenant key never leaves the gateway, so a client
///   cannot compute the resulting root and therefore cannot aim at a run it
///   did not create — even its own from a different credential.
/// * **Stateless and idempotent.** No lookup, no Redis, no row: the same
///   inputs always derive the same root, so steps correlate without the
///   gateway remembering anything between them.
///
/// The ULID's timestamp prefix is derived rather than temporal. Nothing reads
/// it — the ledger orders steps by `proxy_logs.timestamp` — and determinism is
/// worth more here than a sortable prefix.
/// Derive the stable session key a client asserted via
/// [`HEADER_SESSION_ID`].
///
/// Same construction as [`derive_asserted_root`] with a DIFFERENT domain
/// separator. That separation is the point: without it a client that reused
/// one id for both headers would derive the same value for its run and its
/// session, silently welding two different scopes together.
///
/// Pure — no clock, no randomness — so a conversation resumed next week
/// derives the same key and its spend keeps accumulating instead of
/// resetting.
/// Resolve the session a request belongs to, from its headers.
///
/// Returns the derived session key, or `None` when the client asserted
/// nothing (or asserted something empty / over-long). Absence is normal and
/// not an error: a service-to-service caller has no conversation, and a
/// malformed assertion must not fail the request — session identity is
/// correlation, and refusing traffic over a client's telemetry bug would be
/// a worse outcome than not knowing the session.
pub fn resolve_asserted_session<F>(getter: F, tenant_key: &[u8]) -> Option<Ulid>
where
    F: Fn(&str) -> Option<String>,
{
    let raw = getter(HEADER_SESSION_ID)?;
    let raw = raw.trim();
    if raw.is_empty() || raw.len() > MAX_SESSION_ID_LEN {
        return None;
    }
    let fp = credential_fingerprint(&getter, tenant_key)?;
    Some(derive_asserted_session(tenant_key, &fp, raw))
}

pub fn derive_asserted_session(tenant_key: &[u8], credential_fp: &str, session_id: &str) -> Ulid {
    let mut mac = HmacSha256::new_from_slice(tenant_key).expect("HMAC accepts any key length");
    mac.update(b"brutor-session-assert-v1\0");
    mac.update(credential_fp.as_bytes());
    mac.update(b"\0");
    mac.update(session_id.as_bytes());
    let digest = mac.finalize().into_bytes();
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(&digest[..16]);
    Ulid::from(u128::from_be_bytes(bytes))
}

pub fn derive_asserted_root(tenant_key: &[u8], credential_fp: &str, run_id: &str) -> Ulid {
    let mut mac = HmacSha256::new_from_slice(tenant_key).expect("HMAC accepts any key length");
    mac.update(b"brutor-run-assert-v1\0");
    mac.update(credential_fp.as_bytes());
    mac.update(b"\0");
    mac.update(run_id.as_bytes());
    let digest = mac.finalize().into_bytes();
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(&digest[..16]);
    Ulid::from(u128::from_be_bytes(bytes))
}

/// Parse the four `x-brutor-delegation-*` headers if present and
/// verify the HMAC signature with `tenant_key`; if any header is
/// missing OR the signature is invalid, mint a fresh root chain.
///
/// Returns `(chain, current_task_id, continuity)` where:
///
/// - `chain` is the *parent* chain — i.e. the chain as it arrived (or
///   a fresh root if minted). Caller invokes `chain.fork_child(new_id)`
///   to construct the outgoing chain when delegating downstream.
/// - `current_task_id` is the ULID this step is identified by;
///   callers write it to `proxy_logs.current_task_id`.
/// - `continuity` records how the chain was established (for the
///   `trace_continuity` audit column).
///
/// `getter` is the same closure shape `DelegationChain::parse_and_verify`
/// uses — pass a closure that reads from your HTTP framework's header
/// map.
/// The run root a client asserted via [`HEADER_RUN_ID`], if it is entitled to
/// one on this request.
///
/// Returns `None` when the caller supplied any delegation-chain header — a
/// signed chain outranks an assertion, and a step that genuinely belongs to
/// someone else's delegation must never re-root itself onto an id it chose.
/// Also `None` when the header is absent, blank, over-long, or the caller is
/// anonymous (there is no credential to bind the derivation to, and an
/// unbound assertion would let anyone name anyone's run).
///
/// Shared by both A2A surfaces — the inbound `message:send` route and the
/// outbound proxy — so the precedence rule has exactly one definition. It
/// deliberately routes through [`parse_or_mint`] rather than deriving
/// directly: the entitlement checks that matter (credential binding, tenant
/// keying) live there, and a second copy of them would be a second thing to
/// get wrong.
pub fn asserted_root<F>(getter: &F, tenant_key: &[u8]) -> Option<Ulid>
where
    F: Fn(&str) -> Option<String>,
{
    if getter(HEADER_ROOT).is_some()
        || getter(HEADER_PARENT).is_some()
        || getter(HEADER_DEPTH).is_some()
        || getter(HEADER_SIG).is_some()
    {
        return None;
    }
    let run_id = getter(HEADER_RUN_ID)?;
    let trimmed = run_id.trim();
    if trimmed.is_empty() || trimmed.len() > MAX_RUN_ID_LEN {
        return None;
    }
    match parse_or_mint(getter, tenant_key) {
        (chain, _, TraceContinuity::ClientAsserted) => Some(chain.root_task_id),
        _ => None,
    }
}

pub fn parse_or_mint<F>(getter: F, tenant_key: &[u8]) -> (CallChain, Ulid, TraceContinuity)
where
    F: Fn(&str) -> Option<String>,
{
    let new_task_id = Ulid::new();

    // Probe whether the caller supplied any chain headers at all. Any
    // of the four → treat the inbound as a chain attempt; none → mint.
    let any_header_present = getter(HEADER_ROOT).is_some()
        || getter(HEADER_PARENT).is_some()
        || getter(HEADER_DEPTH).is_some()
        || getter(HEADER_SIG).is_some();

    if !any_header_present {
        // No delegation chain. Before minting an orphan root, honour an
        // explicit client-asserted run correlation — this is what
        // lets a self-driving agent's steps land as one run instead of N.
        //
        // A signed chain always wins over an assertion, which is why this is
        // reached only when no chain headers were supplied at all: a step that
        // is genuinely part of someone else's delegation must not be able to
        // re-root itself into a run of its own choosing.
        if let Some(run_id) = getter(HEADER_RUN_ID) {
            let run_id = run_id.trim();
            if !run_id.is_empty() && run_id.len() <= MAX_RUN_ID_LEN {
                if let Some(fp) = credential_fingerprint(&getter, tenant_key) {
                    let root = derive_asserted_root(tenant_key, &fp, run_id);
                    return (
                        CallChain::root(root),
                        new_task_id,
                        TraceContinuity::ClientAsserted,
                    );
                }
            }
        }
        return (
            CallChain::root(new_task_id),
            new_task_id,
            TraceContinuity::Minted,
        );
    }

    match DelegationChain::parse_and_verify(&getter, tenant_key) {
        Ok(parent_chain) => (parent_chain, new_task_id, TraceContinuity::Verified),
        Err(DelegationError::MissingSignature) => (
            // Some chain headers present but not the signature → client
            // dropped or never produced one. Mint a fresh root and flag.
            CallChain::root(new_task_id),
            new_task_id,
            TraceContinuity::ClientDropped,
        ),
        Err(_) => (
            // SignatureMismatch / MalformedHeader / DepthOverflow → treat
            // as spoofed and mint a fresh root.
            CallChain::root(new_task_id),
            new_task_id,
            TraceContinuity::SigFailed,
        ),
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    // -----------------------------------------------------------------------
    // XREPO-13 — the trace_continuity vocabulary is a cross-repo contract
    // -----------------------------------------------------------------------

    /// Pin every value `TraceContinuity` can write.
    ///
    /// The control plane keeps its own copy in
    /// `app/services/trace_service.py::KNOWN_CONTINUITY`, and had four of
    /// these six for months: this enum gained `ClientAsserted` and the OTel
    /// inlet added `external`, and nothing on either side had to notice. The
    /// CP's test pinned only the CP's constant, so it stayed green while the
    /// contract drifted — a one-sided pin is not a contract test, it is a
    /// changelog for one repo.
    ///
    /// This is the other end. A new variant fails here; the matching CP test
    /// fails there. Add a value to both, in the same change.
    #[test]
    fn trace_continuity_values_are_pinned() {
        use super::TraceContinuity;
        let all = [
            TraceContinuity::Minted,
            TraceContinuity::Verified,
            TraceContinuity::ClientDropped,
            TraceContinuity::SigFailed,
            TraceContinuity::ClientAsserted,
        ];
        let mut seen: Vec<&str> = all.iter().map(|c| c.as_str()).collect();
        seen.sort_unstable();
        assert_eq!(
            seen,
            vec![
                "client_asserted",
                "client_dropped",
                "minted",
                "sig_failed",
                "verified",
            ],
            "TraceContinuity changed. Update KNOWN_CONTINUITY in \
             brutor-gateway-control/app/services/trace_service.py and its test \
             in the same change — the control plane validates filters against \
             that set, so a value it does not know is a filter that silently \
             matches nothing."
        );
        // `external` completes the column's vocabulary but is written by the
        // OTel inlet (services/otel_ingest.rs::CONTINUITY_EXTERNAL), not by
        // this enum. Named here so the two lists can be compared by eye.
        assert_eq!(
            crate::services::otel_ingest::CONTINUITY_EXTERNAL,
            "external"
        );
    }

    use super::*;

    // ── Client-asserted run correlation ──────────────────────
    //
    // The threat these guard against: a client using `x-brutor-run-id` to
    // write its calls into a run belonging to someone else.

    fn getter_for<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        let owned: Vec<(String, String)> = pairs
            .iter()
            .map(|(k, v)| (k.to_lowercase(), v.to_string()))
            .collect();
        move |name: &str| {
            let want = name.to_lowercase();
            owned
                .iter()
                .find(|(k, _)| *k == want)
                .map(|(_, v)| v.clone())
        }
    }

    #[test]
    fn asserted_run_groups_steps_from_the_same_credential() {
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let g = getter_for(&[
            ("authorization", "Bearer sk_agent_one"),
            (HEADER_RUN_ID, "task-42"),
        ]);

        let (c1, id1, k1) = parse_or_mint(&g, &key);
        let (c2, id2, k2) = parse_or_mint(&g, &key);

        assert_eq!(k1, TraceContinuity::ClientAsserted);
        assert_eq!(k2, TraceContinuity::ClientAsserted);
        assert_eq!(
            c1.root_task_id, c2.root_task_id,
            "same credential + run id must share a root"
        );
        assert_eq!(c1.depth, 0, "an asserted run is a root, not a continuation");
        assert_ne!(id1, id2, "each step still gets its own current_task_id");
    }

    #[test]
    fn asserted_run_is_isolated_per_credential() {
        // The core security property. Two agents choosing the same run id
        // must NOT land in the same run.
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let a = getter_for(&[
            ("authorization", "Bearer sk_agent_one"),
            (HEADER_RUN_ID, "task-42"),
        ]);
        let b = getter_for(&[
            ("authorization", "Bearer sk_agent_two"),
            (HEADER_RUN_ID, "task-42"),
        ]);

        let (ca, _, _) = parse_or_mint(&a, &key);
        let (cb, _, _) = parse_or_mint(&b, &key);
        assert_ne!(
            ca.root_task_id, cb.root_task_id,
            "a different credential must never join another principal's run"
        );
    }

    #[test]
    fn asserted_run_is_isolated_per_tenant() {
        let a = derive_tenant_key(MASTER_KEY, "tenant-a");
        let b = derive_tenant_key(MASTER_KEY, "tenant-b");
        let g = getter_for(&[
            ("authorization", "Bearer sk_same"),
            (HEADER_RUN_ID, "task-42"),
        ]);

        let (ca, _, _) = parse_or_mint(&g, &a);
        let (cb, _, _) = parse_or_mint(&g, &b);
        assert_ne!(
            ca.root_task_id, cb.root_task_id,
            "tenants must not share a run space"
        );
    }

    #[test]
    fn anonymous_caller_cannot_assert_a_run() {
        // With no credential there is nothing to scope the run to, so the
        // assertion is ignored rather than honoured into a shared space.
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let g = getter_for(&[(HEADER_RUN_ID, "task-42")]);
        let (_, _, kind) = parse_or_mint(&g, &key);
        assert_eq!(kind, TraceContinuity::Minted);
    }

    #[test]
    fn a_signed_chain_outranks_an_assertion() {
        // A step that is genuinely part of someone else's delegation must not
        // be able to re-root itself by also sending a run id.
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let root = Ulid::new();
        let parent = Ulid::new();
        let signed = DelegationChain {
            root_task_id: root,
            parent_task_id: parent,
            depth: 1,
            actor_agent_id: None,
            subject: None,
        };
        let mut pairs: Vec<(String, String)> = signed
            .to_header_pairs(&key)
            .into_iter()
            .map(|(k, v): (&'static str, String)| (k.to_string(), v))
            .collect();
        pairs.push((
            "authorization".to_string(),
            "Bearer sk_agent_one".to_string(),
        ));
        pairs.push((HEADER_RUN_ID.to_string(), "task-42".to_string()));
        let refs: Vec<(&str, &str)> = pairs
            .iter()
            .map(|(k, v): &(String, String)| (k.as_str(), v.as_str()))
            .collect();

        let (chain, _, kind) = parse_or_mint(getter_for(&refs), &key);
        assert_eq!(kind, TraceContinuity::Verified);
        assert_eq!(chain.root_task_id, root, "the signed chain wins");
    }

    #[test]
    fn oversized_or_blank_run_id_is_ignored() {
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let long = "x".repeat(MAX_RUN_ID_LEN + 1);
        for bad in ["", "   ", long.as_str()] {
            let pairs = [("authorization", "Bearer sk_a"), (HEADER_RUN_ID, bad)];
            let g = getter_for(&pairs);
            let (_, _, kind) = parse_or_mint(&g, &key);
            assert_eq!(
                kind,
                TraceContinuity::Minted,
                "run id {bad:?} must not be honoured"
            );
        }
    }

    #[test]
    fn session_derivation_is_stable_across_processes() {
        // Pure — no clock, no randomness. A conversation resumed next week
        // must derive the same key or its accumulated spend resets, which is
        // the one thing a session budget cannot survive.
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        assert_eq!(
            derive_asserted_session(&key, "fp-1", "chat-42"),
            derive_asserted_session(&key, "fp-1", "chat-42"),
        );
    }

    #[test]
    fn different_conversations_derive_different_sessions() {
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        assert_ne!(
            derive_asserted_session(&key, "fp-1", "chat-42"),
            derive_asserted_session(&key, "fp-1", "chat-43"),
        );
    }

    #[test]
    fn one_caller_cannot_claim_another_principals_session() {
        // The security property. Same asserted id, different credential →
        // different session. Otherwise a caller could guess a colleague's
        // chat id and spend against their budget.
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        assert_ne!(
            derive_asserted_session(&key, "fp-1", "chat-42"),
            derive_asserted_session(&key, "fp-2", "chat-42"),
        );
    }

    #[test]
    fn sessions_do_not_cross_tenants() {
        assert_ne!(
            derive_asserted_session(
                &derive_tenant_key(MASTER_KEY, "tenant-a"),
                "fp-1",
                "chat-42"
            ),
            derive_asserted_session(
                &derive_tenant_key(MASTER_KEY, "tenant-b"),
                "fp-1",
                "chat-42"
            ),
        );
    }

    #[test]
    fn a_session_and_a_run_never_collide_on_the_same_id() {
        // The reason the two derivations use different domain separators. A
        // client that reuses one id for both headers must not weld its run
        // scope and its session scope into the same key.
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        assert_ne!(
            derive_asserted_session(&key, "fp-1", "same-id"),
            derive_asserted_root(&key, "fp-1", "same-id"),
        );
    }

    /// Build a getter over a real `HeaderMap`, exactly as the LLM handlers do.
    fn header_getter(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> + use<> {
        let mut headers = axum::http::HeaderMap::new();
        for (k, v) in pairs {
            headers.insert(
                axum::http::HeaderName::from_bytes(k.as_bytes()).unwrap(),
                v.parse().unwrap(),
            );
        }
        move |h: &str| {
            headers
                .get(h)
                .and_then(|v| v.to_str().ok())
                .map(str::to_string)
        }
    }

    #[test]
    fn resolve_asserted_session_reads_the_header_off_a_real_request() {
        // The five tests above all call `derive_asserted_session` directly and
        // so prove nothing about the wrapper the request path actually uses.
        // Every one of them passed while sessions were silently unresolved in
        // production: the derivation was correct and never reached.
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let getter = header_getter(&[
            ("x-brutor-session-id", "chat-42"),
            ("authorization", "Bearer tok"),
        ]);
        let got = resolve_asserted_session(&getter, &key);
        assert!(
            got.is_some(),
            "a well-formed session assertion must resolve; None here means \
             every conversation silently falls back to the auth session key \
             and no per-session cap can ever fire"
        );

        // And it must agree with the direct derivation — a wrapper that
        // resolves to some *other* id would scope spend to a key nothing
        // else in the system knows about.
        let fp = credential_fingerprint(&getter, &key).expect("fingerprint");
        assert_eq!(got.unwrap(), derive_asserted_session(&key, &fp, "chat-42"));
    }

    #[test]
    fn resolve_asserted_session_is_case_insensitive_about_the_header() {
        // Clients send `X-Brutor-Session-Id`; HTTP header names are
        // case-insensitive and `HeaderMap` lowercases on insert. Pinned
        // because the constant is lowercase and a case-sensitive lookup
        // would fail for every real client while passing every unit test
        // that constructs the header lowercase.
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let getter = header_getter(&[
            ("X-Brutor-Session-Id", "chat-42"),
            ("Authorization", "Bearer tok"),
        ]);
        assert!(resolve_asserted_session(&getter, &key).is_some());
    }

    #[test]
    fn resolve_asserted_session_needs_a_credential_to_bind_to() {
        // No auth header → no fingerprint → no session. This is deliberate
        // (an unauthenticated caller must not be able to claim a session),
        // but it is also the most likely way the resolve silently returns
        // None in production, so it is pinned as intended behaviour.
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let getter = header_getter(&[("x-brutor-session-id", "chat-42")]);
        assert!(resolve_asserted_session(&getter, &key).is_none());
    }

    #[test]
    fn resolve_asserted_session_ignores_an_over_long_claim() {
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let long = "x".repeat(MAX_SESSION_ID_LEN + 1);
        let getter = header_getter(&[
            ("x-brutor-session-id", long.as_str()),
            ("authorization", "Bearer tok"),
        ]);
        assert!(resolve_asserted_session(&getter, &key).is_none());
    }

    #[test]
    fn asserted_root_is_stable_across_processes() {
        // Derivation must be pure — no clock, no randomness — or a restarted
        // agent would fragment its own run.
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let a = derive_asserted_root(&key, "fp-1", "task-42");
        let b = derive_asserted_root(&key, "fp-1", "task-42");
        assert_eq!(a, b);
        assert_ne!(a, derive_asserted_root(&key, "fp-1", "task-43"));
    }

    // ── asserted_root: the shared precedence rule ───────────────────────
    //
    // Both A2A surfaces (inbound `message:send`, outbound proxy) resolve a
    // caller's asserted run through this one function, so these pin the rule
    // for both at once.

    #[test]
    fn asserted_root_agrees_with_parse_or_mint() {
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let g = getter_for(&[
            ("authorization", "Bearer sk_agent_one"),
            (HEADER_RUN_ID, "task-42"),
        ]);

        let (chain, _, kind) = parse_or_mint(&g, &key);
        assert_eq!(kind, TraceContinuity::ClientAsserted);
        assert_eq!(
            asserted_root(&g, &key),
            Some(chain.root_task_id),
            "the A2A routes must root a step exactly where parse_or_mint would"
        );
    }

    #[test]
    fn asserted_root_yields_nothing_without_an_assertion() {
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let g = getter_for(&[("authorization", "Bearer sk_agent_one")]);
        assert_eq!(asserted_root(&g, &key), None, "no run id → mint as before");
    }

    #[test]
    fn asserted_root_defers_to_any_chain_header() {
        // The case that would be a security bug: a caller carrying someone
        // else's delegation must not be able to re-root its step onto an id it
        // picked. Presence of ANY chain header disqualifies the assertion,
        // before signature checking — an invalid chain is refused elsewhere,
        // never quietly downgraded to "assert whatever you like".
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        for hdr in [HEADER_ROOT, HEADER_PARENT, HEADER_DEPTH, HEADER_SIG] {
            let pairs = [
                ("authorization", "Bearer sk_agent_one"),
                (HEADER_RUN_ID, "task-42"),
                (hdr, "01ARZ3NDEKTSV4RRFFQ69G5FAV"),
            ];
            let g = getter_for(&pairs);
            assert_eq!(
                asserted_root(&g, &key),
                None,
                "{hdr} present must disqualify the assertion"
            );
        }
    }

    #[test]
    fn asserted_root_rejects_blank_and_oversized_run_ids() {
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let long = "x".repeat(MAX_RUN_ID_LEN + 1);
        for bad in ["", "   ", long.as_str()] {
            let pairs = [
                ("authorization", "Bearer sk_agent_one"),
                (HEADER_RUN_ID, bad),
            ];
            let g = getter_for(&pairs);
            assert_eq!(
                asserted_root(&g, &key),
                None,
                "run id {bad:?} must be ignored"
            );
        }
    }

    #[test]
    fn an_anonymous_caller_cannot_assert_a_run() {
        // Nothing to bind the derivation to, so an assertion would let any
        // unauthenticated caller name any run.
        let key = derive_tenant_key(MASTER_KEY, "tenant-a");
        let g = getter_for(&[(HEADER_RUN_ID, "task-42")]);
        assert_eq!(asserted_root(&g, &key), None);
    }

    #[test]
    fn asserted_root_is_isolated_across_credentials_and_tenants() {
        let key_a = derive_tenant_key(MASTER_KEY, "tenant-a");
        let key_b = derive_tenant_key(MASTER_KEY, "tenant-b");
        let one = getter_for(&[
            ("authorization", "Bearer sk_one"),
            (HEADER_RUN_ID, "task-42"),
        ]);
        let two = getter_for(&[
            ("authorization", "Bearer sk_two"),
            (HEADER_RUN_ID, "task-42"),
        ]);

        let a1 = asserted_root(&one, &key_a).expect("entitled");
        let a2 = asserted_root(&two, &key_a).expect("entitled");
        let b1 = asserted_root(&one, &key_b).expect("entitled");

        assert_ne!(a1, a2, "same run id, different credential → different runs");
        assert_ne!(
            a1, b1,
            "same run id + credential, different tenant → different runs"
        );
    }

    const MASTER_KEY: &[u8] = b"test-master-key-DO-NOT-USE-IN-PROD";

    fn tenant_key(tid: &str) -> Vec<u8> {
        derive_tenant_key(MASTER_KEY, tid)
    }

    fn make_getter<'a>(
        pairs: &'a [(&'static str, String)],
    ) -> impl Fn(&str) -> Option<String> + 'a {
        move |name: &str| {
            pairs
                .iter()
                .find(|(k, _)| *k == name)
                .map(|(_, v)| v.clone())
        }
    }

    // --- Canonical bytes --------------------------------------------------

    #[test]
    fn canonical_bytes_are_stable_per_chain() {
        let id = Ulid::from_string("01HQJ8MYV6RYMHVY9V2YBKFQP4").unwrap();
        let c = DelegationChain::root(id);
        let a = c.canonical_signing_bytes();
        let b = c.canonical_signing_bytes();
        assert_eq!(a, b);
    }

    #[test]
    fn canonical_bytes_include_all_three_fields() {
        let id = Ulid::from_string("01HQJ8MYV6RYMHVY9V2YBKFQP4").unwrap();
        let c = DelegationChain::root(id);
        let bytes = c.canonical_signing_bytes();
        let as_str = String::from_utf8_lossy(&bytes);
        assert!(as_str.contains(&id.to_string()));
        assert!(as_str.contains("\u{0}0")); // depth=0 after a NUL
    }

    // --- Sign / verify round-trip ----------------------------------------

    #[test]
    fn sign_then_verify_round_trip() {
        let id = Ulid::new();
        let chain = DelegationChain::root(id);
        let key = tenant_key("t1");
        let headers = chain.to_header_pairs(&key);

        let pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        let getter = make_getter(&pairs);
        let verified =
            DelegationChain::parse_and_verify(getter, &key).expect("valid chain must verify");
        assert_eq!(verified.root_task_id, chain.root_task_id);
        assert_eq!(verified.parent_task_id, chain.parent_task_id);
        assert_eq!(verified.depth, chain.depth);
    }

    #[test]
    fn verify_fails_when_depth_tampered() {
        let chain = DelegationChain::root(Ulid::new());
        let key = tenant_key("t1");
        let headers = chain.to_header_pairs(&key);
        let mut pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        // Pretend a malicious caller bumped the claimed depth but
        // left the signature intact. Use 5 (well within ABSOLUTE_MAX_
        // DEPTH so the overflow check doesn't short-circuit) — we're
        // exercising the signature-mismatch branch specifically.
        for p in pairs.iter_mut() {
            if p.0 == HEADER_DEPTH {
                p.1 = "5".to_string();
            }
        }
        let getter = make_getter(&pairs);
        let err = DelegationChain::parse_and_verify(getter, &key)
            .expect_err("tampered depth must not verify");
        assert_eq!(err, DelegationError::SignatureMismatch);
    }

    #[test]
    fn verify_fails_when_root_tampered() {
        let chain = DelegationChain::root(Ulid::new());
        let key = tenant_key("t1");
        let headers = chain.to_header_pairs(&key);
        let mut pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        for p in pairs.iter_mut() {
            if p.0 == HEADER_ROOT {
                p.1 = Ulid::new().to_string();
            }
        }
        let getter = make_getter(&pairs);
        assert_eq!(
            DelegationChain::parse_and_verify(getter, &key).unwrap_err(),
            DelegationError::SignatureMismatch
        );
    }

    #[test]
    fn verify_fails_with_wrong_tenant_key() {
        let chain = DelegationChain::root(Ulid::new());
        let key_a = tenant_key("t1");
        let key_b = tenant_key("t2");
        let headers = chain.to_header_pairs(&key_a);
        let pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        let getter = make_getter(&pairs);
        assert_eq!(
            DelegationChain::parse_and_verify(getter, &key_b).unwrap_err(),
            DelegationError::SignatureMismatch
        );
    }

    // --- Tenant isolation at the KDF level -------------------------------

    #[test]
    fn derived_keys_differ_across_tenants() {
        let k1 = tenant_key("t1");
        let k2 = tenant_key("t2");
        assert_ne!(k1, k2);
    }

    #[test]
    fn derived_keys_are_stable_within_tenant() {
        assert_eq!(tenant_key("t1"), tenant_key("t1"));
    }

    // --- Chain propagation -------------------------------------------------

    #[test]
    fn fork_child_increments_depth_and_updates_parent() {
        let root_id = Ulid::new();
        let chain = DelegationChain::root(root_id);

        let step_task_id = Ulid::new();
        let child = chain.fork_child(step_task_id).unwrap();
        assert_eq!(child.depth, 1);
        assert_eq!(child.root_task_id, root_id);
        assert_eq!(child.parent_task_id, step_task_id);
    }

    #[test]
    fn fork_child_refuses_to_exceed_absolute_cap() {
        // Construct by hand to get to the cap quickly.
        let chain = DelegationChain {
            root_task_id: Ulid::new(),
            parent_task_id: Ulid::new(),
            depth: ABSOLUTE_MAX_DEPTH,
            actor_agent_id: None,
            subject: None,
        };
        let err = chain.fork_child(Ulid::new()).unwrap_err();
        assert!(matches!(err, DelegationError::DepthOverflow { .. }));
    }

    // --- Parse-and-verify error paths ------------------------------------

    #[test]
    fn missing_signature_header_returns_specific_error() {
        let pairs = [
            (HEADER_ROOT, Ulid::new().to_string()),
            (HEADER_PARENT, Ulid::new().to_string()),
            (HEADER_DEPTH, "0".to_string()),
        ];
        let v: Vec<_> = pairs.iter().map(|(k, v)| (*k, v.clone())).collect();
        let getter = make_getter(&v);
        assert_eq!(
            DelegationChain::parse_and_verify(getter, &tenant_key("t1")).unwrap_err(),
            DelegationError::MissingSignature
        );
    }

    #[test]
    fn malformed_depth_returns_specific_error() {
        let chain = DelegationChain::root(Ulid::new());
        let key = tenant_key("t1");
        let headers = chain.to_header_pairs(&key);
        let mut pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        for p in pairs.iter_mut() {
            if p.0 == HEADER_DEPTH {
                p.1 = "not-a-number".to_string();
            }
        }
        let getter = make_getter(&pairs);
        assert_eq!(
            DelegationChain::parse_and_verify(getter, &key).unwrap_err(),
            DelegationError::MalformedHeader("depth")
        );
    }

    #[test]
    fn overflow_depth_rejected_before_signature_check() {
        // Any depth above ABSOLUTE_MAX_DEPTH must be rejected regardless
        // of whether the signature would have been valid. Crafting a
        // valid signature for an overflow depth is possible only if the
        // key leaks; even then, refusing such requests is the right
        // response.
        let chain = DelegationChain {
            root_task_id: Ulid::new(),
            parent_task_id: Ulid::new(),
            depth: ABSOLUTE_MAX_DEPTH + 5,
            actor_agent_id: None,
            subject: None,
        };
        let key = tenant_key("t1");
        let headers = chain.to_header_pairs(&key);
        let pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        let getter = make_getter(&pairs);
        let err = DelegationChain::parse_and_verify(getter, &key).unwrap_err();
        assert!(matches!(err, DelegationError::DepthOverflow { .. }));
    }

    // --- Error display -----------------------------------------------------

    #[test]
    fn error_display_is_human_readable() {
        let err = DelegationError::DepthOverflow { depth: 99 };
        let msg = err.to_string();
        assert!(msg.contains("99"));
        assert!(msg.contains("exceeds"));
    }

    // -----------------------------------------------------------------------
    // parse_or_mint branch coverage
    //
    // Every inbound proxy call routes through `parse_or_mint` so the
    // four `TraceContinuity` branches are the hot path. These tests
    // pin each branch so a future refactor of the header-parsing
    // code can't accidentally collapse e.g. SigFailed into
    // ClientDropped (the dashboard distinguishes them).
    // -----------------------------------------------------------------------

    #[test]
    fn parse_or_mint_with_no_headers_returns_minted_root() {
        let key = tenant_key("t1");
        let getter = make_getter(&[]);
        let (chain, current, continuity) = parse_or_mint(getter, &key);

        assert_eq!(continuity, TraceContinuity::Minted);
        assert_eq!(chain.depth, 0);
        // A freshly-minted root has root == parent (root chain
        // invariant from `DelegationChain::root`) and current is
        // the same fresh ULID.
        assert_eq!(chain.root_task_id, chain.parent_task_id);
        assert_eq!(chain.root_task_id, current);
    }

    #[test]
    fn parse_or_mint_with_valid_signed_headers_verifies() {
        let key = tenant_key("t1");
        let parent_chain = DelegationChain::root(Ulid::new());
        let headers = parent_chain.to_header_pairs(&key);
        let pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        let getter = make_getter(&pairs);

        let (chain, current, continuity) = parse_or_mint(getter, &key);

        assert_eq!(continuity, TraceContinuity::Verified);
        // The chain we return IS the parent's chain (the caller
        // forks via fork_child if they're emitting outbound). The
        // current_task_id is freshly minted for THIS step.
        assert_eq!(chain.root_task_id, parent_chain.root_task_id);
        assert_eq!(chain.parent_task_id, parent_chain.parent_task_id);
        assert_eq!(chain.depth, parent_chain.depth);
        assert_ne!(current.to_string(), parent_chain.root_task_id.to_string());
    }

    #[test]
    fn parse_or_mint_with_headers_but_no_signature_is_client_dropped() {
        let key = tenant_key("t1");
        let parent_chain = DelegationChain::root(Ulid::new());
        let headers = parent_chain.to_header_pairs(&key);
        // Strip the signature header. A well-meaning but un-aware
        // client emits the three identity headers and forgets the
        // sig. The trace continues but flagged as ClientDropped so
        // operators can see "this caller is missing the SDK".
        let pairs: Vec<_> = headers
            .iter()
            .filter(|(k, _)| *k != HEADER_SIG)
            .map(|(k, v)| (*k, v.clone()))
            .collect();
        let getter = make_getter(&pairs);

        let (chain, _current, continuity) = parse_or_mint(getter, &key);

        assert_eq!(continuity, TraceContinuity::ClientDropped);
        // Fresh root minted — we do NOT trust unsigned headers as
        // chain identity, so the inbound parent claim is discarded.
        assert_eq!(chain.depth, 0);
        assert_eq!(chain.root_task_id, chain.parent_task_id);
    }

    #[test]
    fn parse_or_mint_with_tampered_signature_is_sig_failed() {
        let key_real = tenant_key("t1");
        let key_attacker = tenant_key("malicious-tenant");
        let parent_chain = DelegationChain::root(Ulid::new());
        // Attacker signs with the wrong tenant key — verification
        // against the real key fails with SignatureMismatch which
        // parse_or_mint folds into SigFailed.
        let headers = parent_chain.to_header_pairs(&key_attacker);
        let pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        let getter = make_getter(&pairs);

        let (chain, _current, continuity) = parse_or_mint(getter, &key_real);

        assert_eq!(continuity, TraceContinuity::SigFailed);
        // Spoofed inbound chain is discarded; fresh root minted so
        // the row still has SOME identity for the dashboard.
        assert_eq!(chain.depth, 0);
    }

    #[test]
    fn parse_or_mint_with_malformed_root_header_is_sig_failed() {
        let key = tenant_key("t1");
        // ROOT present but not a valid ULID. parse_and_verify
        // returns MalformedHeader which parse_or_mint folds into
        // SigFailed (not ClientDropped — the headers ARE present
        // but unparseable, so the inbound is broken not absent).
        let pairs = vec![
            (HEADER_ROOT, "not-a-ulid".to_string()),
            (HEADER_PARENT, Ulid::new().to_string()),
            (HEADER_DEPTH, "0".to_string()),
            (HEADER_SIG, "garbage".to_string()),
        ];
        let getter = make_getter(&pairs);

        let (chain, _current, continuity) = parse_or_mint(getter, &key);

        assert_eq!(continuity, TraceContinuity::SigFailed);
        assert_eq!(chain.depth, 0);
    }

    #[test]
    fn parse_or_mint_with_only_sig_header_treats_as_sig_failed() {
        // Edge case: caller sent ONLY the sig header — clearly a
        // bug, but we should still classify it predictably. The
        // `any_header_present` probe triggers chain parsing which
        // fails MalformedHeader on the missing root → SigFailed.
        let key = tenant_key("t1");
        let pairs = vec![(HEADER_SIG, "garbage".to_string())];
        let getter = make_getter(&pairs);

        let (_chain, _current, continuity) = parse_or_mint(getter, &key);

        assert_eq!(continuity, TraceContinuity::SigFailed);
    }

    #[test]
    fn parse_or_mint_current_task_id_is_unique_per_call() {
        // Same headers, two calls — current_task_id must differ.
        // This is the "this step" identity that distinguishes
        // sibling audit rows in a verified trace.
        let key = tenant_key("t1");
        let getter = make_getter(&[]);
        let (_, current_a, _) = parse_or_mint(&getter, &key);
        let (_, current_b, _) = parse_or_mint(&getter, &key);
        assert_ne!(current_a, current_b);
    }

    // --- Replay freshness (check_fresh) ----------------------------------

    fn chain_with_parent_minted_at(ms: u64) -> DelegationChain {
        DelegationChain {
            root_task_id: Ulid::new(),
            parent_task_id: Ulid::from_parts(ms, 0),
            depth: 1,
            actor_agent_id: None,
            subject: None,
        }
    }

    #[test]
    fn check_fresh_accepts_a_recent_chain() {
        let now_ms = 1_700_000_000_000;
        let chain = chain_with_parent_minted_at(now_ms - 5_000); // 5s old
        assert!(chain.check_fresh(now_ms, 600, 60).is_ok());
    }

    #[test]
    fn check_fresh_rejects_a_stale_chain_as_replay() {
        let now_ms = 1_700_000_000_000;
        // Parent minted 20 minutes ago, window 600s (+60s leeway) → stale.
        let chain = chain_with_parent_minted_at(now_ms - 1_200_000);
        match chain.check_fresh(now_ms, 600, 60) {
            Err(DelegationError::Stale {
                age_secs,
                max_age_secs,
            }) => {
                assert_eq!(max_age_secs, 600);
                assert!(age_secs >= 1_140, "age should be ~1200s, got {age_secs}");
            }
            other => panic!("expected Stale, got {other:?}"),
        }
    }

    #[test]
    fn check_fresh_tolerates_forward_clock_skew_within_leeway() {
        let now_ms = 1_700_000_000_000;
        // Parent minted 30s in the "future" (other host's clock ahead).
        let chain = chain_with_parent_minted_at(now_ms + 30_000);
        assert!(
            chain.check_fresh(now_ms, 600, 60).is_ok(),
            "a small forward skew must not be rejected"
        );
    }

    #[test]
    fn check_fresh_boundary_at_window_plus_leeway() {
        let now_ms = 1_700_000_000_000;
        // Exactly at window+leeway (660s) → allowed; one second past → stale.
        let at_edge = chain_with_parent_minted_at(now_ms - 660_000);
        assert!(at_edge.check_fresh(now_ms, 600, 60).is_ok());
        let past_edge = chain_with_parent_minted_at(now_ms - 661_000);
        assert!(matches!(
            past_edge.check_fresh(now_ms, 600, 60),
            Err(DelegationError::Stale { .. })
        ));
    }

    /// Is a fixed 600 s window safe for long-running parent tasks? No. The parent ULID is minted
    /// when the gateway forwards the call to the agent, so an agent that
    /// works on that task for longer than window + leeway and only then
    /// delegates further presents a chain the gateway refuses as a replay.
    /// The window therefore stays a setting (documented as advanced): an
    /// operator whose agents run long tasks raises it, and the refusal is
    /// lifted by exactly that.
    #[test]
    fn a_long_running_parent_task_that_delegates_late_is_refused_unless_the_window_is_raised() {
        let now_ms = 1_700_000_000_000;
        // Forwarded to the agent 15 minutes ago; it delegates only now.
        let forwarded_15_min_ago = chain_with_parent_minted_at(now_ms - 15 * 60 * 1000);
        assert!(matches!(
            forwarded_15_min_ago.check_fresh(
                now_ms,
                DEFAULT_DELEGATION_MAX_AGE_SECS,
                DELEGATION_FRESHNESS_LEEWAY_SECS
            ),
            Err(DelegationError::Stale { .. })
        ));
        let env = crate::env_registry::Env::from_pairs(
            &crate::env_vars::REGISTRY,
            [("BRUTOR_A2A_DELEGATION_MAX_AGE_SECS", "3600")],
        )
        .unwrap();
        let raised = max_age_from(&env).unwrap();
        assert_eq!(raised, 3600);
        assert!(
            forwarded_15_min_ago
                .check_fresh(now_ms, raised, DELEGATION_FRESHNESS_LEEWAY_SECS)
                .is_ok()
        );
        // And the default still bounds a replay to the window.
        let default = crate::env_registry::Env::from_pairs(
            &crate::env_vars::REGISTRY,
            [] as [(&str, &str); 0],
        )
        .unwrap();
        assert_eq!(max_age_from(&default), Ok(DEFAULT_DELEGATION_MAX_AGE_SECS));
    }

    // --- Principal carry -------------------------------------------------

    #[test]
    fn principal_round_trips_and_emits_extra_headers() {
        let key = tenant_key("t1");
        let chain = DelegationChain::root(Ulid::new())
            .with_principal(Some("agent-x".to_string()), Some("user:42".to_string()));
        let headers = chain.to_header_pairs(&key);
        // 4 base headers + actor + subject.
        assert_eq!(headers.len(), 6, "principal chain emits 6 headers");
        assert!(
            headers
                .iter()
                .any(|(k, v)| *k == HEADER_ACTOR && v == "agent-x")
        );
        assert!(
            headers
                .iter()
                .any(|(k, v)| *k == HEADER_SUBJECT && v == "user:42")
        );

        let pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        let verified = DelegationChain::parse_and_verify(make_getter(&pairs), &key)
            .expect("principal chain must verify");
        assert_eq!(verified.actor_agent_id.as_deref(), Some("agent-x"));
        assert_eq!(verified.subject.as_deref(), Some("user:42"));
    }

    #[test]
    fn principal_less_chain_stays_v1_compatible() {
        // A root with no principal must emit exactly the legacy 4 headers and
        // sign the v1 canonical bytes, so old verifiers/chains interoperate.
        let key = tenant_key("t1");
        let chain = DelegationChain::root(Ulid::new());
        let headers = chain.to_header_pairs(&key);
        assert_eq!(
            headers.len(),
            4,
            "principal-less chain emits the legacy 4 headers"
        );
        assert!(!headers.iter().any(|(k, _)| *k == HEADER_ACTOR));
        assert!(!headers.iter().any(|(k, _)| *k == HEADER_SUBJECT));
    }

    #[test]
    fn verify_fails_when_actor_tampered() {
        let key = tenant_key("t1");
        let chain = DelegationChain::root(Ulid::new())
            .with_principal(Some("agent-x".to_string()), Some("user:42".to_string()));
        let headers = chain.to_header_pairs(&key);
        let mut pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        // Forge the actor while keeping the signature — must not verify.
        for p in pairs.iter_mut() {
            if p.0 == HEADER_ACTOR {
                p.1 = "agent-evil".to_string();
            }
        }
        let err = DelegationChain::parse_and_verify(make_getter(&pairs), &key)
            .expect_err("forged actor must not verify");
        assert_eq!(err, DelegationError::SignatureMismatch);
    }

    #[test]
    fn verify_fails_when_subject_tampered() {
        let key = tenant_key("t1");
        let chain = DelegationChain::root(Ulid::new())
            .with_principal(Some("agent-x".to_string()), Some("user:42".to_string()));
        let headers = chain.to_header_pairs(&key);
        let mut pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        for p in pairs.iter_mut() {
            if p.0 == HEADER_SUBJECT {
                p.1 = "user:9999".to_string();
            }
        }
        let err = DelegationChain::parse_and_verify(make_getter(&pairs), &key)
            .expect_err("forged subject must not verify");
        assert_eq!(err, DelegationError::SignatureMismatch);
    }

    #[test]
    fn fork_child_preserves_principal() {
        let key = tenant_key("t1");
        let root = DelegationChain::root(Ulid::new())
            .with_principal(Some("agent-x".to_string()), Some("user:42".to_string()));
        let child = root.fork_child(Ulid::new()).expect("fork within cap");
        assert_eq!(child.actor_agent_id.as_deref(), Some("agent-x"));
        assert_eq!(child.subject.as_deref(), Some("user:42"));
        assert_eq!(child.depth, 1);
        // The child chain must itself sign + verify with the carried principal.
        let headers = child.to_header_pairs(&key);
        let pairs: Vec<_> = headers.iter().map(|(k, v)| (*k, v.clone())).collect();
        let verified = DelegationChain::parse_and_verify(make_getter(&pairs), &key)
            .expect("forked principal chain verifies");
        assert_eq!(verified.actor_agent_id.as_deref(), Some("agent-x"));
        assert_eq!(verified.depth, 1);
    }

    #[test]
    fn principal_added_to_v1_signature_is_rejected() {
        // Attacker takes a valid principal-less (v1) chain and bolts on actor/
        // subject headers without re-signing — the v2 canonical form won't match
        // the v1 signature, so it must be rejected.
        let key = tenant_key("t1");
        let chain = DelegationChain::root(Ulid::new());
        let mut pairs: Vec<_> = chain
            .to_header_pairs(&key)
            .iter()
            .map(|(k, v)| (*k, v.clone()))
            .collect();
        pairs.push((HEADER_ACTOR, "agent-injected".to_string()));
        let err = DelegationChain::parse_and_verify(make_getter(&pairs), &key)
            .expect_err("principal bolted onto a v1 sig must not verify");
        assert_eq!(err, DelegationError::SignatureMismatch);
    }
}
