// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Log output: `RUST_LOG` filters it; `BRUTOR_LOG_JSON_ENABLED` chooses one
//! JSON object per line (for a log pipeline such as Loki or OTel, which can
//! correlate lines by `execution_id`) or human-readable text. Both carry the
//! same fields: time, level, target, the current span and the event's fields.

use tracing::Subscriber;
use tracing_subscriber::fmt::{self, MakeWriter};
use tracing_subscriber::prelude::*;
use tracing_subscriber::registry::LookupSpan;
use tracing_subscriber::{EnvFilter, Layer};

use crate::env_registry::Env;
use crate::env_vars;

/// Whether log lines are JSON.
pub fn json_enabled(env: &Env) -> bool {
    env.flag(&env_vars::BRUTOR_LOG_JSON_ENABLED)
}

/// The formatting layer writing to `writer`: JSON lines when `json`, text
/// otherwise.
pub fn format_layer<S, W>(json: bool, writer: W) -> Box<dyn Layer<S> + Send + Sync>
where
    S: Subscriber + for<'a> LookupSpan<'a>,
    W: for<'w> MakeWriter<'w> + Send + Sync + 'static,
{
    let layer = fmt::layer().with_writer(writer).with_target(true);
    if json {
        layer.json().with_current_span(true).boxed()
    } else {
        layer.boxed()
    }
}

/// Install the process-wide subscriber, writing to standard output.
pub fn init(env: &Env) {
    // `RUST_LOG`, or its default from the registry. A filter that does not
    // parse falls back to the default rather than stopping the runner.
    let default = env_vars::RUST_LOG.default.unwrap_or("info");
    let filter = env
        .string(&env_vars::RUST_LOG)
        .and_then(|directives| EnvFilter::try_new(directives).ok())
        .unwrap_or_else(|| EnvFilter::new(default));
    tracing_subscriber::registry()
        .with(filter)
        .with(format_layer(json_enabled(env), std::io::stdout))
        .init();
}
