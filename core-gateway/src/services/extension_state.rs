// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! State that an extension owns.
//!
//! `AppState` holds the state of the built-in gateway as named fields. An
//! extension (a second distribution composed onto this one) has state of
//! its own that the built-in gateway must not know by name, so `AppState`
//! carries one `ExtensionState`: a map from a Rust type to one value of that
//! type. An extension defines a type, inserts one value while the state is
//! being built, and reads it back by type from any handler or task.
//!
//! The map is filled before `AppState` is shared and read afterwards; there
//! is no interior mutability, so a value is as immutable as any other field
//! of `AppState` unless the extension's own type says otherwise (an
//! `Arc<RwLock<_>>` inside it, for example).
//!
//! Nothing built in lives here: the source-scan guard in
//! `tests/extension_state_guard.rs` asserts that no code under `src/`
//! inserts into the map: the builder inserts on an
//! extension's behalf.

use std::any::{Any, TypeId, type_name};
use std::collections::HashMap;
use std::fmt;

/// One value per type, inserted while the state is built.
#[derive(Default)]
pub struct ExtensionState {
    values: HashMap<TypeId, Box<dyn Any + Send + Sync>>,
    /// The type names, in insertion order, for diagnostics and the System console.
    names: Vec<&'static str>,
}

/// Why an insertion was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExtensionStateError {
    /// A value of this type is already present. One extension's state is one
    /// value; a second insertion is two owners or a bug, so it is refused
    /// rather than replacing what the first inserted.
    AlreadyPresent { type_name: &'static str },
}

impl fmt::Display for ExtensionStateError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::AlreadyPresent { type_name } => {
                write!(
                    f,
                    "extension state already holds a value of type {type_name}"
                )
            }
        }
    }
}

impl std::error::Error for ExtensionStateError {}

impl ExtensionState {
    pub fn new() -> Self {
        Self::default()
    }

    /// Insert the one value of type `T`. Refused when one is present.
    pub fn insert<T: Any + Send + Sync>(&mut self, value: T) -> Result<(), ExtensionStateError> {
        let id = TypeId::of::<T>();
        if self.values.contains_key(&id) {
            return Err(ExtensionStateError::AlreadyPresent {
                type_name: type_name::<T>(),
            });
        }
        self.values.insert(id, Box::new(value));
        self.names.push(type_name::<T>());
        Ok(())
    }

    /// The value of type `T`, if an extension inserted one.
    pub fn get<T: Any + Send + Sync>(&self) -> Option<&T> {
        self.values
            .get(&TypeId::of::<T>())
            .and_then(|boxed| boxed.downcast_ref::<T>())
    }

    /// Whether a value of type `T` is present.
    pub fn contains<T: Any + Send + Sync>(&self) -> bool {
        self.values.contains_key(&TypeId::of::<T>())
    }

    /// The type names of the values present, in insertion order.
    pub fn type_names(&self) -> &[&'static str] {
        &self.names
    }

    pub fn len(&self) -> usize {
        self.values.len()
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }
}

impl fmt::Debug for ExtensionState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ExtensionState")
            .field("types", &self.names)
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Reports {
        retention_days: u32,
    }
    struct Billing(String);

    #[test]
    fn a_value_is_read_back_by_its_type_and_an_absent_type_is_none() {
        let mut state = ExtensionState::new();
        assert!(state.is_empty());
        assert_eq!(state.get::<Reports>().map(|r| r.retention_days), None);

        state.insert(Reports { retention_days: 30 }).unwrap();
        state.insert(Billing("eur".into())).unwrap();

        assert_eq!(state.len(), 2);
        assert_eq!(state.get::<Reports>().unwrap().retention_days, 30);
        assert_eq!(state.get::<Billing>().unwrap().0, "eur");
        assert!(state.contains::<Reports>());
        assert!(!state.contains::<u64>());
        assert_eq!(state.get::<u64>(), None);
    }

    #[test]
    fn a_second_value_of_the_same_type_is_refused_and_the_first_kept() {
        let mut state = ExtensionState::new();
        state.insert(Reports { retention_days: 30 }).unwrap();
        let refused = state.insert(Reports { retention_days: 7 });
        assert_eq!(
            refused,
            Err(ExtensionStateError::AlreadyPresent {
                type_name: type_name::<Reports>(),
            })
        );
        assert_eq!(state.get::<Reports>().unwrap().retention_days, 30);
        assert_eq!(state.len(), 1);
        assert!(refused.unwrap_err().to_string().contains("Reports"));
    }

    #[test]
    fn two_types_with_the_same_shape_are_two_values() {
        struct A(u8);
        struct B(u8);
        let mut state = ExtensionState::new();
        state.insert(A(1)).unwrap();
        state.insert(B(2)).unwrap();
        assert_eq!(state.get::<A>().unwrap().0, 1);
        assert_eq!(state.get::<B>().unwrap().0, 2);
    }

    #[test]
    fn type_names_follow_insertion_order_and_debug_shows_them() {
        let mut state = ExtensionState::new();
        state.insert(Billing("x".into())).unwrap();
        state.insert(Reports { retention_days: 1 }).unwrap();
        assert_eq!(
            state.type_names(),
            &[type_name::<Billing>(), type_name::<Reports>()]
        );
        let shown = format!("{state:?}");
        assert!(
            shown.contains("Billing") && shown.contains("Reports"),
            "{shown}"
        );
    }

    #[tokio::test]
    async fn a_new_app_state_has_an_empty_map_that_is_filled_before_it_is_shared() {
        use crate::config::AppConfig;
        use crate::services::AppState;
        use crate::services::telemetry::TelemetryState;

        let pool = sqlx::postgres::PgPoolOptions::new()
            .connect_lazy("postgres://test:test@127.0.0.1:1/never_connected")
            .expect("connect_lazy never fails on a syntactically valid URL");
        let mut state = AppState::new(
            AppConfig::for_test("postgres://test:test@127.0.0.1:1/never_connected".to_string()),
            pool,
            None,
            reqwest::Client::new(),
            TelemetryState {
                prometheus_handle: None,
            },
        );
        assert!(
            state.extensions.is_empty(),
            "nothing built in lives in the map"
        );

        state
            .extensions
            .insert(Reports { retention_days: 90 })
            .unwrap();
        let shared: std::sync::Arc<AppState> = std::sync::Arc::new(state);
        assert_eq!(
            shared.extensions.get::<Reports>().unwrap().retention_days,
            90
        );
        assert_eq!(shared.extensions.type_names().len(), 1);
    }

    #[test]
    fn the_state_is_send_and_sync_so_app_state_stays_shareable() {
        fn assert_send_sync<T: Send + Sync>() {}
        assert_send_sync::<ExtensionState>();
    }
}
