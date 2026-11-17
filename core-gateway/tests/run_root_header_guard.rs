// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every LLM response returns the run it belongs to (`x-brutor-run-id`).
//!
//! An agent learns its run root only from this header, and needs it to close
//! the run (`POST /v1/runs/{root}/end`) or attribute feedback. It was set by
//! hand at each response site, and four sites forgot: native Anthropic
//! `/v1/messages` (both paths), the Responses API streaming path (both) and
//! embeddings. The class: a response that is given a
//! correlation id but not its run root. This scan pins every LLM surface.

use std::fs;

/// The LLM surfaces that build responses.
const SURFACES: &[&str] = &["src/routes/llm.rs", "src/routes/anthropic.rs"];

/// How far after the correlation header the run root must be stamped.
const WINDOW: usize = 12;

/// Line numbers (1-based) of correlation-header inserts with no run-root
/// stamp within the next `WINDOW` lines.
fn unstamped(src: &str) -> Vec<usize> {
    let lines: Vec<&str> = src.lines().collect();
    lines
        .iter()
        .enumerate()
        .filter(|(_, l)| l.contains("headers::X_CORRELATION_ID"))
        .filter(|(i, _)| {
            !lines[*i..(*i + WINDOW).min(lines.len())]
                .iter()
                .any(|l| l.contains("stamp_run_root(") || l.contains("headers::X_BRUTOR_RUN_ID"))
        })
        .map(|(i, _)| i + 1)
        .collect()
}

#[test]
fn every_llm_response_with_a_correlation_id_returns_its_run_root() {
    for path in SURFACES {
        let src = fs::read_to_string(path).unwrap_or_else(|e| panic!("{path}: {e}"));
        let src = src.split("#[cfg(test)]").next().unwrap_or(&src);
        let missing = unstamped(src);
        assert!(
            missing.is_empty(),
            "{path}: response(s) at line(s) {missing:?} set X_CORRELATION_ID but not the run \
             root — call a2a_delegation::stamp_run_root(response.headers_mut(), &root)"
        );
    }
}

#[test]
fn the_scan_catches_the_shape_it_forbids() {
    let forgot = "let mut response = body.into_response();\n\
                  response.headers_mut().insert(\n    crate::constants::headers::X_CORRELATION_ID,\n    id,\n);\n\
                  response\n";
    assert_eq!(unstamped(forgot), vec![3]);
    let stamped = format!(
        "{forgot}crate::services::a2a_delegation::stamp_run_root(response.headers_mut(), &root);\n"
    );
    assert!(unstamped(&stamped).is_empty());
}
