// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: the tool calls a model asks for are judged against the run's data
//! scope before the answer goes back.
//!
//! The bug class: an agent runs a tool itself (a local function, a direct
//! REST call), so the only call the gateway sees is the model asking for it,
//! and the answer carrying that call reaches the agent unjudged.
//! The data scope is checked on MCP,
//! skills and A2A, and on a model's tool calls.
//!
//! One function judges them: `llm_preflight::apply_response_governance`,
//! which every LLM surface already calls with its tool calls in the chat
//! shape. The rules:
//!
//! 1. It calls `judge_model_tool_calls(` before the LLM governance lookup,
//!    whose "no governance configured" early return would otherwise skip it.
//! 2. Each LLM surface file keeps at least its known number of calls into it
//!    (one per response path: buffered and streamed). A path that stops
//!    calling it returns tool calls unjudged.
//! 3. A run's declared scope is stored in `run_preflight` after
//!    authentication, which every LLM surface calls, and not by a route
//!    before it: an unauthenticated request must not bind a run, and a
//!    surface that forgot to store it left an agent that only talks to a
//!    model `unbound`.
//! 4. Every streamed surface holds the end of a turn that asks for tools
//!    until the check has run. A client runs a tool call when the turn ends
//!    (or, on Gemini and the Responses API, as soon as it has the call); a
//!    refusal sent after that arrives too late. Every stream holds: the
//!    chat stream and the native Claude and Gemini streams judged after
//!    the turn had ended, and the Responses stream never judged at all.
//!
//! Behaviour is pinned by `tests/integration/data_scope_model_tool_calls.rs`.

use std::fs;

const PREFLIGHT: &str = "src/services/llm_preflight.rs";

/// `(file, calls to apply_response_governance it must keep, its paths)`.
const SURFACES: [(&str, usize, &str); 4] = [
    (
        "src/routes/llm.rs",
        4,
        "chat buffered, Responses buffered, chat / completions streamed, Responses streamed",
    ),
    ("src/routes/anthropic.rs", 1, "/v1/messages translated"),
    (
        "src/services/anthropic_adapter/mod.rs",
        2,
        "native /v1/messages buffered and streamed (Anthropic, Bedrock, Vertex)",
    ),
    (
        "src/routes/gemini.rs",
        2,
        "generateContent and streamGenerateContent",
    ),
];

/// `(file, what holds the turn's end, the stream)` — rule 4.
const HOLDS: [(&str, &str, &str); 4] = [
    (
        "src/routes/llm.rs",
        "ingest_and_maybe_hold_finish(",
        "chat / completions stream: the finish chunk",
    ),
    (
        "src/routes/llm.rs",
        "sink.hold()",
        "Responses stream: from the first function-call event",
    ),
    (
        "src/services/anthropic_adapter/mod.rs",
        "let out = if holding { &held_tx } else { &tx };",
        "native /v1/messages stream: message_delta / message_stop",
    ),
    (
        "src/routes/gemini.rs",
        "held_events.push(",
        "Gemini stream: from the chunk carrying the functionCall",
    ),
];

/// Routes that call `run_preflight`; none may store a declaration itself.
const ROUTES: [&str; 3] = [
    "src/routes/llm.rs",
    "src/routes/anthropic.rs",
    "src/routes/gemini.rs",
];

/// `src` without `//` comment lines.
fn code(src: &str) -> String {
    src.lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n")
}

/// The body of `fn name` in `src` (from its signature to the next top-level
/// `fn`), comments removed.
fn body_of(src: &str, name: &str) -> String {
    let src = code(src);
    let start = src
        .find(&format!("fn {name}("))
        .unwrap_or_else(|| panic!("fn {name} not found"));
    let rest = &src[start + 3..];
    let end = rest
        .find("\npub async fn ")
        .into_iter()
        .chain(rest.find("\nasync fn "))
        .chain(rest.find("\npub fn "))
        .chain(rest.find("\nfn "))
        .min()
        .unwrap_or(rest.len());
    rest[..end].to_string()
}

/// Rule 1 on one body.
fn judges_before_governance(body: &str) -> Result<(), String> {
    let judge = body
        .find("judge_model_tool_calls(")
        .ok_or("apply_response_governance never judges the model's tool calls")?;
    if let Some(gov) = body.find("collect_effective_llm_governance(")
        && gov < judge
    {
        return Err("the data-scope check runs after the governance lookup, \
                    whose early return skips it"
            .into());
    }
    Ok(())
}

/// Rule 3 on `run_preflight`'s body.
fn captures_after_authentication(body: &str) -> Result<(), String> {
    let auth = body
        .find("authenticate_request(")
        .ok_or("run_preflight no longer authenticates")?;
    let capture = body
        .find("data_scope::capture(")
        .ok_or("run_preflight does not store the run's declared scope")?;
    if capture < auth {
        return Err("the declared scope is stored before authentication".into());
    }
    Ok(())
}

#[test]
fn response_governance_judges_the_data_scope_first() {
    let src = fs::read_to_string(PREFLIGHT).unwrap();
    judges_before_governance(&body_of(&src, "apply_response_governance")).unwrap();
}

#[test]
fn every_llm_response_path_reaches_response_governance() {
    let mut short = Vec::new();
    for (file, want, paths) in SURFACES {
        let found = code(&fs::read_to_string(file).unwrap())
            .matches("apply_response_governance(")
            .count();
        if found < want {
            short.push(format!("{file}: {found} of {want} ({paths})"));
        }
    }
    assert!(
        short.is_empty(),
        "an LLM response path returns tool calls without the data-scope check \
         ; call llm_preflight::apply_response_governance:\n{}",
        short.join("\n")
    );
}

#[test]
fn every_stream_holds_the_turn_until_its_tool_calls_are_judged() {
    let mut missing = Vec::new();
    for (file, needle, stream) in HOLDS {
        if !code(&fs::read_to_string(file).unwrap()).contains(needle) {
            missing.push(format!("{file}: {stream} (`{needle}`)"));
        }
    }
    assert!(
        missing.is_empty(),
        "a stream delivers the end of a tool-call turn before the calls are \
         judged, so a refusal comes too late:\n{}",
        missing.join("\n")
    );
}

#[test]
fn the_declared_scope_is_stored_after_authentication_in_one_place() {
    let src = fs::read_to_string(PREFLIGHT).unwrap();
    captures_after_authentication(&body_of(&src, "run_preflight")).unwrap();
    for file in ROUTES {
        let src = code(&fs::read_to_string(file).unwrap());
        assert!(
            !src.contains("data_scope::capture("),
            "{file} stores a declaration itself; run_preflight stores it, after \
             authentication"
        );
    }
}

/// The shapes the rules forbid.
#[test]
fn the_rules_catch_the_shapes_that_shipped() {
    let shipped = r#"
pub async fn apply_response_governance(s: S) -> Result<(), Response> {
    let gov = match usage::collect_effective_llm_governance(&db, tg, m).await {
        Ok(Some(g)) => g,
        Ok(None) => return Ok(()),
    };
    Ok(())
}
"#;
    assert!(judges_before_governance(&body_of(shipped, "apply_response_governance")).is_err());
    let late = shipped.replace(
        "    Ok(())\n}",
        "    judge_model_tool_calls(s).await;\n    Ok(())\n}",
    );
    assert!(judges_before_governance(&body_of(&late, "apply_response_governance")).is_err());
    let fixed = shipped.replace(
        "    let gov",
        "    if let Some(r) = judge_model_tool_calls(s).await { return Err(r); }\n    let gov",
    );
    judges_before_governance(&body_of(&fixed, "apply_response_governance")).unwrap();

    let before_auth = r#"
pub async fn run_preflight(s: S) -> Result<P, Response> {
    crate::services::data_scope::capture(&db, t, &run, h).await;
    let auth_ctx = auth::authenticate_request(h, t).await?;
}
"#;
    assert!(captures_after_authentication(&body_of(before_auth, "run_preflight")).is_err());
    let after = r#"
pub async fn run_preflight(s: S) -> Result<P, Response> {
    let auth_ctx = auth::authenticate_request(h, t).await?;
    crate::services::data_scope::capture(&db, t, &run, h).await;
}
"#;
    captures_after_authentication(&body_of(after, "run_preflight")).unwrap();
}
