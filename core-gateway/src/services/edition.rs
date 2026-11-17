// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Which edition this process is
//!
//! There is no build flag. A process is the **Enterprise Edition** when its
//! composition holds anything that belongs to an Enterprise feature, and the
//! **Community Edition** when it holds nothing of the kind. A state as
//! `AppState::new` leaves it is a Community composition; every Enterprise
//! piece says so when it is installed ([`Composition::enterprise_piece`]),
//! and `tests/edition_start_guard.rs` fails when one installs itself without
//! saying so.
//!
//! The one consumer today is the Enterprise-data check at start
//! (`services::edition_guard`).

use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Edition {
    Community,
    Enterprise,
}

impl Edition {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Community => "community",
            Self::Enterprise => "enterprise",
        }
    }
}

/// The Enterprise features this process is composed of, by the feature names
/// of `editions.yaml` (`enterprise_features`). Empty in a Community Edition.
#[derive(Clone, Debug, Default)]
pub struct Composition {
    enterprise: BTreeSet<&'static str>,
    /// What the composition is entitled to, when one of its extensions
    /// grants entitlements (`Extension::entitlements`); `None` when none
    /// does.
    entitlements: Option<BTreeMap<String, bool>>,
    /// The extensions left out because the composition is not entitled to
    /// them: `(entitlement, extension)`.
    withheld: Vec<(&'static str, &'static str)>,
}

impl Composition {
    /// Nothing Enterprise: what `AppState::new` holds.
    pub fn community() -> Self {
        Self::default()
    }

    /// An Enterprise piece of `feature` was installed on this state. Called
    /// by the piece's own `install`, so that the edition is what is
    /// composed, not what somebody declared.
    pub fn enterprise_piece(&mut self, feature: &'static str) {
        self.enterprise.insert(feature);
    }

    /// The Enterprise features installed, sorted.
    pub fn enterprise_features(&self) -> Vec<&'static str> {
        self.enterprise.iter().copied().collect()
    }

    /// The entitlements the composition's grantor states, by name.
    pub fn set_entitlements(&mut self, entitlements: BTreeMap<String, bool>) {
        self.entitlements = Some(entitlements);
    }

    /// The entitlements of the composition; `None` when no extension grants
    /// any (the Community Edition).
    pub fn entitlements(&self) -> Option<&BTreeMap<String, bool>> {
        self.entitlements.as_ref()
    }

    /// `extension` was left out: the composition is not entitled to
    /// `entitlement`.
    pub fn withhold(&mut self, entitlement: &'static str, extension: &'static str) {
        if !self.withheld.contains(&(entitlement, extension)) {
            self.withheld.push((entitlement, extension));
        }
    }

    /// The extensions left out, as `(entitlement, extension)`.
    pub fn withheld(&self) -> &[(&'static str, &'static str)] {
        &self.withheld
    }

    pub fn edition(&self) -> Edition {
        if self.enterprise.is_empty() {
            Edition::Community
        } else {
            Edition::Enterprise
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nothing_installed_is_the_community_edition() {
        let composition = Composition::community();
        assert_eq!(composition.edition(), Edition::Community);
        assert!(composition.enterprise_features().is_empty());
    }

    #[test]
    fn one_enterprise_piece_is_the_enterprise_edition() {
        let mut composition = Composition::community();
        composition.enterprise_piece("portal");
        composition.enterprise_piece("portal");
        composition.enterprise_piece("knowledge_base");
        assert_eq!(composition.edition(), Edition::Enterprise);
        assert_eq!(
            composition.enterprise_features(),
            vec!["knowledge_base", "portal"]
        );
    }
}
