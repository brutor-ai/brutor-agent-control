// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Cloud-identity guard.
//!
//! **The gateway's own cloud identity is not a tenant's.** The process the
//! gateway runs in usually has a cloud identity of its own: `AWS_*`
//! variables or an instance role, `GOOGLE_APPLICATION_CREDENTIALS` or the
//! node's service account. It is the operator's. A Vertex AI row with no
//! credential was called with it (`gcp_auth::provider()`, Application
//! Default Credentials), and a Bedrock guardrail with no credential was
//! signed with the process's `AWS_*` variables: a tenant that can create a
//! model row or a guardrail acted as the operator. Tenant traffic is
//! authenticated with the credential the tenant's own row or guardrail
//! carries, or it is refused.
//!
//! Forbidden in product code:
//!
//! * `gcp_auth::provider` (the ambient Google credential chain), anywhere;
//! * `aws_config::` (the ambient AWS credential chain) outside the
//!   operator's own infrastructure (`OPERATOR_INFRASTRUCTURE`: the media
//!   store's S3 client, which is the operator's bucket, signed as the
//!   operator);
//! * a string literal that is the name of an `AWS_…` or `GOOGLE_…`
//!   variable, outside `ALLOWED_NAMES`. A literal, not a call: the Bedrock
//!   guardrail read its variables through a helper of its own.
//!
//! **`vertex` is `vertex_ai`**. A line that matches the provider
//! name `"vertex_ai"` (a match arm, `matches!`, a comparison) names
//! `"vertex"` too: the chat route sent a Claude row of `vertex` to a bridge
//! whose backend lookup knew `vertex_ai` only.
//!
//! **No cloud arm where no cloud row arrives**. A Bedrock or
//! Vertex row is refused at dispatch on the image, speech, transcription,
//! video and batch APIs (`providers::dispatch_refusal`: the gateway has no
//! backend of the cloud for them), so the code of those surfaces
//! (`NO_CLOUD_ROW_ARRIVES`) names no cloud provider. Two did: the batch
//! adapters for Vertex AI and Bedrock, which sent a bearer token, and an
//! arm of the image route. An arm added there for a cloud is either dead
//! or sends a cloud row somewhere without the cloud's own backend.
//!
//! Scans `src/` (not `src/bin/`) and `enterprise/src/`, with `#[cfg(test)]`
//! modules, test files and comments removed.
#[path = "support/roots.rs"]
mod roots;

use std::fs;
use std::path::{Path, PathBuf};

/// Is `file` one this run has? A file of the Enterprise workspace is not, in
/// a Community tree alone (`tests/support/roots.rs`); what this guard holds
/// about it is held where the workspace is.
fn here(file: &str) -> bool {
    !file.starts_with("enterprise/") || roots::tree("enterprise").is_some()
}

/// Files that are the operator's own infrastructure and use the operator's
/// AWS identity on purpose, with the reason.
const OPERATOR_INFRASTRUCTURE: &[(&str, &str)] = &[(
    "src/services/media_storage/s3.rs",
    "the media store's bucket is the operator's: BRUTOR_MEDIA_BUCKET, BRUTOR_MEDIA_S3_REGION",
)];

/// `(file, literal, why it is not a tenant's request authenticated with the
/// gateway's identity)`.
const ALLOWED_NAMES: &[(&str, &str, &str)] = &[(
    "src/env_vars.rs",
    "AWS_REGION",
    "the registry's definition of the variable `AppConfig` reads as the default of \
         BRUTOR_MEDIA_S3_REGION: the region of the operator's media bucket",
)];

fn rust_files(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for e in entries.flatten() {
        let p = e.path();
        if p.is_dir() {
            if p != Path::new("src/bin") {
                rust_files(&p, out);
            }
        } else if p.extension().and_then(|s| s.to_str()) == Some("rs") {
            let name = p.file_name().and_then(|s| s.to_str()).unwrap_or("");
            if name != "tests.rs" && !name.ends_with("_tests.rs") {
                out.push(p);
            }
        }
    }
}

/// The end of the brace-balanced block that starts at the first `{` of
/// `src` (exclusive), or `src.len()`.
fn block_end(src: &str) -> usize {
    let mut depth = 0usize;
    for (i, c) in src.char_indices() {
        match c {
            '{' => depth += 1,
            '}' => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    return i + 1;
                }
            }
            _ => {}
        }
    }
    src.len()
}

/// Where the next `#[cfg(test)]` module of `src` starts: the attribute,
/// then `mod`, `pub mod` or `pub(crate) mod` on the next line.
fn next_test_module(src: &str) -> Option<usize> {
    let mut from = 0;
    while let Some(at) = src[from..].find("#[cfg(test)]") {
        let start = from + at;
        let after = src[start + "#[cfg(test)]".len()..].trim_start();
        let after = after
            .strip_prefix("pub(crate) ")
            .or_else(|| after.strip_prefix("pub "))
            .unwrap_or(after);
        if after.starts_with("mod ") {
            return Some(start);
        }
        from = start + "#[cfg(test)]".len();
    }
    None
}

/// The product code of a file: without its `#[cfg(test)]` modules (code
/// after such a module is product code) and without comment lines.
fn product(src: &str) -> String {
    let mut code = String::new();
    let mut rest = src;
    while let Some(start) = next_test_module(rest) {
        code.push_str(&rest[..start]);
        let block = &rest[start..];
        rest = &block[block_end(block)..];
    }
    code.push_str(rest);
    code.lines()
        .filter(|l| !l.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n")
}

/// The string literals of `code` (plain `"…"` literals, with `\"` escapes).
fn literals(code: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut chars = code.chars();
    while let Some(c) = chars.next() {
        if c != '"' {
            continue;
        }
        let mut lit = String::new();
        while let Some(c) = chars.next() {
            match c {
                '\\' => {
                    if let Some(next) = chars.next() {
                        lit.push('\\');
                        lit.push(next);
                    }
                }
                '"' => break,
                _ => lit.push(c),
            }
        }
        out.push(lit);
    }
    out
}

/// True for the name of an `AWS_…` or `GOOGLE_…` variable.
fn is_cloud_variable_name(literal: &str) -> bool {
    ["AWS_", "GOOGLE_"].iter().any(|prefix| {
        literal.strip_prefix(prefix).is_some_and(|rest| {
            !rest.is_empty()
                && rest
                    .bytes()
                    .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_')
        })
    })
}

fn ambient_identity_violations(file: &str, src: &str) -> Vec<String> {
    let code = product(src);
    let mut out = Vec::new();
    // Split so that this file does not contain what it forbids.
    let google_chain = concat!("gcp_auth::", "provider");
    if code.contains(google_chain) {
        out.push(format!(
            "{file}: calls `{google_chain}` (the gateway's own Google identity)"
        ));
    }
    let aws_chain = concat!("aws_", "config::");
    if code.contains(aws_chain) && !OPERATOR_INFRASTRUCTURE.iter().any(|(f, _)| *f == file) {
        out.push(format!(
            "{file}: uses `{aws_chain}` (the gateway's own AWS identity)"
        ));
    }
    for literal in literals(&code) {
        if is_cloud_variable_name(&literal)
            && !ALLOWED_NAMES
                .iter()
                .any(|(f, name, _)| *f == file && *name == literal)
        {
            out.push(format!(
                "{file}: names the variable \"{literal}\" (the gateway's own cloud identity or \
                 region)"
            ));
        }
    }
    out
}

/// The lines of `src` that match the provider name `vertex_ai` and not
/// `vertex`.
fn vertex_name_violations(file: &str, src: &str) -> Vec<String> {
    let both = concat!("\"vertex", "_ai\"");
    let short = concat!("\"vert", "ex\"");
    product(src)
        .lines()
        .filter(|line| line.contains(both) && !line.contains(short))
        .filter(|line| {
            ["=>", "matches!", "==", "!=", " | "]
                .iter()
                .any(|m| line.contains(m))
        })
        .map(|line| format!("{file}: `{}`", line.trim()))
        .collect()
}

/// The code of the surfaces on which dispatch refuses every cloud row.
const NO_CLOUD_ROW_ARRIVES: &[&str] = &[
    "src/routes/images.rs",
    "src/routes/audio_speech.rs",
    "src/routes/audio_transcribe.rs",
    "src/routes/video.rs",
    "enterprise/src/batch/",
];

/// The lines of a surface no cloud row arrives on that name a cloud
/// provider.
fn cloud_arm_violations(file: &str, src: &str) -> Vec<String> {
    if !NO_CLOUD_ROW_ARRIVES.iter().any(|p| file.starts_with(p)) {
        return Vec::new();
    }
    let names = [
        concat!("vertex", "_ai"),
        concat!("vert", "ex"),
        concat!("bed", "rock"),
        concat!("bedrock", "_converse"),
    ];
    product(src)
        .lines()
        .filter(|line| literals(line).iter().any(|l| names.contains(&l.as_str())))
        .map(|line| format!("{file}: `{}`", line.trim()))
        .collect()
}

fn scan(check: fn(&str, &str) -> Vec<String>) -> Vec<String> {
    let mut files = Vec::new();
    rust_files(Path::new("src"), &mut files);
    if let Some(enterprise) = roots::tree("enterprise") {
        rust_files(&enterprise.join("src"), &mut files);
    }
    assert!(files.len() > 100, "the scan found the sources");
    let mut out = Vec::new();
    for f in files {
        let name = f.to_string_lossy().replace('\\', "/");
        out.extend(check(&name, &fs::read_to_string(&f).unwrap_or_default()));
    }
    out
}

#[test]
fn tenant_traffic_is_never_authenticated_with_the_gateways_own_cloud_identity() {
    let found = scan(ambient_identity_violations);
    assert!(
        found.is_empty(),
        "product code can pick up the gateway process's own cloud identity. A tenant's row or \
         guardrail is authenticated with its own credential or refused; the operator's own \
         infrastructure is listed in OPERATOR_INFRASTRUCTURE with its reason:\n{}",
        found.join("\n")
    );
}

#[test]
fn vertex_and_vertex_ai_are_one_provider() {
    let found = scan(vertex_name_violations);
    assert!(
        found.is_empty(),
        "a line matches the provider \"vertex_ai\" and not \"vertex\" (they are one provider):\n{}",
        found.join("\n")
    );
}

#[test]
fn no_cloud_provider_is_named_where_no_cloud_row_arrives() {
    for surface in NO_CLOUD_ROW_ARRIVES.iter().copied().filter(|s| here(s)) {
        assert!(
            Path::new(surface).exists(),
            "{surface} is gone: update NO_CLOUD_ROW_ARRIVES"
        );
    }
    let found = scan(cloud_arm_violations);
    assert!(
        found.is_empty(),
        "a surface on which every cloud row is refused at dispatch names a cloud provider \
         (dead code, or a cloud row sent without the cloud's backend):\n{}",
        found.join("\n")
    );
}

/// The allow-lists do not outlive what they allow.
#[test]
fn the_allow_lists_are_not_stale() {
    for (file, why) in OPERATOR_INFRASTRUCTURE {
        let src = fs::read_to_string(file).unwrap_or_default();
        assert!(
            product(&src).contains(concat!("aws_", "config::")),
            "{file} no longer uses the AWS credential chain ({why}): remove the entry"
        );
    }
    for (file, name, why) in ALLOWED_NAMES {
        let src = fs::read_to_string(file).unwrap_or_default();
        assert!(
            literals(&product(&src)).iter().any(|l| l == name),
            "{file} no longer names \"{name}\" ({why}): remove the entry"
        );
    }
}

/// The scanners find the shapes they forbid, and leave the ones they allow.
#[test]
fn the_guard_finds_what_it_forbids() {
    // The ambient identity, as a Vertex backend could take it.
    let adc = format!(
        "async fn token() {{\n    let p = {}().await;\n}}\n",
        concat!("gcp_auth::", "provider")
    );
    assert_eq!(ambient_identity_violations("src/x.rs", &adc).len(), 1);
    // The same in a Bedrock guardrail: through a helper.
    let env = "fn build() {\n    let k = cfg(\"aws_access_key_id\")\n        \
               .or_else(|| env_nonempty(\"AWS_ACCESS_KEY_ID\"));\n}\n";
    assert_eq!(ambient_identity_violations("src/x.rs", env).len(), 1);
    let google = "fn f() { let _ = std::env::var(\"GOOGLE_APPLICATION_CREDENTIALS\"); }\n";
    assert_eq!(ambient_identity_violations("src/x.rs", google).len(), 1);
    // The SDK's default chain, outside the operator's infrastructure.
    let sdk = format!(
        "async fn c() {{ let _ = {}defaults(v).load().await; }}\n",
        concat!("aws_", "config::")
    );
    assert_eq!(ambient_identity_violations("src/x.rs", &sdk).len(), 1);
    assert!(ambient_identity_violations("src/services/media_storage/s3.rs", &sdk).is_empty());
    // An allowed name is allowed in its file only.
    let region = "fn f() { let _ = var(\"AWS_REGION\"); }\n";
    assert!(ambient_identity_violations("src/env_vars.rs", region).is_empty());
    assert_eq!(ambient_identity_violations("src/x.rs", region).len(), 1);
    // Test modules, comments and other literals are not product code.
    let quiet = "// env_nonempty(\"AWS_ACCESS_KEY_ID\")\nfn f() { let _ = \"AWS\"; let _ = \
                 \"[REDACTED-AWS_SECRET_KEY]\"; let _ = \"aws_region\"; }\n#[cfg(test)]\npub(crate) \
                 mod t {\n    fn g() { let _ = var(\"AWS_PROFILE\"); }\n}\n";
    assert!(
        ambient_identity_violations("src/x.rs", quiet).is_empty(),
        "{:?}",
        ambient_identity_violations("src/x.rs", quiet)
    );
    // Code after a test module is product code again.
    let after =
        "#[cfg(test)]\nmod t {\n    fn g() {}\n}\nfn f() { let _ = var(\"AWS_PROFILE\"); }\n";
    assert_eq!(ambient_identity_violations("src/x.rs", after).len(), 1);

    // A shared client in an image route and in a batch
    // factory; the same line elsewhere is not this rule's.
    let arm = format!(
        "match p {{\n    \"gemini\" | {} | {} => google(),\n    _ => no(),\n}}\n",
        concat!("\"vertex", "_ai\""),
        concat!("\"vert", "ex\"")
    );
    assert_eq!(cloud_arm_violations("src/routes/images.rs", &arm).len(), 1);
    assert!(cloud_arm_violations("src/routes/llm.rs", &arm).is_empty());
    let factory = format!(
        "match p {{\n    {} => Some(Bedrock::new()),\n    _ => None,\n}}\n",
        concat!("\"bed", "rock\"")
    );
    assert_eq!(
        cloud_arm_violations("enterprise/src/batch/provider.rs", &factory).len(),
        1
    );
    let prose = "fn f() { let _ = \"Amazon bedrock and vertex are refused\"; }\n";
    assert!(cloud_arm_violations("src/routes/video.rs", prose).is_empty());

    // One backend for every row, in the backend lookup.
    let one = format!(
        "match p {{\n    {} => Some(&VertexBackend),\n    _ => None,\n}}\n",
        concat!("\"vertex", "_ai\"")
    );
    assert_eq!(vertex_name_violations("src/x.rs", &one).len(), 1);
    let matches = format!(
        "fn n(p: &str) -> bool {{ matches!(p, \"anthropic\" | {}) }}\n",
        concat!("\"vertex", "_ai\"")
    );
    assert_eq!(vertex_name_violations("src/x.rs", &matches).len(), 1);
    let both = format!(
        "match p {{\n    {} | {} => 1,\n    _ => 0,\n}}\n",
        concat!("\"vertex", "_ai\""),
        concat!("\"vert", "ex\"")
    );
    assert!(vertex_name_violations("src/x.rs", &both).is_empty());
    // A name that is stated, not matched, is not this rule's.
    let stated = format!(
        "fn provider_name(&self) -> &str {{\n    {}\n}}\n",
        concat!("\"vertex", "_ai\"")
    );
    assert!(vertex_name_violations("src/x.rs", &stated).is_empty());
}
