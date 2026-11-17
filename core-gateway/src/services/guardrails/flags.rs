// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Whether a guardrail flagged the request being served without blocking it.
//!
//! A check that warns or redacts lets the request go on; what it found is
//! recorded here, per request, so a later decision can ask: the response
//! cache's "exclude guardrail-flagged" neither stores nor serves such a
//! request. Every outcome of `guardrails::check_input` / `check_output` that
//! is a warning or a redaction is recorded ([`record`]).
//!
//! The record is a task-local set around the request ([`layer`]) and carried
//! into a task the request spawns ([`carried`]). Asked outside of one
//! ([`raised`]), the answer is "flagged": a request whose record is lost is
//! not one a cache may treat as clean.

use std::future::Future;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

tokio::task_local! {
    static FLAGGED: Arc<AtomicBool>;
}

/// Record that a guardrail flagged the current request. Outside a request's
/// scope there is nothing to record on.
pub fn record() {
    let _ = FLAGGED.try_with(|f| f.store(true, Ordering::Relaxed));
}

/// Has a guardrail flagged the current request? `true` outside a request's
/// scope.
pub fn raised() -> bool {
    FLAGGED
        .try_with(|f| f.load(Ordering::Relaxed))
        .unwrap_or(true)
}

/// Run `fut` as one request: with a record of its own, clean at the start.
pub async fn scope<F: Future>(fut: F) -> F::Output {
    FLAGGED.scope(Arc::new(AtomicBool::new(false)), fut).await
}

/// `fut` with the current request's record, for a task the request spawns
/// (a stream's relay). Outside a request's scope the task gets a record that
/// says "flagged".
pub fn carried<F: Future>(fut: F) -> impl Future<Output = F::Output> {
    let record = FLAGGED
        .try_with(Arc::clone)
        .unwrap_or_else(|_| Arc::new(AtomicBool::new(true)));
    FLAGGED.scope(record, fut)
}

/// The per-request scope, as a route layer.
pub async fn layer(
    req: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    scope(next.run(req)).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_request_starts_clean_and_keeps_what_was_recorded() {
        scope(async {
            assert!(!raised());
            record();
            assert!(raised());
        })
        .await;
        scope(async { assert!(!raised(), "a new request starts clean") }).await;
    }

    #[tokio::test]
    async fn outside_a_request_the_answer_is_flagged() {
        assert!(raised());
        record();
        assert!(raised());
    }

    #[tokio::test]
    async fn a_spawned_task_carries_the_requests_record() {
        scope(async {
            let task = tokio::spawn(carried(async {
                assert!(!raised());
                record();
            }));
            task.await.expect("the task");
            assert!(raised(), "what the task recorded is the request's");
        })
        .await;
        let outside = tokio::spawn(carried(async { raised() }));
        assert!(outside.await.expect("the task"));
    }
}
