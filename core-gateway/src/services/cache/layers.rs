// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Cache layers of the System Operations console
//!
//! The console names a cache by its *layer*: it flushes
//! one over the `brutor:cache:invalidate` notification and reads each
//! replica's counters over the statistics request. `services::system_notify`
//! answers both. Three layers are the Community Edition's and are written
//! out there (`policy`, `group`, `tool`); any other layer is one somebody
//! registered here.
//!
//! The registry exists so that the listener names no Enterprise cache: the
//! prompt-cache policy layer (`response`) and the response cache
//! (`semantic`) are registered by their extensions when they are installed.
//! A gateway composed without them has neither layer: a flush that names
//! one is an unknown layer (logged, ignored), and the statistics reply does
//! not carry it.
//!
//! Like every registry of the state, it is filled before the state is
//! shared and read-only afterwards.

use std::future::Future;
use std::pin::Pin;

use super::CacheStatsSnapshot;
use crate::services::AppState;

/// The layers `services::system_notify` answers itself. An extension cannot
/// register one of these names.
pub const COMMUNITY_LAYERS: &[&str] = &["policy", "group", "tool"];

/// What a layer's function returns.
pub type LayerFuture<'a> = Pin<Box<dyn Future<Output = ()> + Send + 'a>>;

/// What a flush names, as the notification carried it.
#[derive(Debug, Clone, Copy)]
pub struct LayerFlush<'a> {
    /// `tenant` for the whole tenant; anything else names one entry by `id`.
    pub scope: &'a str,
    pub id: &'a str,
    /// `None` when a cross-tenant operator fired the flush: there is no key
    /// to scope by, so the layer drops everything it can address.
    pub tenant_id: Option<&'a str>,
}

/// Drops what a console flush names.
pub type LayerFlushFn = for<'a> fn(&'a AppState, LayerFlush<'a>) -> LayerFuture<'a>;

/// Drops the whole layer, after the listener (re)subscribes: a notification
/// published while it was not listening is gone.
pub type LayerFlushAllFn = for<'a> fn(&'a AppState) -> LayerFuture<'a>;

/// The layer's counters for the statistics reply.
pub type LayerStatsFn = fn(&AppState) -> CacheStatsSnapshot;

/// One layer.
#[derive(Clone, Copy)]
pub struct CacheLayer {
    /// The name the console uses. Unique.
    pub name: &'static str,
    /// The Enterprise feature the layer belongs to.
    pub feature: &'static str,
    pub flush: LayerFlushFn,
    /// `None` for a layer a missed notification cannot make stale (the
    /// response cache: its entries are not configuration).
    pub flush_after_listen: Option<LayerFlushAllFn>,
    /// `None` for a layer that keeps no in-process counters.
    pub stats: Option<LayerStatsFn>,
}

/// Why a layer was not registered.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LayerNameTaken(pub String);

impl std::fmt::Display for LayerNameTaken {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "cache layer name {:?} is empty, a Community layer, or already registered",
            self.0
        )
    }
}

impl std::error::Error for LayerNameTaken {}

/// The layers registered beyond the Community ones, in registration order.
#[derive(Clone, Default)]
pub struct CacheLayers {
    layers: Vec<CacheLayer>,
}

impl CacheLayers {
    /// No layer beyond the Community ones: what `AppState::new` holds.
    pub fn community() -> Self {
        Self::default()
    }

    /// Add a layer. Refused when the name is empty, is one of
    /// [`COMMUNITY_LAYERS`], or is taken: nothing replaces a layer that is
    /// in place.
    pub fn register(&mut self, layer: CacheLayer) -> Result<(), LayerNameTaken> {
        if layer.name.is_empty()
            || COMMUNITY_LAYERS.contains(&layer.name)
            || self.layers.iter().any(|l| l.name == layer.name)
        {
            return Err(LayerNameTaken(layer.name.to_string()));
        }
        self.layers.push(layer);
        Ok(())
    }

    /// The layer registered under `name`. The match is exact.
    pub fn get(&self, name: &str) -> Option<&CacheLayer> {
        self.layers.iter().find(|l| l.name == name)
    }

    /// Every registered layer, in registration order.
    pub fn layers(&self) -> &[CacheLayer] {
        &self.layers
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn nothing<'a>(_state: &'a AppState, _flush: LayerFlush<'a>) -> LayerFuture<'a> {
        Box::pin(async {})
    }

    fn layer(name: &'static str) -> CacheLayer {
        CacheLayer {
            name,
            feature: "a_feature",
            flush: nothing,
            flush_after_listen: None,
            stats: None,
        }
    }

    #[test]
    fn a_state_starts_with_no_layer_beyond_the_community_ones() {
        let layers = CacheLayers::community();
        assert!(layers.layers().is_empty());
        assert!(layers.get("response").is_none());
        // The Community layers are the listener's own, never entries here.
        for name in COMMUNITY_LAYERS {
            assert!(layers.get(name).is_none());
        }
    }

    #[test]
    fn a_layer_is_found_by_its_exact_name_in_registration_order() {
        let mut layers = CacheLayers::community();
        layers.register(layer("response")).unwrap();
        layers.register(layer("semantic")).unwrap();
        assert_eq!(layers.get("response").map(|l| l.name), Some("response"));
        assert!(layers.get("Response").is_none() && layers.get("response ").is_none());
        let names: Vec<_> = layers.layers().iter().map(|l| l.name).collect();
        assert_eq!(names, ["response", "semantic"]);
    }

    #[test]
    fn a_name_that_is_empty_taken_or_a_community_layer_is_refused() {
        let mut layers = CacheLayers::community();
        layers.register(layer("response")).unwrap();
        for name in ["", "response", "policy", "group", "tool"] {
            assert_eq!(
                layers.register(layer(name)),
                Err(LayerNameTaken(name.to_string())),
                "{name:?}"
            );
        }
        assert_eq!(layers.layers().len(), 1, "a refusal registers nothing");
    }
}
