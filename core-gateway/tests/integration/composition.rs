// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The composition this suite runs against.
//!
//! The Community gateway composes no extension, so the list is empty here.
//! The Enterprise workspace re-runs this suite with a module of the same
//! name that returns its distribution's extensions.

use brutor_gateway_core::builder::Extension;

/// The extensions of the edition running the suite, in composition order.
pub fn extensions() -> Vec<Box<dyn Extension>> {
    Vec::new()
}

/// What a tenant whose trial is over is told where the edition running the
/// suite gates trials. The trial and its message are the Enterprise
/// Edition's: the Community gateway has none, and the Community tree does
/// not carry the message.
pub fn trial_expired_message() -> Option<&'static str> {
    None
}
