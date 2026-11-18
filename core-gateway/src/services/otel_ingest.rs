// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! OpenTelemetry GenAI inlet — evidence the gateway did not witness.
//!
//! The ledger has only ever held what the proxy saw. Some systems in an
//! estate never route through it: a framework that exports its own
//! `gen_ai.*` spans, a vendor runtime with its own tracing. Those systems
//! were invisible — no runs, no baseline, no liveness — and an assurance
//! report that cannot see a system cannot say anything about it.
//!
//! This module maps an OTLP/HTTP JSON `ExportTraceServiceRequest` onto step
//! rows so an exported trace becomes a run in the same ledger. What it never
//! does is pretend the gateway was there. Every row it produces carries
//! `source = otel` and `trace_continuity = external`; the run rolls up to
//! `chain_integrity = observed_external` and `source = otel`; the report
//! discloses the share and the verdict basis is capped. Reported and
//! witnessed evidence share a table and never a claim.
//!
//! ## Shape of a run
//!
//! One trace is one run: `root_task_id = otel:<trace id>`. Steps keep their
//! span and parent ids. Only spans carrying GenAI semantic-convention
//! attributes are evidence; everything else in the trace (HTTP server
//! spans, DB calls) is skipped but *used* — it tells us whether the trace's
//! top-level span arrived, which is how a run knows it is finished.
//!
//! ## What is deliberately not read
//!
//! Prompt and completion content (`gen_ai.prompt`, `gen_ai.completion`,
//! event bodies) is never ingested. The inlet records that a call happened,
//! to which model or tool, with what token counts and outcome — the facts
//! the ledger needs — and nothing a data subject could be identified from
//! beyond what the exporter put in `gen_ai.conversation.id`.

use std::collections::{HashMap, HashSet};

use chrono::{DateTime, Utc};
use serde_json::{Value, json};

use crate::models::auth::AuthContext;
use crate::models::db::ProxyLogInsert;
use crate::services::proxy_logger::build_proxy_log;
use crate::services::run_ledger::terminal;

/// `proxy_logs.source` / `ai_system_runs.source` for proxied traffic.
pub const SOURCE_GATEWAY: &str = "gateway";
/// `proxy_logs.source` for rows produced by this inlet.
pub const SOURCE_OTEL: &str = "otel";
/// `trace_continuity` stamped on every ingested step. Distinct from every
/// gateway-minted value so the ledger's chain rollup can rank it below
/// `client_asserted`: nothing here was bound to a credential.
pub const CONTINUITY_EXTERNAL: &str = "external";
/// `trace_root_kind` on the root step of an ingested run.
pub const ROOT_KIND_EXTERNAL_TELEMETRY: &str = "external_telemetry";
/// Prefix that makes an ingested run id unmistakable in any listing.
pub const RUN_ID_PREFIX: &str = "otel:";
/// Span or resource attribute naming the AI System to attribute to, for
/// exporters that cannot set a request header per batch.
pub const AI_SYSTEM_ATTRIBUTE: &str = "brutor.ai_system";
/// Ceilings: a collector batch is a few hundred spans; these stop a
/// misconfigured exporter from turning the inlet into a write amplifier.
pub const MAX_SPANS_PER_EXPORT: usize = 5_000;
pub const MAX_BODY_BYTES: usize = 8 * 1024 * 1024;
/// Endpoint recorded on every ingested row, so a log reader knows where it
/// came from without consulting `source`.
pub const INGEST_ENDPOINT: &str = "/v1/ingest/otel/v1/traces";
/// Stops a cyclic parent map from spinning.
const MAX_DEPTH: i32 = 32;

/// GenAI semantic-convention operation names this inlet understands.
pub mod operation {
    pub const CHAT: &str = "chat";
    pub const TEXT_COMPLETION: &str = "text_completion";
    pub const GENERATE_CONTENT: &str = "generate_content";
    pub const EMBEDDINGS: &str = "embeddings";
    pub const EXECUTE_TOOL: &str = "execute_tool";
    pub const INVOKE_AGENT: &str = "invoke_agent";
}

/// Which kind of step a span becomes. Mirrors the `proxy_type` vocabulary
/// the ledger already counts (`llm_call_count`, `tool_call_count`,
/// `a2a_call_count`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StepKind {
    Llm,
    Tool,
    Agent,
}

impl StepKind {
    pub fn from_operation(op: &str) -> Option<Self> {
        match op {
            operation::CHAT
            | operation::TEXT_COMPLETION
            | operation::GENERATE_CONTENT
            | operation::EMBEDDINGS => Some(StepKind::Llm),
            operation::EXECUTE_TOOL => Some(StepKind::Tool),
            operation::INVOKE_AGENT => Some(StepKind::Agent),
            _ => None,
        }
    }

    pub fn proxy_type(self) -> &'static str {
        match self {
            StepKind::Llm => "llm",
            StepKind::Tool => "mcp",
            StepKind::Agent => "a2a",
        }
    }
}

/// One GenAI span, reduced to the facts the ledger records.
#[derive(Debug, Clone, PartialEq)]
pub struct GenAiSpan {
    pub trace_id: String,
    pub span_id: String,
    pub parent_span_id: Option<String>,
    pub name: String,
    pub operation: String,
    pub kind: StepKind,
    pub start: DateTime<Utc>,
    pub end: DateTime<Utc>,
    pub error: bool,
    pub error_type: Option<String>,
    pub provider: Option<String>,
    pub request_model: Option<String>,
    pub response_model: Option<String>,
    pub input_tokens: Option<i32>,
    pub output_tokens: Option<i32>,
    pub tool_name: Option<String>,
    pub agent_name: Option<String>,
    pub conversation_id: Option<String>,
    /// `brutor.ai_system` from the span, else from the resource.
    pub ai_system_claim: Option<String>,
    pub service_name: Option<String>,
    pub scope_name: Option<String>,
}

/// A parsed export: the GenAI spans plus the trace topology of everything
/// else, which decides depth and completion.
#[derive(Debug, Default)]
pub struct ParsedExport {
    pub spans: Vec<GenAiSpan>,
    /// Spans present in the export that were not GenAI evidence.
    pub skipped: usize,
    /// span_id → parent_span_id for EVERY span in the export.
    parents: HashMap<String, Option<String>>,
    /// The GenAI span ids, for counting GenAI ancestors.
    genai_ids: HashSet<String>,
    /// Trace ids whose top-level span (no parent at all) was in this export.
    completed_traces: HashSet<String>,
    /// `brutor.outcome.id` per span that carried it, and per trace
    /// from its top-level span (stored form; judged at finalize like a header).
    span_outcomes: HashMap<String, String>,
    trace_outcomes: HashMap<String, String>,
}

impl ParsedExport {
    /// Number of GenAI ancestors — the ledger's delegation depth. A chat
    /// span under an HTTP server span is a root step (depth 0): the HTTP
    /// span is context, not a step.
    pub fn depth_of(&self, span_id: &str) -> i32 {
        let mut depth = 0;
        let mut cursor = self.parents.get(span_id).cloned().flatten();
        let mut hops = 0;
        while let Some(pid) = cursor {
            hops += 1;
            if hops > MAX_DEPTH {
                break;
            }
            if self.genai_ids.contains(&pid) {
                depth += 1;
            }
            cursor = self.parents.get(&pid).cloned().flatten();
        }
        depth
    }

    /// Nearest GenAI ancestor, which is what the step's `parent_task_id`
    /// should point at so the ledger's tree joins step to step.
    pub fn genai_parent_of(&self, span_id: &str) -> Option<String> {
        let mut cursor = self.parents.get(span_id).cloned().flatten();
        let mut hops = 0;
        while let Some(pid) = cursor {
            hops += 1;
            if hops > MAX_DEPTH {
                return None;
            }
            if self.genai_ids.contains(&pid) {
                return Some(pid);
            }
            cursor = self.parents.get(&pid).cloned().flatten();
        }
        None
    }

    /// The outcome a run-root step reports: its own
    /// `brutor.outcome.id`, else the trace's top-level span's. Only a run
    /// root carries it (the run's outcome is the root's to report).
    pub fn outcome_of(&self, span: &GenAiSpan) -> Option<String> {
        if !self.is_run_root(span) {
            return None;
        }
        self.span_outcomes
            .get(&span.span_id)
            .or_else(|| self.trace_outcomes.get(&span.trace_id))
            .cloned()
    }

    pub fn is_run_root(&self, span: &GenAiSpan) -> bool {
        self.depth_of(&span.span_id) == 0
    }

    /// Whether this span should carry the run-end marker: it is a root step
    /// and the trace's top-level span arrived in the same export, so the
    /// exporter considers the trace finished. Otherwise the idle sweeper
    /// closes the run, exactly as for a proxied client that never called
    /// `/v1/runs/{id}/end`.
    pub fn closes_run(&self, span: &GenAiSpan) -> bool {
        self.is_run_root(span) && self.completed_traces.contains(&span.trace_id)
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum ParseError {
    /// No `resourceSpans` array — not an `ExportTraceServiceRequest`.
    NotAnExport,
    TooManySpans(usize),
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ParseError::NotAnExport => write!(
                f,
                "expected an OTLP ExportTraceServiceRequest (JSON object with resourceSpans)"
            ),
            ParseError::TooManySpans(n) => write!(
                f,
                "export carries {n} spans; the inlet accepts at most {MAX_SPANS_PER_EXPORT} per request"
            ),
        }
    }
}

/// Ingested run id for a trace.
pub fn run_id(trace_id: &str) -> String {
    format!("{RUN_ID_PREFIX}{trace_id}")
}

/// Parse an OTLP/HTTP JSON export. Non-GenAI spans are counted as skipped
/// and folded into the topology; malformed spans (no trace or span id) are
/// skipped too — an exporter that cannot identify a span cannot be
/// evidence for it.
pub fn parse_export(body: &Value) -> Result<ParsedExport, ParseError> {
    let resource_spans = body
        .get("resourceSpans")
        .and_then(Value::as_array)
        .ok_or(ParseError::NotAnExport)?;

    let mut out = ParsedExport::default();
    let mut seen = 0usize;

    for rs in resource_spans {
        let res_attrs = attributes(rs.get("resource").and_then(|r| r.get("attributes")));
        let service_name = res_attrs.get("service.name").and_then(as_string);
        let res_claim = res_attrs.get(AI_SYSTEM_ATTRIBUTE).and_then(as_string);

        for ss in rs
            .get("scopeSpans")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let scope_name = ss
                .get("scope")
                .and_then(|s| s.get("name"))
                .and_then(Value::as_str)
                .map(str::to_owned);

            for span in ss
                .get("spans")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                seen += 1;
                if seen > MAX_SPANS_PER_EXPORT {
                    return Err(ParseError::TooManySpans(seen));
                }
                let Some((trace_id, span_id, parent)) = identity(span) else {
                    out.skipped += 1;
                    continue;
                };
                if parent.is_none() {
                    out.completed_traces.insert(trace_id.clone());
                }
                if let Some(o) = attributes(span.get("attributes"))
                    .get(crate::services::run_outcome::OTEL_OUTCOME_ID_ATTRIBUTE)
                    .and_then(as_string)
                    .and_then(|v| crate::services::run_outcome::stored(&v))
                {
                    if parent.is_none() {
                        out.trace_outcomes.insert(trace_id.clone(), o.clone());
                    }
                    out.span_outcomes.insert(span_id.clone(), o);
                }
                out.parents.insert(span_id.clone(), parent.clone());

                match parse_genai(
                    span,
                    trace_id,
                    span_id.clone(),
                    parent,
                    service_name.as_deref(),
                    res_claim.as_deref(),
                    scope_name.as_deref(),
                ) {
                    Some(s) => {
                        out.genai_ids.insert(span_id);
                        out.spans.push(s);
                    }
                    None => out.skipped += 1,
                }
            }
        }
    }
    Ok(out)
}

fn identity(span: &Value) -> Option<(String, String, Option<String>)> {
    let trace_id = span
        .get("traceId")
        .and_then(Value::as_str)?
        .trim()
        .to_owned();
    let span_id = span
        .get("spanId")
        .and_then(Value::as_str)?
        .trim()
        .to_owned();
    if trace_id.is_empty() || span_id.is_empty() {
        return None;
    }
    let parent = span
        .get("parentSpanId")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .map(str::to_owned);
    Some((trace_id, span_id, parent))
}

#[allow(clippy::too_many_arguments)]
fn parse_genai(
    span: &Value,
    trace_id: String,
    span_id: String,
    parent_span_id: Option<String>,
    service_name: Option<&str>,
    res_claim: Option<&str>,
    scope_name: Option<&str>,
) -> Option<GenAiSpan> {
    let attrs = attributes(span.get("attributes"));
    let get = |k: &str| attrs.get(k).and_then(as_string);
    let get_i32 = |k: &str| {
        attrs
            .get(k)
            .and_then(as_i64)
            .map(|n| n.clamp(0, i32::MAX as i64) as i32)
    };

    // Operation: explicit first; otherwise inferred from the attributes the
    // older convention versions set without an operation name.
    let operation = get("gen_ai.operation.name").or_else(|| {
        if attrs.contains_key("gen_ai.tool.name") {
            Some(operation::EXECUTE_TOOL.to_owned())
        } else if attrs.contains_key("gen_ai.request.model")
            || attrs.contains_key("gen_ai.response.model")
        {
            Some(operation::CHAT.to_owned())
        } else {
            None
        }
    })?;
    let kind = StepKind::from_operation(&operation)?;

    let start = nanos(span.get("startTimeUnixNano"))?;
    let end = nanos(span.get("endTimeUnixNano"))
        .unwrap_or(start)
        .max(start);

    let error = span
        .get("status")
        .and_then(|s| s.get("code"))
        .map(|c| c.as_u64() == Some(2) || c.as_str() == Some("STATUS_CODE_ERROR"))
        .unwrap_or(false);

    let name = span
        .get("name")
        .and_then(Value::as_str)
        .filter(|n| !n.trim().is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| operation.clone());

    Some(GenAiSpan {
        trace_id,
        span_id,
        parent_span_id,
        name,
        kind,
        start,
        end,
        error,
        error_type: get("error.type"),
        provider: get("gen_ai.provider.name").or_else(|| get("gen_ai.system")),
        request_model: get("gen_ai.request.model"),
        response_model: get("gen_ai.response.model"),
        input_tokens: get_i32("gen_ai.usage.input_tokens")
            .or_else(|| get_i32("gen_ai.usage.prompt_tokens")),
        output_tokens: get_i32("gen_ai.usage.output_tokens")
            .or_else(|| get_i32("gen_ai.usage.completion_tokens")),
        tool_name: get("gen_ai.tool.name"),
        agent_name: get("gen_ai.agent.name"),
        conversation_id: get("gen_ai.conversation.id"),
        ai_system_claim: get(AI_SYSTEM_ATTRIBUTE).or_else(|| res_claim.map(str::to_owned)),
        service_name: service_name.map(str::to_owned),
        scope_name: scope_name.map(str::to_owned),
        operation,
    })
}

/// OTLP JSON attributes are `[{key, value: {stringValue|intValue|...}}]`.
fn attributes(v: Option<&Value>) -> HashMap<String, Value> {
    let mut out = HashMap::new();
    for kv in v.and_then(Value::as_array).into_iter().flatten() {
        if let (Some(k), Some(val)) = (kv.get("key").and_then(Value::as_str), kv.get("value")) {
            out.insert(k.to_owned(), val.clone());
        }
    }
    out
}

fn as_string(v: &Value) -> Option<String> {
    if let Some(s) = v.get("stringValue").and_then(Value::as_str) {
        let s = s.trim();
        return (!s.is_empty()).then(|| s.to_owned());
    }
    if let Some(i) = v.get("intValue") {
        return match i {
            Value::String(s) => Some(s.clone()),
            Value::Number(n) => Some(n.to_string()),
            _ => None,
        };
    }
    if let Some(d) = v.get("doubleValue").and_then(Value::as_f64) {
        return Some(d.to_string());
    }
    if let Some(b) = v.get("boolValue").and_then(Value::as_bool) {
        return Some(b.to_string());
    }
    None
}

fn as_i64(v: &Value) -> Option<i64> {
    if let Some(i) = v.get("intValue") {
        return match i {
            Value::String(s) => s.trim().parse().ok(),
            Value::Number(n) => n.as_i64(),
            _ => None,
        };
    }
    v.get("doubleValue")
        .and_then(Value::as_f64)
        .map(|d| d as i64)
}

/// `startTimeUnixNano` is a uint64 — JSON encoders emit it as a string.
fn nanos(v: Option<&Value>) -> Option<DateTime<Utc>> {
    let n: i64 = match v? {
        Value::String(s) => s.trim().parse().ok()?,
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64))?,
        _ => return None,
    };
    if n < 0 {
        return None;
    }
    DateTime::<Utc>::from_timestamp(n / 1_000_000_000, (n % 1_000_000_000) as u32)
}

/// What every row of one export shares.
pub struct IngestContext<'a> {
    pub auth: &'a AuthContext,
    /// Resolved AI System (resource group) the run is attributed to.
    pub ai_system_id: Option<&'a str>,
    pub correlation_id: &'a str,
}

/// Turn a span into the step row the ledger will roll up. The row goes
/// through the ordinary background writer, so it is hash-chained, subject
/// to retention, and finalizes the run when it carries the end marker.
pub fn span_to_log(
    span: &GenAiSpan,
    export: &ParsedExport,
    ctx: &IngestContext<'_>,
) -> ProxyLogInsert {
    let duration_ms = (span.end - span.start).num_milliseconds().max(0) as f64;
    let status = if span.error { 500 } else { 200 };
    // The export's HTTP request is the collector's, not the
    // agent's: its User-Agent, baggage and Brutor headers describe the
    // exporter. A reported span carries no release of the gateway's
    // observing, so none is stamped.
    let reporter = AuthContext {
        session_token_type: None,
        agent_impl: Default::default(),
        ..ctx.auth.clone()
    };
    let mut log = build_proxy_log(
        span.kind.proxy_type(),
        &reporter,
        "OTEL",
        INGEST_ENDPOINT,
        None,
        None,
        status,
        duration_ms,
        !span.error,
        span.error_type.as_deref(),
        ctx.correlation_id,
    );

    log.timestamp = span.start;
    log.source = SOURCE_OTEL.to_owned();
    log.trace_id = Some(span.trace_id.clone());
    log.span_id = Some(span.span_id.clone());
    log.resource_group_id = ctx.ai_system_id.map(str::to_owned);
    if let Some(c) = &span.conversation_id {
        log.end_user_session_id = format!("otel:{c}");
    }

    let depth = export.depth_of(&span.span_id);
    log.root_task_id = Some(run_id(&span.trace_id));
    log.current_task_id = Some(span.span_id.clone());
    log.parent_task_id = export.genai_parent_of(&span.span_id);
    log.delegation_depth = Some(depth);
    log.trace_continuity = Some(CONTINUITY_EXTERNAL.to_owned());
    log.trace_root_kind = (depth == 0).then(|| ROOT_KIND_EXTERNAL_TELEMETRY.to_owned());
    // Judged at finalize exactly like `X-Brutor-Outcome-Id`.
    log.outcome_id = export.outcome_of(span);
    log.run_end_state = export.closes_run(span).then(|| {
        if span.error {
            terminal::ERRORED
        } else {
            terminal::COMPLETED
        }
        .to_owned()
    });

    match span.kind {
        StepKind::Llm => {
            log.llm_model_requested = span.request_model.clone();
            log.llm_model_used = span
                .response_model
                .clone()
                .or_else(|| span.request_model.clone());
            log.llm_provider = span.provider.clone();
            log.llm_prompt_tokens = span.input_tokens;
            log.llm_completion_tokens = span.output_tokens;
            log.llm_total_tokens = match (span.input_tokens, span.output_tokens) {
                (None, None) => None,
                (a, b) => Some(a.unwrap_or(0) + b.unwrap_or(0)),
            };
        }
        StepKind::Tool => {
            log.mcp_method = Some("tools/call".to_owned());
            log.mcp_capability_name = span.tool_name.clone();
        }
        StepKind::Agent => {}
    }

    log.custom_metadata = Some(json!({
        "otel": {
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "span_name": span.name,
            "operation": span.operation,
            "service_name": span.service_name,
            "scope": span.scope_name,
            "agent_name": span.agent_name,
        }
    }));
    log
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::models::auth::AuthType;

    pub(crate) fn sample_export() -> Value {
        json!({
            "resourceSpans": [{
                "resource": {"attributes": [
                    {"key": "service.name", "value": {"stringValue": "claims-agent"}},
                    {"key": "brutor.ai_system", "value": {"stringValue": "sys-claims"}}
                ]},
                "scopeSpans": [{
                    "scope": {"name": "openinference.instrumentation.langchain"},
                    "spans": [
                        {"traceId": "aaaa0000aaaa0000aaaa0000aaaa0000", "spanId": "0000000000000001",
                         "name": "POST /chat", "startTimeUnixNano": "1725000000000000000",
                         "endTimeUnixNano": "1725000003000000000", "status": {"code": 1},
                         "attributes": [{"key": "http.request.method", "value": {"stringValue": "POST"}}]},
                        {"traceId": "aaaa0000aaaa0000aaaa0000aaaa0000", "spanId": "0000000000000002",
                         "parentSpanId": "0000000000000001", "name": "chat gpt-4o",
                         "startTimeUnixNano": "1725000000100000000", "endTimeUnixNano": "1725000001100000000",
                         "attributes": [
                            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                            {"key": "gen_ai.provider.name", "value": {"stringValue": "openai"}},
                            {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
                            {"key": "gen_ai.response.model", "value": {"stringValue": "gpt-4o-2024-08-06"}},
                            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "120"}},
                            {"key": "gen_ai.usage.output_tokens", "value": {"intValue": 40}},
                            {"key": "gen_ai.conversation.id", "value": {"stringValue": "conv-9"}}
                         ]},
                        {"traceId": "aaaa0000aaaa0000aaaa0000aaaa0000", "spanId": "0000000000000003",
                         "parentSpanId": "0000000000000002", "name": "execute_tool lookup_policy",
                         "startTimeUnixNano": "1725000000200000000", "endTimeUnixNano": "1725000000400000000",
                         "status": {"code": "STATUS_CODE_ERROR"},
                         "attributes": [
                            {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                            {"key": "gen_ai.tool.name", "value": {"stringValue": "lookup_policy"}},
                            {"key": "error.type", "value": {"stringValue": "timeout"}}
                         ]},
                        {"traceId": "aaaa0000aaaa0000aaaa0000aaaa0000", "spanId": "0000000000000004",
                         "parentSpanId": "0000000000000001", "name": "chat gpt-4o",
                         "startTimeUnixNano": "1725000001200000000", "endTimeUnixNano": "1725000002900000000",
                         "attributes": [
                            {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
                            {"key": "gen_ai.usage.prompt_tokens", "value": {"intValue": "300"}},
                            {"key": "gen_ai.usage.completion_tokens", "value": {"intValue": "25"}}
                         ]},
                        {"traceId": "bbbb0000bbbb0000bbbb0000bbbb0000", "spanId": "0000000000000009",
                         "parentSpanId": "0000000000000008", "name": "chat claude",
                         "startTimeUnixNano": "1725000010000000000", "endTimeUnixNano": "1725000011000000000",
                         "attributes": [
                            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                            {"key": "gen_ai.system", "value": {"stringValue": "anthropic"}},
                            {"key": "gen_ai.request.model", "value": {"stringValue": "claude-sonnet-5"}}
                         ]}
                    ]
                }]
            }]
        })
    }

    pub(crate) fn auth() -> AuthContext {
        AuthContext {
            credential_kind: crate::models::auth::CredentialKind::ApiKey,
            credential_assurance: Some(crate::models::auth::CredentialAssurance::Key),
            credential_ref: None,
            credential_detail: Default::default(),
            identity_verdict: None,
            auth_type: AuthType::ApiKey,
            tenant_id: "t-otel".into(),
            gateway_user_id: None,
            gateway_username: None,
            gateway_tenant_id: None,
            idp_user_id: None,
            idp_username: None,
            idp_email: None,
            api_key_id: Some("key-1".into()),
            api_key_name: Some("collector".into()),
            api_key_scope: None,
            api_key_group_id: Some("sys-claims".into()),
            api_key_is_platform: false,
            api_key_user_id: None,
            allowed_skill_ids: None,
            session_key: None,
            client_ip: None,
            resolved_group_id: None,
            actor_agent_id: None,
            session_token_type: None,
            agent_impl: crate::services::agent_impl::request_carriers(),
            admission_hold: None,
        }
    }

    #[test]
    fn keeps_genai_spans_and_skips_the_rest() {
        let export = parse_export(&sample_export()).unwrap();
        assert_eq!(
            export.spans.len(),
            4,
            "three GenAI spans in trace A, one in trace B"
        );
        assert_eq!(
            export.skipped, 1,
            "the HTTP server span is context, not evidence"
        );
        let kinds: Vec<StepKind> = export.spans.iter().map(|s| s.kind).collect();
        assert_eq!(
            kinds,
            [StepKind::Llm, StepKind::Tool, StepKind::Llm, StepKind::Llm]
        );
    }

    #[test]
    fn reads_the_semantic_conventions_including_legacy_names() {
        let export = parse_export(&sample_export()).unwrap();
        let chat = &export.spans[0];
        assert_eq!(chat.provider.as_deref(), Some("openai"));
        assert_eq!(chat.request_model.as_deref(), Some("gpt-4o"));
        assert_eq!(chat.response_model.as_deref(), Some("gpt-4o-2024-08-06"));
        assert_eq!(
            (chat.input_tokens, chat.output_tokens),
            (Some(120), Some(40)),
            "string and number intValue"
        );
        assert_eq!(chat.conversation_id.as_deref(), Some("conv-9"));
        assert_eq!(
            chat.ai_system_claim.as_deref(),
            Some("sys-claims"),
            "resource attribute"
        );
        assert_eq!(chat.service_name.as_deref(), Some("claims-agent"));
        assert!(!chat.error);

        let tool = &export.spans[1];
        assert!(tool.error, "STATUS_CODE_ERROR as a string");
        assert_eq!(tool.error_type.as_deref(), Some("timeout"));
        assert_eq!(tool.tool_name.as_deref(), Some("lookup_policy"));

        let legacy = &export.spans[2];
        assert_eq!(
            legacy.operation,
            operation::CHAT,
            "inferred from gen_ai.request.model"
        );
        assert_eq!(
            (legacy.input_tokens, legacy.output_tokens),
            (Some(300), Some(25))
        );

        let claude = &export.spans[3];
        assert_eq!(
            claude.provider.as_deref(),
            Some("anthropic"),
            "gen_ai.system fallback"
        );
    }

    #[test]
    fn depth_counts_genai_ancestors_only() {
        let export = parse_export(&sample_export()).unwrap();
        assert_eq!(
            export.depth_of("0000000000000002"),
            0,
            "under an HTTP span, still a root step"
        );
        assert_eq!(
            export.depth_of("0000000000000003"),
            1,
            "tool under the chat"
        );
        assert_eq!(export.depth_of("0000000000000004"), 0);
        assert_eq!(
            export.genai_parent_of("0000000000000003").as_deref(),
            Some("0000000000000002")
        );
        assert_eq!(
            export.genai_parent_of("0000000000000002"),
            None,
            "the HTTP span is not a step"
        );
    }

    #[test]
    fn a_run_closes_only_when_its_trace_root_arrived() {
        let export = parse_export(&sample_export()).unwrap();
        assert!(
            export.closes_run(&export.spans[0]),
            "trace A's top span is in the export"
        );
        assert!(
            !export.closes_run(&export.spans[1]),
            "a child step never closes the run"
        );
        assert!(export.is_run_root(&export.spans[3]));
        assert!(
            !export.closes_run(&export.spans[3]),
            "trace B's parent 08 never arrived — the sweeper closes it"
        );
    }

    #[test]
    fn rejects_non_exports_and_oversized_batches() {
        assert_eq!(
            parse_export(&json!({"spans": []})).unwrap_err(),
            ParseError::NotAnExport
        );
        assert_eq!(
            parse_export(&json!([])).unwrap_err(),
            ParseError::NotAnExport
        );
        let spans: Vec<Value> = (0..=MAX_SPANS_PER_EXPORT)
            .map(|i| json!({"traceId": "t", "spanId": format!("{i}"), "startTimeUnixNano": "1"}))
            .collect();
        let big = json!({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]});
        assert!(matches!(
            parse_export(&big).unwrap_err(),
            ParseError::TooManySpans(_)
        ));
    }

    #[test]
    fn malformed_spans_are_skipped_not_fatal() {
        let body = json!({"resourceSpans": [{"scopeSpans": [{"spans": [
            {"spanId": "1", "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}}]},
            {"traceId": "t", "spanId": "2", "startTimeUnixNano": "nonsense",
             "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}}]},
            {"traceId": "t", "spanId": "3", "startTimeUnixNano": "5",
             "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "create_agent"}}]}
        ]}]}]});
        let export = parse_export(&body).unwrap();
        assert!(export.spans.is_empty());
        assert_eq!(
            export.skipped, 3,
            "no trace id; unparseable time; operation that is not a step"
        );
    }

    #[test]
    fn maps_a_span_onto_a_step_row_the_ledger_can_roll_up() {
        let export = parse_export(&sample_export()).unwrap();
        let a = auth();
        let ctx = IngestContext {
            auth: &a,
            ai_system_id: Some("sys-claims"),
            correlation_id: "corr-1",
        };

        let root = span_to_log(&export.spans[0], &export, &ctx);
        assert_eq!(root.proxy_type, "llm");
        assert_eq!(root.source, SOURCE_OTEL);
        assert_eq!(
            root.root_task_id.as_deref(),
            Some("otel:aaaa0000aaaa0000aaaa0000aaaa0000")
        );
        assert_eq!(root.current_task_id.as_deref(), Some("0000000000000002"));
        assert_eq!(root.parent_task_id, None);
        assert_eq!(root.delegation_depth, Some(0));
        assert_eq!(root.trace_continuity.as_deref(), Some(CONTINUITY_EXTERNAL));
        assert_eq!(
            root.trace_root_kind.as_deref(),
            Some(ROOT_KIND_EXTERNAL_TELEMETRY)
        );
        assert_eq!(
            root.run_end_state.as_deref(),
            Some(terminal::COMPLETED),
            "trace root arrived → the run closes"
        );
        assert_eq!(root.resource_group_id.as_deref(), Some("sys-claims"));
        assert_eq!(root.end_user_session_id, "otel:conv-9");
        assert_eq!(root.llm_model_used.as_deref(), Some("gpt-4o-2024-08-06"));
        assert_eq!(root.llm_provider.as_deref(), Some("openai"));
        assert_eq!(root.llm_total_tokens, Some(160));
        assert_eq!(root.duration_ms, Some(1000.0));
        assert!(root.success);
        assert_eq!(root.endpoint, INGEST_ENDPOINT);
        assert_eq!(root.http_method, "OTEL");
        assert!(
            root.request_body_preview.is_none() && root.response_body_preview.is_none(),
            "never any content"
        );
        assert_eq!(root.timestamp.timestamp(), 1_725_000_000);

        let tool = span_to_log(&export.spans[1], &export, &ctx);
        assert_eq!(tool.proxy_type, "mcp");
        assert_eq!(tool.mcp_capability_name.as_deref(), Some("lookup_policy"));
        assert_eq!(tool.parent_task_id.as_deref(), Some("0000000000000002"));
        assert_eq!(tool.delegation_depth, Some(1));
        assert!(
            tool.trace_root_kind.is_none(),
            "only the root step carries the kind"
        );
        assert!(tool.run_end_state.is_none());
        assert!(!tool.success);
        assert_eq!(tool.error_message.as_deref(), Some("timeout"));

        let unfinished = span_to_log(&export.spans[3], &export, &ctx);
        assert!(
            unfinished.run_end_state.is_none(),
            "trace B is not known to be complete"
        );
        assert_eq!(
            unfinished.llm_model_used.as_deref(),
            Some("claude-sonnet-5"),
            "request model when no response model"
        );
    }

    #[test]
    fn the_root_spans_outcome_attribute_rides_on_the_root_step() {
        // `brutor.outcome.id` on the trace's top-level span (here
        // an HTTP span, not GenAI) reaches the run-root step; a nested step
        // never carries the run's outcome.
        let body = json!({"resourceSpans": [{"scopeSpans": [{"spans": [
            {"traceId": "t1", "spanId": "http", "startTimeUnixNano": "1700000000000000000",
             "attributes": [{"key": "brutor.outcome.id", "value": {"stringValue": "claim_settled.paid"}}]},
            {"traceId": "t1", "spanId": "root", "parentSpanId": "http",
             "startTimeUnixNano": "1700000000000000000",
             "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}}]},
            {"traceId": "t1", "spanId": "child", "parentSpanId": "root",
             "startTimeUnixNano": "1700000000000000000",
             "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                            {"key": "brutor.outcome.id", "value": {"stringValue": "not_the_runs"}}]}
        ]}]}]});
        let export = parse_export(&body).unwrap();
        let a = auth();
        let ctx = IngestContext {
            auth: &a,
            ai_system_id: None,
            correlation_id: "c",
        };
        let root = export.spans.iter().find(|s| s.span_id == "root").unwrap();
        let child = export.spans.iter().find(|s| s.span_id == "child").unwrap();
        assert_eq!(
            span_to_log(root, &export, &ctx).outcome_id.as_deref(),
            Some("claim_settled.paid")
        );
        assert_eq!(span_to_log(child, &export, &ctx).outcome_id, None);
        // A malformed value is carried verbatim (judged undeclared later).
        let bad = json!({"resourceSpans": [{"scopeSpans": [{"spans": [
            {"traceId": "t2", "spanId": "r", "startTimeUnixNano": "1700000000000000000",
             "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                            {"key": "brutor.outcome.id", "value": {"stringValue": "Not Valid"}}]}
        ]}]}]});
        let export = parse_export(&bad).unwrap();
        assert_eq!(
            span_to_log(&export.spans[0], &export, &ctx)
                .outcome_id
                .as_deref(),
            Some("Not Valid")
        );
    }

    #[test]
    fn an_errored_root_closes_the_run_as_errored() {
        let body = json!({"resourceSpans": [{"scopeSpans": [{"spans": [
            {"traceId": "t1", "spanId": "s1", "startTimeUnixNano": "1725000000000000000",
             "endTimeUnixNano": "1725000000500000000", "status": {"code": 2},
             "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
                            {"key": "gen_ai.agent.name", "value": {"stringValue": "triage"}}]}
        ]}]}]});
        let export = parse_export(&body).unwrap();
        let a = auth();
        let ctx = IngestContext {
            auth: &a,
            ai_system_id: None,
            correlation_id: "c",
        };
        let log = span_to_log(&export.spans[0], &export, &ctx);
        assert_eq!(log.proxy_type, "a2a");
        assert_eq!(log.run_end_state.as_deref(), Some(terminal::ERRORED));
        assert!(
            log.resource_group_id.is_none(),
            "unattributed stays unattributed — the funnel sees it"
        );
        assert_eq!(
            log.custom_metadata.as_ref().unwrap()["otel"]["agent_name"],
            "triage"
        );
    }
}

#[cfg(test)]
mod trace_context_tests {
    use super::*;

    #[test]
    fn ingested_rows_carry_the_spans_own_trace_and_span_ids() {
        let export = parse_export(&tests::sample_export()).unwrap();
        let a = tests::auth();
        let ctx = IngestContext {
            auth: &a,
            ai_system_id: Some("sys-claims"),
            correlation_id: "corr-1",
        };
        let root = span_to_log(&export.spans[0], &export, &ctx);
        assert_eq!(
            root.trace_id.as_deref(),
            Some("aaaa0000aaaa0000aaaa0000aaaa0000")
        );
        assert_eq!(root.span_id.as_deref(), Some("0000000000000002"));
        assert!(
            root.request_content_hash.is_none(),
            "no content was ever seen, so no digest"
        );
    }
}
