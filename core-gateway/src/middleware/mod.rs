// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

pub mod cancellation_marker;
/// Cross-origin policy: protocol routes open, the rest to configured origins.
pub mod cors;
// Maintenance gate. Reads the tenant's
// maintenance_mode field and 503s requests when read_only/drain/paused.
pub mod maintenance;
// Scopes per-request memos for lookups that several stages
// of one request re-derive but nothing between them can change.
pub mod request_memo;
// Scopes `X-Brutor-Run-End` so the step's proxy_logs row can
// carry the client's explicit run-finalization signal.
pub mod run_end;
pub mod slow_request;
pub mod tenant;
