// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Request-scoped memo for lookups that are re-derived several times within
//! a single request but cannot change during it.
//!
//! Without the memo the effective-LLM-limits chain is walked three or four
//! times per request (preflight quota, concurrency acquire, rate limits,
//! post-response booking), each walk being a parent-chain climb plus a model
//! lookup. Nothing between those calls can alter the answer, so the repeats
//! bought nothing but round trips.
//!
//! ## Why a task-local and not a TTL cache
//!
//! A short-TTL cache would have been less code, and would have traded
//! exactness for cost: an operator's limit edit would take up to the TTL to
//! take effect, and quota enforcement is the wrong place for "eventually".
//! A request-scoped memo trades nothing — it only collapses lookups that a
//! single request makes microseconds apart, and it pins one request to one
//! consistent snapshot of its limits rather than letting an edit land
//! between the preflight that admitted the call and the booking that charges
//! it.
//!
//! ## What is deliberately NOT covered
//!
//! Task-locals do not propagate into `tokio::spawn`. Work that outlives the
//! handler future — the spawned throughput charge, an SSE body streaming
//! after the response head — finds no memo and queries the database, exactly
//! as it did before. That is correct rather than merely tolerable: those
//! paths can run long after the request, where a fresh read is the right
//! answer.

use axum::extract::Request;
use axum::middleware::Next;
use axum::response::Response;

/// Install an empty memo for the duration of one request.
///
/// Allocation is one `Arc<HashMap>` per request, on a path that already
/// allocates far more than that; the first avoided chain walk pays for it
/// many times over.
pub async fn scope_request_memos(req: Request, next: Next) -> Response {
    crate::services::usage::LLM_LIMITS_MEMO
        .scope(
            std::sync::Arc::new(crate::services::usage::LlmLimitsMemo::default()),
            next.run(req),
        )
        .await
}
