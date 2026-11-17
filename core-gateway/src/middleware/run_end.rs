// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Run-end marker middleware
//!
//! Reads `X-Brutor-Run-End` off the inbound request and scopes it as a
//! task-local for the life of the response future, so
//! [`crate::services::proxy_logger::build_proxy_log`] can stamp it onto the
//! step's `proxy_logs` row without every route handler having to thread it
//! down its own call graph.
//!
//! ## Why the header was previously inert
//!
//! There are three ways a run closes — explicit, terminal, idle —
//! and "explicit" means *the client sent `X-Brutor-Run-End` or called
//! `POST /v1/runs/{root_task_id}/end`*. Only the dedicated endpoint was
//! implemented. A client that set the header on its last proxy call (the
//! "zero-round-trip alternative" the runs route's own comment advertises) got
//! nothing, and its run was later closed by the idle sweeper as
//! `terminal_state=abandoned, finalized_by=idle` — a successful run reported
//! as abandoned, which is precisely the signal operators are told to act on.
//!
//! ## Why a task-local, and why the row rather than a direct finalize
//!
//! Two constraints shape this:
//!
//! 1. `proxy_logs` are persisted by a batched background writer, so when the
//!    response returns the *last* step is usually not yet in Postgres.
//!    Finalizing from the request path would aggregate an incomplete run and
//!    then be reopened by its own final step.
//! 2. The marker must not be able to overtake or outlive the step it belongs
//!    to. Carrying it on the same row makes reordering structurally
//!    impossible, and means that if the writer sheds the row under
//!    backpressure the marker is shed with it — no step, no contribution.
//!
//! So the middleware only records intent. The actual finalization happens in
//! [`crate::services::background_writer`] once the batch containing the
//! marked row has committed, where the row's own `root_task_id` is available
//! and the ordering is guaranteed.
//!
//! Validation happens here rather than at the writer: an unknown state is a
//! client mistake worth surfacing early, and dropping it at the edge keeps
//! junk out of the column. We reject by ignoring rather than by failing the
//! request — the caller's actual work (an LLM call, a tool call) has nothing
//! to do with their bookkeeping header, and failing a completed inference
//! because a trailing header was misspelled would be the wrong trade.

use std::pin::Pin;
use std::task::{Context, Poll};

use axum::body::Body;
use axum::http::Request;
use axum::response::Response;
use futures::future::Future;
use tower::{Layer, Service};

use crate::routes::runs::{
    RUN_END_HEADER, SESSION_END_HEADER, SESSION_ID_HEADER, STEP_ID_HEADER, STEP_NAME_HEADER,
    TURN_ID_HEADER, TURN_SEQ_HEADER,
};
use crate::services::run_ledger::terminal;

/// Longest value we will look at. The accepted vocabulary is a handful of
/// short literals; anything longer is not a terminal state and we would
/// rather not carry an attacker-sized string through a task-local.
const MAX_RUN_END_LEN: usize = 32;

/// Same reasoning as [`MAX_RUN_END_LEN`], for the turn id: it is an
/// opaque correlation key, not a payload, and the column is VARCHAR(64).
const MAX_TURN_ID_LEN: usize = 64;
const MAX_STEP_ID_LEN: usize = 64;
/// The session id length rule is the session budget's
/// (`a2a_delegation::MAX_SESSION_ID_LEN`): only the derived 26-character
/// session ULID is stored, and the end-of-conversation marker must be read
/// for every id the budget accepts, or that session's live counter is never
/// dropped. One definition, so the two readers of this header cannot drift.
use crate::services::a2a_delegation::MAX_SESSION_ID_LEN;
const MAX_STEP_NAME_LEN: usize = 128;

tokio::task_local! {
    /// Terminal state the client asked for on this request, already
    /// validated against [`terminal::ALL`]. `None` when the header was
    /// absent, empty, or unrecognised. Unset entirely outside a request
    /// scope (background tasks, tests), which readers treat as "no signal".
    pub static RUN_END_STATE: Option<String>;
    /// The outcome the client reported on this request
    /// (`X-Brutor-Outcome-Id`), stored verbatim (truncated to
    /// `run_outcome::MAX_STORED_LEN`) on the step row so the run is judged
    /// from it however it is finalized. A malformed value is KEPT (it is
    /// judged `undeclared` at finalize) and never fails the request: the
    /// outcome rides on the call that records the work, and failing it would
    /// lose the work to punish the label. `None` when absent or blank, and on
    /// the A2A outbound route, whose row carries the DELEGATE's outcome.
    pub static OUTCOME_ID: Option<String>;

    /// Client-asserted turn identity for this request, as
    /// `(turn_id, turn_seq)`. `None` when the client asserted nothing,
    /// in which case the ledger infers turns from model-call boundaries
    /// and records that it did.
    pub static TURN_CONTEXT: Option<(String, Option<i32>)>;

    /// The raw session id the client asserted (`X-Brutor-Session-Id`), and
    /// whether this request also declared the session finished
    /// (`X-Brutor-Session-End`), as `(session_id, ended)`.
    ///
    /// Stored raw here; the HMAC derivation needs the tenant key, which the
    /// middleware does not have. Resolved downstream where the key is in
    /// scope. `None` when nothing was asserted, which is the normal case for
    /// service-to-service traffic that has no conversation.
    pub static SESSION_CONTEXT: Option<(String, bool)>;

    /// The DERIVED session key for this request, once a route with the
    /// tenant key in scope has resolved it. A `RefCell` because it is filled
    /// in mid-request, after auth — the same pattern as `CONSUMED_APPROVAL`.
    ///
    /// Separate from `SESSION_CONTEXT` (which holds the client's raw claim)
    /// because the raw value must never reach the log: a caller could
    /// otherwise assert a colleague's chat id and have their traffic filed
    /// under it. Only the HMAC-derived key is recorded.
    pub static RESOLVED_SESSION: std::cell::RefCell<Option<String>>;

    /// Client-asserted logical phase for this request, as
    /// `(step_id, step_name)`. `None` when the client asserted nothing —
    /// and unlike turns, nothing is inferred in that case. A step the
    /// client did not name did not happen as far as the ledger is
    /// concerned, which is why `step_count` reads 0 rather than 1.
    pub static STEP_CONTEXT: Option<(String, Option<String>)>;

    /// The approval consumed during this request, if any.
    ///
    /// Unlike the two above, this is discovered *during* the request rather
    /// than read from a header, so it is a `RefCell` the approval path writes
    /// into. Scoping it here — around the whole handler — is what lets the
    /// retried call's audit row carry the approval that authorised it without
    /// threading an id through every surface that can consume a token.
    pub static CONSUMED_APPROVAL: std::cell::RefCell<Option<String>>;

    /// The upstream provider's own request id for this call.
    /// Same shape and rationale as CONSUMED_APPROVAL: discovered during
    /// the request, needed on the audit row, and not worth threading
    /// through every forward-path return type to get there.
    pub static UPSTREAM_REQUEST_ID: std::cell::RefCell<Option<String>>;

    /// Set when an LLM provider answered this request 2xx: from then on the
    /// call's admission is SPEND — an end nobody reported (the client hung
    /// up mid-stream) keeps the reservation instead of handing it back. An
    /// `Arc` so the call's hold, which can outlive this task inside a
    /// response stream, reads the same flag (`llm_preflight::PreflightHold`).
    pub static UPSTREAM_BILLABLE: std::sync::Arc<std::sync::atomic::AtomicBool>;

    /// Values that must live as long as this request's RESPONSE — until its
    /// body has been sent in full or dropped (client gone) — not merely as
    /// long as the handler: an LLM call's admission hold, which a streaming
    /// response outlives its handler by the whole stream.
    pub static RESPONSE_KEEPALIVE: KeepAlive;

    /// The in-flight slot keys this request holds (`usage::
    /// acquire_llm_concurrency`). A request is ONE request in flight however
    /// many layers gate it: the chat route takes the group's slot and the
    /// dispatch gate below it must not take the same slot again : taken twice, a
    /// group's `max_concurrent` admits half as many calls as configured.
    pub static REQUEST_SLOTS: HeldSlots;

    /// Guardrail evidence for this request, as
    /// `(config_id, check, action)`.
    ///
    /// Recorded by the orchestrator when a check hits, rather than by each
    /// site that builds an audit row. Four of the fourteen block sites
    /// receive only a boolean across a boundary — a skill verdict, an SSE
    /// stream flag, a helper taking `check` as a string — and could not
    /// name the config that fired. Recording at the source fixes all of
    /// them without threading evidence through three unrelated carriers.
    pub static GUARDRAIL_EVIDENCE: std::cell::RefCell<Option<(String, String, String)>>;

    /// Checks that were configured but did NOT run on this call, because the
    /// detector errored or timed out and the config opted into log-and-allow
    /// (`fail_open = true`). Each entry is `(config_name, check, reason)`.
    ///
    /// The mirror of `GUARDRAIL_EVIDENCE`: that one records a check that
    /// fired, this one records a check that was skipped. Without it a
    /// fail-open call is indistinguishable in `proxy_logs` from a call that
    /// was checked and found clean — which would let the assurance report
    /// claim guardrail coverage that never happened. An unchecked call that
    /// looks checked is worse than a refused one.
    ///
    /// Accumulates (a call can skip several checks); recorded at the source
    /// for the same reason as the evidence above.
    pub static GUARDRAILS_SKIPPED: std::cell::RefCell<Vec<(String, String, String)>>;

    /// Checks that ran on a *degraded* rung of the resilience ladder:
    /// the remote detector was retried, or was down and the local built-in
    /// answered instead.
    ///
    /// Separate from `GUARDRAILS_SKIPPED` on purpose. That column means "the
    /// check did not run"; this one means "it ran, but not on the detector
    /// you configured". Folding them together would either overstate coverage
    /// or understate it, and the assurance report needs to tell a genuine
    /// lower-fidelity pass from no pass at all.
    pub static GUARDRAILS_DEGRADED: std::cell::RefCell<Vec<(String, String, String)>>;

    /// State a quota/budget decision was taken against.
    ///
    /// Recorded where the decision is made, because that is the only place
    /// the aggregates exist — the enforcement functions return a verdict,
    /// not the numbers behind it, and threading those back through every
    /// caller would change a lot of signatures to carry evidence.
    pub static PRE_ACTION_STATE: std::cell::RefCell<Option<serde_json::Value>>;
}

/// Record the approval whose one-time token authorised this request.
///
/// Silently no-ops outside a request scope (background tasks, tests), which is
/// the same "no signal" the readers already treat as absent.
pub fn record_consumed_approval(request_id: &str) {
    let _ = CONSUMED_APPROVAL.try_with(|c| {
        *c.borrow_mut() = Some(request_id.to_string());
    });
}

/// Record the state a quota/budget decision was taken against.
///
/// First writer wins. Several quota planes can run on one call (tenant-wide,
/// then group, then per-model); the first is the one that governed the
/// broadest scope, and later, narrower checks only ever tighten. Overwriting
/// would leave the row describing the last check rather than the binding one.
pub fn record_pre_action_state(state: serde_json::Value) {
    let _ = PRE_ACTION_STATE.try_with(|c| {
        let mut slot = c.borrow_mut();
        if slot.is_none() {
            *slot = Some(state);
        }
    });
}

/// Record a check that was configured but did not run (fail-open).
pub fn record_guardrail_skipped(config_name: &str, check: &str, reason: &str) {
    let _ = GUARDRAILS_SKIPPED.try_with(|c| {
        c.borrow_mut().push((
            config_name.to_string(),
            check.to_string(),
            reason.to_string(),
        ));
    });
}

/// Record a check that ran on a degraded rung (retried / local fallback).
pub fn record_guardrail_degraded(config_name: &str, check: &str, tier: &str) {
    let _ = GUARDRAILS_DEGRADED.try_with(|c| {
        c.borrow_mut()
            .push((config_name.to_string(), check.to_string(), tier.to_string()));
    });
}

/// Checks that ran degraded on this call. Empty on the happy path, which is
/// the overwhelming majority of rows.
pub fn guardrails_degraded() -> Vec<(String, String, String)> {
    GUARDRAILS_DEGRADED
        .try_with(|c| c.borrow().clone())
        .unwrap_or_default()
}

/// Checks skipped on this call, for the audit row. Empty when every
/// configured check actually ran.
pub fn guardrails_skipped() -> Vec<(String, String, String)> {
    GUARDRAILS_SKIPPED
        .try_with(|c| c.borrow().clone())
        .unwrap_or_default()
}

/// The pre-action state recorded on this call, for the audit row.
pub fn pre_action_state() -> Option<serde_json::Value> {
    PRE_ACTION_STATE
        .try_with(|c| c.borrow().clone())
        .ok()
        .flatten()
}

/// Record which guardrail check fired, and what it did.
///
/// Last writer wins. A request can trip checks on more than one surface
/// (input, then output); the audit row shows the most recent, and the block
/// sites that hold the violation still stamp it explicitly, which is the more
/// precise answer where one is available.
pub fn record_guardrail_evidence(config_id: &str, check: &str, action: &str) {
    let _ = GUARDRAIL_EVIDENCE.try_with(|c| {
        *c.borrow_mut() = Some((config_id.to_string(), check.to_string(), action.to_string()));
    });
}

/// Guardrail evidence seen on this call, for the audit row.
pub fn guardrail_evidence() -> Option<(String, String, String)> {
    GUARDRAIL_EVIDENCE
        .try_with(|c| c.borrow().clone())
        .ok()
        .flatten()
}

/// Record the upstream provider's own request id for this call.
///
/// The handle a vendor asks for when a call is disputed, and the only join
/// between our audit trail and theirs. Recorded from the response as it
/// arrives rather than plumbed back through the return type, because the
/// forward path already returns several different shapes across streaming,
/// non-streaming and fallback.
///
/// **Last writer wins**, deliberately. On a routing-group fallback several
/// upstreams may be tried; the id that belongs on the audit row is the one
/// from the model that actually answered, which is the final recorded send.
pub fn record_upstream_request_id(headers: &reqwest::header::HeaderMap) {
    // OpenAI and most compatible providers use `x-request-id`; Anthropic
    // sends `request-id`. Checked in that order, first hit wins.
    for name in ["x-request-id", "request-id"] {
        if let Some(v) = headers.get(name).and_then(|v| v.to_str().ok()) {
            let v = v.trim();
            if !v.is_empty() && v.len() <= 128 {
                let _ = UPSTREAM_REQUEST_ID.try_with(|c| {
                    *c.borrow_mut() = Some(v.to_string());
                });
            }
            return;
        }
    }
}

/// Record an LLM provider's response: its request id (for the audit row) and,
/// when it is 2xx, that the call is now billable. One call at every send
/// site, so neither can be forgotten without the other.
pub fn record_upstream_response(status: u16, headers: &reqwest::header::HeaderMap) {
    record_upstream_request_id(headers);
    if (200..300).contains(&status) {
        mark_upstream_billable();
    }
}

/// The provider answered this request 2xx (see `UPSTREAM_BILLABLE`). Call
/// at every LLM send site the moment a 2xx status is seen.
pub fn mark_upstream_billable() {
    let _ = UPSTREAM_BILLABLE.try_with(|f| f.store(true, std::sync::atomic::Ordering::Release));
}

/// Keep `value` alive until this request's response body is done. `Err`
/// hands it back when called outside a request (no middleware scope), so
/// the caller keeps it itself.
pub fn keep_alive_with_response<T: Send + 'static>(value: T) -> Result<(), T> {
    let mut slot = Some(value);
    let _ = RESPONSE_KEEPALIVE.try_with(|c| {
        if let (Some(v), Ok(mut kept)) = (slot.take(), c.lock()) {
            kept.push(Box::new(v));
        }
    });
    match slot {
        None => Ok(()),
        Some(v) => Err(v),
    }
}

/// A response body that owns this request's keep-alive values until the
/// body ends (or is dropped).
struct KeptBody {
    inner: axum::body::BodyDataStream,
    keep: Vec<Box<dyn std::any::Any + Send>>,
}

impl futures::Stream for KeptBody {
    type Item = Result<bytes::Bytes, axum::Error>;
    fn poll_next(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Option<Self::Item>> {
        let polled = std::pin::Pin::new(&mut self.inner).poll_next(cx);
        if let std::task::Poll::Ready(None) = polled {
            // Fully sent: end the holds now, not when hyper drops the body.
            self.keep.clear();
        }
        polled
    }
}

/// The keep-alive values of one request (see `RESPONSE_KEEPALIVE`).
pub type KeepAlive = std::sync::Arc<std::sync::Mutex<Vec<Box<dyn std::any::Any + Send>>>>;

/// Tie a request's keep-alive values to its response body.
fn keep_with_body(
    response: axum::response::Response,
    keep: &KeepAlive,
) -> axum::response::Response {
    let kept = keep
        .lock()
        .map(|mut k| std::mem::take(&mut *k))
        .unwrap_or_default();
    if kept.is_empty() {
        return response;
    }
    let (parts, body) = response.into_parts();
    let body = KeptBody {
        inner: body.into_data_stream(),
        keep: kept,
    };
    axum::response::Response::from_parts(parts, axum::body::Body::from_stream(body))
}

/// Slot keys held by one request (see `REQUEST_SLOTS`).
pub type HeldSlots = std::sync::Arc<std::sync::Mutex<std::collections::HashSet<String>>>;

/// This request's held-slot set; `None` outside a request scope.
pub fn request_slots() -> Option<HeldSlots> {
    REQUEST_SLOTS.try_with(std::sync::Arc::clone).ok()
}

/// This request's billable flag, for a hold that must outlive the task.
pub fn upstream_billable_flag() -> Option<std::sync::Arc<std::sync::atomic::AtomicBool>> {
    UPSTREAM_BILLABLE.try_with(std::sync::Arc::clone).ok()
}

/// The upstream provider request id seen on this call, for the audit row.
pub fn upstream_request_id() -> Option<String> {
    UPSTREAM_REQUEST_ID
        .try_with(|c| c.borrow().clone())
        .ok()
        .flatten()
}

/// The approval consumed during this request, for the audit row.
pub fn consumed_approval() -> Option<String> {
    CONSUMED_APPROVAL
        .try_with(|c| c.borrow().clone())
        .ok()
        .flatten()
}

/// Read the session headers.
///
/// An over-long or empty id is treated as absent rather than rejected, for
/// the same reason turn ids are: session identity is a correlation
/// assertion, and failing a request over a malformed one would let a
/// client's telemetry bug take down its own traffic.
///
/// `X-Brutor-Session-End` marks the LAST request of a conversation — the
/// portal sends it when a chat is deleted. It is carried on a request rather
/// than sent as a separate call so the signal cannot be lost to a failed
/// follow-up: the row that records the final action also records that it was
/// final.
fn parse_session(headers: &axum::http::HeaderMap) -> Option<(String, bool)> {
    let id = headers
        .get(SESSION_ID_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|v| !v.is_empty() && v.len() <= MAX_SESSION_ID_LEN)?;
    let ended = headers
        .get(SESSION_END_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(|v| matches!(v.trim(), "1" | "true" | "TRUE" | "True"))
        .unwrap_or(false);
    Some((id.to_string(), ended))
}

/// The session asserted on this request, as `(session_id, ended)`.
/// `None` outside a request scope and when nothing was asserted.
pub fn asserted_session() -> Option<(String, bool)> {
    SESSION_CONTEXT.try_with(Clone::clone).ok().flatten()
}

/// Record the derived session key for this request. Called by the route
/// once the tenant key is in scope. Silently no-ops outside a request scope.
pub fn set_resolved_session(session: String) {
    let _ = RESOLVED_SESSION.try_with(|c| *c.borrow_mut() = Some(session));
}

/// The derived session key, if a route resolved one. `None` when the client
/// asserted nothing — the normal case for service-to-service traffic.
pub fn resolved_session() -> Option<String> {
    RESOLVED_SESSION
        .try_with(|c| c.borrow().clone())
        .ok()
        .flatten()
}

/// Read and validate the turn headers. An over-long or empty id is
/// treated as absent rather than rejected: turn identity is an
/// observability assertion, and refusing the request over a malformed
/// one would let a client's telemetry bug take down its own traffic.
fn parse_turn(headers: &axum::http::HeaderMap) -> Option<(String, Option<i32>)> {
    let id = headers
        .get(TURN_ID_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|v| !v.is_empty() && v.len() <= MAX_TURN_ID_LEN)?;
    let seq = headers
        .get(TURN_SEQ_HEADER)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.trim().parse::<i32>().ok())
        .filter(|n| *n >= 0);
    Some((id.to_string(), seq))
}

/// Parse the asserted step identity from the request headers.
///
/// Name without id is dropped: a label with nothing to group by cannot be
/// aggregated, and a half-assertion recorded as a whole one is exactly the
/// upgrade-a-guess failure `turn_attribution` exists to prevent.
fn parse_step(headers: &axum::http::HeaderMap) -> Option<(String, Option<String>)> {
    let id = headers
        .get(STEP_ID_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|v| !v.is_empty() && v.len() <= MAX_STEP_ID_LEN)?;
    let name = headers
        .get(STEP_NAME_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|v| !v.is_empty() && v.len() <= MAX_STEP_NAME_LEN)
        .map(str::to_string);
    Some((id.to_string(), name))
}

/// Step identity the client asserted on this request.
///
/// `None` outside a request scope and when nothing was asserted. There is no
/// inference fallback by design — see [`STEP_CONTEXT`].
pub fn asserted_step() -> Option<(String, Option<String>)> {
    STEP_CONTEXT.try_with(|s| s.clone()).ok().flatten()
}

/// Read the validated run-end state for the current request.
///
/// Returns `None` both when the client asked for nothing and when there is no
/// request scope at all — the distinction is not useful to callers, since
/// either way there is no end to record.
pub fn requested_run_end() -> Option<String> {
    RUN_END_STATE.try_with(|s| s.clone()).ok().flatten()
}

tokio::task_local! {
    /// W3C `traceparent` the caller sent, parsed to
    /// `(trace_id, span_id)` and validated. `None` when absent or malformed;
    /// a malformed header is dropped, not "repaired", because a trace id
    /// that is almost right joins the wrong trace.
    pub static TRACE_CONTEXT: Option<(String, String)>;

    /// Provenance of the tool this request calls, discovered
    /// mid-request by the MCP route once the tool name is known:
    /// `(server_version, tool_definition_hash)`. Same `RefCell` pattern as
    /// `CONSUMED_APPROVAL`: filled in during the request, read when the
    /// audit row is built.
    pub static TOOL_PROVENANCE: std::cell::RefCell<Option<(Option<String>, Option<String>)>>;
}

/// The trace context the caller asserted on this request. None outside a
/// request scope or when no valid `traceparent` was sent.
pub fn asserted_trace() -> Option<(String, String)> {
    TRACE_CONTEXT.try_with(|t| t.clone()).ok().flatten()
}

/// Record the provenance of the tool this request calls. No-op outside a
/// request scope.
pub fn set_tool_provenance(prov: (Option<String>, Option<String>)) {
    let _ = TOOL_PROVENANCE.try_with(|cell| *cell.borrow_mut() = Some(prov));
}

/// The tool provenance recorded for this request, if any.
pub fn tool_provenance() -> Option<(Option<String>, Option<String>)> {
    TOOL_PROVENANCE
        .try_with(|cell| cell.borrow().clone())
        .ok()
        .flatten()
}

/// Parse a W3C `traceparent` (`00-<32 hex trace>-<16 hex span>-<2 hex flags>`).
///
/// Only the ids are kept. All-zero ids are invalid per the spec and are
/// rejected; so is any version we do not know, since its field layout is
/// not guaranteed to be this one.
pub fn parse_traceparent(raw: &str) -> Option<(String, String)> {
    let raw = raw.trim();
    let mut parts = raw.split('-');
    let version = parts.next()?;
    let trace = parts.next()?;
    let span = parts.next()?;
    let flags = parts.next()?;
    if parts.next().is_some() || version != "00" || flags.len() != 2 {
        return None;
    }
    let hex = |s: &str, n: usize| s.len() == n && s.bytes().all(|b| b.is_ascii_hexdigit());
    if !hex(trace, 32) || !hex(span, 16) || !hex(flags, 2) {
        return None;
    }
    if trace.bytes().all(|b| b == b'0') || span.bytes().all(|b| b == b'0') {
        return None;
    }
    Some((trace.to_ascii_lowercase(), span.to_ascii_lowercase()))
}

fn parse_trace(headers: &axum::http::HeaderMap) -> Option<(String, String)> {
    headers
        .get("traceparent")
        .and_then(|v| v.to_str().ok())
        .and_then(parse_traceparent)
}

/// The outcome the client reported on this request, as stored.
/// `None` outside a request scope and when nothing was reported.
pub fn requested_outcome_id() -> Option<String> {
    OUTCOME_ID.try_with(|s| s.clone()).ok().flatten()
}

/// Read `X-Brutor-Outcome-Id`. Never rejects: a malformed value is
/// stored verbatim (truncated) and judged `undeclared` at finalize.
///
/// Not read on the A2A outbound route: that route's row records the outcome
/// the DELEGATE reported on its reply, which is what the caller's segment is
/// judged on; the caller closes its own run on another call or on
/// `POST /v1/runs/{id}/end`.
fn parse_outcome_id(path: &str, headers: &axum::http::HeaderMap) -> Option<String> {
    if path == crate::constants::routes::A2A_OUTBOUND {
        return None;
    }
    headers
        .get(crate::services::run_outcome::OUTCOME_ID_HEADER)
        .and_then(crate::services::run_outcome::stored_from_header)
}

/// Turn identity the client asserted on this request, as
/// `(turn_id, turn_seq)`.
///
/// `None` outside a request scope and when nothing was asserted — both of
/// which the ledger reads as "infer the turns, and record that you did".
pub fn asserted_turn() -> Option<(String, Option<i32>)> {
    TURN_CONTEXT.try_with(|t| t.clone()).ok().flatten()
}

/// Parse and validate the header value.
///
/// `true` is accepted as "ended, infer the outcome" for symmetry with the
/// `POST /v1/runs/{id}/end` route, which has always taken it that way. It maps
/// to `None` here: the ledger derives the outcome from the steps, and writing a
/// literal "true" into the column would be a value no consumer understands.
/// Returning `None` for it does mean such a run falls back to derivation,
/// which is exactly what "infer the outcome" asks for.
fn parse_run_end(raw: &str) -> Option<String> {
    let trimmed = raw.trim();
    if trimmed.is_empty() || trimmed.len() > MAX_RUN_END_LEN {
        return None;
    }
    if trimmed.eq_ignore_ascii_case("true") {
        return None;
    }
    let lowered = trimmed.to_ascii_lowercase();
    terminal::ALL
        .iter()
        .find(|s| **s == lowered)
        .map(|s| (*s).to_string())
}

/// Tower layer. Its one piece of state is the MCP session store:
/// the per-request values are read on each `call`.
#[derive(Clone)]
pub struct RunEndLayer {
    sessions: crate::services::agent_impl::SessionStore,
}

impl RunEndLayer {
    /// `sessions` is the app's [`crate::services::AppState::mcp_sessions`] —
    /// the shared store, so a session bound on one replica resolves on all.
    pub fn new(sessions: crate::services::agent_impl::SessionStore) -> Self {
        Self { sessions }
    }
}

impl<S> Layer<S> for RunEndLayer {
    type Service = RunEndService<S>;

    fn layer(&self, inner: S) -> Self::Service {
        RunEndService {
            inner,
            sessions: self.sessions.clone(),
        }
    }
}

#[derive(Clone)]
pub struct RunEndService<S> {
    inner: S,
    sessions: crate::services::agent_impl::SessionStore,
}

impl<S> Service<Request<Body>> for RunEndService<S>
where
    S: Service<Request<Body>, Response = Response> + Clone + Send + 'static,
    S::Future: Send + 'static,
    S::Error: Send + 'static,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = Pin<Box<dyn Future<Output = Result<Self::Response, Self::Error>> + Send>>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, req: Request<Body>) -> Self::Future {
        let mut inner = self.inner.clone();
        let sessions = self.sessions.clone();
        Box::pin(async move {
            // Capture the request's implementation carriers
            // (headers, baggage, MCP `clientInfo`) once, for every surface.
            // Async because an MCP call's `clientInfo` lives in its body;
            // the body is handed back untouched.
            let (req, carriers, pending_session) =
                match crate::services::agent_impl::capture(req, &sessions).await {
                    Ok(captured) => captured,
                    Err(refused) => return Ok(refused),
                };

            let requested = req
                .headers()
                .get(RUN_END_HEADER)
                .and_then(|v| v.to_str().ok())
                .and_then(parse_run_end);

            let outcome = parse_outcome_id(req.uri().path(), req.headers());
            let turn = parse_turn(req.headers());
            let step = parse_step(req.headers());
            let session = parse_session(req.headers());
            let trace = parse_trace(req.headers());
            // Request-scoped evidence facts (conversation handle,
            // enqueued approval, two-phase reserved row id).
            let conversation =
                crate::services::evidence::request::handle_from_headers(req.headers());

            let keep_alive: KeepAlive = Default::default();
            // Boxed to a `dyn Future + Send`: the nested scopes are deep
            // enough that the compiler cannot otherwise prove the outer
            // future `Send` for every lifetime of the session-store borrow.
            let scoped: Pin<Box<dyn Future<Output = Result<Response, S::Error>> + Send>> = Box::pin(crate::services::agent_impl::scope(
                carriers,
                OUTCOME_ID.scope(
                    outcome,
                    RUN_END_STATE.scope(
                        requested,
                        TURN_CONTEXT.scope(
                            turn,
                            SESSION_CONTEXT.scope(
                                session,
                                RESOLVED_SESSION.scope(
                                    std::cell::RefCell::new(None),
                                    STEP_CONTEXT.scope(
                                        step,
                                        CONSUMED_APPROVAL.scope(
                                            std::cell::RefCell::new(None),
                                            UPSTREAM_REQUEST_ID.scope(
                                                std::cell::RefCell::new(None),
                                                UPSTREAM_BILLABLE.scope(
                                                std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
                                                GUARDRAIL_EVIDENCE.scope(
                                                    std::cell::RefCell::new(None),
                                                    GUARDRAILS_SKIPPED.scope(
                                                        std::cell::RefCell::new(Vec::new()),
                                                        GUARDRAILS_DEGRADED.scope(
                                                            std::cell::RefCell::new(Vec::new()),
                                                            PRE_ACTION_STATE.scope(
                                                                std::cell::RefCell::new(None),
                                                                TRACE_CONTEXT.scope(
                                                                    trace,
                                                                    TOOL_PROVENANCE.scope(
                                                                        std::cell::RefCell::new(None),
                                                                        RESPONSE_KEEPALIVE.scope(
                                                                            keep_alive.clone(),
                                                                            REQUEST_SLOTS.scope(
                                                                            HeldSlots::default(),
                                                                            crate::services::evidence::request::scope(
                                                                                conversation,
                                                                                inner.call(req),
                                                                            ),
                                                                            ),
                                                                        ),
                                                                    ),
                                                                ),
                                                            ),
                                                        ),
                                                    ),
                                                ),
                                                ),
                                            ),
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ));
            let response = scoped.await?;
            // An `initialize`'s clientInfo is bound to the
            // session id the server issued, for the session's later calls.
            if let Some(pending) = pending_session {
                crate::services::agent_impl::bind_session(&sessions, &pending, response.headers())
                    .await;
            }
            Ok(keep_with_body(response, &keep_alive))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// One session id, one length rule. The session budget is keyed by the
    /// id `a2a_delegation::resolve_asserted_session` accepts (≤ 200), so the
    /// end-of-conversation marker must be read for every id it accepts —
    /// otherwise a 65–200 character id gets a session whose live counter is
    /// never dropped (this parser used to stop at 64, 2026-10-26).
    #[test]
    fn session_end_is_read_for_every_session_id_the_budget_accepts() {
        use crate::services::a2a_delegation::MAX_SESSION_ID_LEN as BUDGET_MAX;
        let mut h = axum::http::HeaderMap::new();
        let longest = "s".repeat(BUDGET_MAX);
        h.insert(SESSION_ID_HEADER, longest.parse().unwrap());
        h.insert(SESSION_END_HEADER, "true".parse().unwrap());
        assert_eq!(parse_session(&h), Some((longest, true)));

        let too_long = "s".repeat(BUDGET_MAX + 1);
        h.insert(SESSION_ID_HEADER, too_long.parse().unwrap());
        assert_eq!(
            parse_session(&h),
            None,
            "over the shared limit: no session, as the budget"
        );
    }

    #[test]
    fn accepts_every_terminal_state() {
        for state in terminal::ALL {
            assert_eq!(
                parse_run_end(state).as_deref(),
                Some(state),
                "{state} should be accepted verbatim"
            );
        }
    }

    #[test]
    fn accepts_mixed_case_and_padding() {
        assert_eq!(parse_run_end("  Completed  ").as_deref(), Some("completed"));
        assert_eq!(parse_run_end("ERRORED").as_deref(), Some("errored"));
    }

    #[test]
    fn true_means_infer_not_a_literal_state() {
        // Symmetry with POST /v1/runs/{id}/end, which has always read a bare
        // `true` as "ended, you work out the outcome". Writing "true" into
        // run_end_state would put a value in the column that no consumer maps
        // to anything.
        assert_eq!(parse_run_end("true"), None);
        assert_eq!(parse_run_end("TRUE"), None);
    }

    #[test]
    fn rejects_unknown_states() {
        // The specific worry: a client sending something that *looks* like
        // success would otherwise write an unrecognised terminal state that
        // dashboards would have to guess at.
        assert_eq!(parse_run_end("done"), None);
        assert_eq!(parse_run_end("ok"), None);
        assert_eq!(parse_run_end("success"), None);
        assert_eq!(parse_run_end("completed_ish"), None);
    }

    #[test]
    fn rejects_blank_and_oversized() {
        assert_eq!(parse_run_end(""), None);
        assert_eq!(parse_run_end("   "), None);
        assert_eq!(parse_run_end(&"a".repeat(MAX_RUN_END_LEN + 1)), None);
    }

    #[test]
    fn outside_a_request_scope_there_is_no_signal() {
        // Background tasks and unit tests never enter the layer; they must not
        // panic on the task-local access.
        assert_eq!(requested_run_end(), None);
    }

    #[tokio::test]
    async fn scoped_value_is_visible_to_the_handler() {
        RUN_END_STATE
            .scope(Some(terminal::COMPLETED.to_string()), async {
                assert_eq!(requested_run_end().as_deref(), Some(terminal::COMPLETED));
            })
            .await;
    }

    #[tokio::test]
    async fn scoped_none_reads_as_no_signal() {
        RUN_END_STATE
            .scope(None, async {
                assert_eq!(requested_run_end(), None);
            })
            .await;
    }

    // -----------------------------------------------------------------
    // Request-scoped audit facts
    //
    // These four task-locals carry evidence discovered *during* a request
    // onto its audit row. Two failure modes matter and neither errors:
    // a value recorded outside a scope would panic, and a reader outside a
    // scope would panic — both are silent no-ops instead, because a proxy
    // must not fall over recording an audit detail.
    // -----------------------------------------------------------------

    #[tokio::test]
    async fn recorded_audit_facts_are_visible_within_the_request_scope() {
        CONSUMED_APPROVAL
            .scope(std::cell::RefCell::new(None), async {
                assert_eq!(consumed_approval(), None, "nothing recorded yet");
                record_consumed_approval("appr-1");
                assert_eq!(consumed_approval().as_deref(), Some("appr-1"));
            })
            .await;

        GUARDRAIL_EVIDENCE
            .scope(std::cell::RefCell::new(None), async {
                record_guardrail_evidence("cfg-1", "pii", "redact");
                let (cfg, check, action) = guardrail_evidence().expect("recorded");
                assert_eq!(
                    (cfg.as_str(), check.as_str(), action.as_str()),
                    ("cfg-1", "pii", "redact")
                );
            })
            .await;
    }

    // -----------------------------------------------------------------
    // Checks that did NOT run.
    //
    // The mirror of the evidence above. Without this a fail-open call is
    // indistinguishable in proxy_logs from a call that was checked and
    // found clean, and the assurance report claims coverage that never
    // happened.
    // -----------------------------------------------------------------

    #[tokio::test]
    async fn skipped_checks_accumulate_within_the_request_scope() {
        GUARDRAILS_SKIPPED
            .scope(std::cell::RefCell::new(Vec::new()), async {
                assert!(
                    guardrails_skipped().is_empty(),
                    "clean call records nothing"
                );

                // A call can skip more than one check, so this accumulates
                // rather than first-writer-wins like PRE_ACTION_STATE.
                record_guardrail_skipped("cfg-1", "pii", "provider timeout");
                record_guardrail_skipped("cfg-1", "secrets", "connection refused");

                let skipped = guardrails_skipped();
                assert_eq!(skipped.len(), 2, "both skips recorded");
                assert_eq!(skipped[0].1, "pii");
                assert_eq!(skipped[1].1, "secrets");
                assert!(
                    skipped[0].2.contains("timeout"),
                    "the cause must survive to the audit row",
                );
            })
            .await;
    }

    #[tokio::test]
    async fn a_checked_call_records_no_skips() {
        // The distinction the whole column exists for: empty means every
        // configured check actually ran, and the row is trustworthy.
        GUARDRAILS_SKIPPED
            .scope(std::cell::RefCell::new(Vec::new()), async {
                assert!(guardrails_skipped().is_empty());
            })
            .await;
    }

    #[test]
    fn recording_outside_a_request_scope_is_a_silent_no_op() {
        // Background tasks and unit tests have no scope. A proxy must not
        // fall over recording an audit detail.
        record_guardrail_skipped("cfg", "pii", "boom");
        assert!(guardrails_skipped().is_empty());
    }

    #[test]
    fn recording_outside_a_request_scope_is_a_no_op_not_a_panic() {
        // Background tasks and tests build proxy logs with no scope. This
        // must degrade to "no signal", never take the process down.
        record_consumed_approval("appr-x");
        record_guardrail_evidence("cfg-x", "pii", "block");
        record_pre_action_state(serde_json::json!({"kind": "llm_quota"}));
        assert_eq!(consumed_approval(), None);
        assert_eq!(guardrail_evidence(), None);
        assert_eq!(pre_action_state(), None);
        assert_eq!(upstream_request_id(), None);
    }

    #[tokio::test]
    async fn the_last_guardrail_to_fire_is_the_one_reported() {
        // A request can trip checks on input and then on output. The row
        // shows the most recent, which is what the block site would have
        // stamped explicitly had it held the violation.
        GUARDRAIL_EVIDENCE
            .scope(std::cell::RefCell::new(None), async {
                record_guardrail_evidence("cfg-in", "prompt_injection", "warn");
                record_guardrail_evidence("cfg-out", "pii", "block");
                let (cfg, _, action) = guardrail_evidence().expect("recorded");
                assert_eq!(cfg, "cfg-out");
                assert_eq!(action, "block");
            })
            .await;
    }

    #[tokio::test]
    async fn the_first_quota_plane_is_the_one_reported() {
        // Tenant-wide runs before group, which runs before per-model, and
        // the narrower checks only tighten. Overwriting would leave the row
        // describing the last check rather than the one that governed.
        PRE_ACTION_STATE
            .scope(std::cell::RefCell::new(None), async {
                record_pre_action_state(serde_json::json!({"scope": "tenant"}));
                record_pre_action_state(serde_json::json!({"scope": "resource_group"}));
                assert_eq!(
                    pre_action_state().unwrap()["scope"].as_str(),
                    Some("tenant"),
                    "first writer must win for quota state",
                );
            })
            .await;
    }

    #[tokio::test]
    async fn the_upstream_id_reflects_the_model_that_answered() {
        // On a routing-group fallback several upstreams are tried; the id
        // that belongs on the row is the last successful send.
        UPSTREAM_REQUEST_ID
            .scope(std::cell::RefCell::new(None), async {
                let mut h = reqwest::header::HeaderMap::new();
                h.insert("x-request-id", "req_first".parse().unwrap());
                record_upstream_request_id(&h);

                let mut h2 = reqwest::header::HeaderMap::new();
                h2.insert("x-request-id", "req_second".parse().unwrap());
                record_upstream_request_id(&h2);

                assert_eq!(upstream_request_id().as_deref(), Some("req_second"));
            })
            .await;
    }

    #[tokio::test]
    async fn anthropics_header_name_is_accepted_too() {
        UPSTREAM_REQUEST_ID
            .scope(std::cell::RefCell::new(None), async {
                let mut h = reqwest::header::HeaderMap::new();
                h.insert("request-id", "req_anthropic".parse().unwrap());
                record_upstream_request_id(&h);
                assert_eq!(upstream_request_id().as_deref(), Some("req_anthropic"));
            })
            .await;
    }

    #[tokio::test]
    async fn an_absent_or_junk_upstream_id_records_nothing() {
        UPSTREAM_REQUEST_ID
            .scope(std::cell::RefCell::new(None), async {
                record_upstream_request_id(&reqwest::header::HeaderMap::new());
                assert_eq!(upstream_request_id(), None, "no header, no value");

                let mut h = reqwest::header::HeaderMap::new();
                h.insert("x-request-id", "   ".parse().unwrap());
                record_upstream_request_id(&h);
                assert_eq!(upstream_request_id(), None, "whitespace is not an id");

                let mut h2 = reqwest::header::HeaderMap::new();
                h2.insert("x-request-id", "x".repeat(200).parse().unwrap());
                record_upstream_request_id(&h2);
                assert_eq!(upstream_request_id(), None, "over-long is rejected");
            })
            .await;
    }
}

#[cfg(test)]
mod step_assertion_tests {
    //! The Step layer is client-asserted ONLY.
    //!
    //! These pin the property the whole layer rests on: absence of a header
    //! means absence of a phase. A future change that starts inferring steps
    //! from traffic — the obvious "helpful" optimisation — has to delete a
    //! test that says why not.
    use super::*;
    use axum::http::HeaderMap;

    fn hm(pairs: &[(&str, &str)]) -> HeaderMap {
        let mut h = HeaderMap::new();
        for (k, v) in pairs {
            h.insert(
                axum::http::HeaderName::from_bytes(k.as_bytes()).unwrap(),
                v.parse().unwrap(),
            );
        }
        h
    }

    #[test]
    fn no_header_asserts_no_step() {
        assert!(parse_step(&hm(&[])).is_none());
    }

    #[test]
    fn id_alone_is_a_valid_assertion() {
        let got = parse_step(&hm(&[(STEP_ID_HEADER, "s-1")])).unwrap();
        assert_eq!(got, ("s-1".to_string(), None));
    }

    #[test]
    fn id_and_name_are_both_carried() {
        let got = parse_step(&hm(&[
            (STEP_ID_HEADER, "s-1"),
            (STEP_NAME_HEADER, "research"),
        ]))
        .unwrap();
        assert_eq!(got, ("s-1".to_string(), Some("research".to_string())));
    }

    #[test]
    fn name_without_id_is_not_an_assertion() {
        // A label with nothing to group by cannot be aggregated, and recording
        // half an assertion as a whole one is the exact failure
        // `turn_attribution` exists to prevent.
        assert!(parse_step(&hm(&[(STEP_NAME_HEADER, "research")])).is_none());
    }

    #[test]
    fn blank_and_oversized_ids_are_rejected() {
        assert!(parse_step(&hm(&[(STEP_ID_HEADER, "   ")])).is_none());
        let long = "x".repeat(MAX_STEP_ID_LEN + 1);
        assert!(parse_step(&hm(&[(STEP_ID_HEADER, &long)])).is_none());
    }

    #[test]
    fn oversized_name_drops_the_name_but_keeps_the_step() {
        // The grouping key is what matters; a too-long label is cosmetic and
        // must not cost us the phase itself.
        let long = "n".repeat(MAX_STEP_NAME_LEN + 1);
        let got = parse_step(&hm(&[(STEP_ID_HEADER, "s-1"), (STEP_NAME_HEADER, &long)])).unwrap();
        assert_eq!(got, ("s-1".to_string(), None));
    }

    #[tokio::test]
    async fn asserted_step_is_visible_within_the_request_scope() {
        STEP_CONTEXT
            .scope(Some(("s-9".into(), Some("settle".into()))), async {
                assert_eq!(
                    asserted_step(),
                    Some(("s-9".to_string(), Some("settle".to_string())))
                );
            })
            .await;
    }

    #[test]
    fn asserted_step_is_none_outside_a_request() {
        // Background sweepers write rows too; they must not inherit a phase.
        assert!(asserted_step().is_none());
    }

    // -- X-Brutor-Outcome-Id --------------------------------------

    const PROXY: &str = "/v1/proxy/llm/chat/completions";

    #[test]
    fn a_well_formed_outcome_is_carried_verbatim() {
        let h = hm(&[("X-Brutor-Outcome-Id", "application_processed.loan_approved")]);
        assert_eq!(
            parse_outcome_id(PROXY, &h).as_deref(),
            Some("application_processed.loan_approved")
        );
        let h = hm(&[("x-brutor-outcome-id", "  nothing_to_do ")]);
        assert_eq!(
            parse_outcome_id(PROXY, &h).as_deref(),
            Some("nothing_to_do")
        );
    }

    #[test]
    fn a_malformed_outcome_is_kept_truncated_and_never_fails_the_request() {
        // Kept, not dropped: it is judged `undeclared` at finalize — dropping
        // it would turn a deviation into `unreported`.
        let h = hm(&[("X-Brutor-Outcome-Id", "Loan Approved!")]);
        assert_eq!(
            parse_outcome_id(PROXY, &h).as_deref(),
            Some("Loan Approved!")
        );
        let long = "x".repeat(400);
        let h = hm(&[("X-Brutor-Outcome-Id", &long)]);
        assert_eq!(
            parse_outcome_id(PROXY, &h).map(|v| v.len()),
            Some(crate::services::run_outcome::MAX_STORED_LEN)
        );
        // Blank is nothing reported.
        assert_eq!(
            parse_outcome_id(PROXY, &hm(&[("X-Brutor-Outcome-Id", "  ")])),
            None
        );
        assert_eq!(parse_outcome_id(PROXY, &hm(&[])), None);
    }

    #[test]
    fn the_a2a_outbound_route_never_takes_the_callers_outcome_header() {
        // That row carries the DELEGATE's reported outcome (set by the route
        // from the reply); the caller's header there would be judged against
        // the delegate's contract.
        let h = hm(&[("X-Brutor-Outcome-Id", "task_completed")]);
        assert_eq!(
            parse_outcome_id(crate::constants::routes::A2A_OUTBOUND, &h),
            None
        );
    }

    #[test]
    fn the_removed_run_outcome_header_is_not_read() {
        // No compatibility: `X-Brutor-Run-Outcome` is gone.
        let h = hm(&[("X-Brutor-Run-Outcome", "resolved")]);
        assert_eq!(parse_outcome_id(PROXY, &h), None);
        const SRC: &str = include_str!("run_end.rs");
        let prod = SRC.split("#[cfg(test)]").next().unwrap();
        assert!(
            !prod.contains("Run-Outcome"),
            "the removed header is read somewhere"
        );
    }

    #[tokio::test]
    async fn the_outcome_is_visible_within_the_request_scope_only() {
        assert_eq!(requested_outcome_id(), None);
        OUTCOME_ID
            .scope(Some("task_completed".into()), async {
                assert_eq!(requested_outcome_id().as_deref(), Some("task_completed"));
            })
            .await;
    }
}

#[cfg(test)]
mod trace_context_tests {
    use super::parse_traceparent;

    #[test]
    fn a_well_formed_traceparent_yields_lowercase_ids() {
        let got = parse_traceparent(" 00-0AF7651916CD43DD8448EB211C80319C-B7AD6B7169203331-01 ");
        assert_eq!(
            got,
            Some((
                "0af7651916cd43dd8448eb211c80319c".to_string(),
                "b7ad6b7169203331".to_string()
            ))
        );
    }

    #[test]
    fn malformed_or_forbidden_values_are_dropped_not_repaired() {
        for bad in [
            "",
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331", // no flags
            "01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", // unknown version
            "00-0af7651916cd43dd8448eb211c80319-b7ad6b7169203331-01", // short trace
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b716920333-01", // short span
            "00-00000000000000000000000000000000-b7ad6b7169203331-01", // zero trace
            "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01", // zero span
            "00-0af7651916cd43dd8448eb211c80319g-b7ad6b7169203331-01", // non-hex
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01-extra",
        ] {
            assert_eq!(parse_traceparent(bad), None, "{bad:?} should be rejected");
        }
    }
}
