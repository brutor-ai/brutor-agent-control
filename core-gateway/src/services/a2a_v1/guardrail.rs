// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Shared guardrail glue used by both the inbound A2A v1.0 routes
//! (`routes::a2a_v1`) and the outbound delegation route
//! (`routes::a2a`). Keeps the guardrail surface, the structured
//! `PolicyBlocked` error envelope and the `proxy_guardrail_blocks_total`
//! metric in a single place so both directions stay symmetric.

use serde_json::Value;

use crate::models::auth::AuthContext;
use crate::services::SharedState;
use crate::services::a2a_v1::errors::{A2aError, A2aErrorKind};
use crate::services::a2a_v1::{Artifact, Message, PartContent, Task};
use crate::services::{guardrails, proxy_metrics};

/// Concatenate every text part on a Message into a single string for
/// guardrail scanning. Non-text parts (raw bytes, URLs, structured
/// data) are intentionally skipped — guardrail providers are
/// text-oriented, and a binary attachment is best treated as
/// out-of-scope rather than fed through banned-words / Lakera.
pub fn message_text(msg: &Message) -> String {
    let mut buf = String::new();
    for part in &msg.parts {
        if let PartContent::Text(t) = &part.content {
            if !buf.is_empty() {
                buf.push('\n');
            }
            buf.push_str(t);
        }
    }
    buf
}

/// Concatenate every text part on an [`Artifact`] into one string,
/// mirroring [`message_text`]. Used by the output-guardrail path to
/// scan an artifact we're about to return to an external caller.
pub fn artifact_text(artifact: &Artifact) -> String {
    let mut buf = String::new();
    for part in &artifact.parts {
        if let PartContent::Text(t) = &part.content {
            if !buf.is_empty() {
                buf.push('\n');
            }
            buf.push_str(t);
        }
    }
    buf
}

/// Splice `redacted` text back into a single [`Artifact`], the inverse
/// of [`artifact_text`]. Same `\n`-split + split-count-mismatch
/// fallback as [`apply_message_redactions`]. Used by the SSE path,
/// which guardrails one artifact frame at a time.
pub fn apply_artifact_redactions(artifact: &mut Artifact, redacted: &str) -> bool {
    let text_indices: Vec<usize> = artifact
        .parts
        .iter()
        .enumerate()
        .filter_map(|(i, p)| matches!(&p.content, PartContent::Text(_)).then_some(i))
        .collect();
    if text_indices.is_empty() {
        return false;
    }
    let parts: Vec<&str> = redacted.split('\n').collect();
    if parts.len() != text_indices.len() {
        tracing::warn!(
            target: "guardrails",
            surface = "a2a_outbound",
            "Artifact redaction split-count mismatch (got {}, expected {}); forwarding original",
            parts.len(),
            text_indices.len(),
        );
        return false;
    }
    for (slot, part) in text_indices.iter().zip(parts.iter()) {
        if let Some(p) = artifact.parts.get_mut(*slot) {
            p.content = PartContent::Text((*part).to_string());
        }
    }
    true
}

/// Extract the scannable OUTPUT text for a [`Task`] result we're about
/// to hand back to an external caller: every text part of every
/// artifact, followed by the status message's text parts. Joined with
/// `\n` so it lines up with the redaction-splice helpers.
///
/// History is intentionally NOT included — it carries the *inbound*
/// messages we already scanned on the `a2a_inbound` input surface, and
/// re-scanning them here would double-count and risk blocking a
/// response for content the caller themselves sent in.
pub fn task_output_text(task: &Task) -> String {
    let mut buf = String::new();
    let mut push = |s: &str| {
        if s.is_empty() {
            return;
        }
        if !buf.is_empty() {
            buf.push('\n');
        }
        buf.push_str(s);
    };
    for artifact in &task.artifacts {
        push(&artifact_text(artifact));
    }
    if let Some(msg) = &task.status.message {
        push(&message_text(msg));
    }
    buf
}

/// Splice `redacted` text back into a [`Task`]'s output (artifacts +
/// status message), the inverse of [`task_output_text`]. The detector
/// was fed every artifact text-part followed by every status-message
/// text-part, joined by `\n`; split the redacted blob on the same
/// separator and overwrite each text part in that exact order.
///
/// Same split-count-mismatch fallback as the message variant: if the
/// detector altered the newline structure (split count drifts from the
/// original text-part count) the function is a no-op and the caller
/// forwards the original task — better to leak an un-redacted artifact
/// than to scramble the part order.
pub fn apply_task_output_redactions(task: &mut Task, redacted: &str) -> bool {
    // Gather mutable references to every text part in extraction order:
    // artifacts first (each part in order), then the status message.
    let mut slots: Vec<&mut String> = Vec::new();
    for artifact in &mut task.artifacts {
        for part in &mut artifact.parts {
            if let PartContent::Text(t) = &mut part.content {
                slots.push(t);
            }
        }
    }
    if let Some(msg) = &mut task.status.message {
        for part in &mut msg.parts {
            if let PartContent::Text(t) = &mut part.content {
                slots.push(t);
            }
        }
    }
    if slots.is_empty() {
        return false;
    }
    let parts: Vec<&str> = redacted.split('\n').collect();
    if parts.len() != slots.len() {
        tracing::warn!(
            target: "guardrails",
            surface = "a2a_outbound",
            "Task-output redaction split-count mismatch (got {}, expected {}); forwarding original",
            parts.len(),
            slots.len(),
        );
        return false;
    }
    for (slot, part) in slots.into_iter().zip(parts.iter()) {
        *slot = (*part).to_string();
    }
    true
}

/// Best-effort text extraction for the outbound delegation handler,
/// which receives a free-form `serde_json::Value` rather than a
/// strongly-typed v1.0 `Message`. We walk the JSON tree and collect
/// every string value — the false-positive rate (matching e.g. URL
/// fragments) is acceptable here because the policy is opt-in via
/// the per-tenant `a2a_outbound_enabled` flag.
pub fn extract_outbound_text(payload: &Value) -> String {
    fn walk(v: &Value, out: &mut String) {
        match v {
            Value::String(s) => {
                if !out.is_empty() {
                    out.push('\n');
                }
                out.push_str(s);
            }
            Value::Array(arr) => {
                for x in arr {
                    walk(x, out);
                }
            }
            Value::Object(map) => {
                for x in map.values() {
                    walk(x, out);
                }
            }
            _ => {}
        }
    }
    let mut buf = String::new();
    walk(payload, &mut buf);
    buf
}

/// Outcome of an A2A guardrail check from the caller's
/// perspective. `Pass` means forward the original body
/// unchanged; `Redacted(text)` means the detector rewrote one or
/// more substrings and the caller MUST splice the new text back
/// into the request body (via `apply_message_redactions` for
/// strongly-typed inbound `Message`, or `apply_outbound_redactions`
/// for the free-form outbound JSON `Value`). Block is signalled
/// via `Err` and never reaches the caller as `Ok`.
#[derive(Debug, Clone)]
pub enum CheckResult {
    Pass,
    Redacted(String),
}

/// Apply tenant guardrails to a chunk of A2A message text. Loads the
/// caller's effective guardrail configs (Agent Card → bound user/group)
/// then runs `guardrails::check_input` with the supplied surface
/// (`"a2a_inbound"` or `"a2a_outbound"`).
///
/// On block: emits the `proxy_guardrail_blocks_total{surface=…}`
/// metric + a warn log, then returns a v1.0 `PolicyBlocked` structured
/// error with `surface`, `check`, `config` metadata so the calling
/// agent can surface the refusal to its operator.
///
/// On redact: returns `Ok(CheckResult::Redacted(new_text))`. The
/// caller MUST splice the new text back into the request body —
/// otherwise the redaction is a no-op. The splice helpers
/// (`apply_message_redactions` / `apply_outbound_redactions`) do
/// the body-shape-specific work.
///
/// On internal failure (DB hiccup loading configs): fail-open with a
/// warn log, matching the LLM/MCP path's behaviour. A transient DB
/// problem must not take down legitimate A2A traffic.
pub async fn check(
    state: &SharedState,
    auth_ctx: &AuthContext,
    tenant_id: &str,
    text: &str,
    surface: &'static str,
) -> Result<CheckResult, A2aError> {
    run(state, auth_ctx, tenant_id, text, surface, Direction::Input).await
}

/// OUTPUT-direction counterpart of [`check`]. Runs the tenant's
/// OUTPUT guardrails (each check's `*_output_enabled` toggle) over a
/// chunk of A2A *response* / *artifact* text. Same block / redact
/// contract as [`check`]:
///
///   * the dispatch RESULT we return to an external caller — checked
///     on the `a2a_outbound` surface (data LEAVING our tenant);
///   * the peer's RESPONSE relayed back from an outbound call —
///     checked on the `a2a_inbound` surface (data ENTERING our
///     tenant).
///
/// On block the caller MUST refuse the response with the
/// A2A-appropriate error frame (it must never forward the offending
/// text). On `Redacted` the caller splices the rewritten text back
/// into the response body before returning it.
pub async fn check_output(
    state: &SharedState,
    auth_ctx: &AuthContext,
    tenant_id: &str,
    text: &str,
    surface: &'static str,
) -> Result<CheckResult, A2aError> {
    run(state, auth_ctx, tenant_id, text, surface, Direction::Output).await
}

/// Which guardrail direction (input vs output) the orchestrator
/// should evaluate. Kept private — callers pick via [`check`] /
/// [`check_output`].
#[derive(Debug, Clone, Copy)]
enum Direction {
    Input,
    Output,
}

/// Shared core for [`check`] and [`check_output`]. Loads the
/// caller's effective guardrail configs (Agent Card → bound
/// user/group) then runs the input- or output-direction
/// orchestrator with the supplied surface. Same fail-closed-on-DB-error
/// and block/redact envelope for both directions.
async fn run(
    state: &SharedState,
    auth_ctx: &AuthContext,
    tenant_id: &str,
    text: &str,
    surface: &'static str,
    direction: Direction,
) -> Result<CheckResult, A2aError> {
    if text.is_empty() {
        return Ok(CheckResult::Pass);
    }

    let configs = match state
        .guardrails_cache
        .get_configs(
            tenant_id,
            auth_ctx.guardrail_user_id().as_deref(),
            auth_ctx.guardrail_group_id(),
            &state.db,
        )
        .await
    {
        Ok(c) => c,
        // Fail closed. A message the gateway cannot check is refused,
        // never passed as if it had been checked and found clean.
        Err(e) => {
            tracing::error!(error = %e, surface = %surface, "guardrail configs unavailable for A2A; refusing (fail closed)");
            proxy_metrics::record_guardrail_block(surface, "guardrails_unavailable");
            return Err(A2aError::new(
                A2aErrorKind::GuardrailsUnavailable,
                "guardrails_unavailable: the gateway cannot load this tenant's guardrails, so it cannot check the message",
            )
            .with_metadata("surface", surface)
            .with_metadata("check", "guardrails_unavailable"));
        }
    };

    if configs.is_empty() {
        return Ok(CheckResult::Pass);
    }

    let outcome = match direction {
        Direction::Input => {
            guardrails::check_input(text, &configs, surface, &state.http_client).await
        }
        Direction::Output => {
            guardrails::check_output(text, &configs, surface, &state.http_client).await
        }
    };

    match outcome {
        guardrails::CheckOutcome::Block(violation) => {
            tracing::warn!(
                guardrail = %violation.config_name,
                check = %violation.check,
                surface = %surface,
                "A2A guardrail violation — 403 returned"
            );
            proxy_metrics::record_guardrail_block(surface, &violation.check);

            Err(A2aError::new(
                A2aErrorKind::PolicyBlocked,
                format!(
                    "Request blocked by tenant guardrail '{}' (check={})",
                    violation.config_name, violation.check
                ),
            )
            .with_metadata("surface", surface)
            .with_metadata("check", violation.check)
            .with_metadata("config", violation.config_name))
        }
        guardrails::CheckOutcome::Redacted {
            content: redacted,
            warnings,
        } => {
            for w in &warnings {
                tracing::warn!(
                    target: "guardrails",
                    check = %w.check,
                    surface = %surface,
                    "{}",
                    w.message
                );
            }
            Ok(CheckResult::Redacted(redacted))
        }
        guardrails::CheckOutcome::Warn { warnings } => {
            for w in &warnings {
                tracing::warn!(
                    target: "guardrails",
                    check = %w.check,
                    surface = %surface,
                    "{}",
                    w.message
                );
            }
            Ok(CheckResult::Pass)
        }
        guardrails::CheckOutcome::Pass => Ok(CheckResult::Pass),
    }
}

/// Splice `redacted` text back into an A2A `Message`. The detector
/// was fed the concatenation of `message_text` parts joined with
/// `\n`; split the redacted output on the same separator and
/// overwrite each text part in order. Non-text parts (bytes, URLs,
/// structured data) pass through unchanged.
///
/// If the split count drifts from the original part count (the
/// detector did something unexpected to newlines) the function
/// is a no-op and the caller forwards the original body — better
/// to leak an unredacted message than to corrupt the part order.
pub fn apply_message_redactions(msg: &mut Message, redacted: &str) -> bool {
    let text_indices: Vec<usize> = msg
        .parts
        .iter()
        .enumerate()
        .filter_map(|(i, p)| matches!(&p.content, PartContent::Text(_)).then_some(i))
        .collect();
    if text_indices.is_empty() {
        return false;
    }
    let parts: Vec<&str> = redacted.split('\n').collect();
    if parts.len() != text_indices.len() {
        tracing::warn!(
            target: "guardrails",
            surface = "a2a",
            "Redaction split-count mismatch (got {}, expected {}); forwarding original",
            parts.len(),
            text_indices.len(),
        );
        return false;
    }
    for (slot, part) in text_indices.iter().zip(parts.iter()) {
        if let Some(p) = msg.parts.get_mut(*slot) {
            p.content = PartContent::Text((*part).to_string());
        }
    }
    true
}

/// Splice `redacted` text back into a free-form outbound JSON
/// payload. Mirrors the recursion order in `extract_outbound_text`
/// — collect every string leaf (object values, array entries),
/// split the redacted blob on `\n`, replace each leaf in order.
/// Same split-count-mismatch fallback as the message variant.
pub fn apply_outbound_redactions(payload: &mut Value, redacted: &str) -> bool {
    fn collect(v: &Value, out: &mut Vec<()>) {
        match v {
            Value::String(_) => out.push(()),
            Value::Array(arr) => {
                for x in arr {
                    collect(x, out);
                }
            }
            Value::Object(map) => {
                for x in map.values() {
                    collect(x, out);
                }
            }
            _ => {}
        }
    }
    fn replace(v: &mut Value, parts: &[&str], idx: &mut usize) {
        match v {
            Value::String(s) => {
                if let Some(p) = parts.get(*idx) {
                    *s = (*p).to_string();
                }
                *idx += 1;
            }
            Value::Array(arr) => {
                for x in arr.iter_mut() {
                    replace(x, parts, idx);
                }
            }
            Value::Object(map) => {
                for x in map.values_mut() {
                    replace(x, parts, idx);
                }
            }
            _ => {}
        }
    }
    let mut counter = Vec::new();
    collect(payload, &mut counter);
    let expected = counter.len();
    if expected == 0 {
        return false;
    }
    let parts: Vec<&str> = redacted.split('\n').collect();
    if parts.len() != expected {
        tracing::warn!(
            target: "guardrails",
            surface = "a2a_outbound",
            "Redaction split-count mismatch (got {}, expected {}); forwarding original",
            parts.len(),
            expected,
        );
        return false;
    }
    let mut idx = 0usize;
    replace(payload, &parts, &mut idx);
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::a2a_v1::{Message, Part, PartContent, Role};
    use serde_json::json;

    fn msg(parts: Vec<Part>) -> Message {
        Message {
            message_id: "m1".to_string(),
            context_id: None,
            task_id: None,
            role: Role::RoleUser,
            parts,
            metadata: None,
            extensions: vec![],
            reference_task_ids: vec![],
        }
    }

    fn text_part(t: &str) -> Part {
        Part {
            content: PartContent::Text(t.to_string()),
            metadata: None,
            filename: None,
            media_type: None,
        }
    }

    #[test]
    fn message_text_concatenates_text_parts() {
        let m = msg(vec![text_part("hello"), text_part("world")]);
        assert_eq!(message_text(&m), "hello\nworld");
    }

    #[test]
    fn message_text_skips_non_text_parts() {
        let m = msg(vec![
            text_part("kept"),
            Part {
                content: PartContent::Url("https://x.example".to_string()),
                metadata: None,
                filename: None,
                media_type: None,
            },
            text_part("also-kept"),
        ]);
        assert_eq!(message_text(&m), "kept\nalso-kept");
    }

    #[test]
    fn message_text_empty_for_no_text_parts() {
        let m = msg(vec![Part {
            content: PartContent::Raw(vec![1, 2, 3]),
            metadata: None,
            filename: None,
            media_type: None,
        }]);
        assert_eq!(message_text(&m), "");
    }

    // ── output extraction + splice (G10) ────────────────────────────

    fn artifact(parts: Vec<Part>) -> Artifact {
        Artifact {
            artifact_id: "a1".to_string(),
            name: None,
            description: None,
            parts,
            metadata: None,
            extensions: vec![],
        }
    }

    fn task_with(artifacts: Vec<Artifact>, status_msg: Option<Message>) -> Task {
        use crate::services::a2a_v1::{TaskState, TaskStatus};
        Task {
            id: "t1".to_string(),
            context_id: Some("c1".to_string()),
            status: TaskStatus {
                state: TaskState::TaskStateCompleted,
                message: status_msg,
                timestamp: None,
            },
            artifacts,
            history: vec![],
            metadata: None,
        }
    }

    #[test]
    fn artifact_text_concatenates_text_parts() {
        let a = artifact(vec![text_part("one"), text_part("two")]);
        assert_eq!(artifact_text(&a), "one\ntwo");
    }

    #[test]
    fn task_output_text_walks_artifacts_then_status() {
        let t = task_with(
            vec![
                artifact(vec![text_part("art-a"), text_part("art-b")]),
                artifact(vec![text_part("art-c")]),
            ],
            Some(msg(vec![text_part("status-msg")])),
        );
        // Artifacts first (each part in order), then status message.
        assert_eq!(task_output_text(&t), "art-a\nart-b\nart-c\nstatus-msg");
    }

    #[test]
    fn task_output_text_excludes_history() {
        // History carries inbound messages (already scanned on input);
        // it must NOT appear in the output text.
        let mut t = task_with(vec![], Some(msg(vec![text_part("only-status")])));
        t.history = vec![msg(vec![text_part("inbound-secret")])];
        assert_eq!(task_output_text(&t), "only-status");
    }

    #[test]
    fn apply_artifact_redactions_overwrites_in_order() {
        let mut a = artifact(vec![text_part("one"), text_part("two")]);
        assert!(apply_artifact_redactions(&mut a, "ONE\nTWO"));
        let texts: Vec<&str> = a
            .parts
            .iter()
            .filter_map(|p| match &p.content {
                PartContent::Text(t) => Some(t.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(texts, vec!["ONE", "TWO"]);
    }

    #[test]
    fn apply_artifact_redactions_no_op_on_split_mismatch() {
        let mut a = artifact(vec![text_part("one"), text_part("two")]);
        assert!(!apply_artifact_redactions(&mut a, "single"));
        assert!(matches!(&a.parts[0].content, PartContent::Text(t) if t == "one"));
    }

    #[test]
    fn apply_task_output_redactions_overwrites_artifacts_then_status() {
        let mut t = task_with(
            vec![artifact(vec![text_part("art-a")])],
            Some(msg(vec![text_part("status-msg")])),
        );
        assert!(apply_task_output_redactions(&mut t, "ART-A\nSTATUS-MSG"));
        assert!(matches!(&t.artifacts[0].parts[0].content, PartContent::Text(x) if x == "ART-A"));
        let sm = t.status.message.unwrap();
        assert!(matches!(&sm.parts[0].content, PartContent::Text(x) if x == "STATUS-MSG"));
    }

    #[test]
    fn apply_task_output_redactions_no_op_on_split_mismatch() {
        let mut t = task_with(
            vec![artifact(vec![text_part("art-a")])],
            Some(msg(vec![text_part("status-msg")])),
        );
        // One redacted line vs. two text slots → no-op.
        assert!(!apply_task_output_redactions(&mut t, "only-one"));
        assert!(matches!(&t.artifacts[0].parts[0].content, PartContent::Text(x) if x == "art-a"));
    }

    #[test]
    fn extract_outbound_text_walks_nested_json() {
        let v = json!({
            "message": {
                "parts": [{ "text": "hello" }, { "text": "world" }],
                "metadata": { "user_note": "extra" }
            },
            "ignored_int": 42,
            "ignored_bool": true,
        });
        let out = extract_outbound_text(&v);
        assert!(out.contains("hello"));
        assert!(out.contains("world"));
        assert!(out.contains("extra"));
    }

    #[test]
    fn extract_outbound_text_empty_for_no_strings() {
        let v = json!({"a": 1, "b": [2, 3], "c": null});
        assert_eq!(extract_outbound_text(&v), "");
    }

    // ── apply_message_redactions ────────────────────────────────────

    #[test]
    fn apply_message_redactions_overwrites_text_parts_in_order() {
        let mut m = msg(vec![
            text_part("alpha"),
            text_part("beta"),
            text_part("gamma"),
        ]);
        let redacted = "ALPHA\nBETA\nGAMMA";
        assert!(apply_message_redactions(&mut m, redacted));
        let texts: Vec<&str> = m
            .parts
            .iter()
            .filter_map(|p| match &p.content {
                PartContent::Text(t) => Some(t.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(texts, vec!["ALPHA", "BETA", "GAMMA"]);
    }

    #[test]
    fn apply_message_redactions_skips_non_text_parts() {
        // Mixed parts: text, url, text. The url should not be
        // overwritten — only the two text parts.
        let mut m = msg(vec![
            text_part("first"),
            Part {
                content: PartContent::Url("https://example.test".into()),
                metadata: None,
                filename: None,
                media_type: None,
            },
            text_part("second"),
        ]);
        assert!(apply_message_redactions(&mut m, "FIRST\nSECOND"));
        assert!(matches!(&m.parts[0].content, PartContent::Text(t) if t == "FIRST"));
        assert!(matches!(&m.parts[1].content, PartContent::Url(u) if u == "https://example.test"));
        assert!(matches!(&m.parts[2].content, PartContent::Text(t) if t == "SECOND"));
    }

    #[test]
    fn apply_message_redactions_no_op_on_split_mismatch() {
        let mut m = msg(vec![text_part("a"), text_part("b")]);
        // Only one part in the redacted string vs. two text parts.
        assert!(!apply_message_redactions(&mut m, "single-line"));
        // Original parts unchanged.
        assert!(matches!(&m.parts[0].content, PartContent::Text(t) if t == "a"));
        assert!(matches!(&m.parts[1].content, PartContent::Text(t) if t == "b"));
    }

    // ── apply_outbound_redactions ───────────────────────────────────

    #[test]
    fn apply_outbound_redactions_replaces_strings_in_traversal_order() {
        // serde_json without the `preserve_order` feature iterates
        // object keys in BTreeMap (alphabetical) order. Walk order
        // for the payload below:
        //   1. nested.list[0]   ("third")
        //   2. nested.list[1]   ("fourth")
        //   3. nested.second    ("second-val")
        //   4. outer            ("first")
        // The detector sees them concatenated in that order; the
        // redacted string must mirror it.
        let mut v = json!({
            "outer": "first",
            "nested": {
                "second": "second-val",
                "list": ["third", "fourth"]
            }
        });
        let original_text = extract_outbound_text(&v);
        // Sanity: pin the traversal order so an accidental switch
        // to `preserve_order` becomes a loud test failure rather
        // than a silent semantics drift.
        assert_eq!(original_text, "third\nfourth\nsecond-val\nfirst");

        let redacted = "THIRD\nFOURTH\nSECOND-VAL\nFIRST";
        assert!(apply_outbound_redactions(&mut v, redacted));
        assert_eq!(v["outer"], "FIRST");
        assert_eq!(v["nested"]["second"], "SECOND-VAL");
        assert_eq!(v["nested"]["list"][0], "THIRD");
        assert_eq!(v["nested"]["list"][1], "FOURTH");
    }

    #[test]
    fn apply_outbound_redactions_no_op_on_split_mismatch() {
        let mut v = json!({"a": "x", "b": "y"});
        assert!(!apply_outbound_redactions(&mut v, "only-one"));
        assert_eq!(v["a"], "x");
        assert_eq!(v["b"], "y");
    }

    #[test]
    fn apply_outbound_redactions_no_op_on_empty_payload() {
        let mut v = json!({"a": 1, "b": null});
        assert!(!apply_outbound_redactions(&mut v, "anything"));
    }
}
