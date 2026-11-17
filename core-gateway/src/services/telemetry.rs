// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! # Telemetry Initialization
//!
//! For internal database logging see background_write.rs
//!
//! Configures the three pillars of observability for the proxy:
//!
//! ### 1. Logging (always on)
//! Human-readable lines, or JSON lines with `BRUTOR_LOG_JSON_ENABLED=true`
//! (for a log collector), via `tracing-subscriber`, filtered by `RUST_LOG`.
//!
//! ### 2. Distributed Tracing (opt-in via `OTEL_EXPORTER_OTLP_ENDPOINT`)
//! Exports spans to any OpenTelemetry-compatible collector using OTLP/gRPC.
//! Works with Datadog, New Relic, Grafana Tempo, Jaeger, and Honeycomb.
//! Propagates W3C `traceparent` headers for cross-service correlation.
//!
//! ### 3. Prometheus Metrics (opt-in via `BRUTOR_METRICS_ENABLED=true`)
//! Exposes a `/metrics` endpoint for Prometheus scraping. Recorded metrics:
//!
//! | Metric | Type | Description |
//! |--------|------|-------------|
//! | `proxy_requests_total` | Counter | Total requests by proxy_type, method, status |
//! | `proxy_request_duration_seconds` | Histogram | Latency distribution |
//! | `proxy_llm_tokens_total` | Counter | Token usage by direction (prompt/completion) |
//! | `proxy_llm_estimated_cost_microdollars` | Counter | Estimated cost in microdollars (÷1M for USD) |
//! | `proxy_active_streams` | Gauge | Currently open SSE streams |
//! | `proxy_guardrail_blocks_total` | Counter | Requests blocked by guardrails |
//!
//! ### Environment Variables
//!
//! | Variable | Default | Description |
//! |----------|---------|-------------|
//! | `OTEL_EXPORTER_OTLP_ENDPOINT` | _(disabled)_ | OTLP collector URL (e.g. `http://localhost:4317`) |
//! | `OTEL_SERVICE_NAME` | `brutor-gateway-core` | Service name in traces |
//! | `BRUTOR_METRICS_ENABLED` | `false` | Enable Prometheus `/metrics` endpoint |
//! | `BRUTOR_METRICS_LISTEN_ADDR` | `0.0.0.0:9464` | Its own listener; `/metrics` is never on the proxy port |
//! | `BRUTOR_LOG_JSON_ENABLED` | `false` | JSON lines instead of human-readable ones |
//! | `RUST_LOG` | `brutor_gateway_core=info` | Log level filter |

use metrics_exporter_prometheus::{PrometheusBuilder, PrometheusHandle};
use tracing_subscriber::EnvFilter;

use crate::env_registry::Env;
use crate::env_vars;
use tracing_subscriber::Layer;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

/// Holds the Prometheus render handle, used by the `/metrics` endpoint.
pub struct TelemetryState {
    pub prometheus_handle: Option<PrometheusHandle>,
}

/// Where the builder takes the telemetry state from.
pub enum Source {
    /// A state that is already made (a test's: no recorder).
    Given(TelemetryState),
    /// [`init`], which the builder calls once it has read the environment.
    /// What a binary's `main` hands over.
    Environment,
}

impl From<TelemetryState> for Source {
    fn from(state: TelemetryState) -> Self {
        Self::Given(state)
    }
}

/// Initialize all telemetry subsystems from `env` (the environment the
/// binary loaded: `config::load`).
///
/// Returns a `TelemetryState` containing the Prometheus handle (if metrics
/// are enabled). Must be called once at startup before any spans or metrics
/// are recorded.
pub fn init(env: &Env) -> TelemetryState {
    // --- Prometheus metrics ---
    let metrics_enabled = env.flag(&env_vars::BRUTOR_METRICS_ENABLED);

    let prometheus_handle = if metrics_enabled {
        let builder = PrometheusBuilder::new();
        match builder.install_recorder() {
            Ok(handle) => {
                tracing::info!(
                    "Prometheus metrics enabled — served on BRUTOR_METRICS_LISTEN_ADDR /metrics"
                );
                Some(handle)
            }
            Err(e) => {
                eprintln!("Failed to install Prometheus recorder: {e}");
                None
            }
        }
    } else {
        None
    };

    // --- Tracing subscriber ---
    let env_filter = log_filter(env);

    // The run judgement is OTel export only (`RUN_JUDGEMENT_TARGET`).
    let colour = std::io::IsTerminal::is_terminal(&std::io::stdout());
    let fmt_layer =
        fmt_layer(log_json_from(env), colour, std::io::stdout).with_filter(stdout_log_filter());

    // Build optional OTel layer — `Option<Layer>` implements `Layer` as a
    // pass-through when `None`, so a single subscriber stack works for both
    // the OTel-enabled and OTel-disabled cases.
    let otel_endpoint = env.string(&env_vars::OTEL_EXPORTER_OTLP_ENDPOINT);
    let service_name = env
        .string(&env_vars::OTEL_SERVICE_NAME)
        .unwrap_or_else(|| "brutor-gateway-core".into());
    let otel_layer = otel_endpoint.as_deref().and_then(|endpoint| {
        match init_otel_tracer(endpoint, service_name) {
            Ok(layer) => {
                // Use eprintln since tracing isn't initialized yet
                eprintln!("OpenTelemetry tracing enabled (OTLP → {endpoint})");
                Some(layer)
            }
            Err(e) => {
                eprintln!("OpenTelemetry init failed, traces disabled: {e}");
                None
            }
        }
    });

    tracing_subscriber::registry()
        .with(otel_layer)
        .with(env_filter)
        .with(fmt_layer)
        .init();

    TelemetryState { prometheus_handle }
}

/// The log filter `env` configures (`RUST_LOG`, the variable's default when
/// it is unset). A filter that does not parse is refused: the gateway does
/// not start with it (`config::check`). It used to be replaced by the
/// default, and the gateway logged at a level nobody had asked for.
pub fn log_filter_from(env: &Env) -> Result<EnvFilter, crate::env_registry::EnvError> {
    let var = &env_vars::RUST_LOG;
    let directives = env.required_string(var)?;
    EnvFilter::try_new(&directives).map_err(|_| env_vars::invalid(var, var.accepted))
}

/// The layer that writes the log to `writer`: JSON lines when `json`
/// (`BRUTOR_LOG_JSON_ENABLED`), human-readable lines otherwise, coloured when
/// `colour` (a terminal).
pub fn fmt_layer<S, W>(
    json: bool,
    colour: bool,
    writer: W,
) -> Box<dyn Layer<S> + Send + Sync + 'static>
where
    S: tracing::Subscriber + for<'a> tracing_subscriber::registry::LookupSpan<'a>,
    W: for<'w> tracing_subscriber::fmt::MakeWriter<'w> + Send + Sync + 'static,
{
    if json {
        tracing_subscriber::fmt::layer()
            .json()
            .with_writer(writer)
            .boxed()
    } else {
        tracing_subscriber::fmt::layer()
            .with_ansi(colour)
            .with_writer(writer)
            .boxed()
    }
}

/// Whether the log is written as JSON lines (`BRUTOR_LOG_JSON_ENABLED`).
pub fn log_json_from(env: &Env) -> bool {
    env.flag(&env_vars::BRUTOR_LOG_JSON_ENABLED)
}

/// [`log_filter_from`] where the environment was accepted (a start has
/// checked it): the filter, and the default's where there is none.
pub fn log_filter(env: &Env) -> EnvFilter {
    log_filter_from(env)
        .unwrap_or_else(|_| EnvFilter::new(env_vars::RUST_LOG.default.unwrap_or("info")))
}

/// Gracefully flush and shut down the OpenTelemetry trace pipeline.
/// Call this during application shutdown to ensure all pending spans are exported.
pub fn shutdown() {
    // SdkTracerProvider shutdown happens via Drop when the global provider
    // is replaced. We trigger it by setting a no-op provider.
    let _ = opentelemetry::global::set_tracer_provider(
        opentelemetry_sdk::trace::SdkTracerProvider::builder().build(),
    );
}

/// Build the OpenTelemetry OTLP trace exporter and return a tracing layer.
fn init_otel_tracer(
    endpoint: &str,
    service_name: String,
) -> Result<
    tracing_opentelemetry::OpenTelemetryLayer<
        tracing_subscriber::Registry,
        opentelemetry_sdk::trace::SdkTracer,
    >,
    Box<dyn std::error::Error>,
> {
    use opentelemetry::KeyValue;
    use opentelemetry::trace::TracerProvider;
    use opentelemetry_otlp::WithExportConfig;
    use opentelemetry_sdk::Resource;

    let exporter = opentelemetry_otlp::SpanExporter::builder()
        .with_tonic()
        .with_endpoint(endpoint)
        .build()?;

    let provider = opentelemetry_sdk::trace::SdkTracerProvider::builder()
        .with_batch_exporter(exporter)
        .with_resource(
            Resource::builder()
                .with_attributes([
                    KeyValue::new("service.name", service_name),
                    KeyValue::new("service.version", env!("CARGO_PKG_VERSION").to_string()),
                ])
                .build(),
        )
        .build();

    let tracer = provider.tracer("brutor-gateway-core");

    // Register as global provider so shutdown works
    opentelemetry::global::set_tracer_provider(provider);

    let layer = tracing_opentelemetry::layer().with_tracer(tracer);

    Ok(layer)
}

// ---------------------------------------------------------------------------
// The run's judgement, exported over OTel
// ---------------------------------------------------------------------------

/// Name of the span a finalized run is reported on. The gateway has no
/// per-run root span of its own — a run is many requests, finalized later by
/// the background writer or the sweeper — so each finalization opens this
/// one short span, carrying the run's identity and its judgement, and the
/// `gen_ai.evaluation.result` event inside it.
pub const RUN_SPAN_NAME: &str = "brutor.run";
/// The GenAI evaluation event (OTel semantic conventions, Development).
pub const EVALUATION_EVENT_NAME: &str = "gen_ai.evaluation.result";
/// `gen_ai.evaluation.name` of the declared-outcome evaluation.
pub const EVALUATION_NAME: &str = "brutor.declared_outcome";
/// The tracing target of the run span and its evaluation event. They are
/// OTel export, not log lines — one per finalized run filled the stdout log
/// — so [`stdout_log_filter`] keeps them off stdout. Under the crate prefix
/// so the default `brutor_gateway_core=info` filter still passes them to the
/// OTel layer.
pub const RUN_JUDGEMENT_TARGET: &str = "brutor_gateway_core::otel::run_judgement";

/// The stdout (JSON log) layer's own filter: everything the global filter
/// lets through, except the run judgement, which only the OTel layer wants.
pub fn stdout_log_filter() -> tracing_subscriber::filter::FilterFn {
    tracing_subscriber::filter::filter_fn(|m| m.target() != RUN_JUDGEMENT_TARGET)
}

/// What a finalized run reports: the three answers and the
/// classification derived from them.
#[derive(Debug, Clone, Copy)]
pub struct RunJudgement<'a> {
    pub run_id: &'a str,
    pub root_task_id: &'a str,
    pub ai_system_id: Option<&'a str>,
    pub terminal_state: &'a str,
    pub outcome_id: Option<&'a str>,
    pub outcome_conformance: &'a str,
    pub counts_toward_baseline: Option<bool>,
    pub error_cause: Option<&'a str>,
    pub run_class: &'a str,
}

/// The span attributes, by their exported names. Absent optionals are
/// omitted, never sent as empty strings: "no outcome" and an outcome named
/// `""` must not look alike in a backend.
pub fn run_span_attributes(j: &RunJudgement<'_>) -> Vec<(&'static str, String)> {
    let mut out = vec![
        ("brutor.run.id", j.run_id.to_string()),
        ("brutor.run.root_task_id", j.root_task_id.to_string()),
        ("brutor.run.terminal_state", j.terminal_state.to_string()),
        (
            "brutor.outcome.conformance",
            j.outcome_conformance.to_string(),
        ),
        ("brutor.run.class", j.run_class.to_string()),
    ];
    if let Some(s) = j.ai_system_id {
        out.push(("brutor.ai_system", s.to_string()));
    }
    if let Some(o) = j.outcome_id {
        out.push(("brutor.outcome.id", o.to_string()));
    }
    if let Some(c) = j.counts_toward_baseline {
        out.push(("brutor.outcome.counts_toward_baseline", c.to_string()));
    }
    if let Some(e) = j.error_cause {
        out.push(("brutor.error.cause", e.to_string()));
    }
    out
}

/// Emit the run's judgement: a `brutor.run` span with the outcome
/// attributes, and a `gen_ai.evaluation.result` event inside it naming the
/// evaluation `brutor.declared_outcome` and labelling it with the run class.
/// With `OTEL_EXPORTER_OTLP_ENDPOINT` set the OTel layer exports both;
/// without it they are ordinary tracing records.
pub fn record_run_judgement(j: &RunJudgement<'_>) {
    let span = tracing::info_span!(
        target: RUN_JUDGEMENT_TARGET,
        "brutor.run",
        "otel.name" = RUN_SPAN_NAME,
        "brutor.run.id" = j.run_id,
        "brutor.run.root_task_id" = j.root_task_id,
        "brutor.run.terminal_state" = j.terminal_state,
        "brutor.run.class" = j.run_class,
        "brutor.ai_system" = tracing::field::Empty,
        "brutor.outcome.id" = tracing::field::Empty,
        "brutor.outcome.conformance" = j.outcome_conformance,
        "brutor.outcome.counts_toward_baseline" = tracing::field::Empty,
        "brutor.error.cause" = tracing::field::Empty,
    );
    if let Some(s) = j.ai_system_id {
        span.record("brutor.ai_system", s);
    }
    if let Some(o) = j.outcome_id {
        span.record("brutor.outcome.id", o);
    }
    if let Some(c) = j.counts_toward_baseline {
        span.record("brutor.outcome.counts_toward_baseline", c);
    }
    if let Some(e) = j.error_cause {
        span.record("brutor.error.cause", e);
    }
    let _entered = span.enter();
    // The OTel layer names an event after its message.
    tracing::event!(
        target: RUN_JUDGEMENT_TARGET,
        tracing::Level::INFO,
        gen_ai.evaluation.name = EVALUATION_NAME,
        gen_ai.evaluation.score.label = j.run_class,
        brutor.outcome.conformance = j.outcome_conformance,
        "gen_ai.evaluation.result"
    );
}

#[cfg(test)]
mod log_format_tests {
    use std::sync::{Arc, Mutex};

    use tracing_subscriber::layer::SubscriberExt;

    /// A writer that keeps what is written.
    #[derive(Clone, Default)]
    struct Captured(Arc<Mutex<Vec<u8>>>);

    impl std::io::Write for Captured {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(buf);
            Ok(buf.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    impl<'w> tracing_subscriber::fmt::MakeWriter<'w> for Captured {
        type Writer = Captured;
        fn make_writer(&'w self) -> Self::Writer {
            self.clone()
        }
    }

    fn log_one_event(json: bool) -> String {
        let captured = Captured::default();
        let subscriber =
            tracing_subscriber::registry().with(super::fmt_layer(json, false, captured.clone()));
        tracing::subscriber::with_default(subscriber, || {
            tracing::info!(tenant_id = "acme", "media store opened");
        });
        String::from_utf8(captured.0.lock().unwrap().clone()).unwrap()
    }

    #[test]
    fn json_on_writes_one_json_object_per_line() {
        let out = log_one_event(true);
        let line = out.lines().next().expect("a line");
        let value: serde_json::Value = serde_json::from_str(line).expect("a JSON line");
        assert_eq!(value["level"], "INFO");
        assert_eq!(value["fields"]["message"], "media store opened");
        assert_eq!(value["fields"]["tenant_id"], "acme");
    }

    #[test]
    fn json_off_writes_human_readable_lines() {
        let out = log_one_event(false);
        let line = out.lines().next().expect("a line");
        assert!(
            serde_json::from_str::<serde_json::Value>(line).is_err(),
            "{line}"
        );
        assert!(line.contains("INFO"), "{line}");
        assert!(line.contains("media store opened"), "{line}");
        assert!(line.contains("tenant_id=\"acme\""), "{line}");
        assert!(!line.contains('\u{1b}'), "no colour off a terminal: {line}");
    }
}

#[cfg(test)]
mod run_judgement_tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use tracing::field::{Field, Visit};
    use tracing_subscriber::layer::{Context, Layer};

    fn judgement() -> RunJudgement<'static> {
        RunJudgement {
            run_id: "run-1",
            root_task_id: "root-1",
            ai_system_id: Some("sys-1"),
            terminal_state: "completed",
            outcome_id: Some("application_processed.loan_approved"),
            outcome_conformance: "declared",
            counts_toward_baseline: Some(true),
            error_cause: None,
            run_class: "success",
        }
    }

    #[test]
    fn the_spec_attributes_are_named_exactly() {
        let attrs = run_span_attributes(&judgement());
        let get = |k: &str| attrs.iter().find(|(n, _)| *n == k).map(|(_, v)| v.as_str());
        assert_eq!(
            get("brutor.outcome.id"),
            Some("application_processed.loan_approved")
        );
        assert_eq!(get("brutor.outcome.conformance"), Some("declared"));
        assert_eq!(get("brutor.outcome.counts_toward_baseline"), Some("true"));
        assert_eq!(get("brutor.error.cause"), None, "absent, never empty");
        let failed = RunJudgement {
            terminal_state: "errored",
            outcome_id: None,
            outcome_conformance: "unreported",
            counts_toward_baseline: None,
            error_cause: Some("model_unavailable"),
            run_class: "failed",
            ..judgement()
        };
        let attrs = run_span_attributes(&failed);
        let get = |k: &str| attrs.iter().find(|(n, _)| *n == k).map(|(_, v)| v.as_str());
        assert_eq!(get("brutor.error.cause"), Some("model_unavailable"));
        assert_eq!(get("brutor.outcome.id"), None);
        assert_eq!(get("brutor.outcome.counts_toward_baseline"), None);
        assert_eq!(EVALUATION_EVENT_NAME, "gen_ai.evaluation.result");
        assert_eq!(EVALUATION_NAME, "brutor.declared_outcome");
    }

    /// Records span fields and event fields as `name=value` strings.
    #[derive(Default, Clone)]
    struct Capture(Arc<Mutex<Vec<String>>>);
    struct V<'a>(&'a mut Vec<String>);
    impl Visit for V<'_> {
        fn record_debug(&mut self, f: &Field, v: &dyn std::fmt::Debug) {
            self.0.push(format!("{}={:?}", f.name(), v));
        }
        fn record_str(&mut self, f: &Field, v: &str) {
            self.0.push(format!("{}={v}", f.name()));
        }
        fn record_bool(&mut self, f: &Field, v: bool) {
            self.0.push(format!("{}={v}", f.name()));
        }
    }
    impl<S: tracing::Subscriber> Layer<S> for Capture {
        fn on_new_span(
            &self,
            attrs: &tracing::span::Attributes<'_>,
            _: &tracing::span::Id,
            _: Context<'_, S>,
        ) {
            let mut g = self.0.lock().unwrap();
            g.push(format!("span:{}", attrs.metadata().name()));
            attrs.record(&mut V(&mut g));
        }
        fn on_record(
            &self,
            _: &tracing::span::Id,
            r: &tracing::span::Record<'_>,
            _: Context<'_, S>,
        ) {
            r.record(&mut V(&mut self.0.lock().unwrap()));
        }
        fn on_event(&self, e: &tracing::Event<'_>, _: Context<'_, S>) {
            let mut g = self.0.lock().unwrap();
            g.push(format!("event:{}", e.metadata().name()));
            e.record(&mut V(&mut g));
        }
    }

    #[test]
    fn the_span_and_the_evaluation_event_carry_the_judgement() {
        use tracing_subscriber::layer::SubscriberExt;
        let cap = Capture::default();
        let sub = tracing_subscriber::registry().with(cap.clone());
        tracing::subscriber::with_default(sub, || {
            record_run_judgement(&RunJudgement {
                terminal_state: "errored",
                outcome_conformance: "unreported",
                outcome_id: None,
                counts_toward_baseline: None,
                error_cause: Some("blocked_by_policy"),
                run_class: "failed",
                ..judgement()
            })
        });
        let got = cap.0.lock().unwrap().clone();
        let has = |s: &str| got.iter().any(|g| g == s);
        assert!(has("span:brutor.run"), "{got:?}");
        assert!(has("brutor.outcome.conformance=unreported"), "{got:?}");
        assert!(has("brutor.error.cause=blocked_by_policy"), "{got:?}");
        assert!(has("brutor.run.class=failed"), "{got:?}");
        assert!(
            !got.iter().any(|g| g.starts_with("brutor.outcome.id=")),
            "{got:?}"
        );
        assert!(
            got.iter()
                .any(|g| g.starts_with("message=") && g.contains(EVALUATION_EVENT_NAME)),
            "the OTel layer names the event after its message: {got:?}"
        );
        assert!(
            has("gen_ai.evaluation.name=brutor.declared_outcome"),
            "{got:?}"
        );
        assert!(has("gen_ai.evaluation.score.label=failed"), "{got:?}");
    }

    /// The judgement is OTel export, not a log line: one per finalized run
    /// would fill the stdout log. The stdout layer's filter
    /// drops it; the OTel layer (unfiltered) still receives it, and every
    /// other record still reaches stdout.
    #[test]
    fn the_judgement_reaches_otel_but_not_the_stdout_log() {
        use tracing_subscriber::layer::SubscriberExt;
        let otel = Capture::default();
        let stdout = Capture::default();
        let sub = tracing_subscriber::registry()
            .with(otel.clone())
            .with(stdout.clone().with_filter(stdout_log_filter()));
        tracing::subscriber::with_default(sub, || {
            record_run_judgement(&judgement());
            tracing::info!("an ordinary log line");
        });
        let otel = otel.0.lock().unwrap().clone();
        let stdout = stdout.0.lock().unwrap().clone();
        let evaluation = |g: &Vec<String>| {
            g.iter()
                .any(|l| l.starts_with("message=") && l.contains(EVALUATION_EVENT_NAME))
        };
        assert!(evaluation(&otel), "{otel:?}");
        assert!(!evaluation(&stdout), "{stdout:?}");
        assert!(
            stdout.iter().any(|l| l.contains("an ordinary log line")),
            "{stdout:?}"
        );
        // The default log filter keys on the crate prefix: the judgement's
        // target must stay under it, or no filter would pass it to OTel.
        assert!(RUN_JUDGEMENT_TARGET.starts_with("brutor_gateway_core::"));
    }
}
