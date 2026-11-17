// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Limits of the installation as a whole, above every tenant's own.
//!
//! A tenant's limits are rows of the tenant (`tenants.media_storage_quota_bytes`).
//! An installation may have ceilings no tenant's row can exceed. They are a
//! named field of the state (`AppState.installation_limits`), empty as
//! `AppState::new` leaves it; an extension that knows the installation's
//! ceilings sets them while the state is composed, and the code that serves
//! a limit takes the lower of the two. A gateway with no such extension has
//! none: the Community Edition.

use crate::services::AppState;

/// The ceilings of this installation. `None` is no ceiling.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct InstallationLimits {
    /// The most bytes one tenant's media store may hold, whatever the
    /// tenant's own quota says.
    pub media_storage_bytes_per_tenant: Option<u64>,
}

/// The media quota that holds for a tenant whose own quota is
/// `tenant_quota` (`None`: unlimited): the lower of it and the
/// installation's ceiling.
pub fn media_quota(state: &AppState, tenant_quota: Option<i64>) -> Option<i64> {
    let ceiling = state
        .installation_limits
        .media_storage_bytes_per_tenant
        .map(|bytes| i64::try_from(bytes).unwrap_or(i64::MAX));
    match (tenant_quota, ceiling) {
        (Some(quota), Some(ceiling)) => Some(quota.min(ceiling)),
        (quota, ceiling) => quota.or(ceiling),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state(limits: Option<InstallationLimits>) -> AppState {
        let mut state = AppState::new(
            crate::config::AppConfig::for_test("postgresql://placeholder/unused".to_string()),
            sqlx::postgres::PgPoolOptions::new()
                .connect_lazy("postgresql://placeholder:placeholder@127.0.0.1:1/none")
                .expect("a lazy pool"),
            None,
            reqwest::Client::new(),
            crate::services::telemetry::TelemetryState {
                prometheus_handle: None,
            },
        );
        if let Some(limits) = limits {
            state.installation_limits = limits;
        }
        state
    }

    #[tokio::test]
    async fn the_lower_of_the_tenants_quota_and_the_installations_ceiling_holds() {
        // No extension installed a ceiling: the tenant's quota, as it was.
        let none = state(None);
        assert_eq!(media_quota(&none, None), None);
        assert_eq!(media_quota(&none, Some(10)), Some(10));
        // A value with no ceiling is no ceiling.
        let empty = state(Some(InstallationLimits::default()));
        assert_eq!(media_quota(&empty, None), None);
        assert_eq!(media_quota(&empty, Some(10)), Some(10));
        let capped = state(Some(InstallationLimits {
            media_storage_bytes_per_tenant: Some(100),
        }));
        assert_eq!(media_quota(&capped, None), Some(100));
        assert_eq!(media_quota(&capped, Some(10)), Some(10));
        assert_eq!(media_quota(&capped, Some(1_000)), Some(100));
        // A ceiling beyond what a quota can count is the largest quota.
        let huge = state(Some(InstallationLimits {
            media_storage_bytes_per_tenant: Some(u64::MAX),
        }));
        assert_eq!(media_quota(&huge, None), Some(i64::MAX));
    }
}
