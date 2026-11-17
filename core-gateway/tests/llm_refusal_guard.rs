// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Every governed refusal on an LLM surface goes through the one writer.
//!
//! The embeddings governance block (dimension whitelist / batch size)
//! returned `403 policy: …` and wrote nothing: no `proxy_logs` row, no sealed
//! `denied` record, nothing on the run it stopped. It was not alone — the
//! media-governance 403s (images, speech, transcription, video), the per-mode
//! media quota 429s, the LLM parameter-governance 403 on chat / responses /
//! completions / Anthropic / Gemini and the resource-group concurrency 429
//! all refused silently. Each site built its own response and each had to
//! remember to write its own row; five forgot.
//!
//! `services::llm_refusal::refuse` is now the only way a refusal leaves an
//! LLM surface: it writes the row (subtype, run, model, group) and returns
//! the response it was handed, so the status the caller sees and the status
//! the row records cannot disagree. This guard scans the source, because the
//! failure is an absence: a new gate that returns a bare 403 compiles, passes
//! every behavioural test that checks the status, and leaves no evidence.
//!
//! The rule: in every LLM-surface file, each place that CONSTRUCTS a 403 or
//! 429 (`StatusCode::FORBIDDEN`, `StatusCode::TOO_MANY_REQUESTS`, a call to
//! the quota response builders, a concurrency `GateRejection`) must sit in a
//! statement that calls `refuse(` / `refuse_with(`. Match-arm patterns are
//! not constructions. Anything else must be named in `ALLOWED` with the
//! reason its evidence is written elsewhere — and an allowlist entry that no
//! longer matches anything fails too, so the list cannot rot.

#[path = "support/roots.rs"]
mod roots;

use std::fs;

/// Is `file` one this run has? A file of the Enterprise workspace is not, in
/// a Community tree alone (`tests/support/roots.rs`); what this guard holds
/// about it is held where the workspace is.
fn here(file: &str) -> bool {
    !file.starts_with("enterprise/") || roots::tree("enterprise").is_some()
}

/// Every file that serves LLM traffic or holds an LLM gate.
const SURFACES: &[&str] = &[
    "src/routes/llm.rs",
    "src/routes/anthropic.rs",
    "src/routes/gemini.rs",
    "src/routes/images.rs",
    "src/routes/audio_speech.rs",
    "src/routes/audio_transcribe.rs",
    "src/routes/video.rs",
    "enterprise/src/batch/routes.rs",
    "src/routes/files.rs",
    "src/routes/llm_helpers.rs",
    "src/services/llm_preflight.rs",
    "src/services/llm_refusal.rs",
    "src/services/multimodal_pipeline.rs",
    "src/services/llm_resilience.rs",
    "src/services/llm_governance.rs",
    "src/services/media_governance.rs",
    "src/services/anthropic_adapter/mod.rs",
];

/// The Enterprise workspace's API of its own: the Portal's routes and
/// session, the system-administrator check, the licence gate and the
/// composition root. A 403 there is that API's answer to its caller, not a
/// refused model call. Every OTHER file of the Enterprise workspace is on
/// the LLM path (the batch API, the response and prompt caches, the
/// knowledge base) and is scanned with [`SURFACES`], where this repository
/// carries the workspace (the guard is root-aware).
const ENTERPRISE_NOT_THE_LLM_PATH: &[&str] = &[
    "enterprise/src/routes/",
    "enterprise/src/routes.rs",
    "enterprise/src/portal.rs",
    "enterprise/src/portal_session.rs",
    "enterprise/src/system_admin.rs",
    "enterprise/src/licence.rs",
    "enterprise/src/main.rs",
    "enterprise/src/lib.rs",
];

fn enterprise_files(dir: &std::path::Path, out: &mut Vec<String>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.is_dir() {
            enterprise_files(&path, out);
        } else if path.extension().is_some_and(|e| e == "rs") {
            out.push(path.to_string_lossy().replace('\\', "/"));
        }
    }
}

/// [`SURFACES`], and the Enterprise workspace's files on the LLM path.
fn surfaces() -> Vec<String> {
    let mut all: Vec<String> = SURFACES
        .iter()
        .filter(|s| here(s))
        .map(|s| s.to_string())
        .collect();
    let mut enterprise = Vec::new();
    if let Some(root) = roots::tree("enterprise") {
        enterprise_files(&root.join("src"), &mut enterprise);
    }
    enterprise.sort();
    for file in enterprise {
        let api = ENTERPRISE_NOT_THE_LLM_PATH
            .iter()
            .any(|p| file == *p || (p.ends_with('/') && file.starts_with(p)));
        if !api && !all.contains(&file) {
            all.push(file);
        }
    }
    all
}

/// A construction that is allowed to live outside `refuse(`, and why.
struct Allowed {
    file: &'static str,
    /// The enclosing `fn`.
    func: &'static str,
    /// When set, only a statement containing this text is covered (so a
    /// large function is not exempted wholesale).
    marker: Option<&'static str>,
    /// When set, the enclosing function must also contain this — the thing
    /// that actually writes the evidence the exemption relies on.
    requires: Option<&'static str>,
    #[allow(dead_code)]
    why: &'static str,
}

const ALLOWED: &[Allowed] = &[
    // --- Response builders: they shape a body; their callers refuse. ------
    Allowed {
        file: "src/services/llm_preflight.rs",
        func: "usage_limit_response",
        marker: None,
        requires: None,
        why: "429 body builder; every call sits inside refuse()",
    },
    Allowed {
        file: "src/services/llm_preflight.rs",
        func: "anthropic_usage_limit_response",
        marker: None,
        requires: None,
        why: "Anthropic-shaped 429 body builder",
    },
    Allowed {
        file: "src/services/llm_preflight.rs",
        func: "llm_rate_limit_response",
        marker: None,
        requires: None,
        why: "rate-limit 429 body builder",
    },
    Allowed {
        file: "src/services/llm_preflight.rs",
        func: "guardrail_violation_response",
        marker: None,
        requires: None,
        why: "guardrail 403 body builder; every call sits inside \
              record_guardrail_block(), which refuses through refuse_with()",
    },
    Allowed {
        file: "src/services/llm_preflight.rs",
        func: "anthropic_guardrail_violation_response",
        marker: None,
        requires: None,
        why: "Anthropic-shaped guardrail 403 body builder",
    },
    Allowed {
        file: "src/routes/gemini.rs",
        func: "gemini_guardrail_violation_response",
        marker: None,
        requires: None,
        why: "Gemini-shaped guardrail 403 body builder",
    },
    Allowed {
        file: "src/services/llm_resilience.rs",
        func: "gate_rejection_response",
        marker: None,
        requires: None,
        why: "resilience-gate body builder; 429s reach it through refuse()",
    },
    Allowed {
        file: "src/services/llm_resilience.rs",
        func: "gate_dispatch",
        marker: Some("ConcurrencySaturated"),
        requires: None,
        why: "constructs the rejection VALUE; the multimodal gate records it \
              through refuse(), the router maps it to ConcurrencyLimitReached",
    },
    // --- Downstream classification: the router's error, the surface's row.
    Allowed {
        file: "src/routes/llm.rs",
        func: "chat_completions",
        marker: Some("LlmRouterError::"),
        requires: Some("router_limit_subtype("),
        why: "maps a router error to the client status; the surface's error \
              row carries router_limit_subtype for the gateway's own limits",
    },
    Allowed {
        file: "src/routes/llm.rs",
        func: "router_error_response",
        marker: None,
        requires: None,
        why: "maps a router error to the client status; its callers write the \
              row with router_limit_subtype",
    },
    Allowed {
        file: "src/routes/anthropic.rs",
        func: "messages",
        marker: Some("LlmRouterError::"),
        requires: Some("router_limit_subtype("),
        why: "maps a router error to the client status; the error row carries \
              router_limit_subtype",
    },
];

/// Functions that refuse on their caller's behalf: each takes the response
/// and hands it to `refuse` / `refuse_with` (checked by
/// `every_writer_goes_through_refuse`), so a construction inside a call to
/// one of them is recorded exactly as if it sat inside `refuse(`.
const WRITERS: &[(&str, &str)] = &[
    // The guardrail block row: pre-flight input and post-response output.
    ("src/services/llm_preflight.rs", "record_guardrail_block"),
];

// ---------------------------------------------------------------------------
// Source handling
// ---------------------------------------------------------------------------

/// Blank out comments and the contents of string / char literals, keeping
/// every byte offset and newline, so prose and literals can neither satisfy
/// nor trip the scan, nor unbalance the bracket walk.
fn sanitize(src: &str) -> String {
    let b = src.as_bytes();
    let mut out = b.to_vec();
    let blank = |out: &mut Vec<u8>, from: usize, to: usize| {
        for c in out.iter_mut().take(to).skip(from) {
            if *c != b'\n' {
                *c = b' ';
            }
        }
    };
    let n = b.len();
    let mut i = 0;
    while i < n {
        if b[i] == b'/' && i + 1 < n && b[i + 1] == b'/' {
            let j = src[i..].find('\n').map_or(n, |k| i + k);
            blank(&mut out, i, j);
            i = j;
        } else if b[i] == b'/' && i + 1 < n && b[i + 1] == b'*' {
            let j = src[i + 2..].find("*/").map_or(n, |k| i + 2 + k + 2);
            blank(&mut out, i, j);
            i = j;
        } else if b[i] == b'r'
            && (i == 0 || !(b[i - 1].is_ascii_alphanumeric() || b[i - 1] == b'_'))
            && i + 1 < n
            && (b[i + 1] == b'"' || b[i + 1] == b'#')
        {
            let mut h = i + 1;
            while h < n && b[h] == b'#' {
                h += 1;
            }
            if h < n && b[h] == b'"' {
                let hashes = h - (i + 1);
                let close = format!("\"{}", "#".repeat(hashes));
                let j = src[h + 1..]
                    .find(&close)
                    .map_or(n, |k| h + 1 + k + close.len());
                blank(&mut out, h + 1, j.saturating_sub(close.len()));
                i = j;
            } else {
                i += 1;
            }
        } else if b[i] == b'"' {
            let mut j = i + 1;
            while j < n && b[j] != b'"' {
                j += if b[j] == b'\\' { 2 } else { 1 };
            }
            blank(&mut out, i + 1, j.min(n));
            i = j + 1;
        } else if b[i] == b'\'' {
            if i + 1 < n && b[i + 1] == b'\\' {
                let j = src[i + 2..].find('\'').map_or(n, |k| i + 2 + k + 1);
                blank(&mut out, i, j);
                i = j;
            } else if i + 2 < n && b[i + 2] == b'\'' {
                blank(&mut out, i, i + 3);
                i += 3;
            } else {
                i += 1; // a lifetime
            }
        } else {
            i += 1;
        }
    }
    String::from_utf8(out).expect("sanitised source stays UTF-8")
}

/// Drop the unit-test module (`#[cfg(test)] mod …` to end of file).
fn without_tests(s: &str) -> &str {
    let mut from = 0;
    while let Some(k) = s[from..].find("#[cfg(test)]") {
        let at = from + k;
        let rest = s[at + "#[cfg(test)]".len()..].trim_start();
        let rest = if rest.starts_with("#[path") {
            rest[rest.find(']').map_or(0, |e| e + 1)..].trim_start()
        } else {
            rest
        };
        if rest.starts_with("mod ") {
            return &s[..at];
        }
        from = at + 1;
    }
    s
}

/// The statement around byte `at`: back to the previous `;` / block open
/// at this level, forward to the next `;` / block close.
fn statement(s: &str, at: usize) -> &str {
    let b = s.as_bytes();
    let (mut brace, mut start) = (0i32, 0usize);
    let mut j = at;
    while j > 0 {
        j -= 1;
        match b[j] {
            b'}' => brace += 1,
            b'{' => {
                if brace == 0 {
                    start = j + 1;
                    break;
                }
                brace -= 1;
            }
            b';' if brace == 0 => {
                start = j + 1;
                break;
            }
            _ => {}
        }
    }
    let (mut paren, mut brace, mut end) = (0i32, 0i32, s.len());
    for (k, &c) in b.iter().enumerate().skip(at) {
        match c {
            b'(' | b'[' => paren += 1,
            b')' | b']' => paren -= 1,
            b'{' => brace += 1,
            b'}' => {
                if brace == 0 {
                    end = k;
                    break;
                }
                brace -= 1;
            }
            b';' if brace == 0 && paren <= 0 => {
                end = k;
                break;
            }
            _ => {}
        }
    }
    &s[start..end]
}

/// Name of the nearest `fn` declared before `at`.
fn enclosing_fn(s: &str, at: usize) -> String {
    let head = &s[..at];
    let mut best = None;
    let mut from = 0;
    while let Some(k) = head[from..].find("fn ") {
        let p = from + k;
        let ok_before = p == 0 || !head.as_bytes()[p - 1].is_ascii_alphanumeric();
        if ok_before {
            let name: String = head[p + 3..]
                .chars()
                .take_while(|c| c.is_ascii_alphanumeric() || *c == '_')
                .collect();
            if !name.is_empty() {
                best = Some(name);
            }
        }
        from = p + 3;
    }
    best.unwrap_or_default()
}

/// The source of `func` in `s` (its declaration to the next top-level fn).
fn fn_body<'a>(s: &'a str, func: &str) -> &'a str {
    let Some(start) = [format!("fn {func}("), format!("fn {func}<")]
        .iter()
        .filter_map(|n| s.find(n.as_str()))
        .min()
    else {
        return "";
    };
    let rest = &s[start + 3..];
    let end = rest
        .find("\nfn ")
        .or_else(|| rest.find("\npub"))
        .unwrap_or(rest.len());
    &rest[..end]
}

fn calls_refuse(stmt: &str) -> bool {
    let writers = WRITERS.iter().map(|(_, f)| format!("{f}("));
    ["refuse(".to_string(), "refuse_with(".to_string()]
        .into_iter()
        .chain(writers)
        .any(|needle| {
            stmt.match_indices(needle.as_str()).any(|(i, _)| {
                i == 0 || {
                    let c = stmt.as_bytes()[i - 1];
                    !(c.is_ascii_alphanumeric() || c == b'_')
                }
            })
        })
}

/// True when the `X {` at `at..at+len` is a pattern (`X { .. } =>` / `|`),
/// not a construction.
fn is_pattern(s: &str, at: usize, needle: &str) -> bool {
    let after_needle = at + needle.len();
    let mut tail = &s[after_needle..];
    if needle.ends_with('{') {
        let mut depth = 1;
        let mut end = tail.len();
        for (k, c) in tail.char_indices() {
            match c {
                '{' => depth += 1,
                '}' => {
                    depth -= 1;
                    if depth == 0 {
                        end = k + 1;
                        break;
                    }
                }
                _ => {}
            }
        }
        tail = &tail[end..];
    }
    let t = tail.trim_start();
    t.starts_with("=>") || (t.starts_with('|') && !t.starts_with("||"))
}

#[derive(Debug)]
#[allow(dead_code)] // read through the Debug dump of an offender
struct Site {
    file: String,
    line: usize,
    func: String,
    needle: &'static str,
    statement: String,
}

/// Every 403/429 construction in `src` that does not go through `refuse`.
fn unrefused(file: &str, raw: &str) -> Vec<Site> {
    let clean = sanitize(raw);
    let s = without_tests(&clean);
    let mut out = Vec::new();
    for needle in [
        "StatusCode::FORBIDDEN",
        "StatusCode::TOO_MANY_REQUESTS",
        "usage_limit_response(",
        "usage_limit_response)(",
        // The guardrail 403 builders (OpenAI, Anthropic and Gemini shapes):
        // a guardrail block built outside the writer leaves no block row.
        "guardrail_violation_response(",
        "guardrail_violation_response)(",
        "GateRejection::ConcurrencySaturated {",
        "GateRejection::RgConcurrencySaturated {",
    ] {
        for (at, _) in s.match_indices(needle) {
            // A declaration of a builder is not a construction.
            if needle.starts_with("usage_limit_response")
                || needle.starts_with("guardrail_violation_response")
            {
                let head = &s[..at];
                if ["fn ", "fn anthropic_", "fn gemini_"]
                    .iter()
                    .any(|d| head.ends_with(d))
                {
                    continue;
                }
            }
            if is_pattern(s, at, needle) {
                continue;
            }
            let stmt = statement(s, at);
            if calls_refuse(stmt) {
                continue;
            }
            out.push(Site {
                file: file.to_string(),
                line: s[..at].matches('\n').count() + 1,
                func: enclosing_fn(s, at),
                needle,
                statement: stmt.split_whitespace().collect::<Vec<_>>().join(" "),
            });
        }
    }
    out
}

fn allowed_for<'a>(site: &Site, src: &str) -> Option<&'a Allowed> {
    ALLOWED.iter().find(|a| {
        a.file == site.file
            && a.func == site.func
            && a.marker.is_none_or(|m| site.statement.contains(m))
            && a.requires.is_none_or(|r| fn_body(src, a.func).contains(r))
    })
}

// ---------------------------------------------------------------------------
// The guard
// ---------------------------------------------------------------------------

#[test]
fn every_llm_refusal_goes_through_the_one_writer() {
    let mut offenders = Vec::new();
    let mut used = vec![false; ALLOWED.len()];
    let surfaces = surfaces();
    if roots::tree("enterprise").is_some() {
        assert!(
            surfaces.len() >= SURFACES.len() + 15,
            "the Enterprise workspace's files on the LLM path: {} scanned",
            surfaces.len()
        );
        for listed in ENTERPRISE_NOT_THE_LLM_PATH {
            assert!(
                std::path::Path::new(listed).exists(),
                "{listed} is listed as the Enterprise API and is gone"
            );
        }
    }
    for file in &surfaces {
        let file = file.as_str();
        let raw = fs::read_to_string(file).unwrap_or_else(|e| panic!("read {file}: {e}"));
        let clean = sanitize(&raw);
        for site in unrefused(file, &raw) {
            match allowed_for(&site, &clean) {
                Some(a) => {
                    let idx = ALLOWED
                        .iter()
                        .position(|x| std::ptr::eq(x, a))
                        .expect("allowlist entry");
                    used[idx] = true;
                }
                None => offenders.push(site),
            }
        }
    }
    assert!(
        offenders.is_empty(),
        "these LLM-surface sites build a 403/429 without `llm_refusal::refuse` — \
         a governed refusal that writes no audit row, is never sealed as a \
         `denied` record and never lands on its run. Wrap the response in \
         `llm_refusal::refuse(state, &scope, Refusal::…, response)`; only if \
         its evidence is genuinely written elsewhere, add it to ALLOWED with \
         the reason.\n{offenders:#?}",
    );
    let stale: Vec<_> = ALLOWED
        .iter()
        .zip(&used)
        .filter(|(a, u)| !**u && here(a.file))
        .map(|(a, _)| format!("{}::{}", a.file, a.func))
        .collect();
    assert!(
        stale.is_empty(),
        "allowlist entries that match nothing — delete them so the list \
         cannot hide a future regression: {stale:?}",
    );
}

#[test]
fn every_scanned_surface_exists() {
    // A renamed file must not silently drop out of the scan.
    for file in SURFACES.iter().filter(|f| here(f)) {
        assert!(
            fs::metadata(file).is_ok(),
            "{file} is gone — update SURFACES"
        );
    }
}

#[test]
fn the_single_writer_records_what_it_returns() {
    // The guarantees the guard relies on, in the writer itself: the row's
    // status is the response's, and the row is written.
    let src = fs::read_to_string("src/services/llm_refusal.rs").expect("read llm_refusal.rs");
    let body = fn_body(&sanitize(&src), "refuse_with").to_string();
    assert!(
        body.contains("response.status()"),
        "the row status must come from the response"
    );
    assert!(
        body.contains("write_proxy_log("),
        "the writer must write the row"
    );
    assert!(
        body.contains("build_row("),
        "the writer must build the row (subtype, run, model, group)"
    );
}

#[test]
fn every_writer_goes_through_refuse() {
    // A WRITERS entry is trusted as a refusal only because it refuses.
    for (file, func) in WRITERS {
        let src = fs::read_to_string(file).unwrap_or_else(|e| panic!("read {file}: {e}"));
        let body = fn_body(&sanitize(&src), func).to_string();
        assert!(!body.is_empty(), "{file}: writer `{func}` is gone");
        assert!(
            calls_refuse(&body.replace(&format!("{func}("), "")),
            "{file}: writer `{func}` must hand its response to refuse()/refuse_with()"
        );
    }
}

/// Every file (outside tests) that runs a streamed response through the
/// output-guardrail state machine.
fn stream_guard_files() -> Vec<String> {
    let mut out = Vec::new();
    let mut stack = vec![std::path::PathBuf::from("src")];
    while let Some(dir) = stack.pop() {
        for e in fs::read_dir(&dir).expect("read src dir").flatten() {
            let p = e.path();
            if p.is_dir() {
                stack.push(p);
            } else if p.extension().is_some_and(|x| x == "rs") {
                let f = p.to_string_lossy().to_string();
                if f.ends_with("stream_guard.rs") {
                    continue;
                }
                let raw = fs::read_to_string(&p).expect("read source");
                if without_tests(&sanitize(&raw)).contains("StreamGuard::new(") {
                    out.push(f);
                }
            }
        }
    }
    out.sort();
    out
}

#[test]
fn every_stream_records_how_its_guard_ended_it_and_watches_its_run() {
    // How a guarded stream ends is recorded on the stream's outcome row
    // (which names the run and step and carries the billed tokens): an
    // output-guardrail block as the violation, an operator abort as
    // `run_aborted`. Before this, a streamed block read as a plain 200/403
    // and was never sealed as a block, and a mid-stream abort was a 403 with
    // no `run_aborted` subtype — on the one stream that polled for it at
    // all. Every stream that runs a guard must:
    //   * arm the abort poll for its run (`.for_run(`),
    //   * ask it per upstream event (`.run_aborted(`),
    //   * stamp the guard's outcome on its row (`.stamp_outcome(` or carry
    //     `.outcome()` to the row writer),
    // one of each per `StreamGuard::new(`.
    let files = stream_guard_files();
    assert!(
        files.len() >= 4,
        "the scan lost the streaming surfaces: {files:?}"
    );
    for f in files {
        let raw = fs::read_to_string(&f).expect("read");
        let clean = sanitize(&raw);
        let s = without_tests(&clean);
        let guards = s.matches("StreamGuard::new(").count();
        for (needle, what) in [
            (".for_run(", "arm the mid-stream abort poll for its run"),
            (
                ".run_aborted(",
                "poll for an operator abort per upstream event",
            ),
        ] {
            let n = s.matches(needle).count();
            assert!(
                n >= guards,
                "{f}: {guards} streamed guard(s) but {n} `{needle}` — every stream must {what}"
            );
        }
        let stamps = s.matches(".stamp_outcome(").count() + s.matches(".outcome()").count();
        assert!(
            stamps >= guards,
            "{f}: {guards} streamed guard(s) but {stamps} outcome stamp(s) — a stream the guard \
             stops must record how on its outcome row (`StreamGuard::stamp_outcome`)"
        );
    }
}

#[test]
fn every_media_route_refuses_through_the_pipeline() {
    // The media-governance and media-quota gates of each media route.
    for f in [
        "src/routes/images.rs",
        "src/routes/audio_speech.rs",
        "src/routes/audio_transcribe.rs",
        "src/routes/video.rs",
    ] {
        let src = fs::read_to_string(f).expect("read media route");
        assert!(src.contains(".refuse_policy("), "{f}: media governance");
        assert!(src.contains(".refuse_quota("), "{f}: media quota");
    }
}

// ---------------------------------------------------------------------------
// Guard the guard
// ---------------------------------------------------------------------------

#[test]
fn the_scanner_catches_a_silent_refusal_and_passes_a_recorded_one() {
    let silent = r#"
        async fn gate() -> Response {
            if bad {
                // refuse( in a comment must not count
                return error_response(StatusCode::FORBIDDEN, "policy: refuse(");
            }
            ok()
        }
    "#;
    let found = unrefused("x.rs", silent);
    assert_eq!(found.len(), 1, "{found:#?}");
    assert_eq!(found[0].func, "gate");

    let recorded = r#"
        async fn gate() -> Response {
            if bad {
                return llm_refusal::refuse(
                    &state,
                    &scope,
                    Refusal::new(st::LLM_GOVERNANCE_BLOCK, "x"),
                    error_response(StatusCode::FORBIDDEN, "x"),
                );
            }
            match e {
                StatusCode::TOO_MANY_REQUESTS => "rate_limit_error",
                _ => "x",
            }
        }
    "#;
    assert!(unrefused("x.rs", recorded).is_empty());

    // A quota builder called outside refuse() is caught, in either spelling.
    let quota = r#"
        fn f() -> Response {
            let r = quota_fn.unwrap_or(usage_limit_response)(&e);
            r
        }
        fn g() -> Response { llm_preflight::usage_limit_response(&e) }
    "#;
    assert_eq!(unrefused("x.rs", quota).len(), 2);

    // A concurrency rejection built outside refuse() is caught; its match
    // pattern is not.
    let conc = r#"
        fn h() -> Response {
            match r {
                GateRejection::RgConcurrencySaturated { scope, .. } => 1,
                _ => 2,
            };
            return gate_rejection_response(&GateRejection::RgConcurrencySaturated {
                scope: s,
                message: m,
            });
        }
    "#;
    assert_eq!(unrefused("x.rs", conc).len(), 1);

    // A guardrail 403 built outside the writer is caught (either spelling,
    // any shape); built inside `record_guardrail_block(` it is recorded, and
    // the builders' own declarations are not constructions.
    let guard = r#"
        fn a() -> Response { anthropic_guardrail_violation_response(&v) }
        fn b() -> Response { f.unwrap_or(guardrail_violation_response)(&v) }
        fn c() -> Response {
            record_guardrail_block(&state, &scope, E, &v, None, None,
                f.unwrap_or(guardrail_violation_response)(&v))
        }
        pub fn gemini_guardrail_violation_response(v: &V) -> Response { x }
    "#;
    let found = unrefused("x.rs", guard);
    assert_eq!(
        found.iter().map(|s| s.func.as_str()).collect::<Vec<_>>(),
        vec!["a", "b"],
        "{found:#?}"
    );
}
