// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! The A2A task state a caller received.
//!
//! `proxy_logs.a2a_task_state` is written by the gateway on every
//! OUTBOUND A2A row whose reply named a task state. It is the single source
//! for everything downstream that asks "what did the delegate answer?": the
//! run ledger's segment outcome ([`crate::services::run_segments`]) and the
//! control plane's boundary metrics for external dependencies. Neither
//! re-parses response bodies any more — a body can be pruned, truncated or
//! reduced to metadata by retention, but the column is decided once, from
//! the bytes the caller actually received, before any of that happens.
//!
//! ## Normalisation
//!
//! Every dialect is folded to one vocabulary (the column's contract):
//! the v1.0 `TASK_STATE_` prefix is dropped, the value is lower-cased and
//! `-` becomes `_` (v0.3 `input-required` → `input_required`). British
//! `cancelled` is read as `canceled`. Anything else is not a task state and
//! yields `None` — never a guess.
//!
//! ## Shapes read
//!
//! A reply is one JSON document or an SSE stream of them:
//!
//! | shape                                   | path                               |
//! |-----------------------------------------|------------------------------------|
//! | bare Task (v0.3 REST / relay)           | `status.state`                     |
//! | v1.0 HTTP+JSON `SendMessageResponse`    | `task.status.state`                |
//! | JSON-RPC result = Task (v0.3)           | `result.status.state`              |
//! | JSON-RPC result = `SendMessageResponse` | `result.task.status.state`         |
//! | v1.0 stream frame (`StreamResponse`)    | `statusUpdate.status.state`        |
//! | JSON-RPC stream frame                   | `result.statusUpdate.status.state` |
//!
//! A v0.3 `status-update` stream frame is `result.status.state` already.
//! For a stream the state is the LAST one any frame carried — the final
//! status the caller was left with. A plain message reply names no task, and
//! so no state.

use serde_json::Value;

/// The column's vocabulary. Every value fits `String(32)`.
pub const TASK_STATES: [&str; 9] = [
    "submitted",
    "working",
    "completed",
    "failed",
    "canceled",
    "rejected",
    "input_required",
    "auth_required",
    "unspecified",
];

/// Where each dialect puts the state, tried in order.
const STATE_PATHS: [&[&str]; 6] = [
    &["status", "state"],
    &["task", "status", "state"],
    &["result", "status", "state"],
    &["result", "task", "status", "state"],
    &["statusUpdate", "status", "state"],
    &["result", "statusUpdate", "status", "state"],
];

/// Normalise an A2A task state across dialects: v1.0
/// `TASK_STATE_INPUT_REQUIRED` and v0.3 `input-required` both become
/// `input_required`. Unknown → `None`.
pub fn normalize(raw: &str) -> Option<String> {
    let s = raw.trim().to_ascii_lowercase().replace('-', "_");
    let s = s.strip_prefix("task_state_").unwrap_or(&s).to_string();
    let s = if s == "cancelled" {
        "canceled".to_string()
    } else {
        s
    };
    TASK_STATES.contains(&s.as_str()).then_some(s)
}

/// The state one JSON document names, wherever its dialect puts it.
pub fn of_document(v: &Value) -> Option<String> {
    STATE_PATHS.iter().find_map(|path| {
        path.iter()
            .try_fold(v, |node, key| node.get(*key))
            .and_then(Value::as_str)
            .and_then(normalize)
    })
}

/// The task state a reply handed the caller: one JSON document, or the last
/// state any frame of an SSE stream carried. Bytes that are neither say
/// nothing.
pub fn from_reply(body: &[u8]) -> Option<String> {
    if let Ok(v) = serde_json::from_slice::<Value>(body) {
        return of_document(&v);
    }
    let text = std::str::from_utf8(body).ok()?;
    sse_events(text)
        .filter_map(|data| serde_json::from_str::<Value>(&data).ok())
        .filter_map(|v| of_document(&v))
        .last()
}

/// The `data` payload of each SSE event (multi-line `data:` fields joined by
/// `\n`, per the SSE spec). Other fields (`event:`, `id:`, comments) are
/// ignored.
fn sse_events(text: &str) -> impl Iterator<Item = String> + '_ {
    let mut out: Vec<String> = Vec::new();
    let mut current: Option<String> = None;
    for line in text.lines() {
        let line = line.strip_suffix('\r').unwrap_or(line);
        if line.is_empty() {
            if let Some(d) = current.take() {
                out.push(d);
            }
            continue;
        }
        if let Some(rest) = line.strip_prefix("data:") {
            let rest = rest.strip_prefix(' ').unwrap_or(rest);
            match current.as_mut() {
                Some(d) => {
                    d.push('\n');
                    d.push_str(rest);
                }
                None => current = Some(rest.to_string()),
            }
        }
    }
    if let Some(d) = current {
        out.push(d);
    }
    out.into_iter()
}

// ---------------------------------------------------------------------------
// The delegate's declared outcome on a reply
// ---------------------------------------------------------------------------

/// Where a reply can carry a Task (or message) whose `metadata` holds the
/// delegate's outcome, tried in order. The bare document (`[]`) is a v0.3
/// Task; the rest mirror [`STATE_PATHS`] plus plain message replies.
const CARRIER_PATHS: [&[&str]; 8] = [
    &["task"],
    &["result", "task"],
    &["statusUpdate"],
    &["result", "statusUpdate"],
    &["result"],
    &[],
    &["message"],
    &["result", "message"],
];

fn at_path<'v>(v: &'v Value, path: &[&str]) -> Option<&'v Value> {
    path.iter().try_fold(v, |node, key| node.get(*key))
}

fn carried_outcome(v: &Value) -> Option<String> {
    CARRIER_PATHS.iter().find_map(|p| {
        at_path(v, p)?
            .get("metadata")?
            .get(crate::services::run_outcome::METADATA_OUTCOME_ID)?
            .as_str()
            .and_then(crate::services::run_outcome::stored)
    })
}

/// The outcome a delegate reported in its reply's final Task metadata
/// (`metadata["brutor.outcome.id"]`): one JSON document, or the LAST frame
/// of an SSE stream that carried one. Stored form (verbatim, truncated);
/// validated by the caller against the delegate's contract.
pub fn outcome_from_reply(body: &[u8]) -> Option<String> {
    if let Ok(v) = serde_json::from_slice::<Value>(body) {
        return carried_outcome(&v);
    }
    let text = std::str::from_utf8(body).ok()?;
    sse_events(text)
        .filter_map(|data| serde_json::from_str::<Value>(&data).ok())
        .filter_map(|v| carried_outcome(&v))
        .last()
}

/// Write the gateway's checked outcome into one document's Task metadata.
/// The target is the carrier that already holds an outcome, else the first
/// object carrier that looks like a Task (`status` or `id`). `false` when
/// the document has no carrier (nothing to annotate).
fn annotate_document(v: &mut Value, outcome: Option<&str>, conformance: &str) -> bool {
    use crate::services::run_outcome::{METADATA_OUTCOME_CONFORMANCE, METADATA_OUTCOME_ID};
    let path = CARRIER_PATHS
        .iter()
        .find(|p| {
            at_path(v, p)
                .and_then(|n| n.get("metadata"))
                .and_then(|m| m.get(METADATA_OUTCOME_ID))
                .is_some()
        })
        .or_else(|| {
            CARRIER_PATHS.iter().find(|p| {
                at_path(v, p).is_some_and(|n| {
                    n.is_object() && (n.get("status").is_some() || n.get("id").is_some())
                })
            })
        });
    let Some(path) = path else {
        return false;
    };
    let mut node = v;
    for key in path.iter() {
        match node.get_mut(*key) {
            Some(n) => node = n,
            None => return false,
        }
    }
    let Some(obj) = node.as_object_mut() else {
        return false;
    };
    let meta = obj
        .entry("metadata")
        .or_insert_with(|| Value::Object(Default::default()));
    if !meta.is_object() {
        *meta = Value::Object(Default::default());
    }
    if let Some(m) = meta.as_object_mut() {
        match outcome {
            Some(o) => m.insert(METADATA_OUTCOME_ID.into(), Value::String(o.into())),
            None => m.remove(METADATA_OUTCOME_ID),
        };
        m.insert(
            METADATA_OUTCOME_CONFORMANCE.into(),
            Value::String(conformance.into()),
        );
    }
    true
}

/// The reply with the gateway's checked outcome pair written into its final
/// Task metadata (`brutor.outcome.id` as reported, `brutor.outcome.conformance`
/// as judged), so a caller reading either carrier — headers or metadata —
/// reads the same checked value. For a stream, the last frame that carries a
/// Task is rewritten. `None` when the reply has nowhere to carry it (a plain
/// body, or no Task): the response headers still do.
pub fn annotate_reply(body: &[u8], outcome: Option<&str>, conformance: &str) -> Option<Vec<u8>> {
    if let Ok(mut v) = serde_json::from_slice::<Value>(body) {
        return annotate_document(&mut v, outcome, conformance)
            .then(|| serde_json::to_vec(&v).ok())
            .flatten();
    }
    let text = std::str::from_utf8(body).ok()?;
    let sep = if text.contains("\r\n\r\n") {
        "\r\n\r\n"
    } else {
        "\n\n"
    };
    let nl = if sep == "\r\n\r\n" { "\r\n" } else { "\n" };
    let mut blocks: Vec<String> = text.split(sep).map(str::to_string).collect();
    for block in blocks.iter_mut().rev() {
        let lines: Vec<&str> = block.split(nl).collect();
        let data: Vec<&str> = lines
            .iter()
            .filter_map(|l| {
                l.strip_prefix("data:")
                    .map(|d| d.strip_prefix(' ').unwrap_or(d))
            })
            .collect();
        if data.is_empty() {
            continue;
        }
        let Ok(mut v) = serde_json::from_str::<Value>(&data.join("\n")) else {
            continue;
        };
        if !annotate_document(&mut v, outcome, conformance) {
            continue;
        }
        let json = serde_json::to_string(&v).ok()?;
        let mut rebuilt: Vec<String> = lines
            .iter()
            .filter(|l| !l.starts_with("data:"))
            .map(|l| l.to_string())
            .collect();
        rebuilt.push(format!("data: {json}"));
        *block = rebuilt.join(nl);
        return Some(blocks.join(sep).into_bytes());
    }
    None
}

/// Whether a stored reply is a JSON-RPC error envelope. Not a task state —
/// the one fact about the reply the segment outcome still reads from the
/// body (a 2xx carrying a JSON-RPC `error` is not an answer).
pub fn is_jsonrpc_error(body: Option<&str>) -> bool {
    body.and_then(|b| serde_json::from_str::<Value>(b).ok())
        .is_some_and(|v| v.get("error").is_some_and(Value::is_object))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state(body: &str) -> Option<String> {
        from_reply(body.as_bytes())
    }

    #[test]
    fn states_normalise_across_dialects() {
        assert_eq!(
            normalize("TASK_STATE_INPUT_REQUIRED").as_deref(),
            Some("input_required")
        );
        assert_eq!(
            normalize("input-required").as_deref(),
            Some("input_required")
        );
        assert_eq!(normalize("completed").as_deref(), Some("completed"));
        assert_eq!(normalize("cancelled").as_deref(), Some("canceled"));
        assert_eq!(
            normalize("TASK_STATE_AUTH_REQUIRED").as_deref(),
            Some("auth_required")
        );
        assert_eq!(normalize("mystery"), None);
        for s in TASK_STATES {
            assert!(s.len() <= 32, "{s} does not fit proxy_logs.a2a_task_state");
        }
    }

    #[test]
    fn every_documented_shape_yields_its_state() {
        // bare Task
        assert_eq!(
            state(r#"{"id":"t","status":{"state":"completed"}}"#).as_deref(),
            Some("completed")
        );
        // v1.0 HTTP+JSON SendMessageResponse
        assert_eq!(
            state(r#"{"task":{"id":"t","status":{"state":"TASK_STATE_COMPLETED"}}}"#).as_deref(),
            Some("completed")
        );
        // JSON-RPC result = Task (v0.3)
        assert_eq!(
            state(r#"{"jsonrpc":"2.0","id":1,"result":{"status":{"state":"failed"}}}"#).as_deref(),
            Some("failed")
        );
        // JSON-RPC result = SendMessageResponse (v1.0)
        assert_eq!(
            state(
                r#"{"jsonrpc":"2.0","id":1,"result":{"task":{"status":{"state":"auth-required"}}}}"#
            )
            .as_deref(),
            Some("auth_required")
        );
        // v1.0 stream frame, as one document
        assert_eq!(
            state(r#"{"statusUpdate":{"taskId":"t","contextId":"c","status":{"state":"TASK_STATE_WORKING"},"final":false}}"#)
                .as_deref(),
            Some("working")
        );
        // JSON-RPC stream frame
        assert_eq!(
            state(r#"{"jsonrpc":"2.0","id":1,"result":{"statusUpdate":{"status":{"state":"TASK_STATE_REJECTED"}}}}"#)
                .as_deref(),
            Some("rejected")
        );
    }

    #[test]
    fn a_stream_yields_its_final_state() {
        let sse = "event: message\n\
                   data: {\"task\":{\"id\":\"t\",\"status\":{\"state\":\"TASK_STATE_SUBMITTED\"}}}\n\n\
                   data: {\"statusUpdate\":{\"status\":{\"state\":\"TASK_STATE_WORKING\"}}}\n\n\
                   data: {\"artifactUpdate\":{\"taskId\":\"t\"}}\n\n\
                   data: {\"statusUpdate\":{\"status\":{\"state\":\"TASK_STATE_COMPLETED\"},\"final\":true}}\n\n";
        assert_eq!(state(sse).as_deref(), Some("completed"));
        // CRLF framing and a JSON-RPC-wrapped v0.3 status-update.
        let rpc = "data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"kind\":\"status-update\",\"status\":{\"state\":\"working\"}}}\r\n\r\n\
                   data: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"kind\":\"status-update\",\"status\":{\"state\":\"input-required\"},\"final\":true}}\r\n\r\n";
        assert_eq!(state(rpc).as_deref(), Some("input_required"));
        // A data field split across lines is one event.
        let split = "data: {\"status\":\ndata: {\"state\":\"failed\"}}\n\n";
        assert_eq!(state(split).as_deref(), Some("failed"));
    }

    #[test]
    fn a_reply_without_a_state_says_nothing() {
        assert_eq!(state(""), None);
        assert_eq!(state("{\"status\":"), None);
        assert_eq!(state("<4 bytes of non-utf8 body>"), None);
        assert_eq!(from_reply(&[0xff, 0xfe, 0x00]), None);
        // A plain message reply (no task) is an answer with no state.
        assert_eq!(state(r#"{"message":{"role":"agent","parts":[]}}"#), None);
        assert_eq!(state(r#"{"task":{"status":{"state":"mystery"}}}"#), None);
        assert_eq!(state("data: {\"message\":{}}\n\n"), None);
    }

    // -- the delegate's outcome on a reply ------------------------

    #[test]
    fn the_outcome_is_read_from_every_task_carrier() {
        for body in [
            r#"{"id":"t","status":{"state":"completed"},"metadata":{"brutor.outcome.id":"screening_completed.hit"}}"#,
            r#"{"task":{"status":{"state":"TASK_STATE_COMPLETED"},"metadata":{"brutor.outcome.id":"screening_completed.hit"}}}"#,
            r#"{"jsonrpc":"2.0","id":1,"result":{"status":{"state":"completed"},"metadata":{"brutor.outcome.id":"screening_completed.hit"}}}"#,
            r#"{"jsonrpc":"2.0","id":1,"result":{"task":{"metadata":{"brutor.outcome.id":"screening_completed.hit"}}}}"#,
        ] {
            assert_eq!(
                outcome_from_reply(body.as_bytes()).as_deref(),
                Some("screening_completed.hit"),
                "{body}"
            );
        }
        assert_eq!(outcome_from_reply(br#"{"task":{"status":{}}}"#), None);
        assert_eq!(outcome_from_reply(b"not json"), None);
    }

    #[test]
    fn a_stream_yields_the_outcome_of_its_final_task() {
        let sse = "data: {\"task\":{\"id\":\"t\",\"status\":{\"state\":\"TASK_STATE_WORKING\"}}}\n\n\
                   data: {\"statusUpdate\":{\"status\":{\"state\":\"TASK_STATE_COMPLETED\"},\"final\":true,\"metadata\":{\"brutor.outcome.id\":\"screening_completed.clear\"}}}\n\n";
        assert_eq!(
            outcome_from_reply(sse.as_bytes()).as_deref(),
            Some("screening_completed.clear")
        );
    }

    #[test]
    fn the_checked_pair_is_written_where_the_caller_reads_it() {
        let body = br#"{"task":{"id":"t","status":{"state":"TASK_STATE_COMPLETED"},"metadata":{"brutor.outcome.id":"screening_completed.maybe","x":1}}}"#;
        let out = annotate_reply(body, Some("screening_completed.maybe"), "undeclared").unwrap();
        let v: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(
            v["task"]["metadata"]["brutor.outcome.conformance"],
            "undeclared"
        );
        assert_eq!(
            v["task"]["metadata"]["brutor.outcome.id"],
            "screening_completed.maybe"
        );
        assert_eq!(
            v["task"]["metadata"]["x"], 1,
            "the delegate's other metadata is kept"
        );
        // A Task with no metadata gains it; an unreported outcome carries no id.
        let out = annotate_reply(
            br#"{"id":"t","status":{"state":"completed"}}"#,
            None,
            "unreported",
        )
        .unwrap();
        let v: Value = serde_json::from_slice(&out).unwrap();
        assert_eq!(v["metadata"]["brutor.outcome.conformance"], "unreported");
        assert!(v["metadata"].get("brutor.outcome.id").is_none());
        // Nothing to carry it: headers only.
        assert_eq!(annotate_reply(br#"{"ok":true}"#, None, "unreported"), None);
        assert_eq!(annotate_reply(b"plain text", None, "unreported"), None);
    }

    #[test]
    fn a_stream_is_annotated_on_its_final_task_frame() {
        let sse = "event: message\ndata: {\"task\":{\"id\":\"t\",\"status\":{\"state\":\"TASK_STATE_WORKING\"}}}\n\n\
                   data: {\"statusUpdate\":{\"status\":{\"state\":\"TASK_STATE_COMPLETED\"},\"metadata\":{\"brutor.outcome.id\":\"screening_completed.clear\"}}}\n\n";
        let out = annotate_reply(
            sse.as_bytes(),
            Some("screening_completed.clear"),
            "declared",
        )
        .unwrap();
        let text = String::from_utf8(out).unwrap();
        let frames: Vec<String> = sse_events(&text).collect();
        assert_eq!(frames.len(), 2);
        let last: Value = serde_json::from_str(&frames[1]).unwrap();
        assert_eq!(
            last["statusUpdate"]["metadata"]["brutor.outcome.conformance"],
            "declared"
        );
        let first: Value = serde_json::from_str(&frames[0]).unwrap();
        assert!(
            first["task"].get("metadata").is_none(),
            "earlier frames untouched"
        );
        assert!(text.starts_with("event: message\n"), "non-data fields kept");
        // The task state is still read the same way.
        assert_eq!(from_reply(text.as_bytes()).as_deref(), Some("completed"));
    }

    #[test]
    fn a_jsonrpc_error_is_recognised_and_is_not_a_state() {
        let err = r#"{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"x"}}"#;
        assert!(is_jsonrpc_error(Some(err)));
        assert_eq!(state(err), None);
        assert!(!is_jsonrpc_error(Some(
            r#"{"error":"a string is not an envelope"}"#
        )));
        assert!(!is_jsonrpc_error(Some("not json")));
        assert!(!is_jsonrpc_error(None));
    }
}
