// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Cancellation marker middleware
//!
//! Tower layer that exposes a per-request `Arc<AtomicBool>` flag
//! flipped to `true` when the request future is dropped before
//! completion (client disconnected, upstream timed out, axum's
//! `with_graceful_shutdown` triggered).
//!
//! [`crate::services::llm_resilience::DispatchGuard::Drop`] reads
//! the flag via a task-local accessor to distinguish:
//!
//! * **Cancelled** — the response future was dropped, expected
//!   under disconnect / timeout. Emit at INFO + counter
//!   `dispatch_guard_drop_total{reason="cancelled"}`.
//! * **Leaked** — the future completed but the guard wasn't
//!   finalized via `record_success` / `record_failure` /
//!   `abandon`. **Indicates a developer bug.** Emit at WARN +
//!   counter `dispatch_guard_drop_total{reason="leaked"}`.
//!
//! Without this distinction we'd be back to either WARN-spamming
//! every legitimate cancellation or DEBUG-burying every real bug.
//!
//! ### Why a task-local instead of a struct field
//!
//! The `DispatchGuard` is acquired deep inside route handlers,
//! often several function calls down from where the request future
//! is constructed. Threading a `&Arc<AtomicBool>` through every
//! intermediate function is invasive. Task-locals give us scoped
//! access via `CANCELLED.with(|flag| ...)` from anywhere inside
//! the request's tokio task.

use std::pin::Pin;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::task::{Context, Poll};

use axum::body::Body;
use axum::http::Request;
use axum::response::Response;
use futures::future::Future;
use tower::{Layer, Service};

tokio::task_local! {
    /// Task-local pointer to the per-request cancellation flag.
    /// Flipped to `true` if the request future is dropped before
    /// resolving. `None` outside a request scope (e.g. background
    /// tasks) — in that case, callers fall back to "no cancellation
    /// signal available" which is the right default for code that
    /// might run from either context.
    pub static CANCELLED: Arc<AtomicBool>;
}

/// Public read accessor used by [`crate::services::llm_resilience`].
/// Returns `Some(true)` when the request was cancelled, `Some(false)`
/// when the request is still in-flight or completed normally,
/// `None` outside a request scope.
pub fn was_cancelled() -> Option<bool> {
    CANCELLED.try_with(|flag| flag.load(Ordering::Relaxed)).ok()
}

/// Tower layer; zero state — the per-request flag is allocated on
/// each `call`.
#[derive(Clone, Debug, Default)]
pub struct CancellationMarkerLayer;

impl CancellationMarkerLayer {
    pub fn new() -> Self {
        Self
    }
}

impl<S> Layer<S> for CancellationMarkerLayer {
    type Service = CancellationMarkerService<S>;

    fn layer(&self, inner: S) -> Self::Service {
        CancellationMarkerService { inner }
    }
}

#[derive(Clone, Debug)]
pub struct CancellationMarkerService<S> {
    inner: S,
}

impl<S> Service<Request<Body>> for CancellationMarkerService<S>
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
        let flag = Arc::new(AtomicBool::new(false));
        let drop_guard = DropGuard {
            flag: flag.clone(),
            armed: true,
        };

        let mut inner = self.inner.clone();
        let scoped = async move {
            // The drop_guard lives until this future is dropped or
            // returns. Successful completion calls disarm() so the
            // Drop impl doesn't flip the flag. Cancellation
            // (future dropped pre-completion) leaves armed=true and
            // the Drop impl flips the flag, which the
            // `DispatchGuard::Drop` reads via `was_cancelled()`.
            let response = inner.call(req).await;
            disarm(drop_guard);
            response
        };

        // Scope the task-local for the entire response future.
        Box::pin(CANCELLED.scope(flag, scoped))
    }
}

/// Local helper — `DropGuard::disarm` consumes self so we can
/// distinguish disarm from drop-on-cancel. Wrapped in a function
/// for clarity at the call site.
fn disarm(mut g: DropGuard) {
    g.armed = false;
    let _ = g; // explicit drop happens at end of scope
}

/// Internal: arms a drop hook that flips the cancellation flag if
/// the future is cancelled (i.e. `Drop` runs without a prior
/// `disarm()`). Trivial Drop impl — no allocation, no log, just
/// the atomic flag.
struct DropGuard {
    flag: Arc<AtomicBool>,
    armed: bool,
}

impl Drop for DropGuard {
    fn drop(&mut self) {
        if self.armed {
            self.flag.store(true, Ordering::Relaxed);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures::future::FutureExt;
    use std::time::Duration;

    #[tokio::test]
    async fn flag_stays_false_on_normal_completion() {
        let flag = Arc::new(AtomicBool::new(false));
        let probe = flag.clone();
        // Mirror the layer's scoping logic.
        let drop_guard = DropGuard {
            flag: flag.clone(),
            armed: true,
        };
        let fut = async move {
            disarm(drop_guard);
            42
        };
        let result = CANCELLED.scope(flag, fut).await;
        assert_eq!(result, 42);
        assert!(!probe.load(Ordering::Relaxed));
    }

    #[tokio::test]
    async fn flag_flips_true_on_cancellation() {
        let flag = Arc::new(AtomicBool::new(false));
        let probe = flag.clone();
        let drop_guard = DropGuard {
            flag: flag.clone(),
            armed: true,
        };
        let fut = async move {
            // Take ownership so it stays alive across the await.
            let _holder = drop_guard;
            tokio::time::sleep(Duration::from_secs(60)).await;
            42
        };
        // Race the future against an immediate timeout — when the
        // timeout wins, the future is dropped (cancelled).
        let scoped = CANCELLED.scope(flag, fut);
        let _ = tokio::time::timeout(Duration::from_millis(10), scoped).await;
        assert!(
            probe.load(Ordering::Relaxed),
            "cancellation flag must flip true on future drop"
        );
    }

    #[tokio::test]
    async fn was_cancelled_returns_none_outside_scope() {
        // Outside any CANCELLED::scope, the accessor returns None.
        // Important for code that may run in both request scope
        // (e.g. via task-local) and background tasks (no scope).
        let r = was_cancelled();
        assert_eq!(r, None);
    }

    #[tokio::test]
    async fn was_cancelled_returns_some_inside_scope() {
        let flag = Arc::new(AtomicBool::new(false));
        let fut = async move {
            // In the typical request flow, the inner code reads the
            // current state — false because no cancellation has
            // happened yet.
            was_cancelled()
        };
        let r = CANCELLED.scope(flag, fut).await;
        assert_eq!(r, Some(false));
    }

    #[tokio::test]
    async fn future_not_polled_to_completion_still_flips_flag() {
        // Construct the future, drop it without awaiting. Drop must
        // still flip the flag (covers a class of cancellations
        // where the future is constructed but never polled).
        let flag = Arc::new(AtomicBool::new(false));
        let probe = flag.clone();
        let drop_guard = DropGuard {
            flag: flag.clone(),
            armed: true,
        };
        let fut = async move {
            let _holder = drop_guard;
            tokio::time::sleep(Duration::from_secs(60)).await;
        };
        let scoped = CANCELLED.scope(flag, fut);
        // Drop without awaiting — equivalent to client
        // disconnect before any progress.
        std::mem::drop(scoped);
        // The future was dropped, the holder was dropped, the flag
        // should be true.
        // FuturesExt::now_or_never() to confirm we don't actually
        // need to poll anything — the assertion holds by virtue of
        // Drop running.
        assert!(probe.load(Ordering::Relaxed));
        let _ = futures::future::ready(()).now_or_never();
    }
}
