// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # The tenant gate hook
//!
//! The tenant layer (`middleware::tenant`) blocks a suspended tenant on the
//! paths whose rule says so. That is the whole of what the Community Edition
//! knows about a tenant's standing: it reads `tenants.suspended` and
//! `tenants.suspension_reason` and nothing else.
//!
//! An installation may have further reasons to refuse a tenant's runtime
//! requests that are not suspension. The Enterprise Edition has one: a trial
//! that is over. Such a reason is a [`TenantGate`], installed by the piece
//! that owns it (`AppState::tenant_gate`); the layer asks it, after its own
//! suspension check, wherever it would block a suspended tenant, and the A2A
//! protocol routes ask it where they check suspension themselves.
//!
//! The default is [`NoTenantGate`] (the Community Edition): it gates nothing,
//! the layer does not even ask it, and nothing but suspension blocks a
//! tenant.

use async_trait::async_trait;
use sqlx::PgPool;

/// Why a gate refuses a tenant's runtime requests: the wire error code and
/// the message the caller is given.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TenantBlock {
    pub code: &'static str,
    pub message: String,
}

/// The gate could not decide (its lookup failed). The request is refused as
/// one whose tenant status could not be read (503): never served.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TenantGateUnavailable;

/// A reason, other than suspension, to refuse a tenant's runtime requests.
#[async_trait]
pub trait TenantGate: Send + Sync {
    /// Whether this gate ever refuses anything. `false` lets the tenant
    /// layer skip the question on every request.
    fn gates(&self) -> bool {
        true
    }

    /// `Ok(None)`: nothing against the tenant. `Ok(Some(block))`: refused,
    /// with the block's code and message.
    async fn check(
        &self,
        db: &PgPool,
        tenant_id: &str,
    ) -> Result<Option<TenantBlock>, TenantGateUnavailable>;
}

/// No tenant gate: the Community default. Nothing but suspension blocks a
/// tenant.
pub struct NoTenantGate;

#[async_trait]
impl TenantGate for NoTenantGate {
    fn gates(&self) -> bool {
        false
    }

    async fn check(
        &self,
        _db: &PgPool,
        _tenant_id: &str,
    ) -> Result<Option<TenantBlock>, TenantGateUnavailable> {
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn the_default_gates_nothing() {
        let gate = NoTenantGate;
        assert!(!gate.gates());
        let db = sqlx::postgres::PgPoolOptions::new()
            .connect_lazy("postgresql://nobody@127.0.0.1:1/nothing")
            .expect("a lazy pool");
        // It answers without touching the database.
        assert_eq!(gate.check(&db, "any-tenant").await, Ok(None));
    }
}
