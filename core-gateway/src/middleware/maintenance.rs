// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//
// Rust-side maintenance gate, the counterpart of the control plane's.
//
// Reads `tenants.maintenance_mode` for the request's tenant via a
// tiny per-tenant TTL cache (1s). The brutor:maintenance NOTIFY
// consumer (wired in main.rs) drops the cached row so a toggle on
// the control plane propagates within milliseconds.
//
// Modes mirror the Python contract:
//   * off       — passthrough
//   * read_only — 503 on POST/PATCH/PUT/DELETE
//   * drain     — same as read_only PLUS Connection: close header
//   * paused    — 503 on every request
//
// Always-allowed prefixes: /health, /healthz, /v1/admin/system/maintenance.
//
// Installed in `main.rs` INSIDE the TenantLayer, because the gate reads
// `TenantContext` out of the request extensions and TenantLayer is what
// puts it there. A request with no tenant context (health probes, the
// OAuth proxy — every path whose rule in `services::tenant_paths` is open) passes
// through ungated, which is deliberate: those paths are how an operator
// reaches a paused deployment.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::http::{HeaderValue, Method, Request, StatusCode};
use axum::response::Response;
use futures::future::BoxFuture;
use tower::{Layer, Service};

use crate::services::SharedState;

const CACHE_TTL: Duration = Duration::from_secs(1);

#[derive(Clone, Default)]
struct ModeEntry {
    mode: String,
    drain_grace_secs: Option<i32>,
    expires_at: Option<Instant>,
}

#[derive(Clone)]
pub struct MaintenanceCache {
    inner: Arc<RwLock<HashMap<String, ModeEntry>>>,
}

impl MaintenanceCache {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(RwLock::new(HashMap::new())),
        }
    }
    pub fn invalidate(&self, tenant_id: Option<&str>) {
        // PoisonError only happens if a prior writer panicked while
        // holding the lock — at that point cache state is suspect anyway,
        // so we treat the poison as "lock is gone, drop the operation"
        // rather than propagating the panic.
        let Ok(mut guard) = self.inner.write() else {
            return;
        };
        match tenant_id {
            Some(t) => {
                guard.remove(t);
            }
            None => guard.clear(),
        }
    }
    pub fn get(&self, tenant_id: &str) -> Option<(String, Option<i32>)> {
        let guard = self.inner.read().ok()?;
        guard.get(tenant_id).and_then(|e| {
            if let Some(exp) = e.expires_at {
                if exp > Instant::now() {
                    return Some((e.mode.clone(), e.drain_grace_secs));
                }
            }
            None
        })
    }
    pub fn put(&self, tenant_id: &str, mode: String, drain: Option<i32>) {
        let Ok(mut guard) = self.inner.write() else {
            return;
        };
        guard.insert(
            tenant_id.to_string(),
            ModeEntry {
                mode,
                drain_grace_secs: drain,
                expires_at: Some(Instant::now() + CACHE_TTL),
            },
        );
    }
}

#[derive(Clone)]
pub struct MaintenanceLayer {
    pub state: SharedState,
    pub cache: MaintenanceCache,
}

impl MaintenanceLayer {
    pub fn new(state: SharedState, cache: MaintenanceCache) -> Self {
        Self { state, cache }
    }
}

impl<S> Layer<S> for MaintenanceLayer {
    type Service = MaintenanceMiddleware<S>;
    fn layer(&self, inner: S) -> Self::Service {
        MaintenanceMiddleware {
            inner,
            state: self.state.clone(),
            cache: self.cache.clone(),
        }
    }
}

#[derive(Clone)]
pub struct MaintenanceMiddleware<S> {
    inner: S,
    state: SharedState,
    cache: MaintenanceCache,
}

fn is_always_allowed(path: &str) -> bool {
    path == "/health" || path == "/healthz" || path.starts_with("/v1/admin/system/maintenance")
}

fn is_write(method: &Method) -> bool {
    matches!(
        *method,
        Method::POST | Method::PATCH | Method::PUT | Method::DELETE
    )
}

fn block_response(mode: &str, drain_grace_secs: Option<i32>, attach_close: bool) -> Response {
    let body = format!(
        r#"{{"detail":"Tenant maintenance mode '{}' rejects this request","maintenance_mode":"{}"}}"#,
        mode, mode,
    );
    let mut resp = Response::builder()
        .status(StatusCode::SERVICE_UNAVAILABLE)
        .header("content-type", "application/json")
        .header("x-brutor-maintenance-mode", mode);
    if attach_close {
        resp = resp.header("connection", "close");
    }
    if let Some(secs) = drain_grace_secs {
        if secs > 0 {
            resp = resp.header("retry-after", HeaderValue::from(secs));
        }
    }
    resp.body(Body::from(body)).unwrap()
}

impl<S> Service<Request<Body>> for MaintenanceMiddleware<S>
where
    S: Service<Request<Body>, Response = Response> + Clone + Send + 'static,
    S::Future: Send + 'static,
{
    type Response = Response;
    type Error = S::Error;
    type Future = BoxFuture<'static, Result<Self::Response, Self::Error>>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, req: Request<Body>) -> Self::Future {
        let mut inner = self.inner.clone();
        let state = self.state.clone();
        let cache = self.cache.clone();
        Box::pin(async move {
            let path = req.uri().path();
            if is_always_allowed(path) {
                return inner.call(req).await;
            }
            // Tenant context must already be attached by TenantLayer.
            let tenant_id = req
                .extensions()
                .get::<crate::middleware::tenant::TenantContext>()
                .map(|c| c.tenant_id.clone());
            let Some(tid) = tenant_id else {
                return inner.call(req).await;
            };

            // Cache miss -> DB read. Failing open on error: a DB blip
            // shouldn't take the whole tenant offline.
            let (mode, drain_grace_secs) = if let Some(v) = cache.get(&tid) {
                v
            } else {
                match sqlx::query_as::<_, (String, Option<i32>)>(
                    "SELECT maintenance_mode, drain_grace_secs FROM tenants WHERE id = $1",
                )
                .bind(&tid)
                .fetch_optional(&state.db)
                .await
                {
                    Ok(Some(row)) => {
                        cache.put(&tid, row.0.clone(), row.1);
                        (row.0, row.1)
                    }
                    Ok(None) => ("off".to_string(), None),
                    Err(e) => {
                        tracing::warn!(error=%e, tenant_id=%tid, "maintenance lookup failed, failing open");
                        ("off".to_string(), None)
                    }
                }
            };

            match mode.as_str() {
                "off" => inner.call(req).await,
                "paused" => Ok(block_response("paused", None, false)),
                "read_only" if is_write(req.method()) => {
                    Ok(block_response("read_only", None, false))
                }
                "drain" if is_write(req.method()) => {
                    Ok(block_response("drain", drain_grace_secs, true))
                }
                "drain" => {
                    // Read passes through; tag Connection: close.
                    let mut resp = inner.call(req).await?;
                    resp.headers_mut()
                        .insert("connection", HeaderValue::from_static("close"));
                    Ok(resp)
                }
                _ => inner.call(req).await,
            }
        })
    }
}
