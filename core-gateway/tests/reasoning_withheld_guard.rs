// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Guard: model reasoning does not carry past an output policy.
//!
//! The bug class: a surface runs the `chat_output` checks on the answer text
//! and forwards the model's reasoning beside it unscanned, so a value a policy
//! blocks or redacts in the answer goes out in the reasoning. The chat route
//! can have it for `reasoning_content`; the Responses API
//! (`reasoning` items and `response.reasoning_*` events) and the native
//! `/v1/messages` passthrough (`thinking` blocks, `thinking_delta`) can too.
//!
//! Every non-test function in the LLM surfaces that runs a `chat_output`
//! check must also handle reasoning (one of `HANDLERS`), or be listed in
//! `EXEMPT` with the reason its response cannot carry reasoning unscanned.
//! Behaviour is pinned by `tests/integration/reasoning_withheld.rs` and the
//! `/responses` tests in `tests/integration/llm_proxy_http.rs`.

#[path = "support/roots.rs"]
mod roots;

use std::fs;

/// Is `file` one this run has? A file of the Enterprise workspace is not, in
/// a Community tree alone (`tests/support/roots.rs`); what this guard holds
/// about it is held where the workspace is.
fn here(file: &str) -> bool {
    !file.starts_with("enterprise/") || roots::tree("enterprise").is_some()
}

const FILES: [&str; 5] = [
    "src/routes/llm.rs",
    "src/routes/anthropic.rs",
    "src/routes/gemini.rs",
    "enterprise/src/batch/routes.rs",
    "src/services/anthropic_adapter/mod.rs",
];

const OUTPUT_CHECK: &str = "guardrail_surface::CHAT_OUTPUT";

const HANDLERS: [&str; 6] = [
    "withhold_unscanned_reasoning(",
    "reasoning_must_be_withheld(",
    "is_responses_reasoning_event",
    "blank_native_thinking(",
    // Chat chunks: `StreamGuard::process` strips reasoning when sync.
    "stream_guard.process(",
    // Native Anthropic events: drops readable `thinking_delta` when sync.
    "redact_and_emit_anthropic_event(",
];

/// `(file, function, why its response cannot carry reasoning unscanned)`.
const EXEMPT: [(&str, &str, &str); 3] = [
    (
        "src/routes/llm.rs",
        "completions",
        "text completions (`choices[].text`) have no reasoning field",
    ),
    (
        "src/routes/gemini.rs",
        "generate_content",
        "Gemini thought parts are `parts[].text` and are scanned with the answer",
    ),
    (
        "src/routes/gemini.rs",
        "stream_generate_content",
        "Gemini thought parts are `parts[].text` and are scanned with the answer",
    ),
];

/// `(name, body)` of each top-level function before the first test module,
/// without line comments.
fn functions(src: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    let lines: Vec<&str> = src.lines().collect();
    for (i, line) in lines.iter().copied().enumerate() {
        let trimmed = line.trim_start();
        if line.starts_with("#[cfg(test)]")
            && lines
                .get(i + 1)
                .is_some_and(|next| next.trim_start().starts_with("mod "))
        {
            break;
        }
        if trimmed.starts_with("//") {
            continue;
        }
        let top_level = !line.starts_with(' ') && !line.starts_with('\t');
        if top_level && let Some(at) = line.find("fn ") {
            let prefix = &line[..at];
            if prefix
                .split_whitespace()
                .all(|w| w == "pub" || w.starts_with("pub(") || w == "async")
            {
                let name: String = line[at + 3..]
                    .chars()
                    .take_while(|c| c.is_alphanumeric() || *c == '_')
                    .collect();
                out.push((name, String::new()));
                continue;
            }
        }
        if let Some((_, body)) = out.last_mut() {
            body.push_str(line);
            body.push('\n');
        }
    }
    out
}

/// Functions that run a `chat_output` check without handling reasoning and
/// are not exempt.
fn violations(file: &str, src: &str) -> Vec<String> {
    functions(src)
        .into_iter()
        .filter(|(_, body)| body.contains(OUTPUT_CHECK))
        .filter(|(_, body)| !HANDLERS.iter().any(|h| body.contains(h)))
        .filter(|(name, _)| !EXEMPT.iter().any(|(f, n, _)| *f == file && n == name))
        .map(|(name, _)| format!("{file}::{name}"))
        .collect()
}

#[test]
fn every_output_checked_surface_handles_reasoning() {
    let mut found = Vec::new();
    for file in FILES.into_iter().filter(|f| here(f)) {
        let src = fs::read_to_string(file).unwrap_or_else(|e| panic!("{file}: {e}"));
        found.extend(violations(file, &src));
    }
    assert!(
        found.is_empty(),
        "these run a chat_output check but forward model reasoning unscanned; \
         withhold it (llm_preflight::withhold_unscanned_reasoning, or the stream \
         equivalents) or add an EXEMPT entry saying why it cannot carry any:\n{}",
        found.join("\n")
    );
}

#[test]
fn every_exemption_still_names_a_function_that_checks_output() {
    for (file, name, why) in EXEMPT {
        assert!(!why.is_empty());
        let src = fs::read_to_string(file).unwrap();
        assert!(
            functions(&src)
                .iter()
                .any(|(n, body)| n == name && body.contains(OUTPUT_CHECK)),
            "stale exemption {file}::{name}"
        );
    }
}

/// The shapes that shipped: the Responses handler scanned `output_text` and
/// returned the body with its `reasoning` items; a handler that withholds
/// passes.
#[test]
fn the_rule_catches_the_shape_that_shipped() {
    let leaking = r#"
pub(crate) async fn responses(state: S) -> Response {
    let r = apply_output_guardrails(&state, &text,
        crate::constants::guardrail_surface::CHAT_OUTPUT, None).await;
    Json(resp_body).into_response()
}
"#;
    assert_eq!(violations("x.rs", leaking), vec!["x.rs::responses"]);

    let fixed = leaking.replace(
        "    Json(resp_body)",
        "    llm_preflight::withhold_unscanned_reasoning(&state, t, a, &mut resp_body).await?;\n    Json(resp_body)",
    );
    assert!(violations("x.rs", &fixed).is_empty());

    // A comment naming the handler is not a handler.
    let commented = leaking.replace(
        "    Json(resp_body)",
        "    // withhold_unscanned_reasoning( later\n    Json(resp_body)",
    );
    assert_eq!(violations("x.rs", &commented), vec!["x.rs::responses"]);
}
