// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Tenant two-factor policy
//!
//! `tenants.mfa_policy` is written by the control plane (the Admin Console's
//! selector). This plane enforces the part that concerns Portal end users:
//! under `required_all`, a password alone never yields a session.
//!
//! The reading rule is shared with the control plane and must stay identical
//! on both sides: no value means `optional`; a value nobody recognises means
//! the most restrictive policy, never the least.

use sqlx::PgPool;

/// What a tenant requires of its users' second factor.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MfaPolicy {
    /// Each user decides.
    Optional,
    /// Administrators must enrol; end users decide. Enforced by the control
    /// plane; for the Portal it reads as `Optional`.
    RequiredAdmins,
    /// Everyone must enrol, end users included.
    RequiredAll,
}

impl MfaPolicy {
    /// Read a stored `tenants.mfa_policy` value.
    pub fn parse(stored: Option<&str>) -> Self {
        match stored.map(str::trim) {
            None | Some("") | Some("optional") => Self::Optional,
            Some("required_admins") => Self::RequiredAdmins,
            // `required_all`, and anything unrecognised: an unknown policy is
            // the most restrictive one.
            Some(_) => Self::RequiredAll,
        }
    }

    /// True when a Portal end user may not hold a session without 2FA.
    pub fn requires_end_user_2fa(self) -> bool {
        self == Self::RequiredAll
    }

    /// The tenant's policy, read from the database now.
    ///
    /// One primary-key read, at sign-in and at "disable 2FA" only: neither is
    /// on the proxy path, and reading it there (rather than from the 5-second
    /// tenant-status cache) means a tightened policy applies to the very next
    /// sign-in. An error is returned, never turned into a policy: the caller
    /// refuses. A tenant row that is not there is the most restrictive policy.
    pub async fn for_tenant(db: &PgPool, tenant_id: &str) -> Result<Self, sqlx::Error> {
        let stored: Option<Option<String>> =
            sqlx::query_scalar("SELECT mfa_policy FROM tenants WHERE id = $1")
                .bind(tenant_id)
                .fetch_optional(db)
                .await?;
        Ok(match stored {
            Some(value) => Self::parse(value.as_deref()),
            None => Self::RequiredAll,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::MfaPolicy;

    #[test]
    fn the_three_stored_values_read_as_themselves() {
        assert_eq!(MfaPolicy::parse(Some("optional")), MfaPolicy::Optional);
        assert_eq!(
            MfaPolicy::parse(Some("required_admins")),
            MfaPolicy::RequiredAdmins
        );
        assert_eq!(
            MfaPolicy::parse(Some("required_all")),
            MfaPolicy::RequiredAll
        );
    }

    #[test]
    fn no_value_is_optional() {
        for stored in [None, Some(""), Some("   ")] {
            assert_eq!(MfaPolicy::parse(stored), MfaPolicy::Optional, "{stored:?}");
        }
    }

    #[test]
    fn an_unrecognised_value_is_the_most_restrictive_policy() {
        for stored in ["required", "REQUIRED_ALL", "Optional", "none", "off", "0"] {
            assert_eq!(
                MfaPolicy::parse(Some(stored)),
                MfaPolicy::RequiredAll,
                "{stored}"
            );
        }
    }

    #[test]
    fn only_required_all_binds_portal_end_users() {
        assert!(MfaPolicy::RequiredAll.requires_end_user_2fa());
        assert!(!MfaPolicy::RequiredAdmins.requires_end_user_2fa());
        assert!(!MfaPolicy::Optional.requires_end_user_2fa());
    }
}
